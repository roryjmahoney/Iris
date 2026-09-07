"""``python3 -m iris`` — run the Iris daemon.

The installed ``irisd`` executable and this module are the same entry point, so
a developer running from a source checkout and systemd running the packaged
service exercise identical code.  Everything lives in :mod:`iris.daemon`; this
file exists only to give the package a runnable form.

Note that the *client* CLI is a separate program (``iris``); ``python3 -m iris``
is deliberately the daemon, because that is the component with a natural
"run me" meaning and the one an administrator needs to start by hand when
debugging a unit file.
"""

from __future__ import annotations

import sys

from .daemon import main

if __name__ == "__main__":
    # SystemExit with an int status, so the shell and systemd both see the
    # daemon's own exit code rather than a traceback.
    sys.exit(main())
