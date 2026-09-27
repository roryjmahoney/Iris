"""``iris test``: a real authentication dry-run."""

from __future__ import annotations

import argparse
import json
import math
import time
from typing import Any, Mapping

from iris import protocol

from iris.cli.common import _failure_text, daemon_request, resolve_user
from iris.cli.constants import EXIT_AUTH_FAILED, EXIT_OK, PROG
from iris.cli.output import console
from iris.cli.settings import load_effective_config


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
