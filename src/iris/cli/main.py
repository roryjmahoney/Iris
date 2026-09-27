"""Entry point."""

from __future__ import annotations

import logging
import os
import sys
from typing import Sequence

from iris.cli.common import _json_mode, emit_json
from iris.cli.constants import EXIT_INTERRUPTED, EXIT_USAGE
from iris.cli.output import CommandError, console
from iris.cli.parser import _validate_args, build_parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    console.configure(args.color)

    # Module logs (config fallbacks, camera warnings) go to stderr so they can
    # never corrupt the machine-readable stdout of --json.
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_USAGE

    _validate_args(parser, args)

    try:
        return int(args.func(args))
    except CommandError as exc:
        # A machine-readable invocation must fail machine-readably: the GTK
        # front end parses stdout and only falls back to stderr when there was
        # nothing there, so an error that appears on stderr alone reaches the
        # user as "that did not work" with no explanation.
        if _json_mode(args):
            emit_json({"ok": False, "error": str(exc), **({"hint": exc.hint} if exc.hint else {})})
        console.error(str(exc))
        if exc.hint:
            console.hint(exc.hint)
        return exc.code
    except KeyboardInterrupt:
        console.print()
        console.error("interrupted")
        return EXIT_INTERRUPTED
    except BrokenPipeError:
        # `iris config | head` closes the pipe under us. Redirect stdout to
        # /dev/null so the interpreter's own flush at exit does not print a
        # second, uglier error on the way out.
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except OSError:  # pragma: no cover
            pass
        return 141  # 128 + SIGPIPE
