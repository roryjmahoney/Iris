"""``irisd`` — the Iris face-authentication daemon.

The daemon is the only component that ever touches the infrared camera, the
recognition models and the encrypted template store.  Everything else (the PAM
module, the CLI, the GTK settings panel) talks to it over the newline-delimited
JSON protocol in :mod:`iris.protocol`.

Why a daemon at all
-------------------
Three reasons, all of them practical:

1. **Model load cost.**  Building a :class:`~iris.engine.FaceEngine` loads two
   ONNX graphs; that is ~100 ms and ~40 MB of RSS.  Paying it inside PAM on
   every login would make face unlock feel slower than typing the password.
   The daemon loads the models once, at startup, so an authentication is
   camera-open plus a handful of 13 ms frames.
2. **Privilege separation.**  Templates live in ``/var/lib/iris`` (0700 root)
   and the master key is TPM-sealed.  Exactly one root process needs that
   access; the GUI does not.
3. **Serialising the camera.**  ``/dev/video2`` is a single-open device with a
   strobing emitter.  Two processes fighting over it produce read errors and
   half-lit frames, so every capture in the system funnels through the single
   :attr:`IrisDaemon._camera_lock` here and concurrent requests queue.

Security posture
----------------
* The socket is ``/run/irisd/socket``, ``root:root 0600``, inside a ``0700``
  directory — the filesystem alone keeps unprivileged users out.
* Every connection is additionally checked with ``SO_PEERCRED`` and refused
  unless the peer's uid is 0.  Belt and braces: a future packaging change that
  loosens the socket mode must not silently open the daemon to every user.
* Requests are capped at 64 KiB by :func:`iris.protocol.read_message` and the
  number of concurrent connections is capped here, so a local client cannot
  exhaust the daemon's memory or thread pool.
* Nothing biometric is ever logged.  Embeddings and frames stay in memory;
  logs carry counters, reason codes and (at debug level) similarity scores,
  which are not invertible to an image.

Failure philosophy
------------------
Every operation fails closed: an unexpected exception anywhere in a request
handler becomes ``{"ok": false}`` with a reason from the closed vocabulary in
:mod:`iris.protocol`, never a partial success and never an unhandled traceback
that drops the connection while PAM is waiting on it.
"""

from __future__ import annotations

import argparse
import copy
import errno
import json
import logging
import os
import re
import signal
import socket
import stat
import struct
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Final, Mapping, Sequence

import numpy as np

from . import __version__
from . import config as config_module
from . import protocol
from .camera import Camera, CameraError, list_cameras
from .engine import EngineError, FaceEngine
from .liveness import LivenessChecker
from .store import (
    DEFAULT_ROOT,
    FailureTracker,
    KeyManager,
    StoreError,
    TemplateStore,
    ensure_root_dir,
)

log = logging.getLogger("iris.daemon")

__all__ = ["IrisDaemon", "DaemonStartupError", "configure_logging", "main"]


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

SOCKET_PATH: Final[str] = protocol.SOCKET_PATH

#: ``/run/irisd`` — only root may traverse it, so the socket inside is
#: unreachable for anyone else even if its own mode were ever wrong.
RUNTIME_DIR_MODE: Final[int] = 0o700
SOCKET_MODE: Final[int] = 0o600

#: Pending connections the kernel will queue for us.  Small on purpose: more
#: than a couple of clients waiting means something is wrong, and a short queue
#: makes the stale-socket probe in :meth:`IrisDaemon._clear_stale_socket`
#: reliable.
LISTEN_BACKLOG: Final[int] = 8

#: Concurrent client connections.  Handlers are one thread each; the camera is
#: serialised anyway, so this only bounds bookkeeping and rejects a local
#: fork-bomb-style client early.
MAX_CONNECTIONS: Final[int] = 16

#: How long a connection may sit idle between requests before we close it.
IDLE_TIMEOUT: Final[float] = 30.0

#: Time given to in-flight requests when shutting down.  Longer than the
#: maximum auth timeout (60 s is the config ceiling) would delay a system
#: shutdown; a client whose request is cut short simply sees the connection
#: close, which every caller already treats as a failure.
SHUTDOWN_GRACE: Final[float] = 10.0

#: Below this much remaining budget there is no point opening the camera: the
#: sensor needs ~0.5 s to deliver its first usable frame.
MIN_CAPTURE_SECONDS: Final[float] = 0.5

#: How long ``enroll`` waits for the camera if an authentication holds it.
ENROLL_LOCK_TIMEOUT: Final[float] = 15.0

# -- enrolment shape -------------------------------------------------------- #

#: Guided capture sequence.  Multiple poses matter more than more frames of one
#: pose: SFace is not pose-invariant, and a template built only from a
#: dead-centre stare rejects the same user the moment they lean on one elbow.
ENROLL_POSES: Final[tuple[tuple[str, str], ...]] = (
    ("centre", "Look straight at the camera"),
    ("left", "Turn your head slightly to your left"),
    ("right", "Turn your head slightly to your right"),
    ("up", "Tilt your chin slightly up"),
    ("down", "Tilt your chin slightly down"),
)
ENROLL_SAMPLES_PER_POSE: Final[int] = 3
ENROLL_TARGET_SAMPLES: Final[int] = len(ENROLL_POSES) * ENROLL_SAMPLES_PER_POSE

#: Enrolment succeeds with fewer samples than the target as long as the centre
#: pose was captured and this many samples exist overall — a user who cannot
#: hold a chin-down pose still gets a usable template rather than an error.
ENROLL_MIN_SAMPLES: Final[int] = 6

ENROLL_POSE_TIMEOUT: Final[float] = 12.0
#: Grace after announcing a pose, so the frames captured are of the *new* pose
#: rather than of the user still turning their head.
ENROLL_POSE_SETTLE: Final[float] = 1.2
#: Total budget; comfortably above 5 poses x (12 s + settle) is not needed
#: because poses that fill up early return their time to the pool.
ENROLL_TOTAL_TIMEOUT: Final[float] = 70.0
#: Minimum spacing between accepted samples, so three "different" samples are
#: not three copies of the same instant.
ENROLL_SAMPLE_INTERVAL: Final[float] = 0.25
#: Rate limit for hint lines, so a user who is out of frame does not receive
#: 7 identical progress messages a second.
ENROLL_HINT_INTERVAL: Final[float] = 0.7

#: Cosine floor for "this is still the same person" during enrolment.  Far
#: below the recognition threshold on purpose: a genuine profile shot can sit
#: well under it, while impostor pairs cluster near 0.0-0.2.  This only has to
#: catch a second person stepping into frame mid-enrolment, which would
#: otherwise silently add their face to the user's template.
ENROLL_IDENTITY_FLOOR: Final[float] = 0.25

# -- reason mapping --------------------------------------------------------- #

#: Liveness rejections that mean "the user is badly positioned", not "this is a
#: spoof".  Reporting spoof_suspected to someone sitting too far away is both
#: wrong and unhelpful, so these collapse to no_face instead.
_GEOMETRIC_LIVENESS_REASONS: Final[frozenset[str]] = frozenset({
    "no_face", "face_too_small", "out_of_frame",
})

#: User-facing guidance per liveness reason, for enrolment progress lines.
#: Authentication deliberately does *not* return these: telling an attacker
#: which test they tripped hands them a tuning signal.
_LIVENESS_HINTS: Final[dict[str, str]] = {
    "no_face": "No face detected — move into view of the camera",
    "face_too_small": "Move a little closer to the camera",
    "out_of_frame": "Centre your face in the frame",
    "dark_frame": "The infrared emitter is not lighting the scene",
    "no_ir_return": "Look directly at the camera — a photo or screen will not work",
    "saturated": "Too much glare — move back slightly",
    "low_contrast": "Not enough detail visible — adjust your position or the lighting",
    "flat_region": "Hold still and look straight at the camera",
    "static_input": "The camera is repeating frames — hold on",
}

#: Mirrors ``iris.store._USER_RE``.  Duplicated rather than imported because it
#: is private there, and because this is a security control the daemon must
#: apply *before* a username reaches anything that builds a path from it.
_USER_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,31}\$?$")

_UCRED_FMT: Final[str] = "3i"  # struct ucred { pid_t pid; uid_t uid; gid_t gid; }


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #


class DaemonStartupError(Exception):
    """The daemon cannot start (socket in use, wrong privileges, bad paths)."""


class RequestError(Exception):
    """A client sent a malformed or unacceptable request.

    Handlers raise this for anything the *client* got wrong; the dispatcher
    turns it into ``{"ok": false, "error": …}`` (or the appropriate auth-shaped
    response) rather than logging a traceback.
    """


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


class _JournalFormatter(logging.Formatter):
    """Prefix each line with a syslog priority journald understands.

    systemd reads a service's stderr and parses a leading ``<N>`` as the
    record's priority, so ``journalctl -p err -u irisd`` filters correctly
    without linking against libsystemd.  Timestamps are omitted because
    journald stamps every record itself.
    """

    _PRIORITY: Final[dict[int, int]] = {
        logging.CRITICAL: 2,  # crit
        logging.ERROR: 3,     # err
        logging.WARNING: 4,   # warning
        logging.INFO: 6,      # info
        logging.DEBUG: 7,     # debug
    }

    def format(self, record: logging.LogRecord) -> str:
        priority = self._PRIORITY.get(record.levelno, 6)
        return f"<{priority}>{record.name}: {super().format(record)}"


def configure_logging(level: str | int = "INFO") -> None:
    """Send structured logs to stderr, formatted for wherever we are running.

    Under systemd (``JOURNAL_STREAM`` is set) records get syslog priority
    prefixes; on a terminal they get timestamps.  The root logger is
    reconfigured so that ``iris.camera``, ``iris.store`` and friends — which all
    log through their own module loggers — are captured too.
    """
    if isinstance(level, str):
        resolved = logging.getLevelNamesMapping().get(level.strip().upper())
        if resolved is None:
            resolved = logging.INFO
    else:
        resolved = int(level)

    handler = logging.StreamHandler(sys.stderr)
    if os.environ.get("JOURNAL_STREAM"):
        handler.setFormatter(_JournalFormatter("%(message)s"))
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(resolved)


def _sd_notify(state: str) -> None:
    """Best-effort ``sd_notify(3)``, so a ``Type=notify`` unit works.

    Silently does nothing when not started by systemd.  Never raises: telling
    the service manager we are ready is not worth failing a start over.
    """
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):  # abstract namespace
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC) as sock:
            sock.settimeout(1.0)
            sock.connect(address)
            sock.sendall(state.encode("utf-8"))
    except OSError as exc:
        log.debug("sd_notify(%r) failed: %s", state, exc)


# --------------------------------------------------------------------------- #
# Small value types
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PeerCredentials:
    """What ``SO_PEERCRED`` told us about the process on the other end."""

    pid: int
    uid: int
    gid: int

    def __str__(self) -> str:
        return f"pid={self.pid} uid={self.uid} gid={self.gid}"


@dataclass(slots=True)
class AuthOutcome:
    """Result of one authentication attempt, before it becomes a response."""

    ok: bool
    confidence: float
    reason: str
    face: str | None = None
    #: Diagnostics for the log line only; never sent to the client.
    frames: int = 0
    faces_seen: int = 0
    embedded: int = 0
    detail: str = ""


@dataclass(slots=True)
class _CaptureCounters:
    """Per-attempt frame bookkeeping shared by auth and enrolment."""

    lit_frames: int = 0
    face_frames: int = 0
    embedded_frames: int = 0
    live_reasons: Counter[str] = field(default_factory=Counter)


# --------------------------------------------------------------------------- #
# The daemon
# --------------------------------------------------------------------------- #


class IrisDaemon:
    """Serves the Iris protocol on a UNIX socket.

    One instance per process.  :meth:`run` blocks until a signal handler or
    :meth:`stop` asks it to shut down, and returns the process exit code.
    """

    def __init__(
        self,
        *,
        socket_path: str = SOCKET_PATH,
        config_path: str = config_module.CONFIG_PATH,
        state_dir: str = DEFAULT_ROOT,
        use_tpm: bool = True,
    ) -> None:
        self.socket_path = socket_path
        self.config_path = config_path
        self.state_dir = state_dir

        self._keys = KeyManager(state_dir, use_tpm=use_tpm)
        self._store = TemplateStore(state_dir, self._keys)
        self._failures = FailureTracker()

        # Guards /dev/video2 *and*, by construction, the FaceEngine: every use
        # of the engine happens inside a camera-locked section, which is what
        # makes it safe to share one non-thread-safe engine across handlers.
        self._camera_lock = threading.Lock()

        self._cfg_lock = threading.Lock()
        self._cfg: dict[str, Any] = config_module.load_config(config_path)
        self._cfg_stamp: tuple[int, int, int] | None = self._config_stamp()

        self._engine: FaceEngine | None = None
        self._engine_signature: str | None = None
        self._engine_error: str = ""

        self._listener: socket.socket | None = None
        self._stop = threading.Event()
        self._wake_r = -1
        self._wake_w = -1

        self._conn_lock = threading.Lock()
        self._threads: set[threading.Thread] = set()

        self._handlers: dict[str, Callable[[dict[str, Any], socket.socket], dict[str, Any]]] = {
            protocol.OP_PING: self._op_ping,
            protocol.OP_AUTH: self._op_auth,
            protocol.OP_LIST: self._op_list,
            protocol.OP_CAMERAS: self._op_cameras,
            protocol.OP_ENROLL: self._op_enroll,
            protocol.OP_REMOVE: self._op_remove,
            protocol.OP_CLEAR: self._op_clear,
            protocol.OP_CONFIG_GET: self._op_config_get,
            protocol.OP_CONFIG_SET: self._op_config_set,
        }

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def run(self) -> int:
        """Serve until asked to stop.  Returns a process exit code."""
        if os.geteuid() != 0:
            raise DaemonStartupError(
                f"irisd must run as root (euid=0); running as euid={os.geteuid()}"
            )

        self._prepare_state_dir()
        self._preload_models()
        self._preload_master_key()

        self._wake_r, self._wake_w = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        self._install_signal_handlers()

        try:
            self._listener = self._create_listener()
        except BaseException:
            # Startup failed (socket in use, bad permissions). Close the self
            # pipe so a caller that retries run() does not leak descriptors,
            # and leave the existing socket file alone: it belongs to whoever
            # is actually listening on it.
            for fd in (self._wake_r, self._wake_w):
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._wake_r = self._wake_w = -1
            raise
        log.info(
            "irisd %s listening on %s (config=%s, state=%s)",
            __version__, self.socket_path, self.config_path, self.state_dir,
        )
        _sd_notify(f"READY=1\nSTATUS=Listening on {self.socket_path}\nMAINPID={os.getpid()}")

        try:
            self._accept_loop()
        finally:
            _sd_notify("STOPPING=1\nSTATUS=Shutting down")
            self._shutdown()
        return 0

    def stop(self) -> None:
        """Ask the accept loop to exit.  Safe from a signal handler or thread."""
        self._stop.set()
        self._wake()

    def _wake(self) -> None:
        """Nudge the selector so a blocked ``select()`` returns immediately."""
        if self._wake_w < 0:
            return
        try:
            os.write(self._wake_w, b"\x01")
        except (BlockingIOError, OSError):
            # Full pipe means a wakeup is already pending, which is all we
            # wanted; a closed pipe means we are already shutting down.
            pass

    def _install_signal_handlers(self) -> None:
        """Handle SIGTERM/SIGINT by draining, not by dying mid-request.

        Only the main thread may install handlers; when the daemon is embedded
        in another program's thread we simply run without them and rely on
        :meth:`stop`.
        """
        def handler(signum: int, _frame: Any) -> None:
            # Runs in the main thread between bytecodes. Keep it to two cheap,
            # reentrancy-safe operations: set a flag and poke the self-pipe.
            log.info("received %s; shutting down", signal.Signals(signum).name)
            self.stop()

        try:
            signal.signal(signal.SIGTERM, handler)
            signal.signal(signal.SIGINT, handler)
            # A client that disappears mid-response must surface as EPIPE on
            # write (which the protocol layer reports), never as a fatal signal.
            signal.signal(signal.SIGPIPE, signal.SIG_IGN)
        except ValueError:
            log.warning("not running in the main thread; signal handlers not installed")

    # -- filesystem setup ----------------------------------------------- #

    def _prepare_state_dir(self) -> None:
        """Create/repair ``/var/lib/iris`` before any request needs it."""
        try:
            ensure_root_dir(self.state_dir)
        except (StoreError, OSError) as exc:
            raise DaemonStartupError(f"cannot prepare {self.state_dir}: {exc}") from exc

    def _prepare_runtime_dir(self) -> str:
        """Create/repair the socket's directory as ``root:root 0700``."""
        directory = os.path.dirname(self.socket_path) or "/"

        try:
            st = os.lstat(directory)
        except FileNotFoundError:
            try:
                os.makedirs(directory, mode=RUNTIME_DIR_MODE, exist_ok=True)
            except OSError as exc:
                raise DaemonStartupError(f"cannot create {directory}: {exc}") from exc
            st = os.lstat(directory)

        if stat.S_ISLNK(st.st_mode):
            # A symlink here would let anyone who can plant it redirect the
            # socket (and our chmod/chown) into a directory they control.
            raise DaemonStartupError(f"{directory} is a symlink; refusing to use it")
        if not stat.S_ISDIR(st.st_mode):
            raise DaemonStartupError(f"{directory} exists and is not a directory")

        try:
            # umask can only clear bits, so mkdir's mode is not authoritative;
            # set it explicitly. systemd's RuntimeDirectory= creates 0755.
            if stat.S_IMODE(st.st_mode) != RUNTIME_DIR_MODE:
                log.warning(
                    "tightening %s from %#o to %#o", directory,
                    stat.S_IMODE(st.st_mode), RUNTIME_DIR_MODE,
                )
                os.chmod(directory, RUNTIME_DIR_MODE)
            if (st.st_uid, st.st_gid) != (0, 0):
                log.warning(
                    "re-owning %s to root:root (was %d:%d)",
                    directory, st.st_uid, st.st_gid,
                )
                os.chown(directory, 0, 0)
        except OSError as exc:
            raise DaemonStartupError(f"cannot secure {directory}: {exc}") from exc

        return directory

    def _clear_stale_socket(self) -> None:
        """Remove a leftover socket, but never one a live daemon is using.

        A crash or ``kill -9`` leaves the socket file behind and ``bind()``
        would fail with EADDRINUSE.  Blindly unlinking it, though, would let a
        second instance steal the socket from a healthy daemon, after which PAM
        would silently talk to whichever one won.  So we probe first: a
        connection refusal proves nobody is accepting.
        """
        try:
            st = os.lstat(self.socket_path)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise DaemonStartupError(f"cannot stat {self.socket_path}: {exc}") from exc

        if not stat.S_ISSOCK(st.st_mode):
            raise DaemonStartupError(
                f"{self.socket_path} exists and is not a socket; refusing to remove it"
            )

        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(0.5)
            probe.connect(self.socket_path)
        except (ConnectionRefusedError, FileNotFoundError):
            pass  # nobody home: the file is stale
        except OSError as exc:
            # ETIMEDOUT/EAGAIN means the backlog is full, i.e. someone *is*
            # listening but busy. Treat anything ambiguous as "occupied".
            raise DaemonStartupError(
                f"another process appears to be listening on {self.socket_path} ({exc})"
            ) from exc
        else:
            raise DaemonStartupError(
                f"another irisd is already listening on {self.socket_path}"
            )
        finally:
            probe.close()

        log.warning("removing stale socket %s", self.socket_path)
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise DaemonStartupError(
                f"cannot remove stale socket {self.socket_path}: {exc}"
            ) from exc

    def _create_listener(self) -> socket.socket:
        """Bind, secure and listen on the UNIX socket."""
        self._prepare_runtime_dir()
        self._clear_stale_socket()

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
        try:
            # Bind under a restrictive umask so the socket is never, not even
            # for an instant, more permissive than 0600. chmod after bind would
            # leave a window in which any user could connect.
            previous_umask = os.umask(0o177)
            try:
                sock.bind(self.socket_path)
            finally:
                os.umask(previous_umask)

            os.chmod(self.socket_path, SOCKET_MODE)
            os.chown(self.socket_path, 0, 0)
            sock.listen(LISTEN_BACKLOG)
            # A timeout on the listener keeps accept() from blocking forever if
            # the selector ever hands us a spurious readiness event.
            sock.settimeout(1.0)
        except OSError as exc:
            sock.close()
            raise DaemonStartupError(f"cannot listen on {self.socket_path}: {exc}") from exc
        return sock

    def _shutdown(self) -> None:
        """Close the listener, unlink the socket and drain worker threads."""
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.close()
            except OSError as exc:
                log.debug("error closing listener: %s", exc)

        # Unlink before waiting for workers: new clients should get "daemon
        # unavailable" immediately rather than connecting to a dying instance.
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("could not remove %s: %s", self.socket_path, exc)

        with self._conn_lock:
            workers = list(self._threads)
        if workers:
            log.info("waiting up to %.0fs for %d request(s) to finish",
                     SHUTDOWN_GRACE, len(workers))
        deadline = time.monotonic() + SHUTDOWN_GRACE
        for worker in workers:
            worker.join(max(0.0, deadline - time.monotonic()))
        still_running = [w for w in workers if w.is_alive()]
        if still_running:
            # Daemon threads: the interpreter will not wait for them. Say so,
            # because an abandoned handler may hold the camera open.
            log.warning("%d request thread(s) did not finish in time", len(still_running))

        for fd in (self._wake_r, self._wake_w):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._wake_r = self._wake_w = -1
        log.info("irisd stopped")

    # -- accept loop ----------------------------------------------------- #

    def _accept_loop(self) -> None:
        """Accept connections until :meth:`stop` is called."""
        import selectors  # local: only the parent process ever needs it

        listener = self._listener
        assert listener is not None

        selector = selectors.DefaultSelector()
        selector.register(listener, selectors.EVENT_READ)
        selector.register(self._wake_r, selectors.EVENT_READ)
        try:
            while not self._stop.is_set():
                try:
                    events = selector.select(timeout=1.0)
                except OSError as exc:
                    if exc.errno == errno.EBADF and self._stop.is_set():
                        break
                    raise
                for key, _mask in events:
                    if key.fileobj is listener:
                        self._accept_one(listener)
                    else:
                        self._drain_wakeup()
        finally:
            selector.close()

    def _drain_wakeup(self) -> None:
        """Empty the self-pipe; its only job was to interrupt ``select()``."""
        try:
            while os.read(self._wake_r, 4096):
                pass
        except (BlockingIOError, OSError):
            pass

    def _accept_one(self, listener: socket.socket) -> None:
        """Accept one connection and hand it to a worker thread."""
        try:
            conn, _address = listener.accept()
        except TimeoutError:
            return
        except OSError as exc:
            if exc.errno in (errno.EMFILE, errno.ENFILE):
                # Out of file descriptors. Sleeping briefly stops a hot loop
                # that would otherwise burn a core while the condition clears.
                log.error("cannot accept connection: %s; backing off", exc)
                time.sleep(0.1)
                return
            if self._stop.is_set():
                return
            log.error("accept() failed: %s", exc)
            return

        with self._conn_lock:
            # Prune finished threads here rather than from the threads
            # themselves, which would need the lock at exit time.
            self._threads = {t for t in self._threads if t.is_alive()}
            if len(self._threads) >= MAX_CONNECTIONS:
                over_capacity = True
            else:
                over_capacity = False

        if over_capacity:
            log.warning("refusing connection: %d already active", MAX_CONNECTIONS)
            self._refuse(conn, "irisd is busy; try again")
            return

        worker = threading.Thread(
            target=self._serve_connection,
            args=(conn,),
            name=f"irisd-conn-{conn.fileno()}",
            daemon=True,
        )
        with self._conn_lock:
            self._threads.add(worker)
        worker.start()

    @staticmethod
    def _refuse(conn: socket.socket, message: str) -> None:
        """Send one error line and close, ignoring any write failure."""
        try:
            conn.settimeout(1.0)
            protocol.write_message(conn, {"ok": False, "error": message})
        except (protocol.ProtocolError, OSError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    # ------------------------------------------------------------------ #
    # Connection handling
    # ------------------------------------------------------------------ #

    def _serve_connection(self, conn: socket.socket) -> None:
        """Read requests from one client until it goes away."""
        peer: PeerCredentials | None = None
        try:
            conn.settimeout(IDLE_TIMEOUT)
            peer = self._peer_credentials(conn)
            if peer.uid != 0:
                # Unreachable through the 0600 socket; this is the backstop for
                # a mis-packaged unit that loosened the permissions.
                log.error("refusing non-root peer (%s)", peer)
                self._refuse(conn, "permission denied: irisd only serves root")
                return

            log.debug("connection accepted from %s", peer)
            while not self._stop.is_set():
                try:
                    request = protocol.read_message(conn)
                except protocol.RequestTimeout:
                    log.debug("closing idle connection from %s", peer)
                    return
                except protocol.MessageTooLarge as exc:
                    log.warning("%s sent an oversized message: %s", peer, exc)
                    self._refuse(conn, "request too large")
                    return
                except protocol.MalformedMessage as exc:
                    log.warning("%s sent a malformed message: %s", peer, exc)
                    self._refuse(conn, f"malformed request: {exc}")
                    return

                if request is None:
                    log.debug("client %s closed the connection", peer)
                    return

                response = self._dispatch(request, conn)
                protocol.write_message(conn, response)

        except protocol.TransportError as exc:
            # Client vanished (PAM timed out and closed, GUI was killed, …).
            log.debug("transport error with %s: %s", peer, exc)
        except protocol.ProtocolError as exc:
            log.warning("protocol error with %s: %s", peer, exc)
        except OSError as exc:
            log.warning("socket error with %s: %s", peer, exc)
        except Exception:  # noqa: BLE001 - a handler bug must not kill the daemon
            log.exception("unhandled error while serving %s", peer)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _peer_credentials(conn: socket.socket) -> PeerCredentials:
        """Read the peer's credentials from the kernel.

        ``SO_PEERCRED`` is filled in by the kernel at ``connect()`` time and
        cannot be forged by the client, which is what makes it a usable
        authorisation check rather than a hint.
        """
        try:
            raw = conn.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize(_UCRED_FMT)
            )
        except OSError as exc:
            raise protocol.TransportError(f"cannot read peer credentials: {exc}") from exc
        pid, uid, gid = struct.unpack(_UCRED_FMT, raw)
        return PeerCredentials(pid=pid, uid=uid, gid=gid)

    def _dispatch(self, request: dict[str, Any], conn: socket.socket) -> dict[str, Any]:
        """Route one request to its handler and normalise every failure."""
        op = request.get("op")
        if not isinstance(op, str) or op not in protocol.OPS:
            log.warning("unknown op %r", op)
            return {"ok": False, "error": f"unknown op {op!r}"}

        handler = self._handlers[op]
        started = time.monotonic()
        try:
            response = handler(request, conn)
        except RequestError as exc:
            log.warning("%s: bad request: %s", op, exc)
            return self._error_for(op, str(exc))
        except protocol.ProtocolError:
            raise  # the connection is broken; the caller closes it
        except Exception as exc:  # noqa: BLE001 - fail closed, never crash
            log.exception("%s: unhandled error", op)
            return self._error_for(op, f"internal error: {exc}")

        log.debug("%s completed in %.0f ms", op, (time.monotonic() - started) * 1000.0)
        return response

    @staticmethod
    def _error_for(op: str, message: str) -> dict[str, Any]:
        """Shape an error the way the requesting op's clients expect.

        ``auth`` clients (PAM above all) switch on ``reason``, so an auth
        failure must always carry one from the closed vocabulary even when the
        cause was a malformed request.
        """
        if op == protocol.OP_AUTH:
            response = protocol.auth_response(False, 0.0, protocol.REASON_NOT_ENROLLED)
            response["error"] = message
            return response
        return {"ok": False, "error": message}

    # ------------------------------------------------------------------ #
    # Request validation
    # ------------------------------------------------------------------ #

    @staticmethod
    def _require_user(request: Mapping[str, Any]) -> str:
        """Extract and validate the ``user`` field.

        The username becomes a path component in :mod:`iris.store`, so it is
        validated here as well as there: defence in depth against a traversal
        bug being one refactor away.
        """
        user = request.get("user")
        if not isinstance(user, str):
            raise RequestError("missing or non-string 'user'")
        user = user.strip()
        if not user:
            raise RequestError("'user' must not be empty")
        if not _USER_RE.match(user):
            raise RequestError(f"refusing unsafe username {user!r}")
        return user

    @staticmethod
    def _require_name(request: Mapping[str, Any]) -> str:
        """Extract and validate the face label."""
        name = request.get("name")
        if not isinstance(name, str):
            raise RequestError("missing or non-string 'name'")
        name = name.strip()
        if not name:
            raise RequestError("'name' must not be empty")
        if len(name) > 64:
            raise RequestError("'name' must be 64 characters or fewer")
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
            raise RequestError("'name' must not contain control characters")
        return name

    # ------------------------------------------------------------------ #
    # Configuration and models
    # ------------------------------------------------------------------ #

    def _config_stamp(self) -> tuple[int, int, int] | None:
        """Cheap identity of the config file, for change detection."""
        try:
            st = os.stat(self.config_path)
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    def current_config(self) -> dict[str, Any]:
        """Return the live configuration, re-reading it if the file changed.

        One ``stat()`` per request buys hot-reload: an administrator editing
        ``/etc/iris/config.toml`` (or the settings panel writing it) takes
        effect on the next authentication without a daemon restart.  The copy
        is deep so a handler can never mutate the cached dict.
        """
        with self._cfg_lock:
            stamp = self._config_stamp()
            if stamp != self._cfg_stamp:
                self._cfg = config_module.load_config(self.config_path)
                self._cfg_stamp = stamp
                log.info("reloaded configuration from %s", self.config_path)
            return copy.deepcopy(self._cfg)

    @staticmethod
    def _engine_signature_for(cfg: Mapping[str, Any]) -> str:
        """Identity of the engine-relevant settings.

        Only these fields are baked into a :class:`FaceEngine` at construction
        time; a change to any of them means the cached engine is stale.
        """
        recognition = dict(cfg.get("recognition") or {})
        camera = dict(cfg.get("camera") or {})
        relevant = {
            "recognition": {k: recognition[k] for k in sorted(recognition)},
            "width": camera.get("width"),
            "height": camera.get("height"),
        }
        return json.dumps(relevant, sort_keys=True, default=str)

    def _ensure_engine(self, cfg: Mapping[str, Any]) -> FaceEngine:
        """Return the shared engine, (re)building it only when settings changed.

        Must be called with :attr:`_camera_lock` held — the engine is not
        thread-safe, and that lock is what serialises its use.

        :raises EngineError: the models cannot be loaded.
        """
        signature = self._engine_signature_for(cfg)
        if self._engine is not None and signature == self._engine_signature:
            return self._engine

        started = time.monotonic()
        engine = FaceEngine(dict(cfg))  # raises EngineError
        self._engine = engine
        self._engine_signature = signature
        self._engine_error = ""
        log.info(
            "recognition models loaded from %s in %.0f ms (threshold=%.3f, detect_score=%.2f)",
            engine.model_dir, (time.monotonic() - started) * 1000.0,
            engine.threshold, engine.detect_score,
        )
        return engine

    def _preload_models(self) -> None:
        """Build the engine at startup so the first auth is not the slow one.

        A failure here is logged but not fatal: ``cameras``, ``list`` and the
        config ops still work, which is exactly what an administrator needs in
        order to diagnose a missing model directory.  Authentication returns
        ``camera_error`` until the models appear, and every attempt retries the
        load, so fixing the install needs no restart.
        """
        with self._camera_lock:
            try:
                self._ensure_engine(self._cfg)
            except EngineError as exc:
                self._engine_error = str(exc)
                log.error("recognition models unavailable: %s", exc)

    def _preload_master_key(self) -> None:
        """Warm the AES key cache when one already exists.

        Unsealing from the TPM costs the better part of a second; doing it once
        at startup keeps that off the login path.  A key is never *created*
        here — a machine where nobody has enrolled should not accumulate
        cryptographic state — so this is a no-op on a fresh install.
        """
        if not (self._keys.key_path.exists() or self._keys.sealed_priv_path.exists()):
            log.info("no master key yet; it will be created on first enrolment")
            return
        try:
            self._keys.load_key()
        except (StoreError, OSError) as exc:
            # Not fatal: `iris clear` + re-enrolment is the fix, and the daemon
            # must stay up to serve that.
            log.error("master key unavailable: %s", exc)
            return
        log.info("master key ready (backend=%s)", self._keys.backend or "unknown")

    # ------------------------------------------------------------------ #
    # Operations: trivial ones
    # ------------------------------------------------------------------ #

    def _op_ping(self, request: dict[str, Any], conn: socket.socket) -> dict[str, Any]:
        """Liveness probe for clients and for ``iris doctor``."""
        return {"ok": True, "version": __version__}

    def _op_cameras(self, request: dict[str, Any], conn: socket.socket) -> dict[str, Any]:
        """Enumerate V4L2 nodes so the settings panel can offer a device list."""
        cameras = list_cameras()
        log.debug("cameras: %d node(s)", len(cameras))
        return {"ok": True, "cameras": cameras}

    def _op_list(self, request: dict[str, Any], conn: socket.socket) -> dict[str, Any]:
        """List a user's enrolled faces (metadata only — never embeddings)."""
        user = self._require_user(request)
        try:
            faces = self._store.list_faces(user)
        except StoreError as exc:
            log.error("list: cannot read templates for %s: %s", user, exc)
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "faces": faces}

    def _op_remove(self, request: dict[str, Any], conn: socket.socket) -> dict[str, Any]:
        """Delete one named face."""
        user = self._require_user(request)
        name = self._require_name(request)
        try:
            removed = self._store.remove(user, name)
        except (StoreError, OSError, PermissionError) as exc:
            log.error("remove: %s/%s failed: %s", user, name, exc)
            return {"ok": False, "error": str(exc)}
        if not removed:
            return {"ok": False, "error": f"no face named {name!r} for user {user}"}
        # A removal changes what auth can match; drop any lockout so the user
        # is not additionally punished by stale failure history.
        self._failures.reset(user)
        return {"ok": True, "removed": name}

    def _op_clear(self, request: dict[str, Any], conn: socket.socket) -> dict[str, Any]:
        """Delete every face template for a user."""
        user = self._require_user(request)
        try:
            self._store.clear(user)
        except (StoreError, OSError, PermissionError) as exc:
            log.error("clear: %s failed: %s", user, exc)
            return {"ok": False, "error": str(exc)}
        self._failures.reset(user)
        return {"ok": True}

    def _op_config_get(self, request: dict[str, Any], conn: socket.socket) -> dict[str, Any]:
        """Return the effective configuration (defaults merged with the file)."""
        return {"ok": True, "config": self.current_config()}

    def _op_config_set(self, request: dict[str, Any], conn: socket.socket) -> dict[str, Any]:
        """Merge *config* into the current settings and persist them.

        The incoming table is merged over what is on disk rather than replacing
        it, so a client that sends only ``{"auth": {"enabled": false}}`` does
        not silently reset every other setting to its default.
        """
        incoming = request.get("config")
        if not isinstance(incoming, Mapping):
            raise RequestError("'config' must be an object")

        problems = self._validate_config(incoming)
        if problems:
            raise RequestError("; ".join(problems))

        merged = self.current_config()
        for section, values in incoming.items():
            target = merged.setdefault(section, {})
            if not isinstance(target, dict):
                raise RequestError(f"[{section}] is not a table")
            target.update(values)

        try:
            config_module.save_config(merged, self.config_path)
        except (OSError, TypeError) as exc:
            log.error("config_set: cannot write %s: %s", self.config_path, exc)
            return {"ok": False, "error": f"cannot write {self.config_path}: {exc}"}

        # Re-read what actually landed on disk: save_config clamps out-of-range
        # values, so the client should be told the effective settings, not the
        # ones it asked for. The engine picks the change up on the next capture,
        # inside the camera lock.
        with self._cfg_lock:
            self._cfg = config_module.load_config(self.config_path)
            self._cfg_stamp = self._config_stamp()
            effective = copy.deepcopy(self._cfg)

        log.info(
            "configuration updated (%s)",
            ", ".join(sorted(f"[{s}]" for s in incoming)) or "no sections",
        )
        return {"ok": True, "config": effective}

    @staticmethod
    def _validate_config(incoming: Mapping[str, Any]) -> list[str]:
        """Type-check an incoming config table against :data:`DEFAULTS`.

        :mod:`iris.config` would silently fall back to the default for a
        mistyped value; over the wire that would look like a successful write
        that did nothing, so reject it here instead.
        """
        problems: list[str] = []
        for section, values in incoming.items():
            if not isinstance(section, str):
                problems.append(f"section key {section!r} is not a string")
                continue
            if not isinstance(values, Mapping):
                problems.append(f"[{section}] must be a table")
                continue
            known = config_module.DEFAULTS.get(section)
            for key, value in values.items():
                if not isinstance(key, str):
                    problems.append(f"[{section}] has a non-string key {key!r}")
                    continue
                if known is not None and key in known:
                    if not _same_kind(known[key], value):
                        expected = type(known[key]).__name__
                        problems.append(
                            f"{section}.{key}: expected {expected}, "
                            f"got {type(value).__name__}"
                        )
                elif not isinstance(value, (bool, int, float, str)):
                    problems.append(
                        f"{section}.{key}: unsupported value type "
                        f"{type(value).__name__}"
                    )

        device = incoming.get("camera", {})
        device = device.get("device") if isinstance(device, Mapping) else None
        if isinstance(device, str) and device:
            problems.extend(_validate_device_choice(device))
        return problems

    # ------------------------------------------------------------------ #
    # Operation: auth
    # ------------------------------------------------------------------ #

    def _op_auth(self, request: dict[str, Any], conn: socket.socket) -> dict[str, Any]:
        """Authenticate *user* against their enrolled templates."""
        started = time.monotonic()
        user = self._require_user(request)
        cfg = self.current_config()
        auth_cfg = cfg["auth"]

        # Guard first, and cheaply: a disabled system must answer instantly
        # rather than powering up the emitter for eight seconds.
        if not bool(auth_cfg.get("enabled", True)):
            log.info("auth user=%s reason=disabled", user)
            return protocol.auth_response(False, 0.0, protocol.REASON_DISABLED)

        max_failures = int(auth_cfg.get("max_failures", 5))
        lockout_seconds = float(auth_cfg.get("lockout_seconds", 60))
        if self._failures.is_locked(user, max_failures, lockout_seconds):
            remaining = self._failures.seconds_until_unlock(
                user, max_failures, lockout_seconds
            )
            log.warning(
                "auth user=%s reason=lockout retry_after=%.1fs failures=%d/%d",
                user, remaining,
                self._failures.failure_count(user, lockout_seconds), max_failures,
            )
            response = protocol.auth_response(False, 0.0, protocol.REASON_LOCKOUT)
            response["retry_after"] = round(remaining, 1)
            return response

        try:
            templates = self._store.embeddings_for(user)
        except StoreError as exc:
            # A tamper-detected or undecryptable store is NOT "no templates":
            # reporting it as such would hide an attack. There is no dedicated
            # reason code for it in the closed vocabulary, so use not_enrolled
            # (the remedy — re-enrol — is the same) and shout in the log.
            log.error("auth user=%s: template store unusable: %s", user, exc)
            response = protocol.auth_response(False, 0.0, protocol.REASON_NOT_ENROLLED)
            response["error"] = str(exc)
            return response
        except (OSError, ValueError) as exc:
            log.error("auth user=%s: cannot read templates: %s", user, exc)
            response = protocol.auth_response(False, 0.0, protocol.REASON_NOT_ENROLLED)
            response["error"] = str(exc)
            return response

        if not templates:
            log.info("auth user=%s reason=not_enrolled", user)
            return protocol.auth_response(False, 0.0, protocol.REASON_NOT_ENROLLED)

        timeout = self._auth_timeout(request, cfg)
        outcome = self._authenticate(user, cfg, templates, started + timeout)

        elapsed = time.monotonic() - started
        if outcome.ok:
            self._failures.reset(user)
            log.info(
                "auth user=%s result=ok face=%s confidence=%.3f frames=%d elapsed=%.2fs",
                user, outcome.face, outcome.confidence, outcome.frames, elapsed,
            )
        else:
            if outcome.reason in _COUNTED_FAILURE_REASONS:
                self._failures.record_failure(user)
                remaining = max_failures - self._failures.failure_count(user, lockout_seconds)
                if remaining <= 0:
                    log.warning("user=%s is now locked out for %.0fs", user, lockout_seconds)
            log.info(
                "auth user=%s result=fail reason=%s best=%.3f frames=%d faces=%d "
                "embedded=%d elapsed=%.2fs%s",
                user, outcome.reason, outcome.confidence, outcome.frames,
                outcome.faces_seen, outcome.embedded, elapsed,
                f" detail={outcome.detail}" if outcome.detail else "",
            )

        return protocol.auth_response(
            outcome.ok, outcome.confidence, outcome.reason, outcome.face
        )

    @staticmethod
    def _auth_timeout(request: Mapping[str, Any], cfg: Mapping[str, Any]) -> float:
        """Resolve the attempt's wall-clock budget.

        A client may shorten (or lengthen) the configured timeout, but never
        past the bounds ``iris.config`` enforces: SAFETY rule 3 says PAM must
        never hold a login shell open indefinitely, and a client-supplied
        ``timeout: 1e9`` must not be able to do it either.
        """
        requested = request.get("timeout")
        default = float(cfg["auth"].get("timeout", 8.0))
        if isinstance(requested, bool) or not isinstance(requested, (int, float)):
            return default
        value = float(requested)
        if value != value or value in (float("inf"), float("-inf")):  # NaN/inf
            return default
        return min(max(value, 0.5), 60.0)

    def _authenticate(
        self,
        user: str,
        cfg: Mapping[str, Any],
        templates: Sequence[tuple[str, np.ndarray]],
        deadline: float,
    ) -> AuthOutcome:
        """Run the capture loop under the camera lock."""
        # Queue rather than fight: /dev/video2 is single-open and the emitter
        # strobes, so two concurrent captures produce read errors for both.
        wait_budget = max(0.0, deadline - time.monotonic())
        acquired = self._camera_lock.acquire(timeout=wait_budget)
        if not acquired:
            log.warning("auth user=%s: camera busy for the whole %.1fs budget",
                        user, wait_budget)
            return AuthOutcome(False, 0.0, protocol.REASON_TIMEOUT, detail="camera busy")

        try:
            try:
                engine = self._ensure_engine(cfg)
            except EngineError as exc:
                self._engine_error = str(exc)
                log.error("auth user=%s: %s", user, exc)
                return AuthOutcome(
                    False, 0.0, protocol.REASON_CAMERA_ERROR, detail="models unavailable"
                )

            if deadline - time.monotonic() < MIN_CAPTURE_SECONDS:
                return AuthOutcome(
                    False, 0.0, protocol.REASON_TIMEOUT, detail="no budget left for capture"
                )

            try:
                # Opening inside the `with` spends the sensor's ~0.5 s start-up
                # cost up front; frames() then gets the honest remainder.
                with Camera.from_config(dict(cfg)) as camera:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return AuthOutcome(
                            False, 0.0, protocol.REASON_TIMEOUT, detail="camera open too slow"
                        )
                    return self._auth_frames(engine, camera, cfg, templates, remaining)
            except CameraError as exc:
                log.error("auth user=%s: camera error: %s", user, exc)
                return AuthOutcome(
                    False, 0.0, protocol.REASON_CAMERA_ERROR, detail=str(exc)
                )
        finally:
            self._camera_lock.release()

    def _auth_frames(
        self,
        engine: FaceEngine,
        camera: Camera,
        cfg: Mapping[str, Any],
        templates: Sequence[tuple[str, np.ndarray]],
        budget: float,
    ) -> AuthOutcome:
        """Iterate illuminated frames until enough of them agree.

        The decision rule is ``required_matches`` *consecutive* frames that all
        clear the recognition threshold and the liveness checks.  This is the
        central defence against a single lucky frame: motion blur, a passing
        reflection or an unlucky embedding can produce one frame above
        threshold, but not three in a row.  At 15 fps with half the frames dark,
        three consecutive matches costs roughly 0.4 s.
        """
        recognition = cfg["recognition"]
        threshold = float(recognition.get("threshold", 0.363))
        required = max(1, int(recognition.get("required_matches", 3)))
        max_frames = max(1, int(recognition.get("max_frames", 120)))

        liveness = LivenessChecker(dict(cfg))
        counters = _CaptureCounters()

        streak = 0
        streak_name: str | None = None
        best_score = -1.0
        best_name: str | None = None

        for gray in camera.frames(budget):
            if self._stop.is_set():
                return AuthOutcome(
                    False, max(best_score, 0.0), protocol.REASON_TIMEOUT,
                    frames=counters.lit_frames, faces_seen=counters.face_frames,
                    embedded=counters.embedded_frames, detail="daemon shutting down",
                )

            counters.lit_frames += 1
            if counters.lit_frames > max_frames:
                break

            faces = engine.detect(gray)
            row = FaceEngine.select_face(faces)
            if row is None:
                # A frame with nobody in it breaks both runs. Feeding it to the
                # liveness checker (rather than calling reset()) keeps its
                # duplicate-frame history intact while clearing its streak.
                streak = 0
                if liveness.enabled:
                    liveness.check(gray, None)
                continue

            counters.face_frames += 1
            live_ok, live_reason = liveness.check(gray, row)
            if not live_ok:
                counters.live_reasons[live_reason] += 1
                streak = 0
                continue

            try:
                embedding = engine.embed(gray, row)
            except (EngineError, ValueError) as exc:
                # One bad crop must not end the attempt; the next frame is 133 ms
                # away and usually fine.
                log.debug("embedding failed on one frame: %s", exc)
                streak = 0
                continue

            counters.embedded_frames += 1
            name, score = FaceEngine.match_best(embedding, templates)
            if score > best_score:
                best_score, best_name = score, name

            # is_confident() adds the liveness module's own consecutive-frame
            # requirement on top of ours, so the first live frame can never be
            # the one that completes a match streak.
            if score >= threshold and liveness.is_confident():
                streak += 1
                streak_name = name or streak_name
                if streak >= required:
                    return AuthOutcome(
                        True, float(score), protocol.REASON_MATCH, streak_name,
                        frames=counters.lit_frames, faces_seen=counters.face_frames,
                        embedded=counters.embedded_frames,
                    )
            else:
                streak = 0

        reason = _classify_failure(counters, progressed=streak > 0)
        detail = ""
        if counters.live_reasons:
            top_reason, count = counters.live_reasons.most_common(1)[0]
            detail = f"liveness={top_reason}x{count}"
        if best_name is not None and reason == protocol.REASON_NO_MATCH:
            detail = (detail + " " if detail else "") + f"closest={best_name}"
        return AuthOutcome(
            False, max(best_score, 0.0), reason,
            frames=counters.lit_frames, faces_seen=counters.face_frames,
            embedded=counters.embedded_frames, detail=detail,
        )

    # ------------------------------------------------------------------ #
    # Operation: enroll
    # ------------------------------------------------------------------ #

    def _op_enroll(self, request: dict[str, Any], conn: socket.socket) -> dict[str, Any]:
        """Guided multi-angle capture, streaming progress as it goes.

        Emits ``{"progress": 0..1, "hint": str}`` lines while collecting, then
        exactly one final ``{"ok": …}`` line, as the protocol requires.
        """
        user = self._require_user(request)
        name = self._require_name(request)
        cfg = self.current_config()
        started = time.monotonic()

        def emit(progress: float, hint: str, **extra: Any) -> None:
            """Send one progress line; a dead client aborts the enrolment.

            ``total`` rides on every line because the GTK front end renders
            "Sample 4 of 15" from the ``samples``/``total`` pair and has no
            other way to learn the target — the pose schedule lives here.
            """
            protocol.write_message(
                conn,
                {
                    "progress": round(min(max(progress, 0.0), 1.0), 3),
                    "hint": hint,
                    "total": ENROLL_TARGET_SAMPLES,
                    **extra,
                },
            )

        emit(0.0, "Preparing the camera…", stage="starting")

        acquired = self._camera_lock.acquire(timeout=ENROLL_LOCK_TIMEOUT)
        if not acquired:
            log.warning("enroll user=%s: camera busy", user)
            return {
                "ok": False,
                "reason": protocol.REASON_CAMERA_ERROR,
                "error": "the camera is in use; try again in a moment",
            }

        try:
            try:
                engine = self._ensure_engine(cfg)
            except EngineError as exc:
                self._engine_error = str(exc)
                log.error("enroll user=%s: %s", user, exc)
                return {
                    "ok": False,
                    "reason": protocol.REASON_CAMERA_ERROR,
                    "error": str(exc),
                }

            try:
                with Camera.from_config(dict(cfg)) as camera:
                    samples, per_pose, counters = self._enroll_capture(
                        engine, camera, cfg, emit
                    )
            except CameraError as exc:
                log.error("enroll user=%s: camera error: %s", user, exc)
                return {
                    "ok": False,
                    "reason": protocol.REASON_CAMERA_ERROR,
                    "error": str(exc),
                }
        finally:
            self._camera_lock.release()

        centre_pose = ENROLL_POSES[0][0]
        if len(samples) < ENROLL_MIN_SAMPLES or per_pose.get(centre_pose, 0) == 0:
            reason = _classify_failure(counters, progressed=bool(samples))
            log.warning(
                "enroll user=%s name=%s failed: reason=%s samples=%d frames=%d faces=%d",
                user, name, reason, len(samples), counters.lit_frames,
                counters.face_frames,
            )
            return {
                "ok": False,
                "reason": reason,
                "error": (
                    f"captured only {len(samples)} usable sample(s); "
                    f"at least {ENROLL_MIN_SAMPLES} including a straight-on view "
                    "are needed"
                ),
                "samples": len(samples),
            }

        try:
            self._store.add(user, name, samples)
        except (StoreError, ValueError, OSError, PermissionError) as exc:
            log.error("enroll user=%s name=%s: cannot store templates: %s", user, name, exc)
            return {"ok": False, "error": str(exc)}

        # A fresh enrolment invalidates whatever made the old one fail.
        self._failures.reset(user)
        log.info(
            "enroll user=%s name=%s stored samples=%d poses=%s elapsed=%.1fs",
            user, name, len(samples),
            ",".join(f"{pose}:{count}" for pose, count in per_pose.items()),
            time.monotonic() - started,
        )
        return {
            "ok": True,
            "name": name,
            "samples": len(samples),
            "poses": dict(per_pose),
            "reason": protocol.REASON_MATCH,
        }

    def _enroll_capture(
        self,
        engine: FaceEngine,
        camera: Camera,
        cfg: Mapping[str, Any],
        emit: Callable[..., None],
    ) -> tuple[list[np.ndarray], dict[str, int], _CaptureCounters]:
        """Walk the pose sequence, collecting one embedding set.

        A single :meth:`Camera.frames` iterator spans every pose: reopening the
        device per pose would cost half a second each time and re-trigger the
        sensor's auto-exposure settling.
        """
        liveness = LivenessChecker(dict(cfg))
        counters = _CaptureCounters()
        samples: list[np.ndarray] = []
        per_pose: dict[str, int] = {pose: 0 for pose, _hint in ENROLL_POSES}
        reference: np.ndarray | None = None

        index = 0
        now = time.monotonic()
        pose_deadline = now + ENROLL_POSE_TIMEOUT
        settle_until = now + ENROLL_POSE_SETTLE
        last_hint_at = 0.0
        last_sample_at = 0.0

        def progress() -> float:
            # Cap below 1.0: the client should see 1.0 only alongside the final
            # result line, never while frames are still being collected.
            return min(len(samples) / ENROLL_TARGET_SAMPLES, 0.99)

        def hint(text: str, *, force: bool = False, **extra: Any) -> None:
            nonlocal last_hint_at
            moment = time.monotonic()
            if not force and moment - last_hint_at < ENROLL_HINT_INTERVAL:
                return
            last_hint_at = moment
            emit(progress(), text, **extra)

        pose, pose_hint = ENROLL_POSES[index]
        hint(pose_hint, force=True, stage="pose", pose=pose, samples=len(samples))

        for gray in camera.frames(ENROLL_TOTAL_TIMEOUT):
            if self._stop.is_set():
                log.warning("enrolment interrupted by shutdown")
                break

            counters.lit_frames += 1
            now = time.monotonic()

            # Advance when this pose is done or has run out of time. A pose that
            # times out is not fatal; the totals are checked at the end.
            if per_pose[pose] >= ENROLL_SAMPLES_PER_POSE or now >= pose_deadline:
                index += 1
                if index >= len(ENROLL_POSES):
                    break
                pose, pose_hint = ENROLL_POSES[index]
                pose_deadline = now + ENROLL_POSE_TIMEOUT
                settle_until = now + ENROLL_POSE_SETTLE
                # Reset the liveness run: the user is about to move, and frames
                # captured mid-turn should not count towards its confidence.
                liveness.reset()
                hint(pose_hint, force=True, stage="pose", pose=pose, samples=len(samples))
                continue

            if now < settle_until:
                continue  # give the user time to actually adopt the pose

            faces = engine.detect(gray)
            row = FaceEngine.select_face(faces)
            if row is None:
                if liveness.enabled:
                    liveness.check(gray, None)
                hint(_LIVENESS_HINTS["no_face"], stage="waiting", pose=pose)
                continue

            counters.face_frames += 1
            live_ok, live_reason = liveness.check(gray, row)
            if not live_ok:
                counters.live_reasons[live_reason] += 1
                hint(
                    _LIVENESS_HINTS.get(live_reason, "Adjust your position"),
                    stage="waiting", pose=pose,
                )
                continue

            if not liveness.is_confident():
                # One good frame is not enough to trust the region; wait for the
                # checker's own run to build up before spending a sample on it.
                continue

            if now - last_sample_at < ENROLL_SAMPLE_INTERVAL:
                # Consecutive frames 133 ms apart are nearly identical; spacing
                # samples out makes the stored template describe a pose rather
                # than an instant.
                continue

            try:
                embedding = engine.embed(gray, row)
            except (EngineError, ValueError) as exc:
                log.debug("enrolment frame rejected: %s", exc)
                continue

            counters.embedded_frames += 1

            if reference is None:
                reference = embedding
            else:
                similarity = FaceEngine.compare(reference, embedding)
                if similarity < ENROLL_IDENTITY_FLOOR:
                    # Someone else stepped in front of the camera. Silently
                    # folding their face into this user's template would be a
                    # permanent, invisible backdoor.
                    hint(
                        "Keep the same person in frame",
                        stage="waiting", pose=pose,
                    )
                    continue

            samples.append(embedding)
            per_pose[pose] += 1
            last_sample_at = now
            hint(
                f"{pose_hint} — captured {len(samples)} of {ENROLL_TARGET_SAMPLES}",
                force=True, stage="captured", pose=pose, samples=len(samples),
            )

        return samples, per_pose, counters


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

#: Failures that count towards the lockout.  Only outcomes where a face was
#: actually presented and rejected: rate limiting exists to stop someone
#: grinding photos or lookalikes against the recogniser.  An empty room
#: producing ``no_face``, or a camera that will not open, is not an attempt and
#: must not lock the legitimate user out when they sit back down.
_COUNTED_FAILURE_REASONS: Final[frozenset[str]] = frozenset({
    protocol.REASON_NO_MATCH,
    protocol.REASON_SPOOF_SUSPECTED,
})


def _classify_failure(counters: _CaptureCounters, *, progressed: bool) -> str:
    """Choose the reason code that best explains an unsuccessful capture."""
    if counters.lit_frames == 0:
        # Camera.frames() yields only illuminated frames; none at all means the
        # emitter never fired or min_frame_brightness is set too high.
        return protocol.REASON_CAMERA_ERROR
    if progressed:
        # We were part-way to a decision and simply ran out of budget.
        return protocol.REASON_TIMEOUT
    if counters.embedded_frames > 0:
        return protocol.REASON_NO_MATCH
    if counters.face_frames > 0 and counters.live_reasons:
        dominant, _count = counters.live_reasons.most_common(1)[0]
        if dominant in _GEOMETRIC_LIVENESS_REASONS:
            # "Too small" / "out of frame" is a positioning problem, not an
            # attack; saying spoof_suspected would be both wrong and unhelpful.
            return protocol.REASON_NO_FACE
        return protocol.REASON_SPOOF_SUSPECTED
    return protocol.REASON_NO_FACE


def _same_kind(default: Any, value: Any) -> bool:
    """Type-compatibility check mirroring ``iris.config._coerce``.

    ``bool`` is tested first everywhere because it is a subclass of ``int``:
    without that, ``width = true`` would look like a valid integer.
    """
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(default, float):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(default, str):
        return isinstance(value, str)
    return False


def _validate_device_choice(device: str) -> list[str]:
    """Refuse a camera device that the kernel says is a metadata node.

    ``/dev/video1`` and ``/dev/video3`` on this hardware deliver UVC payload
    headers, not images.  Accepting one would produce an installation that
    times out on every login with no obvious cause, so the mistake is caught at
    the point it is made.
    """
    try:
        cameras = list_cameras()
    except OSError as exc:  # pragma: no cover - enumeration is best-effort
        log.debug("cannot enumerate cameras to validate %s: %s", device, exc)
        return []

    for camera in cameras:
        if camera["path"] != device:
            continue
        if camera["is_metadata"]:
            return [
                f"camera.device: {device} is a V4L2 metadata node, not a capture "
                "device; it can never deliver frames"
            ]
        return []

    log.warning("camera.device %s is not currently present", device)
    return []


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    """Command-line interface for ``irisd``."""
    parser = argparse.ArgumentParser(
        prog="irisd",
        description="Iris face-authentication daemon (runs as root, serves a UNIX socket).",
    )
    parser.add_argument(
        "--socket", default=SOCKET_PATH, metavar="PATH",
        help=f"UNIX socket to listen on (default: {SOCKET_PATH})",
    )
    parser.add_argument(
        "--config", default=config_module.CONFIG_PATH, metavar="PATH",
        help=f"configuration file (default: {config_module.CONFIG_PATH})",
    )
    parser.add_argument(
        "--state-dir", default=DEFAULT_ROOT, metavar="DIR",
        help=f"template storage directory (default: {DEFAULT_ROOT})",
    )
    parser.add_argument(
        "--log-level", default=os.environ.get("IRIS_LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="stderr log verbosity (default: INFO, or $IRIS_LOG_LEVEL)",
    )
    parser.add_argument(
        "--no-tpm", action="store_true",
        help="do not seal the master key to the TPM (testing and recovery)",
    )
    parser.add_argument("--version", action="version", version=f"irisd {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the daemon.  Returns the process exit status."""
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level)

    daemon = IrisDaemon(
        socket_path=args.socket,
        config_path=args.config,
        state_dir=args.state_dir,
        use_tpm=not args.no_tpm,
    )
    try:
        return daemon.run()
    except DaemonStartupError as exc:
        log.critical("cannot start: %s", exc)
        return 1
    except KeyboardInterrupt:  # pragma: no cover - handled via SIGINT normally
        return 0
    except Exception:  # noqa: BLE001 - report, do not dump a bare traceback
        log.critical("fatal error", exc_info=True)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
