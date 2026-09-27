"""Helpers shared by the subcommands: users, daemon requests, JSON and progress."""

from __future__ import annotations

import argparse
import getpass
import json
import math
import os
import pwd
import stat
import sys
from typing import Any, Mapping, Sequence

from iris import protocol

from iris.cli.constants import (
    CONTROL_TIMEOUT,
    EXIT_FAILURE,
    EXIT_PERMISSION,
    EXIT_UNAVAILABLE,
    EXIT_USAGE,
    PROG,
    STORE_ROOT,
)
from iris.cli.output import CommandError, Console


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
