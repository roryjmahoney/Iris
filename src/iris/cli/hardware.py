"""``iris hardware-report``: a paste-ready report for a GitHub hardware issue."""

from __future__ import annotations

import argparse
import json

from iris.cli.constants import EXIT_OK
from iris.cli.output import CommandError, console
from iris.cli.settings import load_effective_config

#: Where people send the report.  Pre-selects the hardware issue form.
REPORT_URL = "https://github.com/roryjmahoney/Iris/issues/new?template=hardware_report.yml"


def cmd_hardware_report(args: argparse.Namespace) -> int:
    try:
        from iris import hardware
    except ImportError as exc:  # OpenCV/numpy missing is itself a finding
        raise CommandError(
            f"cannot load the camera stack: {exc}",
            hint="sudo apt install python3-opencv python3-numpy",
        ) from exc

    cfg, _source = load_effective_config(args.socket)
    seconds = 0.0 if args.no_capture else float(args.seconds)
    if not args.json and seconds > 0:
        # stderr, so `iris hardware-report > report.md` stays clean.
        print(console.dim(f"sampling the infrared camera for {seconds:g}s (no images are saved)…"),
              file=console.err, flush=True)

    report = hardware.collect_report(cfg, device=args.device, capture_seconds=seconds)

    if args.json:
        print(json.dumps(report, indent=2))
        return EXIT_OK

    print(hardware.render_markdown(report), end="", flush=True)
    print(console.dim(f"\nPaste the report above into a hardware issue: {REPORT_URL}"), file=console.err)
    return EXIT_OK
