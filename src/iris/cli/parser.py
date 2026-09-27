"""Argument parsing."""

from __future__ import annotations

import argparse
import math

from iris import __version__
from iris import protocol

from iris.cli.auth_test import cmd_test
from iris.cli.cameras import cmd_cameras
from iris.cli.config_cmd import cmd_config, cmd_config_set_all
from iris.cli.constants import ENROLL_TIMEOUT, PROG
from iris.cli.doctor import cmd_doctor
from iris.cli.faces import cmd_clear, cmd_enroll, cmd_list, cmd_remove
from iris.cli.status import cmd_keyring, cmd_status


_EPILOG = f"""\
examples:
  sudo {PROG} enroll                     enrol your face as "default"
  sudo {PROG} enroll glasses             add a second look
  sudo {PROG} list                       show what is enrolled
  sudo {PROG} test                       run one real authentication, timed
  {PROG} cameras                         list the video devices
  {PROG} config                          print the whole configuration
  sudo {PROG} config set recognition.threshold 0.5
  {PROG} doctor                          diagnose a broken installation

commands that talk to the daemon need root, because the irisd socket is
0600 root:root. Password authentication is never affected by anything here.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Infrared face authentication for Ubuntu / GNOME / Wayland.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version", action="version", version=f"{PROG} {__version__}"
    )
    parser.add_argument(
        "--color", choices=("auto", "always", "never"), default="auto",
        help="colourise output (default: auto, i.e. only on a terminal)",
    )
    parser.add_argument(
        "--socket", default=protocol.SOCKET_PATH, metavar="PATH",
        help=f"daemon socket (default: {protocol.SOCKET_PATH})",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="show debug logging from the iris modules",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    enroll = subparsers.add_parser(
        "enroll", help="record a face (root)",
        description="Record a new face template. Requires root.",
    )
    enroll.add_argument(
        "name", nargs="?", default=None,
        help="label for this look, e.g. 'glasses' (default: default)",
    )
    # --name is the spelling the GTK front end uses (see the "GUI <-> CLI
    # contract" in iris/gui/backend.py): building an argv from named options
    # only means it never has to reason about positional ordering.
    enroll.add_argument(
        "--name", dest="name_opt", metavar="NAME",
        help="same as the positional argument, for scripted callers",
    )
    enroll.add_argument("--user", help="account to enrol (default: the invoking user)")
    enroll.add_argument(
        "--timeout", type=float, default=ENROLL_TIMEOUT, metavar="SECONDS",
        help="budget for the whole enrolment (default: %(default)s)",
    )
    enroll.add_argument(
        "--json", action="store_true",
        help="stream newline-delimited JSON progress lines and a final result",
    )
    enroll.set_defaults(func=cmd_enroll)

    listing = subparsers.add_parser(
        "list", help="show enrolled faces",
        description="List the faces enrolled for a user.",
    )
    listing.add_argument("--user", help="account to inspect (default: the invoking user)")
    listing.add_argument("--json", action="store_true", help="machine-readable output")
    listing.set_defaults(func=cmd_list)

    remove = subparsers.add_parser(
        "remove", help="delete one face (root)",
        description="Delete a single named face template. Requires root.",
    )
    remove.add_argument("name", nargs="?", default=None, help="the face label to delete")
    remove.add_argument(
        "--name", dest="name_opt", metavar="NAME",
        help="same as the positional argument, for scripted callers",
    )
    remove.add_argument("--user", help="account to edit (default: the invoking user)")
    remove.add_argument("--json", action="store_true", help="machine-readable output")
    remove.set_defaults(func=cmd_remove)

    clear = subparsers.add_parser(
        "clear", help="delete every face (root)",
        description="Delete every face template for a user. Requires root.",
    )
    clear.add_argument("--user", help="account to clear (default: the invoking user)")
    clear.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    clear.add_argument(
        "--json", action="store_true",
        help="machine-readable output; implies --yes (the caller has already asked)",
    )
    clear.set_defaults(func=cmd_clear)

    test = subparsers.add_parser(
        "test", help="try a real authentication",
        description="Run one genuine authentication attempt and report the result.",
    )
    test.add_argument("--user", help="account to authenticate (default: the invoking user)")
    test.add_argument(
        "--timeout", type=float, metavar="SECONDS",
        help="override auth.timeout for this attempt",
    )
    test.add_argument("--json", action="store_true", help="machine-readable output")
    test.set_defaults(func=cmd_test)

    cameras = subparsers.add_parser(
        "cameras", help="list video devices",
        description="Show the V4L2 devices, marking the infrared camera.",
    )
    cameras.add_argument(
        "--all", action="store_true",
        help="also show V4L2 metadata nodes (never usable for capture)",
    )
    cameras.add_argument("--json", action="store_true", help="machine-readable output")
    cameras.set_defaults(func=cmd_cameras)

    config_parser = subparsers.add_parser(
        "config", help="get or set settings",
        description=(
            "Read and write /etc/iris/config.toml. With no arguments the whole "
            "configuration is printed."
        ),
    )
    config_parser.add_argument(
        "--json", action="store_true", dest="json_top",
        help="machine-readable output",
    )
    config_sub = config_parser.add_subparsers(dest="config_action", metavar="<action>")

    config_get = config_sub.add_parser("get", help="print one setting, a section, or everything")
    config_get.add_argument("key", nargs="?", help="dotted key, e.g. camera.device")
    config_get.add_argument("--json", action="store_true", help="machine-readable output")

    config_set = config_sub.add_parser("set", help="change one setting (root)")
    config_set.add_argument("key", help="dotted key, e.g. camera.device")
    config_set.add_argument("value", help="new value; coerced to the setting's type")
    config_set.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    config_unset = config_sub.add_parser("unset", help="restore one setting to its default (root)")
    config_unset.add_argument("key", help="dotted key, e.g. recognition.threshold")
    config_unset.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    config_keys = config_sub.add_parser("keys", help="list every setting with its type and default")
    config_keys.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    config_parser.set_defaults(func=cmd_config)

    # A whole-document writer, separate from `config set KEY VALUE`. The
    # settings panel accumulates several changes and applies them together, and
    # every privileged write costs one polkit prompt — so it needs to say "here
    # is the entire configuration" in a single invocation rather than one
    # prompt per changed key.
    config_set_all = subparsers.add_parser(
        "config-set", help="apply a whole configuration document from stdin (root)",
        description=(
            "Read a JSON configuration object from standard input and write it "
            "to /etc/iris/config.toml (via irisd when it is running). Requires root."
        ),
    )
    config_set_all.add_argument("--json", action="store_true", help="machine-readable output")
    config_set_all.set_defaults(func=cmd_config_set_all)

    doctor = subparsers.add_parser(
        "doctor", help="diagnose the installation",
        description=(
            "Check every part of the installation and print a remediation hint "
            "for anything wrong. Exits non-zero if any check fails."
        ),
    )
    doctor.add_argument("--user", help="account whose enrolment to check")
    doctor.add_argument(
        "--no-camera", action="store_true",
        help="skip the camera and strobe checks (they take a couple of seconds)",
    )
    doctor.add_argument(
        "--camera-timeout", type=float, default=2.5, metavar="SECONDS",
        help="how long to sample frames for (default: %(default)s)",
    )
    doctor.add_argument(
        "--calibrate", action="store_true",
        help="also measure detection latency and genuine-match similarity",
    )
    doctor.add_argument(
        "--samples", type=int, default=30, metavar="N",
        help="faces to collect when calibrating (default: %(default)s)",
    )
    doctor.add_argument(
        "--calibrate-timeout", type=float, default=45.0, metavar="SECONDS",
        help="calibration capture budget (default: %(default)s)",
    )
    doctor.set_defaults(func=cmd_doctor)

    keyring_parser = subparsers.add_parser(
        "keyring", help="opt in to TPM-backed GNOME Keyring auto-unlock (root)",
    )
    keyring_parser.add_argument("keyring_action", choices=("enable", "disable", "status"))
    keyring_parser.add_argument("--user", help="local account (default: invoking user)")
    keyring_parser.add_argument("--json", action="store_true", help="machine-readable output")
    keyring_parser.set_defaults(func=cmd_keyring)

    status = subparsers.add_parser(
        "status", help="summarise the current state",
        description="One-screen summary of the daemon, configuration and enrolment.",
    )
    status.add_argument("--user", help="account to summarise (default: the invoking user)")
    status.add_argument("--json", action="store_true", help="machine-readable output")
    status.set_defaults(func=cmd_status)

    return parser


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Reject argument combinations argparse cannot express."""
    if getattr(args, "samples", None) is not None and args.samples < 2:
        parser.error("--samples must be at least 2 (similarity needs a pair)")
    for name in ("timeout", "camera_timeout", "calibrate_timeout"):
        value = getattr(args, name, None)
        if value is not None and (not math.isfinite(value) or value <= 0):
            parser.error(f"--{name.replace('_', '-')} must be a positive number")


# --------------------------------------------------------------------------
