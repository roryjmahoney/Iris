"""Configuration keys: validation, coercion and the effective config."""

from __future__ import annotations

import difflib
import json
import logging
import math
import os
from pathlib import Path
from typing import Any

from iris import config as config_mod
from iris import protocol

from iris.cli.constants import CONTROL_TIMEOUT, DETECTOR_MODEL, PROG, RECOGNIZER_MODEL
from iris.cli.output import CommandError


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
