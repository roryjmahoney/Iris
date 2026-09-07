"""Client/daemon IPC for Iris.

Wire format
-----------
A ``SOCK_STREAM`` UNIX socket at :data:`SOCKET_PATH` (``root:root 0600``).
Each message is one UTF-8 JSON **object** followed by ``\\n``.  A message,
including its terminating newline, may not exceed :data:`MAX_MESSAGE_BYTES`
(64 KiB).

The cap is a security control, not a tidiness rule.  ``irisd`` runs as root and
accepts connections from unprivileged clients; without a cap a client could
stream bytes forever and exhaust the daemon's memory.  :func:`read_message`
enforces it while reading, aborting as soon as the limit is crossed rather than
after buffering an unbounded line.

Every socket operation in this module is bounded by a timeout.  A blocking read
with no deadline inside the PAM path would hang a login shell, which SAFETY
rule 3 forbids outright, so the helpers refuse to operate on a socket whose
timeout is ``None``.

Both sides use the same two primitives — :func:`read_message` and
:func:`write_message` — so framing bugs cannot differ between client and
daemon.  They are stateless: :func:`read_message` uses ``MSG_PEEK`` to locate
the newline and then consumes exactly up to it, so it never reads past a
message boundary and no per-connection buffer has to be threaded around.
"""

from __future__ import annotations

import json
import socket
import time
from typing import Any, Final, Iterator, Mapping

SOCKET_PATH: Final[str] = "/run/irisd/socket"

#: Maximum bytes in one framed message, terminating newline included.
MAX_MESSAGE_BYTES: Final[int] = 64 * 1024

#: Fallback timeout, in seconds, for callers that do not supply one.
DEFAULT_TIMEOUT: Final[float] = 10.0

#: Bytes requested per ``MSG_PEEK``; sized so a typical message needs one pass.
_READ_CHUNK: Final[int] = 8192


# --------------------------------------------------------------------------
# vocabulary
# --------------------------------------------------------------------------

OP_PING: Final[str] = "ping"
OP_AUTH: Final[str] = "auth"
OP_LIST: Final[str] = "list"
OP_CAMERAS: Final[str] = "cameras"
OP_ENROLL: Final[str] = "enroll"
OP_REMOVE: Final[str] = "remove"
OP_CLEAR: Final[str] = "clear"
OP_CONFIG_GET: Final[str] = "config_get"
OP_CONFIG_SET: Final[str] = "config_set"

#: Every operation the daemon accepts.  Anything else must be refused.
OPS: Final[frozenset[str]] = frozenset({
    OP_PING, OP_AUTH, OP_LIST, OP_CAMERAS, OP_ENROLL,
    OP_REMOVE, OP_CLEAR, OP_CONFIG_GET, OP_CONFIG_SET,
})

# Reason codes.  This vocabulary is closed: PAM, the CLI and the GTK front end
# all switch on these strings to decide what to tell the user, so a daemon that
# invents a new one would surface as a blank error message.
REASON_MATCH: Final[str] = "match"
REASON_NO_MATCH: Final[str] = "no_match"
REASON_NO_FACE: Final[str] = "no_face"
REASON_TIMEOUT: Final[str] = "timeout"
REASON_CAMERA_ERROR: Final[str] = "camera_error"
REASON_NOT_ENROLLED: Final[str] = "not_enrolled"
REASON_DISABLED: Final[str] = "disabled"
REASON_LOCKOUT: Final[str] = "lockout"
REASON_SPOOF_SUSPECTED: Final[str] = "spoof_suspected"

REASONS: Final[frozenset[str]] = frozenset({
    REASON_MATCH, REASON_NO_MATCH, REASON_NO_FACE, REASON_TIMEOUT,
    REASON_CAMERA_ERROR, REASON_NOT_ENROLLED, REASON_DISABLED,
    REASON_LOCKOUT, REASON_SPOOF_SUSPECTED,
})

#: Human-readable text for each reason code, for CLI and GUI use.
REASON_TEXT: Final[dict[str, str]] = {
    REASON_MATCH: "Face recognised.",
    REASON_NO_MATCH: "Face did not match any enrolled model.",
    REASON_NO_FACE: "No face was visible to the infrared camera.",
    REASON_TIMEOUT: "Timed out before a face could be recognised.",
    REASON_CAMERA_ERROR: "The infrared camera could not be used.",
    REASON_NOT_ENROLLED: "No face is enrolled for this user.",
    REASON_DISABLED: "Face authentication is disabled.",
    REASON_LOCKOUT: "Too many failed attempts; temporarily locked out.",
    REASON_SPOOF_SUSPECTED: "The image did not look like a live face.",
}

#: Key that marks a non-final progress line (used by the ``enroll`` op).
PROGRESS_KEY: Final[str] = "progress"


# --------------------------------------------------------------------------
# exceptions
# --------------------------------------------------------------------------

class ProtocolError(Exception):
    """Base class for every failure raised by this module."""


class TransportError(ProtocolError):
    """The socket could not be used (connect, read or write failed)."""


class DaemonUnavailable(TransportError):
    """``irisd`` is not listening on the socket."""


class Disconnected(TransportError):
    """The peer closed the connection in the middle of a message."""


class RequestTimeout(ProtocolError):
    """The deadline expired before the exchange completed."""


class MessageTooLarge(ProtocolError):
    """A message exceeded :data:`MAX_MESSAGE_BYTES`."""


class MalformedMessage(ProtocolError):
    """A message was not a UTF-8 JSON object."""


# --------------------------------------------------------------------------
# encoding
# --------------------------------------------------------------------------

def encode_message(obj: Mapping[str, Any]) -> bytes:
    """Serialise *obj* to a single framed line.

    :raises MalformedMessage: *obj* is not a JSON-serialisable object.
    :raises MessageTooLarge: the encoded line exceeds the cap.
    """
    if not isinstance(obj, Mapping):
        raise MalformedMessage(f"message must be an object, got {type(obj).__name__}")
    try:
        # allow_nan=False: NaN/Infinity are not valid JSON and would be
        # rejected by any conforming parser. A NaN confidence score is a bug
        # worth surfacing here rather than shipping down the wire.
        text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise MalformedMessage(f"cannot encode message: {exc}") from exc

    line = text.encode("utf-8") + b"\n"
    if len(line) > MAX_MESSAGE_BYTES:
        raise MessageTooLarge(
            f"message is {len(line)} bytes, limit is {MAX_MESSAGE_BYTES}"
        )
    return line


def decode_message(line: bytes | bytearray) -> dict[str, Any]:
    """Parse one framed line into a dict.

    :raises MalformedMessage: not valid UTF-8, not JSON, or not an object.
    """
    raw = bytes(line).rstrip(b"\n").rstrip(b"\r")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MalformedMessage(f"message is not valid UTF-8: {exc}") from exc
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MalformedMessage(f"message is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise MalformedMessage(
            f"message must be a JSON object, got {type(obj).__name__}"
        )
    return obj


# --------------------------------------------------------------------------
# framed socket I/O
# --------------------------------------------------------------------------

def write_message(
    sock: socket.socket,
    obj: Mapping[str, Any],
    *,
    deadline: float | None = None,
) -> None:
    """Send *obj* as one framed line on *sock*.

    :param deadline: optional :func:`time.monotonic` timestamp; when given, the
        socket timeout is re-armed from the time remaining so a slow peer
        cannot stretch the exchange past the caller's overall budget.
    :raises RequestTimeout: the deadline expired.
    :raises TransportError: the write failed.
    """
    line = encode_message(obj)
    _require_timeout(sock)
    if deadline is not None:
        _arm(sock, deadline)
    try:
        sock.sendall(line)
    except TimeoutError as exc:
        raise RequestTimeout("timed out sending request") from exc
    except OSError as exc:
        raise TransportError(f"failed to send request: {exc}") from exc


def read_message(
    sock: socket.socket,
    *,
    deadline: float | None = None,
) -> dict[str, Any] | None:
    """Read one framed line from *sock*.

    :param deadline: optional :func:`time.monotonic` timestamp; when given, the
        socket timeout is re-armed before every syscall so a peer that dribbles
        one byte at a time cannot extend the exchange indefinitely.
    :returns: the decoded object, or ``None`` if the peer closed cleanly at a
        message boundary (a normal end of conversation, not an error).
    :raises Disconnected: the peer closed mid-message.
    :raises RequestTimeout: the deadline expired.
    :raises MessageTooLarge: the line exceeded the cap.
    :raises MalformedMessage: the line was not a UTF-8 JSON object.
    """
    _require_timeout(sock)

    chunks: list[bytes] = []
    total = 0

    while True:
        if deadline is not None:
            _arm(sock, deadline)

        # Peek first so we can find the frame boundary without consuming past
        # it; the following recv() takes exactly the bytes we have accounted
        # for, leaving any bytes of the *next* message untouched in the kernel
        # buffer. That keeps this function stateless across calls.
        try:
            peeked = sock.recv(_READ_CHUNK, socket.MSG_PEEK)
        except TimeoutError as exc:
            raise RequestTimeout("timed out waiting for a response") from exc
        except OSError as exc:
            raise TransportError(f"failed to read from socket: {exc}") from exc

        if not peeked:
            if not chunks:
                return None  # clean shutdown between messages
            raise Disconnected(
                f"peer closed after {total} bytes without terminating the message"
            )

        newline = peeked.find(b"\n")
        take = newline + 1 if newline >= 0 else len(peeked)

        if total + take > MAX_MESSAGE_BYTES:
            # Refuse before consuming: the caller will close the connection,
            # and we never buffer more than one chunk beyond the limit.
            raise MessageTooLarge(
                f"message exceeds {MAX_MESSAGE_BYTES} bytes; closing connection"
            )

        chunks.append(_recv_exactly(sock, take, deadline))
        total += take

        if newline >= 0:
            return decode_message(b"".join(chunks))


def connect(
    path: str = SOCKET_PATH,
    timeout: float = DEFAULT_TIMEOUT,
    *,
    deadline: float | None = None,
) -> socket.socket:
    """Open a connection to the daemon socket.

    The returned socket already has a timeout set, so it is safe to hand to
    :func:`read_message` and :func:`write_message`.  The caller owns it and
    must close it.

    :raises DaemonUnavailable: nothing is listening on *path*.
    :raises RequestTimeout: the deadline expired during connect.
    :raises TransportError: any other socket failure.
    """
    if deadline is None:
        deadline = time.monotonic() + max(0.0, float(timeout))

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        _arm(sock, deadline)
        sock.connect(path)
    except TimeoutError as exc:
        sock.close()
        raise RequestTimeout(f"timed out connecting to {path}") from exc
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        sock.close()
        raise DaemonUnavailable(f"irisd is not listening on {path}: {exc}") from exc
    except PermissionError as exc:
        sock.close()
        raise DaemonUnavailable(
            f"not permitted to connect to {path}: {exc}"
        ) from exc
    except OSError as exc:
        sock.close()
        raise TransportError(f"cannot connect to {path}: {exc}") from exc
    return sock


# --------------------------------------------------------------------------
# request helpers
# --------------------------------------------------------------------------

def send_request(
    req: Mapping[str, Any],
    timeout: float,
    path: str = SOCKET_PATH,
) -> dict[str, Any]:
    """Send *req* and return the daemon's final response.

    *timeout* bounds the whole exchange — connect, send and receive — not each
    individual syscall, so a caller can rely on this returning (or raising)
    within roughly *timeout* seconds.

    Intermediate progress lines (see :func:`is_progress`) are skipped; use
    :func:`stream_request` if you want to observe them.

    :raises ProtocolError: any transport, framing or timeout failure.  Callers
        on the authentication path must catch this and fail closed.
    """
    for message in stream_request(req, timeout, path):
        if not is_progress(message):
            return message
    raise Disconnected("daemon closed the connection without a final response")


def stream_request(
    req: Mapping[str, Any],
    timeout: float,
    path: str = SOCKET_PATH,
) -> Iterator[dict[str, Any]]:
    """Send *req* and yield every response message until the final one.

    Used by the enrolment UI, where the daemon emits ``{"progress": …}`` lines
    before its final ``{"ok": …}``.  The connection is closed when the iterator
    is exhausted, closed or garbage-collected.

    :raises ProtocolError: any transport, framing or timeout failure.
    """
    if not isinstance(req, Mapping):
        raise TypeError(f"request must be a mapping, got {type(req).__name__}")

    deadline = time.monotonic() + max(0.0, float(timeout))
    sock = connect(path, deadline=deadline)
    try:
        write_message(sock, dict(req), deadline=deadline)
        while True:
            message = read_message(sock, deadline=deadline)
            if message is None:
                return
            yield message
            if not is_progress(message):
                return
    finally:
        # Runs on exhaustion, on an exception, and on GeneratorExit when a
        # caller abandons the iterator early (as send_request does).
        sock.close()


def is_progress(message: Mapping[str, Any]) -> bool:
    """True if *message* is a non-final progress update.

    A progress line carries ``progress`` and no ``ok``; the final message of
    every exchange carries ``ok``.
    """
    return PROGRESS_KEY in message and "ok" not in message


def auth_response(
    ok: bool,
    confidence: float,
    reason: str,
    face: str | None = None,
) -> dict[str, Any]:
    """Build a well-formed response for the ``auth`` op.

    Shared by the daemon and its tests so the shape stays identical across the
    many places an authentication attempt can end.

    :raises ValueError: *reason* is outside the closed vocabulary.
    """
    if reason not in REASONS:
        raise ValueError(f"unknown reason code {reason!r}; expected one of {sorted(REASONS)}")
    return {
        "ok": bool(ok),
        "confidence": float(confidence),
        "reason": reason,
        "face": face,
    }


def error_response(reason: str, **extra: Any) -> dict[str, Any]:
    """Build a generic ``{"ok": false, "reason": …}`` response."""
    if reason not in REASONS:
        raise ValueError(f"unknown reason code {reason!r}; expected one of {sorted(REASONS)}")
    return {"ok": False, "reason": reason, **extra}


def describe_reason(reason: str) -> str:
    """Human-readable text for a reason code, safe for unknown values."""
    return REASON_TEXT.get(reason, f"Face authentication failed ({reason}).")


# --------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------

def _require_timeout(sock: socket.socket) -> None:
    """Reject sockets that could block forever or would raise on a partial read.

    ``None`` means block indefinitely, which SAFETY rule 3 forbids anywhere
    near the login path.  ``0`` means non-blocking, which these helpers do not
    implement — a non-blocking socket raises ``BlockingIOError`` instead of
    waiting, and callers wanting that should drive the socket with
    :mod:`selectors` and use :func:`encode_message` / :func:`decode_message`
    directly.
    """
    current = sock.gettimeout()
    if current is None:
        raise ProtocolError(
            "socket has no timeout; refusing an operation that could block forever"
        )
    if current == 0:
        raise ProtocolError(
            "socket is non-blocking; use encode_message/decode_message instead"
        )


def _arm(sock: socket.socket, deadline: float) -> None:
    """Set the socket timeout to the time left before *deadline*."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RequestTimeout("deadline expired")
    sock.settimeout(remaining)


def _recv_exactly(sock: socket.socket, count: int, deadline: float | None) -> bytes:
    """Consume exactly *count* bytes that ``MSG_PEEK`` has already shown us.

    The bytes are known to be in the receive buffer, so this normally completes
    in one syscall; the loop only covers a short read.
    """
    parts: list[bytes] = []
    remaining = count
    while remaining > 0:
        if deadline is not None:
            _arm(sock, deadline)
        try:
            chunk = sock.recv(remaining)
        except TimeoutError as exc:
            raise RequestTimeout("timed out mid-message") from exc
        except OSError as exc:
            raise TransportError(f"failed to read from socket: {exc}") from exc
        if not chunk:
            raise Disconnected("peer closed while reading a message")
        parts.append(chunk)
        remaining -= len(chunk)
    return b"".join(parts)
