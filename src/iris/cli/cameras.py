"""``iris cameras``: V4L2 enumeration."""

from __future__ import annotations

import argparse
import json
from typing import Any

from iris import protocol

from iris.cli.common import _failure_text, daemon_request, render_table
from iris.cli.constants import CONTROL_TIMEOUT, EXIT_OK, PROG
from iris.cli.output import CommandError, console
from iris.cli.settings import is_auto_device, load_effective_config, resolve_configured_device


def cmd_cameras(args: argparse.Namespace) -> int:
    cameras, source = _collect_cameras(args.socket)
    cfg, _cfg_source = load_effective_config(args.socket)
    configured = str(cfg["camera"]["device"])
    active = resolve_configured_device(configured) if cameras else None

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
        marker = "*" if path == active else " "
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
    if is_auto_device(configured):
        chosen = active or "none found"
        console.note(f"* = the camera Iris uses (camera.device = auto → {chosen})")
        console.note(f"to pin a different one:  sudo {PROG} config set camera.device /dev/videoN")
    else:
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
    elif not is_auto_device(configured) and not any(c.get("path") == configured for c in cameras):
        console.warn(
            f"the configured device {configured} is not present; "
            f"use  sudo {PROG} config set camera.device auto  to pick the infrared camera"
        )
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
