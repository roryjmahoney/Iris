"""Iris — infrared face authentication for Ubuntu / GNOME / Wayland.

Package layout::

    iris.config     configuration file handling (/etc/iris/config.toml)
    iris.protocol   newline-delimited JSON IPC with irisd over a UNIX socket
    iris.camera     V4L2 device enumeration and IR frame capture
    iris.engine     YuNet detection + SFace embedding/comparison
    iris.liveness   IR-native presentation-attack detection
    iris.store      encrypted, root-only template storage

This module intentionally imports **nothing** from its own submodules.

Why: ``iris.camera`` and ``iris.engine`` pull in OpenCV, which costs a couple
of hundred milliseconds and tens of megabytes of RSS to import.  The PAM
integration path and the CLI both need to read ``__version__`` and
``iris.config`` without paying for that, and a login must never be delayed by
loading a vision stack it may not end up using.  Import the submodule you
actually need.
"""

from __future__ import annotations

__version__ = "1.0.0"

__all__ = ["__version__"]
