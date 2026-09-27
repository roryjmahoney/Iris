"""Iris command-line interface — installed as ``/usr/bin/iris``.

Everything an administrator or a user needs to drive Iris from a terminal:
enrolment, template management, a real authentication dry-run, camera
enumeration, configuration editing and a diagnostic ``doctor``.

Design rules that shape this package
---------------------------------

**The daemon owns the hardware and the templates.**  Almost every subcommand is
a thin, well-presented client of the ``irisd`` UNIX socket
(:mod:`iris.protocol`).  Where the daemon is unreachable but the operation is
still possible locally — reading ``/etc/iris/config.toml`` (0644), enumerating
V4L2 nodes, or touching ``/var/lib/iris`` as root — the command degrades to
doing it directly and says so.  That fallback matters: the most likely moment
someone reaches for this CLI is when the daemon is *not* working.

**Root is required only where it is genuinely required.**  ``enroll``,
``remove`` and ``clear`` mutate root-owned biometric state, so they refuse
early with a copy-pasteable ``sudo`` line rather than failing halfway with
``EACCES``.  The read-only commands run as anybody; on a stock install the
socket is ``0600 root:root``, so the ones that need the daemon explain that
too instead of printing a bare "connection refused".

**Output degrades gracefully.**  Colour is emitted only to a TTY (and never
when ``NO_COLOR`` is set or ``--color never`` is passed), and every box-drawing
or check-mark glyph has an ASCII fallback chosen from the output encoding, so
piping into ``grep``, ``less -R`` or a systemd journal all behave.

**Imports stay lazy.**  ``iris.camera``/``iris.engine`` pull in OpenCV (~200ms,
tens of MB).  ``iris config get`` and ``iris status`` must not pay for that, so
those modules are imported inside the functions that actually need them.

Package layout
--------------

One module per subcommand group, plus the plumbing they share:

* :mod:`~iris.cli.constants` -- exit codes, paths and timeouts;
* :mod:`~iris.cli.output` -- the shared :data:`~iris.cli.output.console`;
* :mod:`~iris.cli.common` -- users, daemon requests, JSON and progress;
* :mod:`~iris.cli.settings` -- configuration key validation and coercion;
* :mod:`~iris.cli.faces`, :mod:`~iris.cli.auth_test`,
  :mod:`~iris.cli.cameras`, :mod:`~iris.cli.config_cmd`,
  :mod:`~iris.cli.status`, :mod:`~iris.cli.doctor` and
  :mod:`~iris.cli.calibrate` -- the subcommands;
* :mod:`~iris.cli.parser` and :mod:`~iris.cli.main` -- argparse and dispatch.

Run it as ``python3 -m iris.cli`` (which is what ``/usr/bin/iris`` and
``bin/iris`` do).
"""

from __future__ import annotations

from iris.cli.constants import (
    EXIT_AUTH_FAILED,
    EXIT_FAILURE,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_PERMISSION,
    EXIT_UNAVAILABLE,
    EXIT_USAGE,
    PROG,
)
from iris.cli.main import main

__all__ = [
    "EXIT_AUTH_FAILED",
    "EXIT_FAILURE",
    "EXIT_INTERRUPTED",
    "EXIT_OK",
    "EXIT_PERMISSION",
    "EXIT_UNAVAILABLE",
    "EXIT_USAGE",
    "PROG",
    "main",
]
