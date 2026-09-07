"""Iris configuration file handling.

The configuration lives at :data:`CONFIG_PATH` (``/etc/iris/config.toml``,
``root:root 0644``).  It is read on every authentication attempt, so the
overriding design rule here is:

    **A broken configuration file must never break authentication.**

:func:`load_config` therefore never raises.  Anything it cannot parse,
type-check or make sense of falls back to the corresponding value in
:data:`DEFAULTS` and is reported through the ``iris.config`` logger.  A user
who fat-fingers a TOML edit gets default face-auth behaviour and a log line,
not a machine they cannot log into.

Reading uses :mod:`tomllib` from the standard library.  Writing uses the small
hand-rolled emitter below rather than a third-party TOML writer: the schema is
exactly two levels deep and contains only ``str``/``int``/``float``/``bool``
scalars, so a dependency-free emitter is both sufficient and auditable.  (No
pip installs are permitted on the target system.)
"""

from __future__ import annotations

import copy
import logging
import math
import os
import tempfile
import time
import tomllib
from typing import Any, Final, Mapping

_LOG = logging.getLogger("iris.config")

CONFIG_PATH: Final[str] = "/etc/iris/config.toml"

#: Canonical schema and fallback values.  The structure of this dict *is* the
#: schema: two levels, scalar leaves.  Every value loaded from disk is checked
#: against the type of its counterpart here before being accepted.
DEFAULTS: Final[dict[str, dict[str, Any]]] = {
    "camera": {
        "device": "/dev/video2",
        "width": 640,
        "height": 360,
        "ir_mode": True,
        "min_frame_brightness": 20.0,
    },
    "recognition": {
        "threshold": 0.363,
        "detect_score": 0.7,
        "model_dir": "/usr/share/iris/models",
        "required_matches": 3,
        "max_frames": 120,
    },
    "auth": {
        "enabled": True,
        "timeout": 8.0,
        "max_failures": 5,
        "lockout_seconds": 60,
    },
    "liveness": {
        "enabled": True,
        "min_variance": 12.0,
    },
}

# A cosine threshold below this is not a strict policy error (an administrator
# may knowingly want a lenient system) but it is close enough to "accept any
# face" that it deserves a loud log line every time the config is read.
_PERMISSIVE_THRESHOLD_WARNING: Final[float] = 0.20

_SCALAR_TYPES: Final[tuple[type, ...]] = (bool, int, float, str)


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

def defaults() -> dict[str, dict[str, Any]]:
    """Return a deep copy of :data:`DEFAULTS`, safe for the caller to mutate."""
    return copy.deepcopy(DEFAULTS)


def load_config(path: str | os.PathLike[str] = CONFIG_PATH) -> dict[str, Any]:
    """Load the configuration, deep-merged over :data:`DEFAULTS`.

    Never raises.  A missing, unreadable, malformed or partially nonsensical
    file yields a usable configuration built from the defaults.

    :param path: file to read; defaults to :data:`CONFIG_PATH`.
    :returns: a complete two-level configuration dict.
    """
    cfg = defaults()

    # The outermost guard is deliberately broad.  This function sits directly
    # in the PAM authentication path; there is no failure mode of config
    # parsing that is worth propagating to a login prompt.
    try:
        try:
            with open(path, "rb") as handle:
                loaded = tomllib.load(handle)
        except FileNotFoundError:
            # Normal before the installer has run, and normal for a
            # site that is happy with every default.
            _LOG.debug("no config at %s; using defaults", path)
            return cfg
        except PermissionError:
            # Readable by all after install (0644); if it is not, say so
            # clearly because the daemon and the GUI will disagree about
            # settings until it is fixed.
            _LOG.warning("cannot read %s (permission denied); using defaults", path)
            return cfg
        except OSError as exc:
            _LOG.warning("cannot read %s (%s); using defaults", path, exc)
            return cfg
        except tomllib.TOMLDecodeError as exc:
            _LOG.error("malformed TOML in %s (%s); using defaults", path, exc)
            return cfg

        if not isinstance(loaded, dict):  # pragma: no cover - tomllib guarantees dict
            _LOG.error("%s did not parse to a table; using defaults", path)
            return cfg

        _merge_into(cfg, loaded, str(path))
        _sanitise(cfg, str(path))
        return cfg

    except Exception:  # noqa: BLE001 - last-resort backstop, see docstring
        _LOG.exception("unexpected error loading %s; using defaults", path)
        return defaults()


def save_config(cfg: Mapping[str, Any], path: str | os.PathLike[str] = CONFIG_PATH) -> None:
    """Write *cfg* to *path* atomically as TOML.

    Unlike :func:`load_config` this *does* raise: a caller asking to persist
    settings has to learn that the write failed.

    The value written is ``cfg`` merged over :data:`DEFAULTS`, so the file on
    disk is always complete and always valid even if the caller passed a
    partial dict.  The write is atomic (temp file in the same directory,
    ``fsync``, then :func:`os.replace`) because a half-written config would be
    read as malformed by every subsequent authentication.

    :raises OSError: the file could not be written or replaced.
    :raises TypeError: *cfg* contains a value that cannot be represented.
    """
    target = os.fspath(path)
    directory = os.path.dirname(target) or "."

    merged = defaults()
    _merge_into(merged, cfg, target)
    _sanitise(merged, target)
    text = _emit_toml(merged)

    os.makedirs(directory, mode=0o755, exist_ok=True)

    # delete=False + explicit replace: we need the file to survive the close.
    fd, tmp_path = tempfile.mkstemp(prefix=".config.toml.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        # mkstemp creates 0600; the contract says the config is world-readable
        # so that the unprivileged GUI can show current settings.
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, target)
        tmp_path = ""  # ownership transferred to `target`
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:  # pragma: no cover - best-effort cleanup
                _LOG.debug("could not remove temp file %s", tmp_path)

    # Fsync the directory so the rename itself is durable, not just the data.
    try:
        dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:  # pragma: no cover - non-fatal on exotic filesystems
        _LOG.debug("could not fsync %s (%s)", directory, exc)

    _LOG.info("wrote configuration to %s", target)


# --------------------------------------------------------------------------
# merging and validation
# --------------------------------------------------------------------------

def _merge_into(base: dict[str, Any], loaded: Mapping[str, Any], origin: str) -> None:
    """Deep-merge *loaded* over *base* in place, rejecting bad values.

    Known keys are type-checked against their default.  Unknown keys are kept
    if they are scalars — an administrator's forward-compatible or
    hand-annotated entry should survive a round trip through the settings GUI
    rather than being silently deleted — and dropped otherwise.
    """
    for section, values in loaded.items():
        if not isinstance(values, Mapping):
            _LOG.warning(
                "%s: ignoring top-level key %r: expected a [section] table, got %s",
                origin, section, type(values).__name__,
            )
            continue

        known_section = section in base
        target = base.setdefault(section, {})
        if not known_section:
            _LOG.debug("%s: keeping unknown section [%s]", origin, section)

        for key, value in values.items():
            if known_section and key in DEFAULTS.get(section, {}):
                accepted, coerced = _coerce(DEFAULTS[section][key], value)
                if not accepted:
                    _LOG.warning(
                        "%s: ignoring %s.%s = %r: expected %s, using default %r",
                        origin, section, key, value,
                        type(DEFAULTS[section][key]).__name__, DEFAULTS[section][key],
                    )
                    continue
                target[key] = coerced
            elif isinstance(value, _SCALAR_TYPES):
                _LOG.debug("%s: keeping unrecognised key %s.%s", origin, section, key)
                target[key] = value
            else:
                _LOG.warning(
                    "%s: ignoring %s.%s: unsupported value type %s",
                    origin, section, key, type(value).__name__,
                )


def _coerce(default: Any, value: Any) -> tuple[bool, Any]:
    """Type-check *value* against *default*.

    Returns ``(accepted, value)``.  The only widening permitted is int -> float,
    because TOML writes ``timeout = 8`` as an int and an administrator should
    not have to remember the trailing ``.0``.

    ``bool`` is checked before ``int`` throughout: in Python ``bool`` is a
    subclass of ``int``, so ``isinstance(True, int)`` is true and a naive check
    would happily accept ``width = true``.
    """
    if isinstance(default, bool):
        return (True, value) if isinstance(value, bool) else (False, default)

    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, int):
            return (False, default)
        return (True, value)

    if isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return (False, default)
        result = float(value)
        if not math.isfinite(result):
            return (False, default)
        return (True, result)

    if isinstance(default, str):
        return (True, value) if isinstance(value, str) else (False, default)

    return (False, default)  # pragma: no cover - DEFAULTS holds no other types


def _sanitise(cfg: dict[str, Any], origin: str) -> None:
    """Clamp accepted-but-nonsensical values in place.

    Type correctness is not enough: ``timeout = -1`` and ``max_frames = 0``
    both parse as the right type and would make the daemon spin or give up
    instantly.  Clamping keeps the system running with a complaint in the log.
    """
    _clamp(cfg, origin, "camera", "width", 1, 8192)
    _clamp(cfg, origin, "camera", "height", 1, 8192)
    _clamp(cfg, origin, "camera", "min_frame_brightness", 0.0, 255.0)

    # Cosine similarity is bounded by definition; anything outside [-1, 1] is a
    # typo that would make the comparison either always or never true.
    _clamp(cfg, origin, "recognition", "threshold", -1.0, 1.0)
    _clamp(cfg, origin, "recognition", "detect_score", 0.0, 1.0)
    _clamp(cfg, origin, "recognition", "required_matches", 1, 1000)
    _clamp(cfg, origin, "recognition", "max_frames", 1, 100_000)

    # An upper bound on timeout is a safety requirement, not tidiness: PAM must
    # never hold a login shell open indefinitely.
    _clamp(cfg, origin, "auth", "timeout", 0.5, 60.0)
    # FailureTracker retains 64 entries per user. A larger threshold could
    # never be reached and would silently disable rate limiting.
    _clamp(cfg, origin, "auth", "max_failures", 1, 64)
    _clamp(cfg, origin, "auth", "lockout_seconds", 0, 86_400)

    _clamp(cfg, origin, "liveness", "min_variance", 0.0, 65_025.0)

    threshold = cfg.get("recognition", {}).get("threshold")
    if isinstance(threshold, float) and threshold < _PERMISSIVE_THRESHOLD_WARNING:
        _LOG.warning(
            "%s: recognition.threshold = %r is dangerously permissive "
            "(SFace default is %r); face authentication may accept strangers",
            origin, threshold, DEFAULTS["recognition"]["threshold"],
        )

    model_dir = cfg.get("recognition", {}).get("model_dir")
    if isinstance(model_dir, str) and not model_dir.strip():
        _LOG.warning("%s: recognition.model_dir is empty; using default", origin)
        cfg["recognition"]["model_dir"] = DEFAULTS["recognition"]["model_dir"]

    device = cfg.get("camera", {}).get("device")
    if isinstance(device, str) and not device.strip():
        _LOG.warning("%s: camera.device is empty; using default", origin)
        cfg["camera"]["device"] = DEFAULTS["camera"]["device"]


def _clamp(cfg: dict[str, Any], origin: str, section: str, key: str, low: Any, high: Any) -> None:
    """Clamp ``cfg[section][key]`` into ``[low, high]``, logging any change."""
    try:
        value = cfg[section][key]
    except (KeyError, TypeError):  # pragma: no cover - _merge_into guarantees presence
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return

    clamped = min(max(value, low), high)
    if clamped != value:
        _LOG.warning(
            "%s: %s.%s = %r out of range [%r, %r]; clamped to %r",
            origin, section, key, value, low, high, clamped,
        )
        # Preserve the declared type of the field (int fields stay int).
        cfg[section][key] = type(DEFAULTS[section][key])(clamped)


# --------------------------------------------------------------------------
# TOML emitter
# --------------------------------------------------------------------------

_HEADER = """\
# /etc/iris/config.toml — Iris face authentication
#
# Written by Iris {version} on {stamp}.
# Hand edits are preserved; this file is rewritten whenever settings change
# in the Iris settings panel, which reformats it and drops comments.
#
# Any value that is missing, has the wrong type or is out of range falls back
# to its built-in default, and a malformed file falls back entirely — face
# authentication never fails closed on a configuration error, it simply runs
# with defaults. Check `journalctl -u irisd` for what was rejected and why.
"""


def _emit_toml(cfg: Mapping[str, Any]) -> str:
    """Render *cfg* as TOML text.

    Known sections and keys are emitted in :data:`DEFAULTS` order so that diffs
    between two saved files stay readable; anything unrecognised follows, in
    sorted order.
    """
    from . import __version__

    stamp = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime())
    lines = [_HEADER.format(version=__version__, stamp=stamp)]

    ordered = list(DEFAULTS) + sorted(k for k in cfg if k not in DEFAULTS)
    for section in ordered:
        values = cfg.get(section)
        if not isinstance(values, Mapping):
            continue

        keys = list(DEFAULTS.get(section, {}))
        keys += sorted(k for k in values if k not in keys)

        lines.append(f"[{_emit_key(section)}]")
        for key in keys:
            if key not in values:
                continue
            lines.append(f"{_emit_key(key)} = {_emit_scalar(values[key], f'{section}.{key}')}")
        lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n"


def _emit_key(key: str) -> str:
    """Emit a TOML key, quoting it only when it is not a bare key."""
    if key and all(c.isascii() and (c.isalnum() or c in "_-") for c in key):
        return key
    return _emit_string(key)


_ESCAPES: Final[dict[int, str]] = {
    0x08: "\\b", 0x09: "\\t", 0x0A: "\\n", 0x0C: "\\f", 0x0D: "\\r",
    0x22: '\\"', 0x5C: "\\\\",
}


def _emit_string(value: str) -> str:
    """Emit a TOML basic string with the escapes the spec requires."""
    out = ['"']
    for char in value:
        code = ord(char)
        escape = _ESCAPES.get(code)
        if escape is not None:
            out.append(escape)
        elif code < 0x20 or code == 0x7F:
            out.append(f"\\u{code:04X}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _emit_scalar(value: Any, where: str) -> str:
    """Emit a single TOML value.

    ``bool`` is tested first because it is a subclass of ``int`` and would
    otherwise be written as ``1``, which reloads as an int and fails the type
    check on the next read.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        # repr() of a finite float always contains '.' or 'e', so the value
        # round-trips through tomllib as a float rather than an int.
        return repr(value)
    if isinstance(value, str):
        return _emit_string(value)
    raise TypeError(f"cannot represent {where} as TOML: {type(value).__name__}")
