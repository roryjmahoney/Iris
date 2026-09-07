#!/usr/bin/env python3
"""Iris command-line interface — installed as ``/usr/bin/iris``.

Everything an administrator or a user needs to drive Iris from a terminal:
enrolment, template management, a real authentication dry-run, camera
enumeration, configuration editing and a diagnostic ``doctor``.

Design rules that shape this file
---------------------------------

**The daemon owns the hardware and the templates.**  Almost every subcommand is
a thin, well-presented client of the ``irisd`` UNIX socket
(:mod:`iris.protocol`).  Where the daemon is unreachable but the operation is
still possible locally — reading ``/etc/iris/config.toml`` (0644), enumerating
V4L2 nodes, or touching ``/var/lib/iris`` as root — the command degrades to
doing it directly and says so.  That fallback matters: the most likely moment
someone reaches for this CLI is when the daemon is *not* working.

**Root is required only where it is genuinely required.**  ``enroll``,
``remove`` and ``clear`` mutate root-owned biometric state, so they refuse
early with a copy-pasteable ``sudo`` line rather than failing halfway with
``EACCES``.  The read-only commands run as anybody; on a stock install the
socket is ``0600 root:root``, so the ones that need the daemon explain that
too instead of printing a bare "connection refused".

**Output degrades gracefully.**  Colour is emitted only to a TTY (and never
when ``NO_COLOR`` is set or ``--color never`` is passed), and every box-drawing
or check-mark glyph has an ASCII fallback chosen from the output encoding, so
piping into ``grep``, ``less -R`` or a systemd journal all behave.

**Imports stay lazy.**  ``iris.camera``/``iris.engine`` pull in OpenCV (~200ms,
tens of MB).  ``iris config get`` and ``iris status`` must not pay for that, so
those modules are imported inside the functions that actually need them.
"""

from __future__ import annotations

import argparse
import difflib
import getpass
import json
import logging
import math
import os
import pwd
import shutil
import stat
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence, TextIO


def _bootstrap_import_path() -> None:
    """Make ``import iris`` work when this file is run straight from a checkout.

    Installed, the package is on ``sys.path`` already and this is a no-op.  In
    the source tree ``src/iris/cli.py`` needs ``src/`` on the path, which is
    exactly what a developer running ``python3 src/iris/cli.py doctor``
    expects.  The existence check keeps us from prepending a junk directory
    (e.g. ``/usr`` when this file has been copied to ``/usr/bin/iris``).
    """
    try:
        import iris  # noqa: F401  (probe only)
        return
    except ImportError:
        pass
    candidate = Path(__file__).resolve().parents[1]
    if (candidate / "iris" / "__init__.py").is_file():
        sys.path.insert(0, str(candidate))


_bootstrap_import_path()

from iris import __version__  # noqa: E402  (must follow the path bootstrap)
from iris import config as config_mod  # noqa: E402
from iris import protocol  # noqa: E402

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

PROG = "iris"

# Exit codes.  Distinct values so shell callers can react without parsing text.
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2          # argparse's own convention; kept for consistency
EXIT_UNAVAILABLE = 3    # irisd unreachable
EXIT_PERMISSION = 4     # needs root, or the socket refused us
EXIT_AUTH_FAILED = 5    # `iris test` ran fine but the face did not match
EXIT_INTERRUPTED = 130  # 128 + SIGINT

#: Model filenames, per SPEC.md "Paths".  Hard-coded rather than imported from
#: :mod:`iris.engine` so that ``iris doctor`` can report "models missing"
#: without first importing OpenCV (which is one of the things doctor checks).
DETECTOR_MODEL = "face_detection_yunet_2023mar.onnx"
RECOGNIZER_MODEL = "face_recognition_sface_2021dec.onnx"

#: Where the installer puts the PAM module.  Additional directories are probed
#: because the multiarch triplet differs on non-amd64 machines.
PAM_MODULE_NAME = "pam_iris.so"
PAM_MODULE_PATHS = (
    "/usr/lib/x86_64-linux-gnu/security/pam_iris.so",
    "/usr/lib/security/pam_iris.so",
    "/lib/security/pam_iris.so",
)
PAM_DIR = "/etc/pam.d"

#: SAFETY rule 2: the module must be stacked so a failure falls through to the
#: password prompt.  ``doctor`` checks the control field of every reference.
PAM_REQUIRED_CONTROL = "[success=done default=ignore]"

STORE_ROOT = "/var/lib/iris"
MASTER_KEY = "master.key"
SEALED_KEY = "master.key.tpm.priv"

TPM_DEVICE = "/dev/tpm0"
TPM_RM_DEVICE = "/dev/tpmrm0"

#: Default budget for the whole enrolment exchange.  Enrolment asks the user to
#: move their head through several poses, so it is a much longer conversation
#: than an authentication; the daemon finishes long before this fires.
ENROLL_TIMEOUT = 180.0

#: Short budget for control-plane requests (ping, list, config).
CONTROL_TIMEOUT = 5.0


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

#: Typographic characters swapped for ASCII when the output encoding cannot
#: represent them.  This covers our own strings *and* text that arrives from
#: elsewhere — a pose hint from the daemon or an exception message from a
#: library can contain anything, and a UnicodeEncodeError raised while printing
#: an error message is a spectacularly unhelpful failure mode.
_ASCII_FALLBACKS = str.maketrans({
    "—": "-", "–": "-", "→": "->", "←": "<-", "…": "...",
    "·": "*", "✓": "OK", "✗": "X", "″": '"', "’": "'", "“": '"', "”": '"',
})

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RED = "\033[31m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_CYAN = "\033[36m"


class Console:
    """Stdout/stderr writer that knows what the terminal can render.

    Two independent capabilities are probed, because they fail independently:
    ANSI colour (a TTY that is not ``dumb``, with ``NO_COLOR`` honoured) and
    non-ASCII glyphs (whether the stream's encoding can represent them —
    ``LC_ALL=C`` gives ASCII-only, and writing ``✓`` there raises).
    """

    def __init__(self, stream: TextIO | None = None, err: TextIO | None = None) -> None:
        self.stream: TextIO = stream if stream is not None else sys.stdout
        self.err: TextIO = err if err is not None else sys.stderr
        self.color = False
        self.unicode = False
        self.tty = False
        self.configure()

    # -- setup ------------------------------------------------------------

    def configure(self, when: str = "auto") -> None:
        self.tty = _is_tty(self.stream)
        if when == "always":
            self.color = True
        elif when == "never":
            self.color = False
        else:
            # https://no-color.org: any value, including empty, disables colour.
            self.color = (
                self.tty
                and "NO_COLOR" not in os.environ
                and os.environ.get("TERM", "") != "dumb"
            )
        self.unicode = _encodable(self.stream, "✓✗█░→·") and _encodable(self.err, "✓✗█░→·")
        if not self.unicode:
            # Last line of defence: transliteration below handles the characters
            # we know about, but a stray glyph in third-party text must degrade
            # to "?" rather than aborting the command mid-sentence.
            for stream in (self.stream, self.err):
                try:
                    stream.reconfigure(errors="replace")  # type: ignore[union-attr]
                except (AttributeError, ValueError, OSError):
                    pass

    # -- painting ---------------------------------------------------------

    def paint(self, text: str, *codes: str) -> str:
        if not self.color or not codes:
            return text
        return "".join(codes) + text + _RESET

    def bold(self, text: str) -> str:
        return self.paint(text, _BOLD)

    def dim(self, text: str) -> str:
        return self.paint(text, _DIM)

    def red(self, text: str) -> str:
        return self.paint(text, _RED)

    def green(self, text: str) -> str:
        return self.paint(text, _GREEN)

    def yellow(self, text: str) -> str:
        return self.paint(text, _YELLOW)

    def cyan(self, text: str) -> str:
        return self.paint(text, _CYAN)

    # -- writing ----------------------------------------------------------

    def text(self, value: str) -> str:
        """Transliterate typography the output encoding cannot represent."""
        return value if self.unicode else value.translate(_ASCII_FALLBACKS)

    def print(self, text: str = "") -> None:
        print(self.text(text), file=self.stream)

    def write(self, text: str) -> None:
        """Write without a newline and flush — used by the progress bar."""
        self.stream.write(self.text(text))
        self.stream.flush()

    def note(self, text: str) -> None:
        self.print(self.dim(text))

    def _flush_out(self) -> None:
        """Drain stdout before writing to stderr.

        stdout is block-buffered when it is a pipe while stderr is not, so
        without this a warning printed halfway through a command jumps ahead of
        every line that logically preceded it — which makes a captured log read
        as if the error happened first.
        """
        try:
            self.stream.flush()
        except (ValueError, OSError):  # closed or already-broken pipe
            pass

    def warn(self, text: str) -> None:
        self._flush_out()
        print(f"{self.paint('warning:', _YELLOW, _BOLD)} {self.text(text)}", file=self.err)

    def error(self, text: str) -> None:
        self._flush_out()
        print(f"{self.paint('error:', _RED, _BOLD)} {self.text(text)}", file=self.err)

    def hint(self, text: str) -> None:
        self._flush_out()
        arrow = "→" if self.unicode else "->"
        print(f"  {self.dim(arrow)} {self.text(text)}", file=self.err)

    # -- glyphs -----------------------------------------------------------

    @property
    def bar_chars(self) -> tuple[str, str]:
        return ("█", "░") if self.unicode else ("#", "-")

    _UNICODE_GLYPHS = {"ok": "✓", "warn": "!", "fail": "✗"}
    _ASCII_GLYPHS = {"ok": "OK", "warn": "WARN", "fail": "FAIL"}

    def status_symbol(self, status: str) -> str:
        """Return the ✓/!/✗ glyph (or its ASCII stand-in) for a check status."""
        glyphs = self._UNICODE_GLYPHS if self.unicode else self._ASCII_GLYPHS
        paint = {"ok": self.green, "warn": self.yellow, "fail": self.red}[status]
        return paint(glyphs[status])

    def status_cell(self, status: str) -> str:
        """The status glyph, painted and padded to :attr:`status_width`.

        Padding is applied to the *unpainted* glyph: ANSI escapes have zero
        display width but plenty of string length, so ``str.ljust`` on the
        coloured text would shove every column out of alignment.
        """
        glyphs = self._UNICODE_GLYPHS if self.unicode else self._ASCII_GLYPHS
        pad = " " * (self.status_width - len(glyphs[status]))
        return self.status_symbol(status) + pad

    @property
    def status_width(self) -> int:
        glyphs = self._UNICODE_GLYPHS if self.unicode else self._ASCII_GLYPHS
        return max(len(g) for g in glyphs.values())


def _is_tty(stream: TextIO) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):  # closed or exotic stream
        return False


def _encodable(stream: TextIO, probe: str) -> bool:
    """True when *probe* survives a round trip through *stream*'s encoding."""
    encoding = getattr(stream, "encoding", None) or "ascii"
    try:
        probe.encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return False
    return True


#: Configured once in :func:`main`; commands write through this.
console = Console()


class CommandError(Exception):
    """A user-facing failure: printed as ``error: …`` plus an optional hint."""

    def __init__(self, message: str, hint: str = "", code: int = EXIT_FAILURE) -> None:
        super().__init__(message)
        self.hint = hint
        self.code = code


# --------------------------------------------------------------------------
# small shared helpers
# --------------------------------------------------------------------------

def require_root(
    command: str, example: str = "", subject: str = "root-owned face templates"
) -> None:
    """Refuse a mutating command when we are not uid 0.

    Checked here, before any work, because the alternative is a half-finished
    enrolment and a raw ``PermissionError`` traceback from :mod:`iris.store`.

    *subject* names what the command would change, so the message is accurate
    for commands that touch the configuration rather than the templates.
    """
    if os.geteuid() == 0:
        return
    argv = example or f"{PROG} {command}"
    raise CommandError(
        f"'{PROG} {command}' changes {subject} and must be run as root",
        hint=f"re-run it with sudo:  sudo {argv}",
        code=EXIT_PERMISSION,
    )


def resolve_user(explicit: str | None) -> str:
    """Work out whose face we are operating on, and check the account exists.

    Under ``sudo`` the interesting user is the one who typed the command, not
    ``root`` — enrolling root's face by accident would be both useless and
    confusing — so ``SUDO_USER`` wins over the effective uid.
    """
    name = explicit or os.environ.get("SUDO_USER") or ""
    if not name:
        try:
            name = pwd.getpwuid(os.getuid()).pw_name
        except KeyError:  # uid with no passwd entry (container, LDAP outage)
            name = getpass.getuser()
    name = name.strip()
    if not name:
        raise CommandError(
            "could not determine which user to act on",
            hint=f"name one explicitly:  {PROG} <command> --user <username>",
        )
    try:
        pwd.getpwnam(name)
    except KeyError:
        raise CommandError(
            f"no such user: {name!r}",
            hint="check the spelling, or pass --user with an existing account",
        ) from None
    return name


#: Mirrors ``iris.store.MAX_NAME_LEN``; duplicated so the CLI can reject a bad
#: label before spending eight seconds of the user's time looking at a camera.
MAX_FACE_NAME = 64


def validate_face_name(name: str) -> str:
    """Check a face label locally so the daemon's rejection is never a surprise."""
    cleaned = name.strip()
    if not cleaned:
        raise CommandError(
            "a face name cannot be empty",
            hint="use a short label for the look, e.g. 'default' or 'glasses'",
        )
    if len(cleaned) > MAX_FACE_NAME:
        raise CommandError(f"a face name must be at most {MAX_FACE_NAME} characters")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in cleaned):
        raise CommandError("a face name must not contain control characters")
    return cleaned


def _socket_hint(path: str) -> str:
    """Explain *why* the daemon socket could not be used, in this situation."""
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return (
            "irisd does not appear to be running — start it with:  "
            "sudo systemctl start irisd"
        )
    except PermissionError:
        return (
            "the socket directory is root-only — re-run this command with sudo"
        )
    except OSError as exc:
        return f"cannot inspect {path}: {exc}"

    if not stat.S_ISSOCK(info.st_mode):
        return f"{path} exists but is not a socket; remove it and restart irisd"
    if os.geteuid() != 0:
        return (
            "the daemon socket is root-only (0600 root:root) — re-run this "
            "command with sudo"
        )
    return (
        "the socket exists but nothing accepted the connection; check:  "
        "systemctl status irisd  and  journalctl -u irisd -n 50"
    )


def _as_command_error(exc: protocol.ProtocolError, socket_path: str) -> CommandError:
    """Translate a transport failure into something worth reading."""
    if isinstance(exc, protocol.DaemonUnavailable):
        # Classify from the filesystem, not from the exception text: a socket
        # that exists but will not accept us is a permissions problem, and the
        # wording of the transport's message is not part of any contract.
        code = (
            EXIT_PERMISSION
            if os.geteuid() != 0 and os.path.exists(socket_path)
            else EXIT_UNAVAILABLE
        )
        return CommandError(
            f"cannot reach the Iris daemon on {socket_path}",
            hint=_socket_hint(socket_path),
            code=code,
        )
    if isinstance(exc, protocol.RequestTimeout):
        return CommandError(
            f"the Iris daemon stopped responding ({exc})",
            hint="check  journalctl -u irisd -n 50  for what it was doing",
            code=EXIT_UNAVAILABLE,
        )
    if isinstance(exc, protocol.Disconnected):
        return CommandError(
            f"the Iris daemon closed the connection ({exc})",
            hint="it may have crashed; check  journalctl -u irisd -n 50",
            code=EXIT_UNAVAILABLE,
        )
    return CommandError(f"protocol error talking to irisd: {exc}", code=EXIT_FAILURE)


def daemon_request(
    request: Mapping[str, Any],
    socket_path: str,
    timeout: float = CONTROL_TIMEOUT,
) -> dict[str, Any]:
    """Send one request and return the final response, or raise CommandError."""
    try:
        return protocol.send_request(request, timeout, socket_path)
    except protocol.ProtocolError as exc:
        raise _as_command_error(exc, socket_path) from exc


def daemon_ping(socket_path: str, timeout: float = 2.0) -> dict[str, Any] | None:
    """Ping the daemon, returning its reply or ``None`` if it is unreachable.

    Used where "no daemon" is a normal branch (``status``, ``doctor``, the
    local-fallback paths) rather than an error.
    """
    try:
        return protocol.send_request({"op": protocol.OP_PING}, timeout, socket_path)
    except protocol.ProtocolError:
        return None


def _failure_text(
    message: Mapping[str, Any],
    default: str = "the daemon reported a failure",
) -> str:
    """Human text for a ``{"ok": false, …}`` response.

    *default* is returned when the response carries no explanation at all, so a
    caller that knows the likely cause (``remove`` on a name that is not
    enrolled) can supply something more useful than a shrug.
    """
    reason = message.get("reason")
    parts: list[str] = []
    if isinstance(reason, str) and reason:
        parts.append(protocol.describe_reason(reason))
    for key in ("error", "message", "detail"):
        extra = message.get(key)
        if isinstance(extra, str) and extra.strip():
            parts.append(extra.strip())
            break
    return " ".join(parts) if parts else default


def emit_json(payload: Mapping[str, Any]) -> None:
    """Write one JSON object to stdout as a single line, flushed immediately.

    Written straight to ``sys.stdout`` rather than through :class:`Console`:
    the console transliterates typography for ASCII terminals, which is right
    for prose and wrong for a machine-readable stream.  ``ensure_ascii``
    (json's default) keeps the line safe under any locale.

    The flush is not optional.  Python block-buffers stdout when it is a pipe,
    which is exactly how the GTK front end runs us, so an unflushed progress
    line would sit in the buffer until the process exited and the enrolment
    ring would jump from 0% to done.
    """
    print(json.dumps(payload, ensure_ascii=True), file=sys.stdout, flush=True)


def _json_mode(args: argparse.Namespace) -> bool:
    """True when the invoking command was asked for machine-readable output."""
    return bool(getattr(args, "json", False) or getattr(args, "json_top", False))


def _resolve_name(args: argparse.Namespace, default: str | None = None) -> str:
    """Take the face label from either the positional or the ``--name`` form.

    Both spellings exist because humans type ``iris enroll glasses`` while the
    GUI builds an argv out of named options only; accepting both here means
    neither caller has to change.
    """
    positional = getattr(args, "name", None)
    named = getattr(args, "name_opt", None)
    if positional and named and positional != named:
        raise CommandError(
            f"conflicting face names: {positional!r} and --name {named!r}",
            code=EXIT_USAGE,
        )
    chosen = named or positional or default
    if not chosen:
        raise CommandError(
            "no face name given",
            hint=f"pass one, e.g.  {PROG} remove default --user USER",
            code=EXIT_USAGE,
        )
    return chosen


def _open_store() -> Any:
    """Construct a :class:`iris.store.TemplateStore` for the local fallback path."""
    try:
        from iris.store import TemplateStore
    except ImportError as exc:  # cryptography/numpy missing
        raise CommandError(
            f"cannot load the template store: {exc}",
            hint="install python3-cryptography and python3-numpy",
        ) from exc
    return TemplateStore(STORE_ROOT)


def _store_error(exc: Exception) -> CommandError:
    return CommandError(f"template store error: {exc}")


def list_faces(user: str, socket_path: str) -> tuple[list[dict[str, Any]], str]:
    """Return ``(faces, source)`` where source is ``"daemon"`` or ``"store"``.

    Falls back to reading ``/var/lib/iris`` directly when the daemon is down
    and we are root — that is precisely the situation in which someone is
    trying to find out what is enrolled.
    """
    try:
        reply = daemon_request(
            {"op": protocol.OP_LIST, "user": user}, socket_path, CONTROL_TIMEOUT
        )
    except CommandError:
        if os.geteuid() != 0:
            raise
        try:
            return _open_store().list_faces(user), "store"
        except Exception as exc:  # noqa: BLE001 - surfaced verbatim below
            raise _store_error(exc) from exc

    if not reply.get("ok"):
        raise CommandError(_failure_text(reply))
    faces = reply.get("faces")
    if not isinstance(faces, list):
        raise CommandError("the daemon returned a malformed face list")
    return [f for f in faces if isinstance(f, dict)], "daemon"


def render_table(headers: Sequence[str], rows: Sequence[Sequence[str]], indent: str = "  ") -> list[str]:
    """Lay out a left-aligned column table, sized to its contents.

    Widths are computed from the *unpainted* strings the caller passes in, so
    colour must be applied after layout (ANSI escapes have no display width but
    do have string length).
    """
    columns = len(headers)
    widths = [len(h) for h in headers]
    for row in rows:
        for index in range(columns):
            widths[index] = max(widths[index], len(row[index]) if index < len(row) else 0)

    def line(cells: Sequence[str]) -> str:
        out = []
        for index in range(columns):
            cell = cells[index] if index < len(cells) else ""
            # The final column is not padded: trailing whitespace is noise when
            # the output is copied out of a terminal.
            out.append(cell if index == columns - 1 else cell.ljust(widths[index]))
        return indent + "  ".join(out).rstrip()

    return [line(headers)] + [line(row) for row in rows]


# --------------------------------------------------------------------------
# progress rendering
# --------------------------------------------------------------------------

class ProgressRenderer:
    """Live text progress bar for the daemon's streamed enrolment updates.

    On a TTY it repaints one line in place::

        [████████████░░░░░░░░░░░░]  50%  Turn your head slowly to the left

    Anywhere else (a pipe, a log, ``script``) carriage returns would produce an
    unreadable smear, so it prints a new line only when the message actually
    changes — every new pose hint, and every 10% of progress.
    """

    def __init__(self, out: Console, width: int = 24) -> None:
        self._console = out
        self._width = width
        self._painted = 0        # length of the line currently on screen
        self._active = False
        self._last_hint = ""
        self._last_decile = -1

    def update(self, fraction: float, hint: str) -> None:
        fraction = _clamp_fraction(fraction)
        hint = " ".join(hint.split())  # collapse newlines: this is one line

        if self._console.tty:
            self._paint(fraction, hint)
            return

        decile = int(fraction * 10)
        if hint == self._last_hint and decile == self._last_decile:
            return
        self._last_hint, self._last_decile = hint, decile
        suffix = f"  {hint}" if hint else ""
        self._console.print(f"  [{fraction * 100:3.0f}%]{suffix}")

    def _paint(self, fraction: float, hint: str) -> None:
        filled_char, empty_char = self._console.bar_chars
        filled = int(round(fraction * self._width))
        bar = filled_char * filled + empty_char * (self._width - filled)
        percent = f"{fraction * 100:3.0f}%"
        suffix = f"  {hint}" if hint else ""
        plain = f"  [{bar}]  {percent}{suffix}"

        # Pad to the previous width so a shorter hint cannot leave the tail of
        # the old one on screen.
        padding = " " * max(0, self._painted - len(plain))
        painted = (
            f"  [{self._console.cyan(bar)}]  "
            f"{self._console.bold(percent)}{suffix}"
        )
        self._console.write("\r" + painted + padding)
        self._painted = len(plain)
        self._active = True

    def finish(self) -> None:
        """Close the bar so subsequent output starts on a fresh line."""
        if self._active:
            self._console.write("\n")
            self._active = False
        self._painted = 0


def _clamp_fraction(value: Any) -> float:
    """Normalise a daemon-supplied progress value into ``[0.0, 1.0]``.

    The contract says 0..1, but a daemon that sends 0..100 is a plausible bug
    and rendering a 4200%-full bar helps nobody, so percentages are folded in
    and everything else is clamped.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    if number > 1.0:
        number = number / 100.0
    return min(max(number, 0.0), 1.0)


# --------------------------------------------------------------------------
# configuration keys: validation and coercion
# --------------------------------------------------------------------------

#: Permitted ranges for numeric settings, keyed by dotted name.
#:
#: These deliberately mirror the clamps in :func:`iris.config._sanitise`.  The
#: loader *clamps* silently (a bad file must never stop a login); the CLI
#: *refuses*, because someone typing a value at a prompt should be told it is
#: out of range instead of discovering later that something else was stored.
_RANGES: dict[str, tuple[float, float]] = {
    "camera.width": (1, 8192),
    "camera.height": (1, 8192),
    "camera.min_frame_brightness": (0.0, 255.0),
    "recognition.threshold": (-1.0, 1.0),
    "recognition.detect_score": (0.0, 1.0),
    "recognition.required_matches": (1, 1000),
    "recognition.max_frames": (1, 100_000),
    "auth.timeout": (0.5, 60.0),
    # Bounded by FailureTracker._MAX_HISTORY: the per-user history deque holds
    # 64 entries, so a larger value here could never be reached and would
    # silently disable the lockout entirely. Keep these two in step.
    "auth.max_failures": (1, 64),
    "auth.lockout_seconds": (0, 86_400),
    "liveness.min_variance": (0.0, 65_025.0),
}

#: One-line descriptions, shown when a key is rejected or listed.
_DESCRIPTIONS: dict[str, str] = {
    "camera.device": "V4L2 node of the infrared camera",
    "camera.width": "capture width in pixels",
    "camera.height": "capture height in pixels",
    "camera.ir_mode": "request GREY 8-bit and drop dark strobe frames",
    "camera.min_frame_brightness": "mean brightness below which a frame is 'dark'",
    "recognition.threshold": "minimum cosine similarity for a match",
    "recognition.detect_score": "minimum YuNet detection confidence",
    "recognition.model_dir": "directory holding the YuNet and SFace ONNX files",
    "recognition.required_matches": "consecutive matching frames needed to authenticate",
    "recognition.max_frames": "frame budget for one authentication attempt",
    "auth.enabled": "master switch for face authentication",
    "auth.timeout": "seconds an authentication attempt may take",
    "auth.max_failures": "failures inside the window before lockout",
    "auth.lockout_seconds": "length of the failure window / lockout",
    "liveness.enabled": "reject flat, screen-like (non-live) faces",
    "liveness.min_variance": "minimum face-region variance for a live face",
}

_TRUE_WORDS = frozenset({"true", "yes", "on", "1", "y", "t"})
_FALSE_WORDS = frozenset({"false", "no", "off", "0", "n", "f"})


def known_keys() -> list[str]:
    """Every settable dotted key, in schema order."""
    return [
        f"{section}.{key}"
        for section, values in config_mod.DEFAULTS.items()
        for key in values
    ]


def default_for(dotted: str) -> Any:
    section, _, key = dotted.partition(".")
    return config_mod.DEFAULTS[section][key]


def split_key(dotted: str) -> tuple[str, str]:
    """Split ``section.key``, rejecting anything that is not a known setting."""
    section, sep, key = dotted.partition(".")
    if not sep or not key:
        raise CommandError(
            f"{dotted!r} is not a setting name",
            hint=(
                "settings are dotted, e.g. camera.device — "
                f"run  {PROG} config  to see them all"
            ),
        )
    if section not in config_mod.DEFAULTS or key not in config_mod.DEFAULTS[section]:
        close = difflib.get_close_matches(dotted, known_keys(), n=3, cutoff=0.5)
        hint = (
            f"did you mean {', '.join(close)}?"
            if close
            else f"run  {PROG} config  to list every setting"
        )
        raise CommandError(f"unknown setting {dotted!r}", hint=hint)
    return section, key


def coerce_value(dotted: str, raw: str) -> Any:
    """Parse *raw* into the type the schema declares for *dotted*.

    ``bool`` is handled before ``int`` throughout this module: in Python
    ``bool`` is a subclass of ``int``, so an ``isinstance(default, int)`` test
    would otherwise swallow every boolean setting.
    """
    default = default_for(dotted)
    text = raw.strip()

    if isinstance(default, bool):
        lowered = text.lower()
        if lowered in _TRUE_WORDS:
            return True
        if lowered in _FALSE_WORDS:
            return False
        raise CommandError(
            f"{dotted} is a true/false setting; {raw!r} is not a boolean",
            hint="use  true  or  false",
        )

    if isinstance(default, int):
        try:
            return int(text, 10)
        except ValueError:
            hint = "whole numbers only" + (
                f"; {text!r} looks like a decimal" if "." in text else ""
            )
            raise CommandError(f"{dotted} must be an integer, got {raw!r}", hint=hint) from None

    if isinstance(default, float):
        try:
            number = float(text)
        except ValueError:
            raise CommandError(
                f"{dotted} must be a number, got {raw!r}",
                hint=f"the default is {default!r}",
            ) from None
        if not math.isfinite(number):
            raise CommandError(f"{dotted} must be a finite number, got {raw!r}")
        return number

    return raw  # string setting: taken verbatim, including any spaces


def check_range(dotted: str, value: Any) -> None:
    """Refuse a numeric value outside the range the loader would clamp it to."""
    bounds = _RANGES.get(dotted)
    if bounds is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return
    low, high = bounds
    if not (low <= value <= high):
        raise CommandError(
            f"{dotted} must be between {_format_number(low)} and {_format_number(high)}, "
            f"got {_format_number(value)}",
            hint=f"the default is {default_for(dotted)!r}",
        )


def semantic_check(dotted: str, value: Any) -> list[str]:
    """Cross-check a value against the machine; returns advisory warnings.

    Hard errors (a value that cannot work at all) are raised; anything that is
    merely suspicious — a device that is not plugged in *yet*, a directory that
    the installer has not populated — comes back as a warning, because
    configuring a system before its hardware arrives is legitimate.
    """
    warnings: list[str] = []

    if dotted == "camera.device":
        device = str(value)
        if not device.startswith("/dev/"):
            warnings.append(f"{device} is not a /dev node; the daemon may not open it")
        elif not os.path.exists(device):
            warnings.append(f"{device} does not exist right now")
        else:
            for camera in _safe_list_cameras():
                if camera.get("path") != device:
                    continue
                if camera.get("is_metadata"):
                    # SPEC: /dev/video1 and /dev/video3 are UVC metadata nodes.
                    # They open successfully and then deliver no images at all,
                    # which surfaces as an unexplainable "no face" forever.
                    raise CommandError(
                        f"{device} is a V4L2 metadata node, not a capture device",
                        hint=f"run  {PROG} cameras  and pick a node marked IR",
                    )
                if not camera.get("is_ir"):
                    warnings.append(
                        f"{device} ({camera.get('name', 'unknown')}) does not look "
                        "like an infrared camera; face auth on a colour camera is "
                        "trivially spoofed with a photograph"
                    )
                break

    elif dotted == "recognition.model_dir":
        directory = Path(str(value))
        if not directory.is_dir():
            warnings.append(f"{directory} is not a directory")
        else:
            missing = [
                name
                for name in (DETECTOR_MODEL, RECOGNIZER_MODEL)
                if not (directory / name).is_file()
            ]
            if missing:
                warnings.append(f"{directory} is missing {', '.join(missing)}")

    elif dotted == "recognition.threshold" and isinstance(value, float) and value < 0.20:
        warnings.append(
            f"a threshold of {value} is dangerously permissive and may accept "
            f"strangers (SFace's published operating point is "
            f"{config_mod.DEFAULTS['recognition']['threshold']})"
        )

    elif dotted == "auth.enabled" and value is False:
        warnings.append("face authentication is now disabled for every user")

    elif dotted == "liveness.enabled" and value is False:
        warnings.append(
            "liveness checking is now off; a phone or monitor showing a face may "
            "be accepted"
        )

    return warnings


def _safe_list_cameras() -> list[dict[str, Any]]:
    """Enumerate cameras, returning ``[]`` rather than raising.

    Used from validation and diagnostics paths, which must still work on a
    machine where OpenCV is broken — that is a thing ``doctor`` reports, not a
    thing that should abort the run.
    """
    try:
        from iris.camera import list_cameras
    except ImportError as exc:
        logging.getLogger("iris.cli").debug("camera enumeration unavailable: %s", exc)
        return []
    try:
        return list_cameras()
    except OSError as exc:
        logging.getLogger("iris.cli").debug("camera enumeration failed: %s", exc)
        return []


def _format_number(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e16:
        return f"{value:.1f}"
    return str(value)


def format_value(value: Any) -> str:
    """Render a config value the way it appears in the TOML file."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value)  # JSON strings are valid TOML basic strings
    if isinstance(value, float):
        return repr(value)
    return str(value)


def load_effective_config(socket_path: str) -> tuple[dict[str, Any], str]:
    """Return ``(config, source)`` preferring what the daemon actually has loaded.

    The file on disk and the daemon's in-memory copy can disagree if someone
    edited the file without restarting the service, and "what is in force right
    now" is the more useful answer.
    """
    try:
        reply = protocol.send_request(
            {"op": protocol.OP_CONFIG_GET}, CONTROL_TIMEOUT, socket_path
        )
    except protocol.ProtocolError:
        return config_mod.load_config(config_mod.CONFIG_PATH), config_mod.CONFIG_PATH
    if reply.get("ok") and isinstance(reply.get("config"), dict):
        merged = config_mod.defaults()
        for section, values in reply["config"].items():
            if isinstance(values, dict):
                merged.setdefault(section, {}).update(values)
        return merged, f"{config_mod.CONFIG_PATH} (as loaded by irisd)"
    return config_mod.load_config(config_mod.CONFIG_PATH), config_mod.CONFIG_PATH


# --------------------------------------------------------------------------
# command: enroll
# --------------------------------------------------------------------------

def cmd_enroll(args: argparse.Namespace) -> int:
    label = _resolve_name(args, default="default")
    require_root("enroll", f"{PROG} enroll {label}")
    user = resolve_user(args.user)
    name = validate_face_name(label)

    if _json_mode(args):
        return _enroll_json(args, user, name)

    existing, _source = _faces_or_empty(user, args.socket)
    if any(face.get("name") == name for face in existing):
        console.warn(f"{user} already has a face called {name!r}; it will be replaced")

    console.print(f"Enrolling face {console.bold(name)} for {console.bold(user)}.")
    console.print(
        "Look straight at the infrared camera and follow the prompts. "
        "Press Ctrl-C to abort."
    )
    console.print()

    progress = ProgressRenderer(console)
    final: dict[str, Any] | None = None
    request = {"op": protocol.OP_ENROLL, "user": user, "name": name}

    try:
        for message in protocol.stream_request(request, args.timeout, args.socket):
            if protocol.is_progress(message):
                progress.update(message.get("progress", 0.0), str(message.get("hint", "")))
            else:
                final = message
    except protocol.ProtocolError as exc:
        progress.finish()
        raise _as_command_error(exc, args.socket) from exc
    except KeyboardInterrupt:
        # stream_request closes the socket on GeneratorExit, which tells the
        # daemon to abandon the enrolment and release the camera.
        progress.finish()
        console.print()
        console.warn("enrolment aborted; nothing was saved")
        return EXIT_INTERRUPTED
    finally:
        progress.finish()

    if final is None:
        raise CommandError(
            "the daemon closed the connection before finishing the enrolment",
            hint="check  journalctl -u irisd -n 50",
            code=EXIT_UNAVAILABLE,
        )

    if not final.get("ok"):
        raise CommandError(_failure_text(final), hint=_enroll_hint(final))

    samples = final.get("samples")
    detail = f" ({samples} samples)" if isinstance(samples, int) and samples > 0 else ""
    console.print(
        f"{console.green('Enrolled')} {console.bold(name)} for {user}{detail}."
    )
    console.note(f"Try it now with:  sudo {PROG} test --user {user}")
    return EXIT_OK


def _enroll_json(args: argparse.Namespace, user: str, name: str) -> int:
    """Relay the daemon's enrolment stream verbatim as newline-delimited JSON.

    The GTK front end drives enrolment through ``pkexec iris enroll --json`` and
    renders a progress ring from these lines, so each one is forwarded the
    moment it arrives.  Progress objects keep the daemon's own shape
    (``progress``/``hint`` plus whatever extras it adds) and exactly one final
    object carrying ``ok`` is printed, including when the exchange fails —
    a client parsing stdout must never have to fall back to reading stderr.
    """
    request = {"op": protocol.OP_ENROLL, "user": user, "name": name}
    final: dict[str, Any] | None = None

    try:
        for message in protocol.stream_request(request, args.timeout, args.socket):
            if protocol.is_progress(message):
                emit_json(message)
            else:
                final = message
    except protocol.ProtocolError as exc:
        error = _as_command_error(exc, args.socket)
        # No "reason" key: the closed vocabulary describes what the *camera*
        # saw, and inventing one for a transport failure would make the GUI
        # tell the user something untrue ("no face was visible") about an
        # attempt the daemon never even started.
        emit_json({"ok": False, "error": str(error)})
        return error.code
    except KeyboardInterrupt:
        emit_json({"ok": False, "error": "enrolment aborted; nothing was saved"})
        return EXIT_INTERRUPTED

    if final is None:
        emit_json({
            "ok": False,
            "error": "the daemon closed the connection before finishing the enrolment",
        })
        return EXIT_UNAVAILABLE

    emit_json(final)
    return EXIT_OK if final.get("ok") else EXIT_FAILURE


def _enroll_hint(message: Mapping[str, Any]) -> str:
    reason = message.get("reason")
    if reason == protocol.REASON_NO_FACE:
        return (
            "sit 40-70cm from the screen, facing the camera, and make sure "
            "nothing is covering the infrared lens"
        )
    if reason == protocol.REASON_CAMERA_ERROR:
        return f"run  {PROG} doctor  to check the infrared camera"
    if reason == protocol.REASON_SPOOF_SUSPECTED:
        return (
            "the frames looked flat rather than three-dimensional; enrol a live "
            "face, not a photograph or a screen"
        )
    if reason == protocol.REASON_TIMEOUT:
        return "try again with more light on your face, or move closer to the camera"
    return ""


def _faces_or_empty(user: str, socket_path: str) -> tuple[list[dict[str, Any]], str]:
    """``list_faces`` that degrades to "unknown" instead of failing the command."""
    try:
        return list_faces(user, socket_path)
    except CommandError:
        return [], "unknown"


# --------------------------------------------------------------------------
# command: list
# --------------------------------------------------------------------------

def cmd_list(args: argparse.Namespace) -> int:
    user = resolve_user(args.user)
    faces, source = list_faces(user, args.socket)

    if args.json:
        # "ok" is part of the shape every machine-readable reply in this system
        # carries (SPEC "iris/protocol.py"); the GUI treats its absence as a
        # failed command, so it is not optional here.
        emit_json({"ok": True, "user": user, "faces": faces, "source": source})
        return EXIT_OK

    if source == "store":
        console.note(f"(irisd is not running; read directly from {STORE_ROOT})")

    if not faces:
        console.print(f"No faces enrolled for {console.bold(user)}.")
        console.note(f"Enrol one with:  sudo {PROG} enroll --user {user}")
        return EXIT_OK

    rows = [
        [
            str(face.get("name", "?")),
            str(face.get("samples", "?")),
            _format_created(face.get("created")),
        ]
        for face in faces
    ]
    plural = "face" if len(faces) == 1 else "faces"
    console.print(f"{len(faces)} enrolled {plural} for {console.bold(user)}:")
    for index, line in enumerate(render_table(["NAME", "SAMPLES", "CREATED"], rows)):
        console.print(console.dim(line) if index == 0 else line)
    return EXIT_OK


def _format_created(value: Any) -> str:
    """Show the stored ISO-8601 timestamp as ``YYYY-MM-DD HH:MM``."""
    if not isinstance(value, str) or not value:
        return "unknown"
    text = value.replace("T", " ")
    for cut in ("+", "Z"):
        index = text.find(cut, 10)
        if index > 0:
            text = text[:index]
    return text.strip()[:16] or "unknown"


# --------------------------------------------------------------------------
# command: remove / clear
# --------------------------------------------------------------------------

def cmd_remove(args: argparse.Namespace) -> int:
    label = _resolve_name(args)
    require_root("remove", f"{PROG} remove {label}")
    user = resolve_user(args.user)
    name = validate_face_name(label)

    try:
        reply = daemon_request(
            {"op": protocol.OP_REMOVE, "user": user, "name": name},
            args.socket,
            CONTROL_TIMEOUT,
        )
        removed = bool(reply.get("ok"))
        # The protocol has no reason code for "no such face", and that is by
        # far the likeliest cause of a bare {"ok": false} here.
        failure = "" if removed else _failure_text(reply, default="")
    except CommandError as exc:
        if exc.code not in (EXIT_UNAVAILABLE, EXIT_PERMISSION):
            raise
        if not _json_mode(args):
            console.note(f"(irisd is not running; editing {STORE_ROOT} directly)")
        try:
            removed = bool(_open_store().remove(user, name))
        except Exception as store_exc:  # noqa: BLE001
            raise _store_error(store_exc) from store_exc
        failure = ""

    if not removed:
        # "no such face" is the overwhelmingly common cause, and the daemon has
        # no reason code for it, so name it explicitly.
        message = failure or f"{user} has no enrolled face called {name!r}"
        if _json_mode(args):
            emit_json({"ok": False, "error": message})
            return EXIT_FAILURE
        raise CommandError(
            message,
            hint=f"list what is enrolled with:  {PROG} list --user {user}",
        )

    if _json_mode(args):
        emit_json({"ok": True, "user": user, "removed": name})
        return EXIT_OK

    console.print(f"{console.green('Removed')} face {console.bold(name)} for {user}.")
    return EXIT_OK


def cmd_clear(args: argparse.Namespace) -> int:
    require_root("clear")
    user = resolve_user(args.user)
    # --json means a program is driving us and has already obtained the user's
    # consent through its own confirmation dialog. Prompting on a pipe would
    # deadlock it, so the flag implies --yes.
    machine = _json_mode(args)

    if not machine:
        faces, _source = _faces_or_empty(user, args.socket)
        if faces:
            names = ", ".join(str(face.get("name", "?")) for face in faces)
            console.print(
                f"This will delete every enrolled face for {console.bold(user)}: {names}"
            )
        else:
            console.print(f"This will delete every enrolled face for {console.bold(user)}.")

    if not args.yes and not machine:
        if not _is_tty(sys.stdin):
            raise CommandError(
                "refusing to clear templates without confirmation",
                hint="pass --yes when running non-interactively",
            )
        try:
            answer = input("Continue? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print()
            console.warn("aborted; nothing was deleted")
            return EXIT_INTERRUPTED
        if answer not in ("y", "yes"):
            console.print("Aborted; nothing was deleted.")
            return EXIT_OK

    try:
        reply = daemon_request(
            {"op": protocol.OP_CLEAR, "user": user}, args.socket, CONTROL_TIMEOUT
        )
        if not reply.get("ok"):
            raise CommandError(_failure_text(reply))
    except CommandError as exc:
        if exc.code not in (EXIT_UNAVAILABLE, EXIT_PERMISSION):
            raise
        if not machine:
            console.note(f"(irisd is not running; editing {STORE_ROOT} directly)")
        try:
            _open_store().clear(user)
        except Exception as store_exc:  # noqa: BLE001
            raise _store_error(store_exc) from store_exc

    if machine:
        emit_json({"ok": True, "user": user})
        return EXIT_OK

    console.print(f"{console.green('Cleared')} all face templates for {user}.")
    console.note("Password login is unaffected.")
    return EXIT_OK


# --------------------------------------------------------------------------
# command: test
# --------------------------------------------------------------------------

def cmd_test(args: argparse.Namespace) -> int:
    """Run one real authentication attempt through the daemon and report it."""
    user = resolve_user(args.user)
    cfg, _source = load_effective_config(args.socket)
    timeout = args.timeout if args.timeout is not None else float(cfg["auth"]["timeout"])
    threshold = float(cfg["recognition"]["threshold"])
    required = int(cfg["recognition"]["required_matches"])

    if not args.json:
        # Suppressed under --json so stdout stays a single parseable document.
        console.print(
            f"Authenticating {console.bold(user)} against the infrared camera "
            f"(timeout {timeout:.1f}s)..."
        )

    request = {"op": protocol.OP_AUTH, "user": user, "timeout": timeout}
    started = time.monotonic()
    # The transport budget is the auth timeout plus slack for the daemon to open
    # the camera and answer; without that margin a perfectly ordinary "timeout"
    # result would surface as a transport error instead.
    reply = daemon_request(request, args.socket, timeout + 5.0)
    elapsed = time.monotonic() - started

    ok = bool(reply.get("ok"))
    reason = reply.get("reason")
    reason = reason if isinstance(reason, str) else ""
    raw_confidence = reply.get("confidence")
    # bool before int: True would otherwise print as a confidence of 1.000.
    confidence = (
        float(raw_confidence)
        if isinstance(raw_confidence, (int, float)) and not isinstance(raw_confidence, bool)
        else float("nan")
    )
    face = reply.get("face")

    if args.json:
        console.print(
            json.dumps(
                {
                    "user": user,
                    "ok": ok,
                    "reason": reason,
                    "confidence": None if math.isnan(confidence) else confidence,
                    "face": face,
                    "elapsed_seconds": round(elapsed, 3),
                    "threshold": threshold,
                    "retry_after": _retry_after(reply),
                },
                indent=2,
            )
        )
        return EXIT_OK if ok else EXIT_AUTH_FAILED

    symbol = console.status_symbol("ok" if ok else "fail")
    if ok:
        matched = f" as {console.bold(str(face))}" if isinstance(face, str) and face else ""
        console.print(f"{symbol} {console.green('Authenticated')}{matched} in {elapsed:.2f}s")
    else:
        label = reason or "unknown"
        console.print(f"{symbol} {console.red('Not authenticated')} ({label}) in {elapsed:.2f}s")
        console.print(f"  {protocol.describe_reason(reason) if reason else _failure_text(reply)}")

    if not math.isnan(confidence):
        margin = confidence - threshold
        sign = "+" if margin >= 0 else ""
        console.print(
            f"  confidence {console.bold(f'{confidence:.3f}')}  "
            f"(threshold {threshold:.3f}, margin {sign}{margin:.3f}, "
            f"{required} consecutive matches required)"
        )

    hint = _test_hint(reason, user, reply)
    if hint:
        console.note(f"  {hint}")
    return EXIT_OK if ok else EXIT_AUTH_FAILED


def _retry_after(message: Mapping[str, Any]) -> float | None:
    """Seconds until a lockout lifts, when the daemon tells us."""
    value = message.get("retry_after")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
        if math.isfinite(seconds) and seconds > 0:
            return seconds
    return None


def _test_hint(reason: str, user: str, message: Mapping[str, Any]) -> str:
    if reason == protocol.REASON_NOT_ENROLLED:
        return f"enrol a face first:  sudo {PROG} enroll --user {user}"
    if reason == protocol.REASON_DISABLED:
        return f"re-enable it with:  sudo {PROG} config set auth.enabled true"
    if reason == protocol.REASON_LOCKOUT:
        seconds = _retry_after(message)
        if seconds is not None:
            return f"too many recent failures; try again in {seconds:.0f}s"
        return "too many recent failures; wait for the lockout window to expire"
    if reason == protocol.REASON_CAMERA_ERROR:
        return f"run  {PROG} doctor  to diagnose the camera"
    if reason == protocol.REASON_NO_FACE:
        return "sit 40-70cm from the screen and face the camera squarely"
    if reason == protocol.REASON_NO_MATCH:
        return (
            "if this is really you, re-enrol under your current appearance:  "
            f"sudo {PROG} enroll --user {user} glasses"
        )
    if reason == protocol.REASON_SPOOF_SUSPECTED:
        return "the frames looked flat; liveness rejected them as a possible replay"
    return ""


# --------------------------------------------------------------------------
# command: cameras
# --------------------------------------------------------------------------

def cmd_cameras(args: argparse.Namespace) -> int:
    cameras, source = _collect_cameras(args.socket)
    cfg, _cfg_source = load_effective_config(args.socket)
    configured = str(cfg["camera"]["device"])

    if args.json:
        console.print(json.dumps(cameras, indent=2))
        return EXIT_OK

    if not cameras:
        raise CommandError(
            "no V4L2 video devices were found",
            hint="check that the camera is enabled in firmware and that "
            "'ls /dev/video*' lists something",
        )

    hidden = 0
    rows: list[list[str]] = []
    painted: list[str | None] = []
    for camera in cameras:
        if camera.get("is_metadata") and not args.all:
            # Metadata nodes are hidden by default: they are siblings of the
            # real capture nodes, they cannot produce images, and offering them
            # as if they were cameras is how people end up configuring one.
            hidden += 1
            continue
        path = str(camera.get("path", "?"))
        marker = "*" if path == configured else " "
        if camera.get("is_metadata"):
            kind = "metadata"
        elif camera.get("is_ir"):
            kind = "infrared"
        else:
            kind = "colour"
        formats = camera.get("formats") or []
        rows.append([
            marker,
            path,
            str(camera.get("name", "unknown")),
            kind,
            ", ".join(str(f) for f in formats) if formats else "-",
        ])
        painted.append("ir" if camera.get("is_ir") else ("meta" if camera.get("is_metadata") else None))

    lines = render_table(["", "DEVICE", "NAME", "TYPE", "FORMATS"], rows)
    console.print(console.dim(lines[0]))
    for line, style in zip(lines[1:], painted):
        if style == "ir":
            console.print(console.cyan(line))
        elif style == "meta":
            console.print(console.dim(line))
        else:
            console.print(line)

    console.print()
    console.note(f"* = camera.device from the configuration ({configured})")
    if hidden:
        plural = "node" if hidden == 1 else "nodes"
        console.note(
            f"{hidden} V4L2 metadata {plural} hidden (they carry UVC headers, "
            "not images); show them with --all"
        )
    if source == "daemon":
        console.note("(enumerated by irisd)")

    if not any(c.get("is_ir") for c in cameras):
        console.warn(
            "no infrared camera was detected; face authentication on a colour "
            "camera can be defeated with a printed photograph"
        )
    elif not any(c.get("path") == configured for c in cameras):
        console.warn(f"the configured device {configured} is not present")
    return EXIT_OK


def _collect_cameras(socket_path: str) -> tuple[list[dict[str, Any]], str]:
    """Enumerate locally (works unprivileged) and fall back to the daemon."""
    try:
        from iris.camera import list_cameras
    except ImportError:
        reply = daemon_request({"op": protocol.OP_CAMERAS}, socket_path, CONTROL_TIMEOUT)
        if not reply.get("ok"):
            raise CommandError(_failure_text(reply))
        cameras = reply.get("cameras")
        if not isinstance(cameras, list):
            raise CommandError("the daemon returned a malformed camera list")
        return [c for c in cameras if isinstance(c, dict)], "daemon"

    try:
        return list_cameras(), "local"
    except OSError as exc:
        raise CommandError(f"cannot enumerate video devices: {exc}") from exc


# --------------------------------------------------------------------------
# command: config
# --------------------------------------------------------------------------

def cmd_config(args: argparse.Namespace) -> int:
    # argparse parses a subcommand into a fresh namespace and copies every
    # attribute back over the parent's, so `iris config --json get x` would
    # otherwise have its --json reset by the sub-action's default. The parent
    # flag therefore lives under its own dest and the two are OR-ed here, which
    # makes both orderings behave the same way.
    args.json = bool(getattr(args, "json", False) or getattr(args, "json_top", False))
    action = args.config_action or "get"
    if action == "get":
        return _config_get(args)
    if action == "set":
        return _config_set(args)
    if action == "unset":
        return _config_unset(args)
    if action == "keys":
        return _config_keys(args)
    raise CommandError(f"unknown config action {action!r}")  # pragma: no cover


def _config_get(args: argparse.Namespace) -> int:
    cfg, source = load_effective_config(args.socket)
    key = getattr(args, "key", None)

    if key:
        if key in config_mod.DEFAULTS:  # whole section, e.g. `config get camera`
            section = cfg.get(key, {})
            if args.json:
                console.print(json.dumps(section, indent=2))
                return EXIT_OK
            for name, value in section.items():
                console.print(f"{key}.{name} = {format_value(value)}")
            return EXIT_OK

        section, name = split_key(key)
        value = cfg.get(section, {}).get(name, default_for(key))
        if args.json:
            console.print(json.dumps(value))
        else:
            # Bare value: `iris config get camera.device` is meant to be usable
            # in a shell substitution, so no quoting and no decoration.
            console.print(value if isinstance(value, str) else format_value(value))
        return EXIT_OK

    if args.json:
        console.print(json.dumps(cfg, indent=2))
        return EXIT_OK

    console.note(f"# {source}")
    for section in cfg:
        values = cfg.get(section)
        if not isinstance(values, dict):
            continue
        console.print(console.bold(f"[{section}]"))
        for name, value in values.items():
            dotted = f"{section}.{name}"
            default = config_mod.DEFAULTS.get(section, {}).get(name)
            comment = ""
            if default is not None and value != default:
                comment = console.dim(f"  # default {format_value(default)}")
            elif dotted not in _DESCRIPTIONS:
                comment = console.dim("  # not a recognised setting")
            console.print(f"{name} = {format_value(value)}{comment}")
        console.print()
    return EXIT_OK


def _config_set(args: argparse.Namespace) -> int:
    dotted = args.key
    section, name = split_key(dotted)
    value = coerce_value(dotted, args.value)
    check_range(dotted, value)
    warnings = semantic_check(dotted, value)

    cfg = config_mod.load_config(config_mod.CONFIG_PATH)
    previous = cfg.get(section, {}).get(name, default_for(dotted))
    if previous == value and type(previous) is type(value):
        console.print(f"{dotted} is already {format_value(value)}; nothing to do.")
        for warning in warnings:
            console.warn(warning)
        return EXIT_OK

    cfg.setdefault(section, {})[name] = value

    written_by, effective = _write_config(cfg, args.socket)

    console.print(
        f"{console.green('Set')} {console.bold(dotted)} = {format_value(value)}  "
        f"{console.dim('(was ' + format_value(previous) + ')')}"
    )
    for warning in warnings:
        console.warn(warning)

    # Trust, then verify: the daemon reports what actually landed after the
    # loader's own clamping, and silently storing something other than what the
    # user typed is exactly the surprise this command must not spring.
    if effective is not None:
        stored = effective.get(section, {}).get(name)
        if stored != value:
            console.warn(
                f"the daemon stored {dotted} = {format_value(stored)}, "
                f"not {format_value(value)}"
            )

    if written_by == "daemon":
        console.note("irisd reloaded its configuration.")
    else:
        console.note(
            f"Wrote {config_mod.CONFIG_PATH}. Restart the daemon to apply it:  "
            "sudo systemctl restart irisd"
        )
    return EXIT_OK


def _config_unset(args: argparse.Namespace) -> int:
    """Restore one setting to its built-in default."""
    dotted = args.key
    section, name = split_key(dotted)
    default = default_for(dotted)

    cfg = config_mod.load_config(config_mod.CONFIG_PATH)
    previous = cfg.get(section, {}).get(name, default)
    cfg.setdefault(section, {})[name] = default

    if previous == default:
        console.print(f"{dotted} is already at its default ({format_value(default)}).")
        return EXIT_OK

    written_by, _effective = _write_config(cfg, args.socket)
    console.print(
        f"{console.green('Reset')} {console.bold(dotted)} to {format_value(default)}  "
        f"{console.dim('(was ' + format_value(previous) + ')')}"
    )
    if written_by != "daemon":
        console.note("Restart the daemon to apply it:  sudo systemctl restart irisd")
    return EXIT_OK


def _write_config(
    cfg: Mapping[str, Any], socket_path: str
) -> tuple[str, dict[str, Any] | None]:
    """Persist *cfg*, preferring the daemon so it reloads at the same moment.

    The whole configuration is sent, not a delta: SPEC leaves the shape of the
    ``config_set`` payload open, and a complete table is unambiguous for any
    daemon implementation (a merging one merges it to the same result).

    :returns: ``(writer, effective)`` — ``writer`` is ``"daemon"`` or
        ``"file"``, and ``effective`` is the configuration the daemon says is
        now in force, when it tells us (it re-reads the file after saving, and
        the loader clamps, so what landed can differ from what was asked).
    """
    try:
        reply = protocol.send_request(
            {"op": protocol.OP_CONFIG_SET, "config": dict(cfg)},
            CONTROL_TIMEOUT,
            socket_path,
        )
    except protocol.ProtocolError as exc:
        transport_error = _as_command_error(exc, socket_path)
    else:
        if reply.get("ok"):
            effective = reply.get("config")
            return "daemon", effective if isinstance(effective, dict) else None
        raise CommandError(_failure_text(reply))

    # Daemon unreachable: write the file ourselves if we are allowed to.  This
    # is what makes `iris config set` usable for repairing a machine whose
    # daemon will not start.
    if os.geteuid() != 0:
        raise CommandError(
            f"cannot write {config_mod.CONFIG_PATH}",
            hint=(
                "the configuration is root-owned and irisd is not reachable — "
                f"re-run with sudo:  sudo {PROG} config set ..."
            ),
            code=EXIT_PERMISSION,
        )
    try:
        config_mod.save_config(cfg, config_mod.CONFIG_PATH)
    except (OSError, TypeError) as exc:
        raise CommandError(
            f"could not write {config_mod.CONFIG_PATH}: {exc}",
            hint=transport_error.hint,
        ) from exc
    return "file", None


def cmd_config_set_all(args: argparse.Namespace) -> int:
    """Apply a whole configuration document read from standard input.

    ``iris config set KEY VALUE`` is the interactive spelling; this is the one
    the settings panel uses.  It exists because each privileged invocation costs
    the user one polkit prompt: a panel with four changed settings must be able
    to apply all four with a single authorisation, and it cannot do that one key
    at a time.

    The document is a JSON object of the same shape as
    :data:`iris.config.DEFAULTS`.  Unknown or mistyped values are rejected here
    (and again by the daemon) rather than silently falling back to a default,
    because over a pipe that would look like a write that succeeded and did
    nothing.
    """
    machine = _json_mode(args)
    require_root(
        "config-set", f"{PROG} config-set", subject=f"the root-owned {config_mod.CONFIG_PATH}"
    )

    raw = sys.stdin.read()
    if not raw.strip():
        raise CommandError(
            "no configuration on standard input",
            hint=f"pipe a JSON object, e.g.  {PROG} config get --json | sudo {PROG} config-set",
            code=EXIT_USAGE,
        )
    try:
        incoming = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CommandError(f"standard input is not valid JSON: {exc}", code=EXIT_USAGE) from exc
    if not isinstance(incoming, dict):
        raise CommandError(
            f"expected a JSON object, got {type(incoming).__name__}", code=EXIT_USAGE
        )

    problems = _validate_config_document(incoming)
    if problems:
        raise CommandError("; ".join(problems), code=EXIT_USAGE)

    # Merge over what is on disk rather than replacing it, so a caller that
    # sends only the sections it cares about does not reset the rest.
    cfg = config_mod.load_config(config_mod.CONFIG_PATH)
    for section, values in incoming.items():
        target = cfg.setdefault(section, {})
        if not isinstance(target, dict):
            raise CommandError(f"[{section}] is not a table", code=EXIT_USAGE)
        target.update(values)

    written_by, effective = _write_config(cfg, args.socket)
    if effective is None:
        effective = config_mod.load_config(config_mod.CONFIG_PATH)

    if machine:
        emit_json({"ok": True, "config": effective, "written_by": written_by})
        return EXIT_OK

    console.print(f"{console.green('Updated')} {config_mod.CONFIG_PATH}.")
    if written_by == "daemon":
        console.note("irisd reloaded its configuration.")
    else:
        console.note("Restart the daemon to apply it:  sudo systemctl restart irisd")
    return EXIT_OK


def _validate_config_document(incoming: Mapping[str, Any]) -> list[str]:
    """Type-check a whole configuration document against the schema.

    Mirrors ``IrisDaemon._validate_config`` so that the file-writing fallback
    path (daemon down, running as root) applies the same rules the daemon would
    have applied, rather than being the lenient way in.
    """
    problems: list[str] = []
    for section, values in incoming.items():
        if not isinstance(values, Mapping):
            problems.append(f"[{section}] must be an object")
            continue
        known = config_mod.DEFAULTS.get(section)
        for key, value in values.items():
            if known is not None and key in known:
                default = known[key]
                # bool before int throughout: bool is a subclass of int, so a
                # naive check would accept `width = true`.
                if isinstance(default, bool):
                    good = isinstance(value, bool)
                elif isinstance(default, int):
                    good = isinstance(value, int) and not isinstance(value, bool)
                elif isinstance(default, float):
                    good = isinstance(value, (int, float)) and not isinstance(value, bool)
                else:
                    good = isinstance(value, str)
                if not good:
                    problems.append(
                        f"{section}.{key}: expected {type(default).__name__}, "
                        f"got {type(value).__name__}"
                    )
            elif not isinstance(value, (bool, int, float, str)):
                problems.append(
                    f"{section}.{key}: unsupported value type {type(value).__name__}"
                )
    return problems


def _config_keys(args: argparse.Namespace) -> int:
    """List every settable key with its type, default and description."""
    cfg, _source = load_effective_config(args.socket)
    rows: list[list[str]] = []
    for dotted in known_keys():
        section, name = dotted.split(".", 1)
        default = default_for(dotted)
        current = cfg.get(section, {}).get(name, default)
        bounds = _RANGES.get(dotted)
        type_name = "bool" if isinstance(default, bool) else type(default).__name__
        if bounds:
            type_name += f" {_format_number(bounds[0])}..{_format_number(bounds[1])}"
        rows.append([
            dotted,
            type_name,
            format_value(current),
            format_value(default),
            _DESCRIPTIONS.get(dotted, ""),
        ])

    lines = render_table(["KEY", "TYPE", "CURRENT", "DEFAULT", "MEANING"], rows)
    console.print(console.dim(lines[0]))
    for line in lines[1:]:
        console.print(line)
    return EXIT_OK


# --------------------------------------------------------------------------
# command: status
# --------------------------------------------------------------------------

def cmd_status(args: argparse.Namespace) -> int:
    user = resolve_user(args.user)
    cfg, source = load_effective_config(args.socket)
    ping = daemon_ping(args.socket)

    faces, faces_source = _faces_or_empty(user, args.socket)
    face_names = [str(f.get("name", "?")) for f in faces]

    if args.json:
        emit_json({
            "version": __version__,
            "daemon": {
                "running": ping is not None,
                "version": ping.get("version") if ping else None,
                "socket": args.socket,
            },
            "config_source": source,
            "config": cfg,
            "user": user,
            "faces": faces if faces_source != "unknown" else None,
        })
        return EXIT_OK

    console.print(f"{console.bold('Iris')} {__version__}")
    console.print()

    if ping is not None:
        version = ping.get("version")
        detail = f"running (irisd {version})" if isinstance(version, str) else "running"
        _kv("daemon", console.green(detail))
        if isinstance(version, str) and version != __version__:
            console.warn(
                f"the daemon is version {version} but this CLI is {__version__}; "
                "restart irisd after an upgrade"
            )
    else:
        _kv("daemon", console.red("not running"))

    auth_enabled = bool(cfg["auth"]["enabled"])
    _kv("face auth", console.green("enabled") if auth_enabled else console.yellow("disabled"))

    device = str(cfg["camera"]["device"])
    present = os.path.exists(device)
    device_text = f"{device} ({cfg['camera']['width']}x{cfg['camera']['height']}"
    device_text += ", IR mode" if cfg["camera"]["ir_mode"] else ", raw mode"
    device_text += ")"
    if not present:
        device_text += console.red("  [missing]")
    _kv("camera", device_text)

    _kv(
        "matching",
        f"threshold {cfg['recognition']['threshold']}, "
        f"{cfg['recognition']['required_matches']} consecutive matches, "
        f"{cfg['auth']['timeout']}s budget",
    )
    _kv(
        "liveness",
        f"enabled (min variance {cfg['liveness']['min_variance']})"
        if cfg["liveness"]["enabled"]
        else console.yellow("disabled"),
    )
    _kv(
        "lockout",
        f"{cfg['auth']['max_failures']} failures per {cfg['auth']['lockout_seconds']}s",
    )

    if faces_source == "unknown":
        _kv("enrollment", console.dim(f"unknown (run 'sudo {PROG} status' to read it)"))
    elif face_names:
        _kv("enrollment", f"{len(face_names)} for {user}: {', '.join(face_names)}")
    else:
        _kv("enrollment", console.yellow(f"none for {user}"))

    if os.geteuid() == 0:
        _kv("key storage", _describe_key_backend())

    console.print()
    console.note(f"config: {source}")
    console.note(f"socket: {args.socket}")
    if not auth_enabled:
        console.note(f"enable face auth with:  sudo {PROG} config set auth.enabled true")
    return EXIT_OK


def _kv(label: str, value: str) -> None:
    console.print(f"  {label:<12}  {value}")


def _describe_key_backend() -> str:
    """Describe how the master key is protected (root-only inspection)."""
    sealed = Path(STORE_ROOT) / SEALED_KEY
    plain = Path(STORE_ROOT) / MASTER_KEY
    if sealed.exists():
        return "TPM-sealed (no plaintext key on disk)"
    if plain.exists():
        if os.path.exists(TPM_RM_DEVICE):
            return console.yellow("plain 0600 key file (a TPM is present but unused)")
        return "plain 0600 key file (no TPM available)"
    return console.dim("no master key yet (nothing enrolled)")


# --------------------------------------------------------------------------
# command: doctor
# --------------------------------------------------------------------------

@dataclass
class Check:
    """One diagnostic result.

    *status* is ``ok`` / ``warn`` / ``fail``; only ``fail`` affects the exit
    code, so "advisory" findings (no TPM, nothing enrolled yet) can be reported
    honestly without making a healthy machine look broken to a script.
    """

    name: str
    status: str
    detail: str
    hint: str = ""
    extra: list[str] = field(default_factory=list)


def cmd_doctor(args: argparse.Namespace) -> int:
    user = resolve_user(args.user)
    cfg, config_source = load_effective_config(args.socket)

    console.print(f"{console.bold('Iris')} {__version__} — system health check")
    console.print()

    checks: list[Check] = [
        check_python_environment(),
        check_models(cfg),
        check_configuration(),
        check_store_directory(),
    ]
    checks.append(check_daemon(args.socket))
    checks.append(check_socket(args.socket))
    checks.append(check_pam())

    if args.no_camera:
        checks.append(Check("IR camera", "warn", "skipped (--no-camera)"))
        checks.append(Check("IR strobe", "warn", "skipped (--no-camera)"))
    else:
        checks.extend(check_camera(cfg, args.camera_timeout))

    checks.append(check_enrollment(user, args.socket))
    checks.append(check_tpm())

    _print_checks(checks)

    failures = sum(1 for c in checks if c.status == "fail")
    warnings = sum(1 for c in checks if c.status == "warn")
    passed = sum(1 for c in checks if c.status == "ok")

    console.print()
    summary = f"{len(checks)} checks: {passed} passed, {warnings} warnings, {failures} failed"
    if failures:
        console.print(console.red(summary))
    elif warnings:
        console.print(console.yellow(summary))
    else:
        console.print(console.green(summary))
    console.note(f"config: {config_source}")

    exit_code = EXIT_FAILURE if failures else EXIT_OK

    if args.calibrate:
        console.print()
        if failures:
            console.warn("running calibration anyway, but fix the failures above first")
        calibration_code = run_calibration(cfg, args.samples, args.calibrate_timeout)
        exit_code = exit_code or calibration_code

    return exit_code


def _print_checks(checks: Sequence[Check]) -> None:
    name_width = max(len(c.name) for c in checks)
    continuation = "  " + " " * console.status_width + "  " + " " * name_width + "  "
    arrow = "→" if console.unicode else "->"
    for check in checks:
        console.print(
            f"  {console.status_cell(check.status)}  "
            f"{check.name.ljust(name_width)}  {check.detail}"
        )
        for line in check.extra:
            console.print(continuation + console.dim(line))
        if check.hint and check.status != "ok":
            console.print(continuation + console.dim(f"{arrow} {check.hint}"))


# -- individual checks ------------------------------------------------------

def check_python_environment() -> Check:
    """Verify the apt-provided runtime dependencies actually import."""
    versions: list[str] = []
    missing: list[str] = []

    try:
        import numpy
        versions.append(f"numpy {numpy.__version__}")
    except ImportError:
        missing.append("python3-numpy")
    try:
        import cv2
        versions.append(f"OpenCV {cv2.__version__}")
    except ImportError:
        missing.append("python3-opencv")
    try:
        import cryptography
        versions.append(f"cryptography {cryptography.__version__}")
    except ImportError:
        missing.append("python3-cryptography")

    runtime = f"Python {sys.version_info.major}.{sys.version_info.minor}"
    if missing:
        return Check(
            "Python runtime",
            "fail",
            f"{runtime}; missing {', '.join(missing)}",
            hint=f"sudo apt install {' '.join(missing)}",
        )
    return Check("Python runtime", "ok", f"{runtime}, " + ", ".join(versions))


def check_models(cfg: Mapping[str, Any]) -> Check:
    configured = Path(str(cfg["recognition"]["model_dir"]))
    names = (DETECTOR_MODEL, RECOGNIZER_MODEL)
    missing = [n for n in names if not (configured / n).is_file()]

    if not missing:
        total = sum((configured / n).stat().st_size for n in names)
        return Check(
            "Models", "ok",
            f"YuNet + SFace present in {configured} ({total / 1024:.0f} KiB)",
        )

    checkout = Path(__file__).resolve().parents[2] / "models"
    if all((checkout / n).is_file() for n in names):
        return Check(
            "Models", "warn",
            f"missing from {configured}, found in the source checkout {checkout}",
            hint=(
                "the daemon runs as root and refuses models from a user-writable "
                "directory — run the installer to copy them into "
                f"{configured}"
            ),
        )
    return Check(
        "Models", "fail",
        f"{', '.join(missing)} missing from {configured}",
        hint="run the installer (sudo ./install.sh) to install the ONNX models",
    )


def check_configuration() -> Check:
    path = Path(config_mod.CONFIG_PATH)
    try:
        info = path.stat()
    except FileNotFoundError:
        return Check(
            "Configuration", "warn",
            f"{path} does not exist; built-in defaults are in force",
            hint="run the installer, or create it with  sudo iris config set ...",
        )
    except OSError as exc:
        return Check("Configuration", "fail", f"cannot stat {path}: {exc}")

    problems: list[str] = []
    try:
        with open(path, "rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        return Check(
            "Configuration", "fail",
            f"{path} is not valid TOML: {exc}",
            hint="fix the syntax; until then every setting falls back to its default",
        )
    except OSError as exc:
        return Check(
            "Configuration", "fail", f"cannot read {path}: {exc}",
            hint="the file should be 0644 root:root",
        )

    mode = stat.S_IMODE(info.st_mode)
    if mode != 0o644:
        problems.append(f"mode is {mode:04o}, expected 0644")
    if info.st_uid != 0 or info.st_gid != 0:
        problems.append(f"owned by uid {info.st_uid}:{info.st_gid}, expected root:root")

    # Compare the raw file against the schema: the loader silently repairs
    # everything below, and a typo that is silently ignored is exactly the kind
    # of thing this command exists to surface.
    for section, values in raw.items():
        if not isinstance(values, dict):
            problems.append(f"[{section}] is not a table")
            continue
        if section not in config_mod.DEFAULTS:
            problems.append(f"unknown section [{section}]")
            continue
        for key, value in values.items():
            dotted = f"{section}.{key}"
            if key not in config_mod.DEFAULTS[section]:
                problems.append(f"unknown key {dotted}")
                continue
            default = config_mod.DEFAULTS[section][key]
            if isinstance(default, bool):
                if not isinstance(value, bool):
                    problems.append(f"{dotted} should be true/false")
                continue
            if isinstance(default, int) and (isinstance(value, bool) or not isinstance(value, int)):
                problems.append(f"{dotted} should be a whole number")
                continue
            if isinstance(default, float) and (
                isinstance(value, bool) or not isinstance(value, (int, float))
            ):
                problems.append(f"{dotted} should be a number")
                continue
            if isinstance(default, str) and not isinstance(value, str):
                problems.append(f"{dotted} should be a string")
                continue
            bounds = _RANGES.get(dotted)
            if bounds and isinstance(value, (int, float)) and not isinstance(value, bool):
                if not (bounds[0] <= value <= bounds[1]):
                    problems.append(
                        f"{dotted} = {value} is outside "
                        f"{_format_number(bounds[0])}..{_format_number(bounds[1])} "
                        "and will be clamped"
                    )

    cfg = config_mod.load_config(config_mod.CONFIG_PATH)
    threshold = float(cfg["recognition"]["threshold"])
    if threshold < 0.20:
        problems.append(f"recognition.threshold = {threshold} is dangerously permissive")

    if problems:
        return Check(
            "Configuration", "warn",
            f"{path} loads, with {len(problems)} problem(s)",
            hint="every problem below falls back to the built-in default",
            extra=problems,
        )
    return Check("Configuration", "ok", f"{path} is valid (0644 root:root)")


def check_store_directory() -> Check:
    path = Path(STORE_ROOT)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return Check(
            "Template store", "warn",
            f"{path} does not exist yet",
            hint=f"it is created on first enrolment:  sudo {PROG} enroll",
        )
    except OSError as exc:
        return Check("Template store", "fail", f"cannot stat {path}: {exc}")

    if stat.S_ISLNK(info.st_mode):
        return Check(
            "Template store", "fail",
            f"{path} is a symlink",
            hint="the store refuses to follow it; remove the link and re-enrol",
        )
    if not stat.S_ISDIR(info.st_mode):
        return Check("Template store", "fail", f"{path} is not a directory")

    mode = stat.S_IMODE(info.st_mode)
    problems = []
    if mode != 0o700:
        problems.append(f"mode is {mode:04o}, expected 0700")
    if info.st_uid != 0 or info.st_gid != 0:
        problems.append(f"owned by {info.st_uid}:{info.st_gid}, expected root:root")
    if problems:
        return Check(
            "Template store", "fail",
            f"{path}: " + "; ".join(problems),
            hint=f"sudo chown root:root {path} && sudo chmod 700 {path}",
        )
    return Check("Template store", "ok", f"{path} is root:root 0700")


def check_daemon(socket_path: str) -> Check:
    reply = daemon_ping(socket_path, timeout=3.0)
    if reply is None:
        # Not being allowed to *try* is different from the daemon being down,
        # and reporting an unprivileged run as a failure would make `iris
        # doctor` exit non-zero on a perfectly healthy machine.
        if os.geteuid() != 0 and os.path.exists(socket_path):
            return Check(
                "Daemon", "warn",
                "cannot be checked without root (the socket is 0600 root:root)",
                hint=f"re-run as  sudo {PROG} doctor",
            )
        return Check(
            "Daemon", "fail",
            f"not reachable on {socket_path}",
            hint=_socket_hint(socket_path),
        )

    version = reply.get("version")
    if isinstance(version, str) and version != __version__:
        return Check(
            "Daemon", "warn",
            f"running version {version}, but this CLI is {__version__}",
            hint="restart the service after upgrading:  sudo systemctl restart irisd",
        )
    return Check(
        "Daemon", "ok", f"running and answering on {socket_path} (version {version})"
    )


def check_socket(socket_path: str) -> Check:
    try:
        info = os.stat(socket_path)
    except FileNotFoundError:
        return Check(
            "Socket permissions", "fail",
            f"{socket_path} does not exist",
            hint="irisd creates it on start:  sudo systemctl start irisd",
        )
    except PermissionError:
        return Check(
            "Socket permissions", "warn",
            f"cannot inspect {socket_path} without root",
            hint=f"re-run as  sudo {PROG} doctor",
        )
    except OSError as exc:
        return Check("Socket permissions", "fail", f"cannot stat {socket_path}: {exc}")

    if not stat.S_ISSOCK(info.st_mode):
        return Check(
            "Socket permissions", "fail",
            f"{socket_path} is not a socket",
            hint="delete the stale file and restart irisd",
        )

    mode = stat.S_IMODE(info.st_mode)
    problems = []
    if mode != 0o600:
        problems.append(f"mode {mode:04o}, expected 0600")
    if info.st_uid != 0 or info.st_gid != 0:
        problems.append(f"owner {info.st_uid}:{info.st_gid}, expected root:root")
    if problems:
        return Check(
            "Socket permissions", "fail",
            "; ".join(problems),
            hint=(
                "a socket any user can write to lets unprivileged processes drive "
                "enrolment and authentication; fix the daemon's umask/unit file"
            ),
        )
    return Check("Socket permissions", "ok", f"{socket_path} is root:root 0600")


def _auth_fallback_missing(path: str) -> bool:
    """True if *path* stacks pam_iris on ``auth`` with nothing to fall back to.

    This catches the single most plausible way to lock yourself out of this
    machine.  The documented edit is to ADD the pam_iris line *above* the
    existing password module::

        auth  [success=done default=ignore]  pam_iris.so
        auth  ...                            pam_unix.so       <-- must remain

    The mistake is to REPLACE that second line instead — the file still looks
    right, `iris doctor` still sees the correct control value, and face auth
    still works, so nothing complains.  But ``default=ignore`` now falls
    through to an empty stack: cover the camera, or stop the daemon, and there
    is no password prompt left to answer.  On ``gdm-password`` that is a
    locked-out desktop; on ``sudo`` it is a machine you can no longer
    administer.

    A file is considered to have a fallback if it has any other ``auth`` line
    (a module or an ``@include``), which is the only thing that can supply
    one.  Unreadable or unparseable files are reported as fine — this is a
    warning aid, and a false alarm on every run trains people to ignore it.
    """
    has_iris_auth = False
    has_other_auth = False

    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue

                # "@include common-auth" pulls in a whole stack, password
                # module included; treat it as a fallback.
                if line.startswith("@include"):
                    has_other_auth = True
                    continue

                fields = line.split()
                if len(fields) < 2 or fields[0].lower() != "auth":
                    continue

                if PAM_MODULE_NAME in line:
                    has_iris_auth = True
                else:
                    has_other_auth = True
    except OSError:
        return False

    return has_iris_auth and not has_other_auth


def check_pam() -> Check:
    installed = [p for p in PAM_MODULE_PATHS if os.path.isfile(p)]
    if not installed:
        # Probe other multiarch triplets before declaring it missing.
        for candidate in sorted(Path("/usr/lib").glob(f"*/security/{PAM_MODULE_NAME}")):
            installed.append(str(candidate))

    references: list[tuple[str, str]] = []
    unreadable = False
    try:
        entries = sorted(os.listdir(PAM_DIR))
    except OSError:
        entries = []
        unreadable = True

    for entry in entries:
        path = os.path.join(PAM_DIR, entry)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    stripped = line.strip()
                    if stripped.startswith("#") or PAM_MODULE_NAME not in stripped:
                        continue
                    references.append((entry, stripped))
        except OSError:
            unreadable = True

    if not installed and not references:
        return Check(
            "PAM module", "warn",
            f"{PAM_MODULE_NAME} is not installed and not referenced",
            hint=(
                "face unlock for login/sudo/polkit needs it — run the installer; "
                "password authentication is unaffected"
            ),
        )
    if not installed and references:
        files = ", ".join(sorted({name for name, _ in references}))
        return Check(
            "PAM module", "fail",
            f"referenced by {files} but {PAM_MODULE_NAME} is not installed",
            hint=(
                "PAM logs a module-unknown error on every login; either install "
                "the module or remove those lines"
            ),
        )
    if installed and not references:
        return Check(
            "PAM module", "warn",
            f"installed at {installed[0]} but not referenced in {PAM_DIR}",
            hint=(
                "nothing will ever call it; the installer adds an "
                f"'auth {PAM_REQUIRED_CONTROL} {PAM_MODULE_NAME}' line"
            ),
        )

    # Collapse runs of whitespace before matching: PAM files are conventionally
    # column-aligned with tabs and multiple spaces, and the control field is
    # written as one token there but with single spaces in our constant.
    unsafe = [
        (name, line)
        for name, line in references
        if PAM_REQUIRED_CONTROL not in " ".join(line.split())
    ]
    files = ", ".join(sorted({name for name, _ in references}))
    if unsafe:
        # SAFETY rule 2: any other control (required/sufficient) either blocks
        # the password fallback or grants access on module success without the
        # "stop here" semantics the design assumes.
        bad_files = ", ".join(sorted({name for name, _ in unsafe}))
        return Check(
            "PAM module", "fail",
            f"stacked without '{PAM_REQUIRED_CONTROL}' in {bad_files}",
            hint=(
                "face auth must fall through to the password prompt on failure; "
                f"use  auth {PAM_REQUIRED_CONTROL} {PAM_MODULE_NAME}"
            ),
            extra=[f"{name}: {line}" for name, line in unsafe],
        )

    # SAFETY rule 5: the control value being right is not enough — there has to
    # be something left to fall through TO.  Checked after the control value so
    # the more common mistake is reported first.
    stranded = sorted({
        name for name, _ in references
        if _auth_fallback_missing(os.path.join(PAM_DIR, name))
    })
    if stranded:
        return Check(
            "PAM module", "fail",
            f"{PAM_MODULE_NAME} is the only auth module in {', '.join(stranded)}",
            hint=(
                "there is no password fallback in that stack — if the camera "
                "fails you cannot authenticate at all; restore the pam_unix.so "
                "line (or '@include common-auth') BELOW the pam_iris.so line, "
                "from a root shell you still have open"
            ),
            extra=[f"{name}: no other auth line" for name in stranded],
        )

    detail = f"{installed[0]}, referenced by {files}"
    if unreadable:
        return Check(
            "PAM module", "warn",
            detail + f" (some files in {PAM_DIR} were unreadable)",
            hint=f"re-run as  sudo {PROG} doctor  for a complete answer",
        )
    return Check("PAM module", "ok", detail)


def check_camera(cfg: Mapping[str, Any], timeout: float) -> list[Check]:
    """Open the configured IR device and look for the emitter's strobe.

    Returns two checks (device, strobe) because they fail for different reasons
    and need different remediation, but they share one capture: opening the IR
    camera twice costs a second and briefly fights the daemon for the device.
    """
    device = str(cfg["camera"]["device"])
    min_brightness = float(cfg["camera"]["min_frame_brightness"])

    if not os.path.exists(device):
        available = [c["path"] for c in _safe_list_cameras() if c.get("is_ir")]
        hint = (
            f"an infrared camera was detected at {available[0]}; select it with  "
            f"sudo {PROG} config set camera.device {available[0]}"
            if available
            else f"run  {PROG} cameras  to see what this machine has"
        )
        return [
            Check("IR camera", "fail", f"{device} does not exist", hint=hint),
            Check("IR strobe", "warn", "skipped (camera unavailable)"),
        ]

    for camera in _safe_list_cameras():
        if camera.get("path") == device and camera.get("is_metadata"):
            return [
                Check(
                    "IR camera", "fail",
                    f"{device} is a V4L2 metadata node, not a capture device",
                    hint=f"run  {PROG} cameras  and pick a node marked IR",
                ),
                Check("IR strobe", "warn", "skipped (camera unavailable)"),
            ]

    try:
        from iris.camera import Camera, CameraError
    except ImportError as exc:
        return [
            Check("IR camera", "fail", f"cannot import iris.camera: {exc}",
                  hint="sudo apt install python3-opencv"),
            Check("IR strobe", "warn", "skipped (camera unavailable)"),
        ]

    means: list[float] = []
    started = time.monotonic()
    try:
        with Camera.from_config(dict(cfg)) as cam:
            # raw_frames(), not frames(): the dark half of the strobe is exactly
            # what the second check is looking for, and frames() drops it.
            for frame in cam.raw_frames(timeout):
                means.append(float(frame.mean()))
    except CameraError as exc:
        message = str(exc)
        hint = f"run  {PROG} cameras  to see the available devices"
        if "Device or resource busy" in message or "busy" in message.lower():
            hint = (
                "another process holds the camera — irisd may be mid-"
                "authentication; try again in a moment"
            )
        elif "permission" in message.lower():
            hint = (
                f"you need read access to {device}: check the ACL "
                f"(getfacl {device}) or add yourself to the 'video' group"
            )
        return [
            Check("IR camera", "fail", message, hint=hint),
            Check("IR strobe", "warn", "skipped (camera could not be opened)"),
        ]
    except Exception as exc:  # noqa: BLE001 - diagnostics must not traceback
        return [
            Check("IR camera", "fail", f"unexpected camera failure: {exc}"),
            Check("IR strobe", "warn", "skipped (camera could not be opened)"),
        ]

    elapsed = time.monotonic() - started
    if not means:
        return [
            Check(
                "IR camera", "fail",
                f"{device} opened but delivered no frames in {elapsed:.1f}s",
                hint="the device may be a metadata node or held by another process",
            ),
            Check("IR strobe", "warn", "skipped (no frames)"),
        ]

    lit = [m for m in means if m >= min_brightness]
    dark = [m for m in means if m < min_brightness]
    fps = len(means) / elapsed if elapsed > 0 else 0.0
    device_check = Check(
        "IR camera", "ok",
        f"{device}: {len(means)} frames in {elapsed:.1f}s ({fps:.1f} fps), "
        f"{cfg['camera']['width']}x{cfg['camera']['height']}",
    )

    if lit and dark:
        strobe = Check(
            "IR strobe", "ok",
            f"emitter strobing: {len(lit)} lit (mean {sum(lit) / len(lit):.1f}) / "
            f"{len(dark)} dark (mean {sum(dark) / len(dark):.1f}), "
            f"threshold {min_brightness:.1f}",
        )
    elif lit:
        strobe = Check(
            "IR strobe", "warn",
            f"every frame was lit (mean {sum(lit) / len(lit):.1f}); the emitter "
            "does not appear to strobe on this device",
            hint=(
                "this is harmless — no frames are discarded — but it means "
                "min_frame_brightness is doing nothing"
            ),
        )
    else:
        brightest = max(means)
        strobe = Check(
            "IR strobe", "fail",
            f"all {len(means)} frames were below min_frame_brightness="
            f"{min_brightness:.1f} (brightest {brightest:.1f})",
            hint=(
                "the infrared emitter is not firing, or the threshold is too "
                f"high: sudo {PROG} config set camera.min_frame_brightness "
                f"{max(1.0, brightest / 2):.0f}"
            ),
        )
    return [device_check, strobe]


def check_enrollment(user: str, socket_path: str) -> Check:
    try:
        faces, source = list_faces(user, socket_path)
    except CommandError as exc:
        if exc.code == EXIT_PERMISSION or os.geteuid() != 0:
            return Check(
                "Enrollment", "warn",
                f"cannot be read without root ({user})",
                hint=f"re-run as  sudo {PROG} doctor",
            )
        return Check("Enrollment", "fail", str(exc), hint=exc.hint)

    if not faces:
        return Check(
            "Enrollment", "warn",
            f"no faces enrolled for {user}",
            hint=f"enrol one with:  sudo {PROG} enroll --user {user}",
        )

    total = sum(int(f.get("samples", 0) or 0) for f in faces)
    names = ", ".join(str(f.get("name", "?")) for f in faces)
    detail = f"{len(faces)} face(s) for {user}: {names} ({total} samples)"
    if source == "store":
        detail += " [read from disk; daemon down]"
    if total < 3:
        return Check(
            "Enrollment", "warn", detail,
            hint="that is very few samples; re-enrol for more reliable matching",
        )
    return Check("Enrollment", "ok", detail)


def check_tpm() -> Check:
    try:
        from iris.store import tpm_available
    except ImportError:
        tpm_available = None  # type: ignore[assignment]

    has_raw = os.path.exists(TPM_DEVICE)
    has_rm = os.path.exists(TPM_RM_DEVICE)
    tools = [t for t in ("tpm2_createprimary", "tpm2_create", "tpm2_load", "tpm2_unseal")
             if shutil.which(t) is None]

    sealed = (Path(STORE_ROOT) / SEALED_KEY).exists()
    plain = (Path(STORE_ROOT) / MASTER_KEY).exists()

    if not has_raw and not has_rm:
        return Check(
            "TPM", "warn",
            "no TPM device; the master key is a plain 0600 root-only file",
            hint="templates are still encrypted, just not bound to this machine",
        )
    if not has_rm:
        return Check(
            "TPM", "warn",
            f"{TPM_DEVICE} exists but {TPM_RM_DEVICE} does not",
            hint=(
                "Iris only uses the kernel resource manager; load the tpm_crb/"
                "tpm_tis driver or start tpm2-abrmd"
            ),
        )
    if tools:
        return Check(
            "TPM", "warn",
            f"{TPM_RM_DEVICE} present but tpm2-tools missing: {', '.join(tools)}",
            hint="sudo apt install tpm2-tools",
        )

    usable = tpm_available() if tpm_available is not None else True
    if not usable:
        return Check(
            "TPM", "warn",
            f"{TPM_RM_DEVICE} present but unusable",
            hint="check permissions on the resource-manager node",
        )
    if sealed:
        return Check("TPM", "ok", f"available; the master key is sealed to it ({TPM_RM_DEVICE})")
    if plain:
        return Check(
            "TPM", "warn",
            "available, but the master key is a plain file",
            hint=(
                "the key predates the TPM being usable; to bind it, clear and "
                f"re-enrol:  sudo {PROG} clear && sudo {PROG} enroll"
            ),
        )
    return Check("TPM", "ok", f"available ({TPM_RM_DEVICE}); the key will be sealed on first enrolment")


# --------------------------------------------------------------------------
# doctor --calibrate
# --------------------------------------------------------------------------

def run_calibration(cfg: Mapping[str, Any], samples: int, timeout: float) -> int:
    """Measure detection latency and genuine-pair similarity on this hardware.

    This is the measurement behind ``docs/CALIBRATION.md``: it collects SFace
    embeddings of one live subject, then reports the *genuine* cosine
    distribution so an administrator can see how much headroom the configured
    threshold really has on their camera and lighting.

    It deliberately does not touch the configuration.  The impostor
    distribution is not measured here (that needs other people's faces), so the
    numbers bound false *rejects* only and the suggested threshold is advice,
    not a validated operating point.
    """
    try:
        from iris.camera import Camera, CameraError
        from iris.engine import EngineError, FaceEngine
        import numpy as np
    except ImportError as exc:
        raise CommandError(
            f"calibration needs OpenCV and numpy: {exc}",
            hint="sudo apt install python3-opencv python3-numpy",
        ) from exc

    console.print(f"{console.bold('Calibration')} — collecting {samples} samples")
    console.print(
        "Look at the camera and move your head slowly through the poses you "
        "would use to log in."
    )
    console.print()

    try:
        engine = FaceEngine(dict(cfg))
    except EngineError as exc:
        raise CommandError(str(exc), hint="run  iris doctor  to check the models") from exc

    detect_ms: list[float] = []
    embed_ms: list[float] = []
    scores: list[float] = []
    embeddings: list[Any] = []
    frames = 0
    progress = ProgressRenderer(console)

    try:
        with Camera.from_config(dict(cfg)) as cam:
            for gray in cam.frames(timeout):
                frames += 1
                start = time.perf_counter()
                faces = engine.detect(gray)
                detect_ms.append((time.perf_counter() - start) * 1000.0)

                face = FaceEngine.select_face(faces)
                if face is None:
                    progress.update(
                        len(embeddings) / samples,
                        f"no face visible ({len(embeddings)}/{samples})",
                    )
                    continue

                start = time.perf_counter()
                embeddings.append(engine.embed(gray, face))
                embed_ms.append((time.perf_counter() - start) * 1000.0)
                scores.append(float(face[14]))
                progress.update(
                    len(embeddings) / samples,
                    f"captured {len(embeddings)}/{samples}",
                )
                if len(embeddings) >= samples:
                    break
    except (CameraError, EngineError) as exc:
        progress.finish()
        raise CommandError(f"calibration failed: {exc}") from exc
    except KeyboardInterrupt:
        progress.finish()
        console.warn("calibration aborted")
        return EXIT_INTERRUPTED
    finally:
        progress.finish()

    if len(embeddings) < 2:
        raise CommandError(
            f"only {len(embeddings)} face(s) captured from {frames} lit frames",
            hint="sit 40-70cm from the screen, facing the camera, and try again",
        )

    similarities = [
        FaceEngine.compare(embeddings[i], embeddings[j])
        for i in range(len(embeddings))
        for j in range(i + 1, len(embeddings))
    ]
    array = np.asarray(similarities, dtype=np.float64)
    p5 = float(np.percentile(array, 5))
    median = float(np.percentile(array, 50))
    worst = float(array.min())
    best = float(array.max())

    detect = np.asarray(detect_ms, dtype=np.float64)
    threshold = float(cfg["recognition"]["threshold"])

    console.print()
    rows = [
        ["frames examined", f"{frames} lit"],
        ["faces embedded", f"{len(embeddings)}"],
        ["embedding dimension", f"{len(embeddings[0].ravel())}"],
        ["detection hit rate", f"{100.0 * len(embeddings) / max(frames, 1):.0f}%"],
        ["detection latency", f"mean {detect.mean():.1f} ms, p95 {float(np.percentile(detect, 95)):.1f} ms"],
        ["embedding latency", f"mean {float(np.mean(embed_ms)):.1f} ms"],
        ["best detection score", f"{max(scores):.3f}"],
        ["genuine cosine min", f"{worst:.3f}"],
        ["genuine cosine p5", f"{p5:.3f}"],
        ["genuine cosine median", f"{median:.3f}"],
        ["genuine cosine max", f"{best:.3f}"],
        ["configured threshold", f"{threshold:.3f}  (margin to worst pair {worst - threshold:+.3f})"],
    ]
    for line in render_table(["MEASUREMENT", "VALUE"], rows):
        console.print(line)

    # A tenth of a cosine below the worst genuine pair keeps a comfortable
    # false-reject margin; never suggest going below SFace's published
    # operating point, and never above 0.6, where the CALIBRATION notes show
    # retries start at awkward angles.
    suggestion = min(max(round(worst - 0.10, 2), config_mod.DEFAULTS["recognition"]["threshold"]), 0.60)
    console.print()
    if worst < threshold:
        console.warn(
            f"the worst genuine pair ({worst:.3f}) is below the configured "
            f"threshold ({threshold:.3f}); expect false rejections"
        )
    console.print(f"Suggested threshold for this hardware: {console.bold(f'{suggestion:.2f}')}")
    console.note(f"  sudo {PROG} config set recognition.threshold {suggestion:.2f}")
    console.note(
        "  This measures one subject, so it bounds false rejections only — it "
        "does not measure impostors."
    )
    return EXIT_OK


# --------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------

_EPILOG = f"""\
examples:
  sudo {PROG} enroll                     enrol your face as "default"
  sudo {PROG} enroll glasses             add a second look
  sudo {PROG} list                       show what is enrolled
  sudo {PROG} test                       run one real authentication, timed
  {PROG} cameras                         list the video devices
  {PROG} config                          print the whole configuration
  sudo {PROG} config set recognition.threshold 0.5
  {PROG} doctor                          diagnose a broken installation

commands that talk to the daemon need root, because the irisd socket is
0600 root:root. Password authentication is never affected by anything here.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Infrared face authentication for Ubuntu / GNOME / Wayland.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version", action="version", version=f"{PROG} {__version__}"
    )
    parser.add_argument(
        "--color", choices=("auto", "always", "never"), default="auto",
        help="colourise output (default: auto, i.e. only on a terminal)",
    )
    parser.add_argument(
        "--socket", default=protocol.SOCKET_PATH, metavar="PATH",
        help=f"daemon socket (default: {protocol.SOCKET_PATH})",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="show debug logging from the iris modules",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    enroll = subparsers.add_parser(
        "enroll", help="record a face (root)",
        description="Record a new face template. Requires root.",
    )
    enroll.add_argument(
        "name", nargs="?", default=None,
        help="label for this look, e.g. 'glasses' (default: default)",
    )
    # --name is the spelling the GTK front end uses (see the "GUI <-> CLI
    # contract" in iris/gui/backend.py): building an argv from named options
    # only means it never has to reason about positional ordering.
    enroll.add_argument(
        "--name", dest="name_opt", metavar="NAME",
        help="same as the positional argument, for scripted callers",
    )
    enroll.add_argument("--user", help="account to enrol (default: the invoking user)")
    enroll.add_argument(
        "--timeout", type=float, default=ENROLL_TIMEOUT, metavar="SECONDS",
        help="budget for the whole enrolment (default: %(default)s)",
    )
    enroll.add_argument(
        "--json", action="store_true",
        help="stream newline-delimited JSON progress lines and a final result",
    )
    enroll.set_defaults(func=cmd_enroll)

    listing = subparsers.add_parser(
        "list", help="show enrolled faces",
        description="List the faces enrolled for a user.",
    )
    listing.add_argument("--user", help="account to inspect (default: the invoking user)")
    listing.add_argument("--json", action="store_true", help="machine-readable output")
    listing.set_defaults(func=cmd_list)

    remove = subparsers.add_parser(
        "remove", help="delete one face (root)",
        description="Delete a single named face template. Requires root.",
    )
    remove.add_argument("name", nargs="?", default=None, help="the face label to delete")
    remove.add_argument(
        "--name", dest="name_opt", metavar="NAME",
        help="same as the positional argument, for scripted callers",
    )
    remove.add_argument("--user", help="account to edit (default: the invoking user)")
    remove.add_argument("--json", action="store_true", help="machine-readable output")
    remove.set_defaults(func=cmd_remove)

    clear = subparsers.add_parser(
        "clear", help="delete every face (root)",
        description="Delete every face template for a user. Requires root.",
    )
    clear.add_argument("--user", help="account to clear (default: the invoking user)")
    clear.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    clear.add_argument(
        "--json", action="store_true",
        help="machine-readable output; implies --yes (the caller has already asked)",
    )
    clear.set_defaults(func=cmd_clear)

    test = subparsers.add_parser(
        "test", help="try a real authentication",
        description="Run one genuine authentication attempt and report the result.",
    )
    test.add_argument("--user", help="account to authenticate (default: the invoking user)")
    test.add_argument(
        "--timeout", type=float, metavar="SECONDS",
        help="override auth.timeout for this attempt",
    )
    test.add_argument("--json", action="store_true", help="machine-readable output")
    test.set_defaults(func=cmd_test)

    cameras = subparsers.add_parser(
        "cameras", help="list video devices",
        description="Show the V4L2 devices, marking the infrared camera.",
    )
    cameras.add_argument(
        "--all", action="store_true",
        help="also show V4L2 metadata nodes (never usable for capture)",
    )
    cameras.add_argument("--json", action="store_true", help="machine-readable output")
    cameras.set_defaults(func=cmd_cameras)

    config_parser = subparsers.add_parser(
        "config", help="get or set settings",
        description=(
            "Read and write /etc/iris/config.toml. With no arguments the whole "
            "configuration is printed."
        ),
    )
    config_parser.add_argument(
        "--json", action="store_true", dest="json_top",
        help="machine-readable output",
    )
    config_sub = config_parser.add_subparsers(dest="config_action", metavar="<action>")

    config_get = config_sub.add_parser("get", help="print one setting, a section, or everything")
    config_get.add_argument("key", nargs="?", help="dotted key, e.g. camera.device")
    config_get.add_argument("--json", action="store_true", help="machine-readable output")

    config_set = config_sub.add_parser("set", help="change one setting (root)")
    config_set.add_argument("key", help="dotted key, e.g. camera.device")
    config_set.add_argument("value", help="new value; coerced to the setting's type")
    config_set.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    config_unset = config_sub.add_parser("unset", help="restore one setting to its default (root)")
    config_unset.add_argument("key", help="dotted key, e.g. recognition.threshold")
    config_unset.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    config_keys = config_sub.add_parser("keys", help="list every setting with its type and default")
    config_keys.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    config_parser.set_defaults(func=cmd_config)

    # A whole-document writer, separate from `config set KEY VALUE`. The
    # settings panel accumulates several changes and applies them together, and
    # every privileged write costs one polkit prompt — so it needs to say "here
    # is the entire configuration" in a single invocation rather than one
    # prompt per changed key.
    config_set_all = subparsers.add_parser(
        "config-set", help="apply a whole configuration document from stdin (root)",
        description=(
            "Read a JSON configuration object from standard input and write it "
            "to /etc/iris/config.toml (via irisd when it is running). Requires root."
        ),
    )
    config_set_all.add_argument("--json", action="store_true", help="machine-readable output")
    config_set_all.set_defaults(func=cmd_config_set_all)

    doctor = subparsers.add_parser(
        "doctor", help="diagnose the installation",
        description=(
            "Check every part of the installation and print a remediation hint "
            "for anything wrong. Exits non-zero if any check fails."
        ),
    )
    doctor.add_argument("--user", help="account whose enrolment to check")
    doctor.add_argument(
        "--no-camera", action="store_true",
        help="skip the camera and strobe checks (they take a couple of seconds)",
    )
    doctor.add_argument(
        "--camera-timeout", type=float, default=2.5, metavar="SECONDS",
        help="how long to sample frames for (default: %(default)s)",
    )
    doctor.add_argument(
        "--calibrate", action="store_true",
        help="also measure detection latency and genuine-match similarity",
    )
    doctor.add_argument(
        "--samples", type=int, default=30, metavar="N",
        help="faces to collect when calibrating (default: %(default)s)",
    )
    doctor.add_argument(
        "--calibrate-timeout", type=float, default=45.0, metavar="SECONDS",
        help="calibration capture budget (default: %(default)s)",
    )
    doctor.set_defaults(func=cmd_doctor)

    status = subparsers.add_parser(
        "status", help="summarise the current state",
        description="One-screen summary of the daemon, configuration and enrolment.",
    )
    status.add_argument("--user", help="account to summarise (default: the invoking user)")
    status.add_argument("--json", action="store_true", help="machine-readable output")
    status.set_defaults(func=cmd_status)

    return parser


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Reject argument combinations argparse cannot express."""
    if getattr(args, "samples", None) is not None and args.samples < 2:
        parser.error("--samples must be at least 2 (similarity needs a pair)")
    for name in ("timeout", "camera_timeout", "calibrate_timeout"):
        value = getattr(args, name, None)
        if value is not None and (not math.isfinite(value) or value <= 0):
            parser.error(f"--{name.replace('_', '-')} must be a positive number")


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    console.configure(args.color)

    # Module logs (config fallbacks, camera warnings) go to stderr so they can
    # never corrupt the machine-readable stdout of --json.
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_USAGE

    _validate_args(parser, args)

    try:
        return int(args.func(args))
    except CommandError as exc:
        # A machine-readable invocation must fail machine-readably: the GTK
        # front end parses stdout and only falls back to stderr when there was
        # nothing there, so an error that appears on stderr alone reaches the
        # user as "that did not work" with no explanation.
        if _json_mode(args):
            emit_json({"ok": False, "error": str(exc), **({"hint": exc.hint} if exc.hint else {})})
        console.error(str(exc))
        if exc.hint:
            console.hint(exc.hint)
        return exc.code
    except KeyboardInterrupt:
        console.print()
        console.error("interrupted")
        return EXIT_INTERRUPTED
    except BrokenPipeError:
        # `iris config | head` closes the pipe under us. Redirect stdout to
        # /dev/null so the interpreter's own flush at exit does not print a
        # second, uglier error on the way out.
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except OSError:  # pragma: no cover
            pass
        return 141  # 128 + SIGPIPE


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
