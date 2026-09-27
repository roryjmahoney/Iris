"""``iris config``: read and edit /etc/iris/config.toml."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Mapping

from iris import config as config_mod
from iris import protocol

from iris.cli.common import (
    _as_command_error,
    _failure_text,
    _json_mode,
    emit_json,
    render_table,
    require_root,
)
from iris.cli.constants import (
    CONTROL_TIMEOUT,
    EXIT_OK,
    EXIT_PERMISSION,
    EXIT_USAGE,
    PROG,
)
from iris.cli.output import CommandError, console
from iris.cli.settings import (
    _DESCRIPTIONS,
    _RANGES,
    _format_number,
    check_range,
    coerce_value,
    default_for,
    format_value,
    known_keys,
    load_effective_config,
    semantic_check,
    split_key,
)


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
