"""``iris status`` and ``iris keyring``."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

from iris import __version__

from iris.cli.common import daemon_ping, emit_json, require_root, resolve_user
from iris.cli.constants import (
    EXIT_OK,
    MASTER_KEY,
    PAM_DIR,
    PAM_MODULE_PATHS,
    PROG,
    SEALED_KEY,
    STORE_ROOT,
    TPM_RM_DEVICE,
)
from iris.cli.faces import _faces_or_empty
from iris.cli.output import CommandError, console
from iris.cli.settings import load_effective_config


def _require_keyring_hooks() -> None:
    """Only enable storage when the optional GDM integration is installed."""
    try:
        lines = (Path(PAM_DIR) / "gdm-password").read_text().splitlines()
        auth = lines.index("auth optional pam_iris_keyring.so")
        session = lines.index("session optional pam_iris_keyring.so")
        gkr = next(i for i, line in enumerate(lines)
                   if re.fullmatch(r"session\s+optional\s+pam_gnome_keyring\.so\s+auto_start", line.strip()))
        module = any(Path(path).with_name("pam_iris_keyring.so").is_file()
                     for path in PAM_MODULE_PATHS)
        if not module or not auth < session < gkr:
            raise ValueError("missing or misordered hooks")
    except (OSError, ValueError, StopIteration) as exc:
        raise CommandError(
            "optional keyring integration is not installed",
            hint="from the Iris checkout, run: sudo ./install.sh --keyring",
        ) from exc


def cmd_keyring(args: argparse.Namespace) -> int:
    require_root("keyring", f"iris keyring {args.keyring_action}",
                 subject="root-owned keyring credential state")
    user = resolve_user(args.user)
    from iris import keyring

    if args.keyring_action == "enable":
        _require_keyring_hooks()
    try:
        result = getattr(keyring, args.keyring_action)(user)
    except (keyring.KeyringError, OSError) as exc:
        raise CommandError(str(exc)) from exc
    if args.json:
        print(json.dumps({"ok": True, **result}))
    else:
        print(f"Keyring auto-unlock for {user}: {result['state']}")
        if result['state'] == "pending-password-login":
            print("Use your normal password for one GDM login to finish setup. "
                  "The login and keyring passwords must match.")
    return EXIT_OK


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
