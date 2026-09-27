"""``iris enroll``, ``list``, ``remove`` and ``clear``."""

from __future__ import annotations

import argparse
import sys
from typing import Any, Mapping

from iris import protocol

from iris.cli.common import (
    ProgressRenderer,
    _as_command_error,
    _failure_text,
    _json_mode,
    _open_store,
    _resolve_name,
    _store_error,
    daemon_request,
    emit_json,
    list_faces,
    render_table,
    require_root,
    resolve_user,
    validate_face_name,
)
from iris.cli.constants import (
    CONTROL_TIMEOUT,
    EXIT_FAILURE,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_PERMISSION,
    EXIT_UNAVAILABLE,
    PROG,
    STORE_ROOT,
)
from iris.cli.output import CommandError, _is_tty, console


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
