"""Allow ``python3 -m iris.gui`` to launch the desktop application.

The installed entry point is the ``iris-settings`` script the installer
creates; this exists so the app can be run straight from a source checkout with
``PYTHONPATH=src python3 -m iris.gui``, which is how it gets tested.
"""

from __future__ import annotations

from .app import main

raise SystemExit(main())
