"""Iris desktop application (GTK4 + libadwaita).

Module layout::

    iris.gui.app        Adw.Application, main window, page navigation, main()
    iris.gui.enroll     the enrolment wizard (preview, progress ring, result)
    iris.gui.settings   camera picker, thresholds, enrolled-face management
    iris.gui.widgets    reusable custom widgets (preview, ring, scan, badge)
    iris.gui.backend    camera worker threads and privileged-helper plumbing
    iris.gui.style.css  the design system

**Version pinning happens here, on purpose.**  ``gi.require_version`` must be
called before the first ``from gi.repository import Gtk`` anywhere in the
process, otherwise PyGObject emits a warning and picks a version by guesswork.
Doing it in the package ``__init__`` means *any* entry point into this package
-- ``python -m iris.gui``, ``iris-settings``, or a test importing
``iris.gui.widgets`` directly -- gets the right versions without having to
remember to pin them first.

Nothing heavyweight is imported at module scope.  ``main`` is fetched lazily by
:func:`__getattr__` so that importing, say, :mod:`iris.gui.widgets` does not
drag in the whole application.
"""

from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")

#: Reverse-DNS application id.  Used for the GApplication, the settings
#: schema-less GSettings-free config, the .desktop file and the window icon.
APP_ID = "org.iris.Iris"

__all__ = ["APP_ID", "main"]


def __getattr__(name: str) -> Any:
    """Expose ``iris.gui.main`` without importing the app eagerly."""
    if name == "main":
        from .app import main

        return main
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
