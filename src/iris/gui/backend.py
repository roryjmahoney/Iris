"""Everything the Iris GUI needs that is not a widget.

Two hard constraints shape this module.

**1. The GUI is unprivileged; the data it manages is not.**
``/var/lib/iris`` is ``root:root 0700`` and ``/run/irisd/socket`` is
``root:root 0600`` (SPEC "Paths"), so a session-bus desktop app can neither
read templates nor talk to the daemon directly.  Every operation that touches
them therefore goes through ``pkexec iris <subcommand>``: polkit prompts once,
the CLI runs as root, and the GUI parses its output.  Reading
``/etc/iris/config.toml`` is the one exception -- it is ``0644`` precisely so
the settings panel can show the current values without a password prompt.

Where the daemon socket *is* reachable (the app running as root, or a site that
has loosened the socket mode) the JSON protocol in :mod:`iris.protocol` is
tried first, because it is the authoritative interface and costs no prompt.

**2. Only one process can hold /dev/video2 at a time.**
V4L2 capture nodes are exclusive.  The GUI's live preview and the privileged
enrolment process cannot both have the IR camera open, so :class:`PreviewWorker`
must be fully stopped -- thread joined, ``VideoCapture`` released -- before
:class:`EnrollProcess` starts.  :meth:`PreviewWorker.stop` is asynchronous for
exactly this reason: it hands control back to the main loop and calls back when
the device is genuinely free, rather than blocking the UI on a ``join()``.

GUI <-> CLI contract
--------------------
The commands below are what this module invokes.  ``--json`` selects
newline-delimited JSON on stdout, using the same message shapes as the socket
protocol in :mod:`iris.protocol`::

    iris enroll --user U --name N --json
        -> {"progress": 0.0..1.0, "hint": "..."}   (zero or more, one per line)
        -> {"ok": true, "samples": 12}             (exactly one, last)
           or {"ok": false, "reason": "...", "error": "..."}

    iris list   --user U --json     -> {"ok": true, "faces": [{name, created, samples}]}
    iris remove --user U --name N --json  -> {"ok": true}
    iris clear  --user U --json           -> {"ok": true}
    iris config-set --json                -> reads a config object on stdin,
                                             writes {"ok": true}

**Progress lines must be flushed as they are produced.**  Python line-buffers
stdout only when it is a tty; through a pipe the default 8 KiB block buffer
would hold every progress line until the process exits, and the ring would jump
from 0 to 100%.  The GUI cannot fix that from its side (``pkexec`` scrubs the
environment, so ``PYTHONUNBUFFERED`` does not survive), so it defends instead:
:class:`EnrollProcess` reports a stalled stream to the UI, which falls back to
an indeterminate "Working" state rather than a frozen ring.

``config-set`` is also accepted spelled ``config set``; a usage error from one
spelling falls through to the other (see :func:`_run_cli`).  Nothing else is
guessed -- an unrecognised command surfaces as a plain-language error naming the
exact command that failed, so the user can run it in a terminal and see why.
"""

from __future__ import annotations

import json
import logging
import os
import pwd
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Final, Sequence

from gi.repository import Gdk, GLib

_LOG = logging.getLogger("iris.gui.backend")

#: Seconds to wait for the daemon before deciding it is unreachable.  Short:
#: this runs on a worker thread but the user is watching a spinner.
_DAEMON_TIMEOUT: Final[float] = 3.0

#: Ceiling for a one-shot privileged command *including* the time the user
#: spends typing their password into the polkit dialog.
_PRIVILEGED_TIMEOUT: Final[float] = 180.0

#: Ceiling for an unprivileged CLI probe.  These never prompt, so if one has
#: not answered in a couple of seconds something is wrong and we move on.
_UNPRIVILEGED_TIMEOUT: Final[float] = 8.0

#: How much of a failing command's stderr to keep for the error message.  A
#: runaway helper must not be able to grow the GUI's memory without bound.
_MAX_STDERR_BYTES: Final[int] = 8 * 1024

# pkexec's own exit codes, distinct from the child's (pkexec(1) "EXIT STATUS").
_PKEXEC_DISMISSED: Final[int] = 126   # authentication failed or dialog dismissed
_PKEXEC_NOT_AUTHORISED: Final[int] = 127  # not authorised, or command not found

# An argparse usage failure, which is how we detect "this CLI spells the
# subcommand differently" and fall through to the next spelling.
_USAGE_ERROR_RE: Final[re.Pattern[str]] = re.compile(
    r"invalid choice|unrecognized argument|unrecognised argument|"
    r"no such (command|option)|^usage:",
    re.IGNORECASE | re.MULTILINE,
)


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------

class BackendError(Exception):
    """A failure with something a person can actually read.

    Every message here follows one rule: describe what the *system* could not
    do, never what the user did wrong.  "The camera did not send any pictures"
    -- not "you failed to present a face".
    """

    def __init__(self, message: str, detail: str = "", *, retryable: bool = True) -> None:
        super().__init__(message)
        self.message = message
        #: Technical text (stderr, an exception string) for the expander that
        #: curious users open and support staff ask for.  May be empty.
        self.detail = detail.strip()
        self.retryable = retryable


class AuthorisationCancelled(BackendError):
    """The user dismissed the polkit prompt, or it timed out.

    Distinguished from a real failure because there is nothing to apologise
    for and nothing to diagnose -- the correct UI is a quiet return to the
    previous screen with the action still available.
    """

    def __init__(self) -> None:
        super().__init__(
            "Administrator approval is needed to change face data.",
            "pkexec exited 126 (prompt dismissed or authentication failed)",
        )


class HelperMissing(BackendError):
    """The ``iris`` command-line helper is not installed or not on PATH."""

    def __init__(self) -> None:
        super().__init__(
            "The Iris helper is not installed yet.",
            "Could not find an 'iris' executable on PATH or in /usr/bin. "
            "Run the Iris installer, then reopen this window.",
            retryable=False,
        )


# --------------------------------------------------------------------------
# identity and helper discovery
# --------------------------------------------------------------------------

def current_user() -> str:
    """Return the account whose faces this app manages.

    Resolved from the real uid rather than ``$USER``/``$LOGNAME``.  The name is
    passed to a helper running as root to decide *whose* template file gets
    written, so it must come from the kernel, not from an environment variable
    any process in the session could have set.
    """
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:  # pragma: no cover - uid with no passwd entry
        _LOG.warning("uid %d has no passwd entry", os.getuid())
        return os.environ.get("USER", "unknown")


def user_display_name() -> str:
    """The user's real name from GECOS, falling back to the account name."""
    try:
        entry = pwd.getpwuid(os.getuid())
    except KeyError:  # pragma: no cover
        return current_user()
    full = entry.pw_gecos.split(",", 1)[0].strip()
    return full or entry.pw_name


_BIN_CANDIDATES: Final[tuple[str, ...]] = ("/usr/bin/iris", "/usr/local/bin/iris")


def iris_binary() -> str | None:
    """Absolute path to the ``iris`` CLI, or ``None`` if it is not installed.

    An absolute path matters for ``pkexec``: it refuses to run anything it
    cannot resolve to a real file, and resolving it ourselves means the polkit
    dialog names the binary the user is actually authorising.
    """
    found = shutil.which("iris")
    if found:
        return os.path.realpath(found)

    for candidate in _BIN_CANDIDATES:
        if os.access(candidate, os.X_OK):
            return candidate

    # Running from a source checkout before `make install`: src/iris/gui/backend.py
    # -> parents[3] is the repository root.
    repo_bin = Path(__file__).resolve().parents[3] / "bin" / "iris"
    if repo_bin.is_file() and os.access(repo_bin, os.X_OK):
        return str(repo_bin)

    return None


def pkexec_binary() -> str | None:
    """Absolute path to ``pkexec``, or ``None`` on a system without polkit."""
    found = shutil.which("pkexec")
    return os.path.realpath(found) if found else None


# --------------------------------------------------------------------------
# value objects
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Face:
    """One enrolled face, as reported by the store.  Never holds embeddings."""

    name: str
    created: str
    samples: int

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Face":
        """Build from a protocol/CLI dict, tolerating missing or odd fields."""
        return cls(
            name=str(raw.get("name", "")),
            created=str(raw.get("created", "")),
            samples=int(raw.get("samples", 0) or 0),
        )

    @property
    def title(self) -> str:
        """Display name.  ``default`` is an implementation detail, not a label."""
        return "Your face" if self.name == "default" else self.name

    @property
    def subtitle(self) -> str:
        """"Added 2 September 2026 - 12 samples", degrading if either is unknown."""
        parts: list[str] = []
        when = self.created_display
        if when:
            parts.append(f"Added {when}")
        if self.samples:
            parts.append(f"{self.samples} sample{'s' if self.samples != 1 else ''}")
        return " · ".join(parts) or "Enrolled"

    @property
    def created_display(self) -> str:
        """Human date, or ``""`` if the stored stamp cannot be parsed."""
        if not self.created:
            return ""
        try:
            # The store writes RFC 3339 with a 'Z' suffix, which
            # fromisoformat only learned to accept in 3.11 -- fine on 3.14,
            # but normalise anyway so a hand-edited stamp still renders.
            stamp = datetime.fromisoformat(self.created.replace("Z", "+00:00"))
        except ValueError:
            return ""
        return stamp.astimezone().strftime("%-d %B %Y")


@dataclass(frozen=True, slots=True)
class CameraInfo:
    """A selectable capture device.  Metadata nodes never become one of these."""

    path: str
    name: str
    is_ir: bool
    formats: tuple[str, ...]

    @property
    def title(self) -> str:
        return self.name or self.path

    @property
    def subtitle(self) -> str:
        kind = "Infrared" if self.is_ir else "Colour"
        formats = ", ".join(self.formats) if self.formats else "unknown format"
        return f"{kind} · {self.path} · {formats}"


def list_capture_cameras() -> list[CameraInfo]:
    """Enumerate selectable capture devices, infrared first.

    Metadata nodes are dropped outright: on this hardware ``/dev/video1`` and
    ``/dev/video3`` deliver UVC payload headers rather than images, and offering
    one in a camera picker would hand the user a device that silently produces
    no frames.

    :mod:`iris.camera` is imported lazily because it pulls in OpenCV (~250 ms,
    tens of megabytes); the welcome screen must not pay for that.
    """
    from iris.camera import list_cameras

    cameras = [
        CameraInfo(
            path=str(entry["path"]),
            name=str(entry["name"]),
            is_ir=bool(entry["is_ir"]),
            formats=tuple(str(f) for f in entry.get("formats") or ()),
        )
        for entry in list_cameras()
        if not entry.get("is_metadata")
    ]
    # Infrared devices first: they are the only ones that work in the dark and
    # the only ones with any spoof resistance, so they are what we want the
    # user to pick by default.
    cameras.sort(key=lambda cam: (not cam.is_ir, cam.path))
    return cameras


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

def load_config() -> dict[str, Any]:
    """Read ``/etc/iris/config.toml``, merged over the defaults.  Never raises."""
    from iris.config import load_config as _load

    return _load()


# --------------------------------------------------------------------------
# running commands
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class _Result:
    """Outcome of one CLI invocation."""

    returncode: int
    stdout: str
    stderr: str
    argv: list[str]


def _spawn_argv(binary: str, subcommand: Sequence[str], args: Sequence[str],
                *, privileged: bool) -> list[str]:
    """Build the argv for one invocation, prefixed with pkexec when needed."""
    argv = [binary, *subcommand, *args]
    if not privileged:
        return argv
    pkexec = pkexec_binary()
    if pkexec is None:
        raise BackendError(
            "This system has no polkit agent, so Iris cannot ask for approval.",
            "pkexec was not found on PATH.",
            retryable=False,
        )
    return [pkexec, *argv]


def _run_cli(
    subcommands: Sequence[Sequence[str]],
    args: Sequence[str],
    *,
    privileged: bool,
    stdin_text: str | None = None,
    timeout: float | None = None,
) -> _Result:
    """Run the CLI, trying each spelling in *subcommands* until one is understood.

    A usage error (argparse exits 2 and prints "invalid choice") means this
    build of the CLI spells the subcommand differently, so we fall through to
    the next candidate.  Any *other* non-zero exit is a real failure and is
    returned immediately -- retrying a command that ran and failed would just
    show the user a second password prompt for the same broken operation.
    """
    binary = iris_binary()
    if binary is None:
        raise HelperMissing()

    if timeout is None:
        timeout = _PRIVILEGED_TIMEOUT if privileged else _UNPRIVILEGED_TIMEOUT

    last: _Result | None = None
    for subcommand in subcommands:
        argv = _spawn_argv(binary, subcommand, args, privileged=privileged)
        _LOG.debug("running %s", " ".join(argv))
        try:
            completed = subprocess.run(
                argv,
                input=stdin_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise HelperMissing() from exc
        except subprocess.TimeoutExpired as exc:
            raise BackendError(
                "The Iris helper did not respond in time.",
                f"{' '.join(argv)} timed out after {timeout:.0f}s",
            ) from exc
        except OSError as exc:
            raise BackendError(
                "The Iris helper could not be started.", str(exc)
            ) from exc

        last = _Result(
            completed.returncode,
            completed.stdout or "",
            (completed.stderr or "")[:_MAX_STDERR_BYTES],
            argv,
        )
        if last.returncode == 2 and _USAGE_ERROR_RE.search(last.stderr):
            _LOG.debug("CLI did not understand %r; trying the next spelling", subcommand)
            continue
        return last

    assert last is not None  # `subcommands` is never empty
    return last


def _final_json(result: _Result) -> dict[str, Any]:
    """Return the last JSON object on stdout, or raise a readable error.

    The CLI may emit progress lines before its verdict, so the *last* parseable
    object is the answer.  A command that exits non-zero without any JSON at
    all still has to produce something a person can act on, which is what the
    exit-code branches below are for.
    """
    payload: dict[str, Any] | None = None
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            payload = parsed

    if result.returncode == _PKEXEC_DISMISSED and payload is None:
        raise AuthorisationCancelled()

    if payload is not None and payload.get("ok"):
        return payload

    raise _failure_error(result, payload)


def _failure_error(result: _Result, payload: dict[str, Any] | None) -> BackendError:
    """Turn a failed invocation into a message written for a human."""
    detail = result.stderr.strip() or result.stdout.strip()
    command = " ".join(result.argv)

    if payload is not None:
        reason = str(payload.get("reason", ""))
        if reason:
            from iris.protocol import describe_reason

            return BackendError(describe_reason(reason), str(payload.get("error", detail)))
        if payload.get("error"):
            return BackendError(str(payload["error"]), detail)

    if result.returncode == _PKEXEC_DISMISSED:
        return AuthorisationCancelled()

    if result.returncode == _PKEXEC_NOT_AUTHORISED:
        return BackendError(
            "This account is not allowed to change face data.",
            f"pkexec exited 127 for: {command}\n{detail}",
            retryable=False,
        )

    return BackendError(
        "That did not work. Nothing was changed.",
        f"{command}\nexit status {result.returncode}\n{detail}",
    )


# --------------------------------------------------------------------------
# asynchronous dispatch
# --------------------------------------------------------------------------

#: ``callback(result, error)`` -- exactly one of the two is ``None``.
DoneCallback = Callable[[Any, BackendError | None], None]


def run_async(work: Callable[[], Any], done: DoneCallback, *, name: str = "iris-gui-op") -> None:
    """Run *work* on a worker thread and deliver its outcome on the main loop.

    Everything in this module that can block -- a subprocess, a socket, a
    polkit prompt the user takes a minute to answer -- goes through here.  The
    GTK main loop is never allowed to wait on any of it.
    """

    def target() -> None:
        try:
            value: Any = work()
            error: BackendError | None = None
        except BackendError as exc:
            value, error = None, exc
        except Exception as exc:  # noqa: BLE001 - a worker crash must reach the UI
            _LOG.exception("unhandled error in background operation %s", name)
            value, error = None, BackendError(
                "Something went wrong inside Iris.", f"{type(exc).__name__}: {exc}"
            )
        GLib.idle_add(_deliver, done, value, error, priority=GLib.PRIORITY_DEFAULT)

    threading.Thread(target=target, name=name, daemon=True).start()


def _deliver(done: DoneCallback, value: Any, error: BackendError | None) -> bool:
    """Main-loop trampoline.  A raising callback must not kill the idle source."""
    try:
        done(value, error)
    except Exception:  # noqa: BLE001
        _LOG.exception("error in completion callback")
    return GLib.SOURCE_REMOVE


# --------------------------------------------------------------------------
# face management
# --------------------------------------------------------------------------

def _daemon_request(request: dict[str, Any]) -> dict[str, Any] | None:
    """Try the daemon socket.  ``None`` means "not reachable, use the CLI"."""
    from iris import protocol

    try:
        response = protocol.send_request(request, _DAEMON_TIMEOUT)
    except protocol.ProtocolError as exc:
        # Expected whenever the GUI is unprivileged: the socket is 0600 root.
        _LOG.debug("daemon unavailable for %s: %s", request.get("op"), exc)
        return None
    return response if response.get("ok") else None


def fetch_faces(user: str, *, allow_prompt: bool) -> list[Face]:
    """List *user*'s enrolled faces.

    Escalates only as far as it has to: daemon socket, then the CLI as this
    user, then -- and only when *allow_prompt* is set, i.e. the user pressed
    "Unlock" -- the CLI under pkexec.  Opening the settings panel must never
    fire a password prompt on its own.

    :raises BackendError: the list could not be read.  When *allow_prompt* is
        false this is the ordinary "we need approval" case and the caller shows
        an unlock affordance rather than an error.
    """
    response = _daemon_request({"op": "list", "user": user})
    if response is not None:
        return [Face.from_dict(f) for f in response.get("faces", []) if isinstance(f, dict)]

    result = _run_cli([["list"]], ["--user", user, "--json"], privileged=False)
    if result.returncode == 0:
        try:
            return _faces_from(_final_json(result))
        except BackendError:
            pass  # fall through to the privileged path

    if not allow_prompt:
        raise BackendError(
            "Face data is protected and needs administrator approval to read.",
            "Templates live in /var/lib/iris (root-only) and irisd's socket is "
            "root-only too, so an unprivileged window cannot read them.",
        )

    return _faces_from(_final_json(
        _run_cli([["list"]], ["--user", user, "--json"], privileged=True)
    ))


def _faces_from(payload: dict[str, Any]) -> list[Face]:
    faces = payload.get("faces")
    if not isinstance(faces, list):
        return []
    return [Face.from_dict(f) for f in faces if isinstance(f, dict)]


def remove_face(user: str, name: str) -> None:
    """Delete one enrolled face.  Prompts for approval."""
    _final_json(_run_cli(
        [["remove"]], ["--user", user, "--name", name, "--json"], privileged=True
    ))


def clear_faces(user: str) -> None:
    """Delete every enrolled face for *user*.  Prompts for approval."""
    _final_json(_run_cli([["clear"]], ["--user", user, "--json"], privileged=True))


def save_config(cfg: dict[str, Any]) -> None:
    """Persist the whole configuration.  Prompts for approval.

    The complete document is sent, not a diff: :func:`iris.config.save_config`
    merges over the defaults and rewrites the file atomically, so one
    invocation -- one password prompt -- applies every pending change at once.
    """
    _final_json(_run_cli(
        [["config-set"], ["config", "set"]],
        ["--json"],
        privileged=True,
        stdin_text=json.dumps(cfg) + "\n",
    ))


# --------------------------------------------------------------------------
# enrolment
# --------------------------------------------------------------------------

#: If no progress line arrives for this long while the helper is still running,
#: the UI switches to an indeterminate state.  Sized well above the ~2 s a
#: polkit prompt plus model load takes, so it only fires on a genuine stall
#: (most often a helper that forgot to flush stdout).
_STALL_SECONDS: Final[float] = 6.0


@dataclass(frozen=True, slots=True)
class EnrollProgress:
    """One progress update from the enrolment helper."""

    fraction: float
    hint: str
    samples: int | None = None
    total: int | None = None


class EnrollProcess:
    """Runs ``pkexec iris enroll`` and streams its progress to the UI.

    Enrolment writes to ``/var/lib/iris``, so it has to happen as root; the GUI
    is a spectator that parses stdout.  Three threads are involved: one draining
    stdout, one draining stderr (a full pipe would deadlock the helper), and one
    waiting for exit.  Every callback is re-entered on the GTK main loop via
    :func:`GLib.idle_add`, so handlers can touch widgets directly.

    All callbacks are optional and every one of them is called at most in the
    order: ``on_progress``* then exactly one of ``on_finished``.
    """

    def __init__(
        self,
        user: str,
        name: str,
        *,
        on_progress: Callable[[EnrollProgress], None] | None = None,
        on_stalled: Callable[[], None] | None = None,
        on_finished: Callable[[dict[str, Any] | None, BackendError | None], None] | None = None,
    ) -> None:
        self._user = user
        self._name = name
        self._on_progress = on_progress
        self._on_stalled = on_stalled
        self._on_finished = on_finished

        self._proc: subprocess.Popen[str] | None = None
        self._argv: list[str] = []
        self._stderr: list[str] = []
        self._final: dict[str, Any] | None = None
        self._cancelled = threading.Event()
        self._finished = threading.Event()
        self._last_progress_at = time.monotonic()
        self._stall_source: int = 0
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._proc is not None and not self._finished.is_set()

    def start(self) -> None:
        """Launch the helper.  Returns immediately; watch the callbacks.

        :raises BackendError: the helper could not be launched at all.  A
            failure *after* launch is reported through ``on_finished`` instead,
            because by then there is a partially-run operation to describe.
        """
        binary = iris_binary()
        if binary is None:
            raise HelperMissing()

        argv = _spawn_argv(
            binary, ["enroll"],
            ["--user", self._user, "--name", self._name, "--json"],
            privileged=True,
        )
        self._argv = argv
        _LOG.info("starting enrolment: %s", " ".join(argv))

        try:
            self._proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,  # line buffered on our side of the pipe
            )
        except OSError as exc:
            raise BackendError(
                "Iris could not start the enrolment helper.", str(exc)
            ) from exc

        self._last_progress_at = time.monotonic()
        threading.Thread(target=self._pump_stdout, name="iris-enroll-out", daemon=True).start()
        threading.Thread(target=self._pump_stderr, name="iris-enroll-err", daemon=True).start()
        threading.Thread(target=self._await_exit, name="iris-enroll-wait", daemon=True).start()
        self._stall_source = GLib.timeout_add(1000, self._check_stall)

    def cancel(self) -> None:
        """Stop the enrolment, as far as an unprivileged parent is able to.

        ``pkexec`` ``execve``s the helper in its own process after raising
        privileges, so once the user has authenticated the process is owned by
        root and ``kill(2)`` from this uid returns EPERM.  Two things do work:

        * before authentication completes the process is still ours, so
          ``terminate()`` cancels the polkit prompt cleanly;
        * afterwards, closing our end of the pipes makes the helper's next
          progress write fail with EPIPE, which is the documented way to tell
          it to stop.

        Either way the UI stops waiting immediately -- a cancel that leaves the
        user staring at a spinner is not a cancel.
        """
        self._cancelled.set()
        proc = self._proc
        if proc is None:
            return

        try:
            proc.terminate()
        except PermissionError:
            _LOG.debug("cannot signal the root-owned helper; closing pipes instead")
        except (ProcessLookupError, OSError) as exc:
            _LOG.debug("terminate() failed: %s", exc)

        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    # -- worker threads ----------------------------------------------------

    def _pump_stdout(self) -> None:
        """Parse the helper's newline-delimited JSON as it arrives."""
        proc = self._proc
        if proc is None or proc.stdout is None:  # pragma: no cover - set in start()
            return
        try:
            for line in proc.stdout:
                if self._cancelled.is_set():
                    return
                self._consume_line(line)
        except (ValueError, OSError):
            # ValueError: our own cancel() closed the pipe mid-read.
            pass

    def _consume_line(self, line: str) -> None:
        text = line.strip()
        if not text:
            return

        message = _parse_progress_line(text)
        if message is None:
            _LOG.debug("enroll: %s", text)
            return

        if "ok" in message:
            with self._lock:
                self._final = message
            return

        self._last_progress_at = time.monotonic()
        progress = EnrollProgress(
            fraction=_clamp01(message.get("progress", 0.0)),
            hint=str(message.get("hint", "") or ""),
            samples=_as_int(message.get("samples")),
            total=_as_int(message.get("total")),
        )
        if self._on_progress is not None:
            GLib.idle_add(self._emit_progress, progress)

    def _emit_progress(self, progress: EnrollProgress) -> bool:
        if not self._cancelled.is_set() and self._on_progress is not None:
            self._on_progress(progress)
        return GLib.SOURCE_REMOVE

    def _pump_stderr(self) -> None:
        """Drain stderr so a chatty helper cannot deadlock on a full pipe."""
        proc = self._proc
        if proc is None or proc.stderr is None:  # pragma: no cover
            return
        kept = 0
        try:
            for line in proc.stderr:
                if kept < _MAX_STDERR_BYTES:
                    self._stderr.append(line)
                    kept += len(line)
        except (ValueError, OSError):
            pass

    def _await_exit(self) -> None:
        proc = self._proc
        if proc is None:  # pragma: no cover
            return
        returncode = proc.wait()
        self._finished.set()
        GLib.idle_add(self._emit_finished, returncode)

    def _emit_finished(self, returncode: int) -> bool:
        if self._stall_source:
            GLib.source_remove(self._stall_source)
            self._stall_source = 0

        if self._cancelled.is_set() or self._on_finished is None:
            return GLib.SOURCE_REMOVE

        with self._lock:
            final = self._final
        stderr = "".join(self._stderr).strip()

        if final is not None and final.get("ok"):
            self._on_finished(final, None)
        else:
            # The real argv, so the error detail names the exact command the
            # user can rerun in a terminal to see the failure for themselves.
            result = _Result(returncode, "", stderr, self._argv)
            self._on_finished(None, _failure_error(result, final))
        return GLib.SOURCE_REMOVE

    def _check_stall(self) -> bool:
        """Tell the UI when the helper has gone quiet, so it can stop lying.

        A progress ring frozen at 12% reads as "broken"; an indeterminate
        "Working" reads as "still going".  The second is true more often, and
        is the honest thing to show when we genuinely do not know.
        """
        if self._finished.is_set() or self._cancelled.is_set():
            self._stall_source = 0
            return GLib.SOURCE_REMOVE
        if time.monotonic() - self._last_progress_at > _STALL_SECONDS:
            if self._on_stalled is not None:
                self._on_stalled()
            self._last_progress_at = time.monotonic()  # report once per interval
        return GLib.SOURCE_CONTINUE


def _parse_progress_line(text: str) -> dict[str, Any] | None:
    """Parse one helper output line, tolerating a non-JSON build.

    The JSON form is the contract.  The percentage fallback exists because a
    frozen ring is a much worse failure than a slightly coarse one, and
    "Capturing... 40%" is a shape any CLI might print.
    """
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    match = re.search(r"(\d{1,3}(?:\.\d+)?)\s*%", text)
    if match is None:
        return None
    hint = text[: match.start()].strip(" .…-:")
    return {"progress": float(match.group(1)) / 100.0, "hint": hint}


def _clamp01(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:  # NaN
        return 0.0
    return min(1.0, max(0.0, number))


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# live preview
# --------------------------------------------------------------------------

#: Longest edge of a preview frame handed to the UI.  The IR sensor is
#: 640x360 and the mask is a circle a few hundred points across, so scaling
#: down first cuts the bytes copied per frame by ~60% for no visible loss.
_PREVIEW_MAX_EDGE: Final[int] = 480

#: Each pass over ``raw_frames`` is bounded; the worker loops until told to
#: stop.  Re-entering does not reopen the device, so this is nearly free.
_PREVIEW_CHUNK_SECONDS: Final[float] = 5.0


@dataclass(frozen=True, slots=True)
class PreviewFrame:
    """One frame from the preview worker.

    Dark frames of the infrared strobe carry ``data=None``: they are reported
    for their statistics but have no picture worth showing.  See
    :meth:`PreviewWorker._handle_frame` for why.
    """

    #: Packed RGB pixels, already scaled, contrast-stretched and mirrored --
    #: or ``None`` for a frame with nothing to display.
    data: GLib.Bytes | None
    width: int
    height: int
    stride: int
    #: Mean brightness of the *original* grayscale frame, 0-255.  The IR
    #: emitter strobes, so this alternates between ~1 and ~55-62; the camera
    #: test uses that alternation to prove the emitter is firing.
    mean: float
    #: False for a frame below the illumination threshold in IR mode.
    lit: bool = True
    #: Number of faces the detector found, or ``None`` when detection is off.
    faces: int | None = None

    def to_texture(self) -> Gdk.Texture | None:
        """Wrap the frame as a texture, or ``None`` if it has no picture.

        Call on the main thread: the texture is handed straight to a widget.
        """
        if self.data is None:
            return None
        return Gdk.MemoryTexture.new(
            self.width, self.height, Gdk.MemoryFormat.R8G8B8, self.data, self.stride
        )


class PreviewWorker:
    """Streams frames from a capture device to the UI, off the main loop.

    The camera is opened, read and released entirely on a worker thread, so a
    device that takes half a second to negotiate a format -- as this IR sensor
    does -- never freezes the window.  Frames are converted to packed RGB on
    the worker too; the main thread only wraps the finished buffer in a
    texture.

    Back-pressure matters here.  If the UI is busy, dropping frames is correct:
    a preview that is 60 ms stale looks fine, whereas a queue of stale frames
    looks like lag and grows without bound.  ``_pending`` enforces at most one
    undelivered frame.
    """

    def __init__(
        self,
        device: str,
        width: int,
        height: int,
        *,
        ir_mode: bool = True,
        min_brightness: float = 20.0,
        detect_faces: bool = False,
        config: dict[str, Any] | None = None,
        on_frame: Callable[[PreviewFrame], None] | None = None,
        on_error: Callable[[BackendError], None] | None = None,
        on_stopped: Callable[[], None] | None = None,
    ) -> None:
        self._device = device
        self._width = int(width)
        self._height = int(height)
        self._ir_mode = bool(ir_mode)
        self._min_brightness = float(min_brightness)
        self._detect_faces = bool(detect_faces)
        self._config = config or {}

        self._on_frame = on_frame
        self._on_error = on_error
        self._on_stopped = on_stopped

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pending = False
        self._lock = threading.Lock()
        self._frames_seen = 0
        #: Built lazily on the worker thread, so constructing a PreviewWorker
        #: on the main loop never pays for importing OpenCV.
        self._clahe_impl: Any = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def frames_seen(self) -> int:
        return self._frames_seen

    def start(self) -> None:
        """Begin streaming.  Safe to call on an already-running worker."""
        if self.running:
            return
        self._stop.clear()
        self._frames_seen = 0
        self._thread = threading.Thread(
            target=self._run, name="iris-preview", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Ask the worker to finish.  Returns immediately.

        ``on_stopped`` fires on the main loop once the ``VideoCapture`` has
        actually been released -- which is the moment, and the only moment, at
        which it is safe to hand ``/dev/video2`` to the enrolment helper.  The
        wait is short: :meth:`iris.camera.Camera.frames` checks its deadline
        between reads and a read returns within one frame period (~67 ms at
        15 fps).
        """
        if not self.running:
            if self._on_stopped is not None:
                GLib.idle_add(self._emit_stopped)
            return
        self._stop.set()

    # -- worker ------------------------------------------------------------

    def _run(self) -> None:
        from iris.camera import Camera, CameraError

        engine = self._make_engine() if self._detect_faces else None

        try:
            with Camera(
                self._device,
                self._width,
                self._height,
                min_brightness=self._min_brightness,
                ir_mode=self._ir_mode,
            ) as camera:
                while not self._stop.is_set():
                    for frame in camera.raw_frames(_PREVIEW_CHUNK_SECONDS):
                        if self._stop.is_set():
                            break
                        self._handle_frame(frame, engine)
        except CameraError as exc:
            self._report(_camera_error(self._device, exc))
        except Exception as exc:  # noqa: BLE001 - the UI must hear about it
            _LOG.exception("preview worker failed")
            self._report(BackendError(
                "The camera preview stopped unexpectedly.",
                f"{type(exc).__name__}: {exc}",
            ))
        finally:
            # Released by the `with` block above; only now is the device free.
            GLib.idle_add(self._emit_stopped)

    def _make_engine(self) -> Any:
        """Build a FaceEngine for the preview, or ``None`` if unavailable.

        Detection in the preview is a nicety -- it is what lets the camera test
        say "we can see a face" instead of just "frames are arriving" -- so a
        missing or unreadable model must degrade to a working preview rather
        than an error.
        """
        try:
            from iris.engine import FaceEngine

            return FaceEngine(self._config or load_config())
        except Exception as exc:  # noqa: BLE001
            _LOG.info("preview face detection unavailable: %s", exc)
            return None

    def _handle_frame(self, frame: Any, engine: Any) -> None:
        import cv2
        import numpy as np

        self._frames_seen += 1

        # raw_frames() yields BGR; measure brightness on the luma channel so the
        # number means the same thing for an IR (grayscale) and an RGB device.
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        mean = float(gray.mean())

        # The infrared emitter strobes: alternate frames are essentially black
        # (mean ~1) and the rest are lit (mean ~55-62). Painting both halves
        # would not be a "live preview", it would be a 7.5 Hz black-and-white
        # strobe light aimed at the user's face -- unpleasant, useless for
        # framing, and a genuine problem for anyone photosensitive. So dark
        # frames are counted and reported but not drawn, and the last lit frame
        # stays on screen: a steady 7.5 fps portrait rather than a flicker.
        lit = (not self._ir_mode) or mean >= self._min_brightness

        faces: int | None = None
        if engine is not None and lit:
            # Only lit frames are worth running the detector on: during the
            # dark half of the strobe there is nothing in the image at all.
            try:
                detections = engine.detect(gray)
                faces = 0 if detections is None else int(len(detections))
            except Exception as exc:  # noqa: BLE001
                _LOG.debug("preview detection failed: %s", exc)

        with self._lock:
            if self._pending:
                return  # the UI has not drawn the previous frame yet
            self._pending = True

        if not lit:
            GLib.idle_add(
                self._emit_frame,
                PreviewFrame(None, 0, 0, 0, mean=mean, lit=False, faces=faces),
            )
            return

        height, width = frame.shape[:2]
        scale = min(1.0, _PREVIEW_MAX_EDGE / float(max(width, height)))
        if scale < 1.0:
            # Scale before the contrast pass: same result, a third of the work.
            frame = cv2.resize(
                frame, (round(width * scale), round(height * scale)),
                interpolation=cv2.INTER_AREA,
            )

        if self._ir_mode:
            # A lit infrared frame is dim and low in contrast -- a face in one
            # is barely distinguishable from the wall behind it. The same CLAHE
            # the recognition engine applies before detection makes it legible,
            # so what the user lines up against is what Iris is working from.
            small_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
            display = cv2.cvtColor(self._clahe().apply(small_gray), cv2.COLOR_GRAY2RGB)
        else:
            display = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        # Mirror horizontally: people expect a preview of themselves to behave
        # like a mirror, and an unmirrored one makes "turn slightly left" read
        # as the wrong direction.
        rgb = np.ascontiguousarray(display[:, ::-1])
        height, width = rgb.shape[:2]

        GLib.idle_add(self._emit_frame, PreviewFrame(
            data=GLib.Bytes.new(rgb.tobytes()),
            width=int(width),
            height=int(height),
            stride=int(width * 3),
            mean=mean,
            lit=True,
            faces=faces,
        ))

    def _clahe(self) -> Any:
        """The contrast equaliser, built once and reused.

        Creating one per frame is pure waste: the object holds no state between
        ``apply`` calls, and this runs 7-8 times a second for as long as the
        preview is on screen.
        """
        if self._clahe_impl is None:
            import cv2

            self._clahe_impl = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        return self._clahe_impl

    # -- main-loop trampolines --------------------------------------------

    def _emit_frame(self, preview: PreviewFrame) -> bool:
        with self._lock:
            self._pending = False
        if not self._stop.is_set() and self._on_frame is not None:
            self._on_frame(preview)
        return GLib.SOURCE_REMOVE

    def _emit_stopped(self) -> bool:
        if self._on_stopped is not None:
            self._on_stopped()
        return GLib.SOURCE_REMOVE

    def _report(self, error: BackendError) -> None:
        if self._on_error is not None:
            GLib.idle_add(self._emit_error, error)

    def _emit_error(self, error: BackendError) -> bool:
        if self._on_error is not None:
            self._on_error(error)
        return GLib.SOURCE_REMOVE


def _camera_error(device: str, exc: Exception) -> BackendError:
    """Translate a camera failure into something worth reading.

    Each branch names the thing to try next, because "camera error" on its own
    tells the user nothing they can act on.
    """
    text = str(exc)
    if "metadata node" in text:
        return BackendError(
            "That device does not produce pictures.",
            f"{device} is a V4L2 metadata node. Choose an infrared camera in "
            f"Settings instead.\n\n{text}",
        )
    if "does not exist" in text:
        return BackendError(
            "The selected camera is no longer connected.",
            f"{device} is gone. Pick another camera in Settings.\n\n{text}",
        )
    if "permission" in text.lower():
        return BackendError(
            "Iris is not allowed to use the camera.",
            f"No read access to {device}. The installer grants an ACL for the "
            f"desktop user; log out and back in, or re-run the installer.\n\n{text}",
        )
    return BackendError(
        "The camera could not be started.",
        f"{device}: {text}",
    )
