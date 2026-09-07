"""V4L2 device enumeration and infrared frame capture.

Two hardware facts drive everything in this module, both verified on the
target machine:

**Sibling metadata nodes must never be opened.**  A UVC camera registers more
than one ``/dev/video*`` node.  On this laptop ``/dev/video0`` and
``/dev/video2`` are the RGB and IR capture nodes, while ``/dev/video1`` and
``/dev/video3`` are metadata nodes that deliver UVC payload headers, not
images.  Opening one for capture yields no frames.  They cannot be told apart
by name — both IR nodes report ``LGE Camera: LGE IR-FHD Camera`` — so
:func:`list_cameras` asks the kernel via ``VIDIOC_QUERYCAP`` and looks for
``V4L2_CAP_META_CAPTURE``.  Observed values::

    /dev/video0  device_caps=0x04200001  VIDEO_CAPTURE  MJPG, YUYV
    /dev/video1  device_caps=0x04a00000  META_CAPTURE   -
    /dev/video2  device_caps=0x04200001  VIDEO_CAPTURE  GREY
    /dev/video3  device_caps=0x04a00000  META_CAPTURE   -

Note that the ``capabilities`` field is useless for this: it reports the union
across every node of the physical device (``0x84a00001`` on all four here, with
the metadata bit set even for the real capture nodes).  Only ``device_caps``
describes the node actually opened, which is why this code checks the
``V4L2_CAP_DEVICE_CAPS`` flag and reads ``device_caps``.

**The infrared emitter strobes.**  The IR camera delivers alternating dark and
lit frames — mean brightness ~1.2 and ~53-65 respectively, at 15 fps, so the
usable rate is 7.5 fps.  Dark frames contain no usable face, and running
detection on them wastes most of the authentication budget and produces
spurious "no face" results.  :meth:`Camera.frames` drops them.
"""

from __future__ import annotations

import fcntl
import glob
import logging
import os
import re
import struct
import time
from typing import Any, Final, Iterator

import cv2
import numpy as np

_LOG = logging.getLogger("iris.camera")

_SYSFS_ROOT: Final[str] = "/sys/class/video4linux"


class CameraError(Exception):
    """Base class for camera failures.

    Callers on the authentication path catch this and report the
    ``camera_error`` reason code; they must never let it escape as a crash.
    """


class CameraOpenError(CameraError):
    """The capture device could not be opened or configured."""


class CameraReadError(CameraError):
    """The device stopped delivering frames while streaming."""


# --------------------------------------------------------------------------
# V4L2 ioctl plumbing
# --------------------------------------------------------------------------

# Linux ioctl request encoding (asm-generic/ioctl.h):
#   bits 31-30 direction, 29-16 size, 15-8 type, 7-0 number
_IOC_NONE: Final[int] = 0
_IOC_WRITE: Final[int] = 1
_IOC_READ: Final[int] = 2


def _ioc(direction: int, type_: str, number: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (ord(type_) << 8) | number


# struct v4l2_capability: driver[16] card[32] bus_info[32] version
#                         capabilities device_caps reserved[3]
_V4L2_CAPABILITY_FMT: Final[str] = "<16s32s32sIII12s"
_V4L2_CAPABILITY_SIZE: Final[int] = struct.calcsize(_V4L2_CAPABILITY_FMT)  # 104

# struct v4l2_fmtdesc: index type flags description[32]
#                      pixelformat mbus_code reserved[3]
_V4L2_FMTDESC_FMT: Final[str] = "<III32sII12s"
_V4L2_FMTDESC_SIZE: Final[int] = struct.calcsize(_V4L2_FMTDESC_FMT)  # 64

_VIDIOC_QUERYCAP: Final[int] = _ioc(_IOC_READ, "V", 0, _V4L2_CAPABILITY_SIZE)
_VIDIOC_ENUM_FMT: Final[int] = _ioc(_IOC_READ | _IOC_WRITE, "V", 2, _V4L2_FMTDESC_SIZE)

_V4L2_BUF_TYPE_VIDEO_CAPTURE: Final[int] = 1

_V4L2_CAP_VIDEO_CAPTURE: Final[int] = 0x00000001
_V4L2_CAP_META_CAPTURE: Final[int] = 0x00800000
_V4L2_CAP_DEVICE_CAPS: Final[int] = 0x80000000

#: Enumeration is bounded so a misbehaving driver cannot spin the loop.
_MAX_FORMATS: Final[int] = 64

#: Monochrome pixel formats.  A node that offers only these is an IR sensor:
#: consumer colour webcams always expose MJPG or YUYV as well.
_MONO_FOURCCS: Final[frozenset[str]] = frozenset({
    "GREY", "Y8", "Y800", "Y10", "Y12", "Y14", "Y16", "Y10B", "Y12B",
})

# Uppercase "IR" as a standalone token, e.g. "LGE IR-FHD Camera". Matching
# case-insensitively would misfire on ordinary words, and matching without the
# boundaries would hit names like "IRIS" or "Kirin".
_IR_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"(?<![A-Za-z0-9])IR(?![A-Za-z0-9])")

_VIDEO_NODE_RE: Final[re.Pattern[str]] = re.compile(r"^/dev/video(\d+)$")


def _ioctl(fd: int, request: int, buffer: bytearray) -> None:
    """Perform an ioctl, tolerating either signedness of the request number.

    ``VIDIOC_QUERYCAP`` has bit 31 set (it is a read ioctl), so the constant
    exceeds ``INT_MAX``.  CPython accepts the unsigned form, but the two's
    complement fallback keeps enumeration working if that ever tightens —
    losing the device list would break enrolment entirely.
    """
    try:
        fcntl.ioctl(fd, request, buffer)
    except OverflowError:  # pragma: no cover - not reached on CPython 3.14
        fcntl.ioctl(fd, request - (1 << 32), buffer)


def _query_capabilities(path: str) -> tuple[int, str]:
    """Return ``(effective_caps, card_name)`` for a V4L2 node.

    :raises OSError: the node could not be opened or queried.
    """
    # O_NONBLOCK so opening never stalls on a device that is mid-reset, and
    # O_RDONLY because QUERYCAP needs no write access and we must not disturb
    # another process that may be streaming.
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        buffer = bytearray(_V4L2_CAPABILITY_SIZE)
        _ioctl(fd, _VIDIOC_QUERYCAP, buffer)
        _driver, card, _bus, _version, capabilities, device_caps, _reserved = struct.unpack(
            _V4L2_CAPABILITY_FMT, buffer
        )
    finally:
        os.close(fd)

    # See the module docstring: `capabilities` is the union over all nodes of
    # the physical device and cannot distinguish them.
    effective = device_caps if capabilities & _V4L2_CAP_DEVICE_CAPS else capabilities
    name = card.split(b"\0", 1)[0].decode("utf-8", "replace").strip()
    return effective, name


def _enumerate_formats(path: str) -> list[str]:
    """Return the video-capture FourCCs a node offers, e.g. ``["GREY"]``."""
    formats: list[str] = []
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as exc:
        _LOG.debug("cannot open %s to enumerate formats: %s", path, exc)
        return formats

    try:
        for index in range(_MAX_FORMATS):
            buffer = bytearray(_V4L2_FMTDESC_SIZE)
            struct.pack_into("<II", buffer, 0, index, _V4L2_BUF_TYPE_VIDEO_CAPTURE)
            try:
                _ioctl(fd, _VIDIOC_ENUM_FMT, buffer)
            except OSError:
                # EINVAL is how V4L2 signals "no format at this index"; it is
                # also what a metadata node returns for index 0.
                break
            _index, _type, _flags, _desc, pixelformat, _mbus, _reserved = struct.unpack(
                _V4L2_FMTDESC_FMT, buffer
            )
            fourcc = "".join(
                chr((pixelformat >> shift) & 0xFF) for shift in (0, 8, 16, 24)
            ).strip()
            if fourcc:
                formats.append(fourcc)
    finally:
        os.close(fd)
    return formats


# --------------------------------------------------------------------------
# enumeration
# --------------------------------------------------------------------------

def list_cameras() -> list[dict[str, Any]]:
    """Enumerate usable V4L2 video nodes.

    :returns: one dict per node, sorted by node number, with keys
        ``path``, ``name``, ``is_ir``, ``is_metadata`` and ``formats``.

    Nodes whose capabilities cannot be queried are omitted with a warning:
    if ``VIDIOC_QUERYCAP`` fails then OpenCV could not have opened the node
    either, and listing it without knowing whether it is a metadata node would
    risk offering an unusable device as a capture source.
    """
    cameras: list[dict[str, Any]] = []

    for sysfs_dir in sorted(glob.glob(f"{_SYSFS_ROOT}/video*"), key=_node_sort_key):
        node = os.path.basename(sysfs_dir)
        path = f"/dev/{node}"
        if not os.path.exists(path):
            # Device removed between the glob and now (hot-unplug).
            continue

        sysfs_name = _read_sysfs_name(sysfs_dir)

        try:
            caps, card_name = _query_capabilities(path)
        except OSError as exc:
            _LOG.warning("skipping %s: cannot query V4L2 capabilities (%s)", path, exc)
            continue

        # Prefer the sysfs name (what the task contract points at); fall back
        # to the QUERYCAP card string, which is the same value by another road.
        name = sysfs_name or card_name or node

        is_metadata = bool(caps & _V4L2_CAP_META_CAPTURE) or not (
            caps & _V4L2_CAP_VIDEO_CAPTURE
        )

        formats = [] if is_metadata else _enumerate_formats(path)

        is_ir = not is_metadata and (
            bool(_IR_TOKEN_RE.search(name))
            or "infrared" in name.lower()
            or (bool(formats) and all(f in _MONO_FOURCCS for f in formats))
        )

        cameras.append({
            "path": path,
            "name": name,
            "is_ir": is_ir,
            "is_metadata": is_metadata,
            "formats": formats,
        })

    return cameras


def _node_sort_key(sysfs_dir: str) -> tuple[int, str]:
    """Sort ``video10`` after ``video9`` rather than after ``video1``."""
    node = os.path.basename(sysfs_dir)
    digits = node[len("video"):]
    return (int(digits), node) if digits.isdigit() else (1 << 30, node)


def _read_sysfs_name(sysfs_dir: str) -> str:
    """Read ``/sys/class/video4linux/<node>/name``, or ``""`` if unavailable."""
    try:
        with open(os.path.join(sysfs_dir, "name"), encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError as exc:
        _LOG.debug("cannot read name for %s: %s", sysfs_dir, exc)
        return ""


def default_ir_camera() -> dict[str, Any] | None:
    """Return the first infrared capture node, or ``None`` if there is none.

    Used to seed ``camera.device`` when the configured device has gone away
    (a docking station change can renumber ``/dev/video*``).
    """
    for camera in list_cameras():
        if camera["is_ir"]:
            return camera
    return None


def _is_metadata_node(path: str) -> bool | None:
    """True/False if the node's kind is known, ``None`` if it could not be queried."""
    try:
        caps, _name = _query_capabilities(path)
    except OSError as exc:
        _LOG.debug("cannot classify %s (%s); leaving it to OpenCV", path, exc)
        return None
    return bool(caps & _V4L2_CAP_META_CAPTURE) or not (caps & _V4L2_CAP_VIDEO_CAPTURE)


# --------------------------------------------------------------------------
# capture
# --------------------------------------------------------------------------

#: Consecutive failed reads tolerated before declaring the device dead.  Paired
#: with the retry sleep below this is ~0.3 s, short enough to leave time to
#: report an error inside an 8 s authentication budget.
_MAX_CONSECUTIVE_READ_FAILURES: Final[int] = 30

#: Pause after a failed read, so a device returning errors instantly cannot
#: spin the CPU at 100%.
_READ_RETRY_SLEEP: Final[float] = 0.01


class Camera:
    """A V4L2 capture device, configured for infrared face authentication.

    Use it as a context manager so the underlying ``VideoCapture`` is always
    released — an IR camera left open keeps the emitter powered and blocks the
    next authentication attempt::

        with Camera("/dev/video2", 640, 360) as cam:
            for gray in cam.frames(timeout=8.0):
                ...
    """

    def __init__(
        self,
        device: str | int,
        width: int,
        height: int,
        min_brightness: float = 20.0,
        ir_mode: bool = True,
    ) -> None:
        self.device = device
        self.width = int(width)
        self.height = int(height)
        self.min_brightness = float(min_brightness)
        self.ir_mode = bool(ir_mode)

        self._cap: cv2.VideoCapture | None = None
        self._last_mean: float | None = None
        self._lit_frames = 0
        self._dark_frames = 0

    # -- construction ------------------------------------------------------

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "Camera":
        """Build a camera from a loaded :mod:`iris.config` dict."""
        camera_cfg = cfg.get("camera", {})
        return cls(
            device=camera_cfg.get("device", "/dev/video2"),
            width=camera_cfg.get("width", 640),
            height=camera_cfg.get("height", 360),
            min_brightness=camera_cfg.get("min_frame_brightness", 20.0),
            ir_mode=camera_cfg.get("ir_mode", True),
        )

    # -- lifecycle ---------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._cap is not None

    @property
    def last_frame_mean(self) -> float | None:
        """Mean brightness of the most recent frame read, for diagnostics.

        The settings panel uses this to help an administrator pick a sensible
        ``min_frame_brightness`` for a different IR module.
        """
        return self._last_mean

    @property
    def frame_stats(self) -> dict[str, int]:
        """Counts of lit and dark frames seen since the device was opened."""
        return {"lit": self._lit_frames, "dark": self._dark_frames}

    def open(self) -> None:
        """Open and configure the device.

        :raises CameraOpenError: the device is a metadata node, cannot be
            opened, or delivers nothing usable.
        """
        if self._cap is not None:
            return

        path = self.device if isinstance(self.device, str) else None
        if path is not None and _VIDEO_NODE_RE.match(path):
            # Fail fast and explain, rather than letting OpenCV open a metadata
            # node and time out with no frames and no clue why.
            if _is_metadata_node(path):
                raise CameraOpenError(
                    f"{path} is a V4L2 metadata node, not a capture device; "
                    f"pick a capture node (see list_cameras())"
                )

        cap = self._open_capture()
        try:
            self._configure(cap)
        except Exception:
            cap.release()
            raise
        self._cap = cap
        self._last_mean = None
        self._lit_frames = 0
        self._dark_frames = 0

    def _open_capture(self) -> cv2.VideoCapture:
        """Open the device with the V4L2 backend, by path then by index."""
        # CAP_V4L2 is requested explicitly: the default backend order can pick
        # GStreamer, which negotiates its own format and silently converts GREY
        # to BGR at a different resolution.
        cap = cv2.VideoCapture(self.device, cv2.CAP_V4L2)
        if cap.isOpened():
            return cap
        cap.release()

        # Some OpenCV builds only accept a numeric index for V4L2.
        if isinstance(self.device, str):
            match = _VIDEO_NODE_RE.match(self.device)
            if match:
                index = int(match.group(1))
                _LOG.debug("retrying %s as V4L2 index %d", self.device, index)
                cap = cv2.VideoCapture(index, cv2.CAP_V4L2)
                if cap.isOpened():
                    return cap
                cap.release()

        hint = ""
        if isinstance(self.device, str) and not os.path.exists(self.device):
            hint = " (device does not exist)"
        elif isinstance(self.device, str) and not os.access(self.device, os.R_OK):
            hint = " (no read permission; the user needs an ACL or the 'video' group)"
        raise CameraOpenError(f"cannot open camera {self.device!r}{hint}")

    def _configure(self, cap: cv2.VideoCapture) -> None:
        """Negotiate pixel format and frame size."""
        if self.ir_mode:
            # Ask for GREY explicitly. The IR sensor offers nothing else, but
            # without this OpenCV may request a format the driver then emulates.
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"GREY"))
            # With conversion off, read() hands back the native 2-D uint8 buffer
            # instead of an expanded 3-channel copy. Frames are normalised in
            # _to_gray() regardless, since drivers may ignore this.
            cap.set(cv2.CAP_PROP_CONVERT_RGB, 0)

        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)

        actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        if (actual_w, actual_h) != (self.width, self.height) and actual_w and actual_h:
            # Not fatal: detection scales to whatever it is given. Worth a log
            # line because it usually means the configured size is unsupported.
            _LOG.warning(
                "%s: requested %dx%d but device negotiated %dx%d",
                self.device, self.width, self.height, actual_w, actual_h,
            )

    def release(self) -> None:
        """Release the device.  Safe to call more than once."""
        cap, self._cap = self._cap, None
        if cap is not None:
            try:
                cap.release()
            except Exception as exc:  # noqa: BLE001 - never mask the real error
                _LOG.debug("error releasing %s: %s", self.device, exc)

    # `close` reads better at call sites that are not using the context manager.
    close = release

    def __enter__(self) -> "Camera":
        self.open()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.release()

    def __del__(self) -> None:
        # Safety net only; the emitter staying lit is a visible, annoying
        # symptom of a leaked handle, so do not rely on the caller alone.
        try:
            self.release()
        except Exception:  # pragma: no cover - interpreter shutdown
            pass

    def __repr__(self) -> str:
        state = "open" if self.is_open else "closed"
        return (
            f"<Camera {self.device!r} {self.width}x{self.height} "
            f"ir_mode={self.ir_mode} min_brightness={self.min_brightness} {state}>"
        )

    # -- streaming ---------------------------------------------------------

    def frames(self, timeout: float) -> Iterator[np.ndarray]:
        """Yield illuminated 2-D uint8 grayscale frames for up to *timeout* seconds.

        In IR mode, frames whose mean brightness is below ``min_brightness``
        are dropped: the emitter strobes, so roughly every other frame is dark
        and contains nothing to detect.

        The generator stops once the wall-clock deadline passes and never
        blocks indefinitely.  The deadline is checked before each read, so the
        final read may overrun it by at most one frame period (~67 ms at
        15 fps); it cannot overrun by more, because the driver always returns
        a frame or an error within that window.

        :raises CameraOpenError: the device is not open and cannot be opened.
        :raises CameraReadError: the device stopped delivering frames.
        """
        for frame in self._read_loop(timeout):
            gray = _to_gray(frame)
            mean = float(gray.mean())
            self._last_mean = mean

            if self.ir_mode and mean < self.min_brightness:
                self._dark_frames += 1
                continue

            self._lit_frames += 1
            yield gray

        if self.ir_mode and self._lit_frames == 0 and self._dark_frames > 0:
            # Every frame was below threshold: either the emitter is not firing
            # or min_frame_brightness is set too high for this module. Both are
            # invisible from the caller's "no face" result, so say it here.
            _LOG.warning(
                "%s: all %d frames were below min_frame_brightness=%.1f "
                "(brightest %.1f); the IR emitter may not be firing",
                self.device, self._dark_frames, self.min_brightness,
                self._last_mean if self._last_mean is not None else float("nan"),
            )

    def raw_frames(self, timeout: float) -> Iterator[np.ndarray]:
        """Yield every frame as 3-channel BGR, unfiltered, for up to *timeout* seconds.

        For the enrolment preview: the user needs to see the live feed to line
        their face up, and dropping the dark half of the strobe would make the
        preview flicker at 7.5 fps.  Do not use this for detection.
        """
        for frame in self._read_loop(timeout):
            if frame.ndim == 2:
                self._last_mean = float(frame.mean())
                yield cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
            elif frame.ndim == 3 and frame.shape[2] == 4:
                yield cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
            else:
                yield frame

    def _read_loop(self, timeout: float) -> Iterator[np.ndarray]:
        """Yield raw frames until the deadline, converting failures to errors."""
        # The deadline is taken *before* any lazy open so that the budget covers
        # opening the device too. Negotiating the format and getting the first
        # frame out of the IR sensor costs ~0.5 s, which is a real slice of an
        # 8 s authentication; charging it to the caller's timeout is what makes
        # "frames(t) returns within about t seconds" true unconditionally
        # (SAFETY rule 3). Opening inside a `with` block spends it up front
        # instead, leaving the whole budget for streaming.
        deadline = time.monotonic() + max(0.0, float(timeout))

        if self._cap is None:
            self.open()
        cap = self._cap
        assert cap is not None  # open() raises rather than returning without one

        failures = 0

        while time.monotonic() < deadline:
            ok, frame = cap.read()
            if not ok or frame is None or getattr(frame, "size", 0) == 0:
                failures += 1
                if failures >= _MAX_CONSECUTIVE_READ_FAILURES:
                    raise CameraReadError(
                        f"{self.device}: {failures} consecutive failed reads; "
                        f"the device was disconnected or reset"
                    )
                time.sleep(_READ_RETRY_SLEEP)
                continue

            failures = 0
            yield frame


def _to_gray(frame: np.ndarray) -> np.ndarray:
    """Normalise a captured frame to a 2-D uint8 array.

    The IR node delivers native GREY, but this also covers a driver that
    ignored ``CAP_PROP_CONVERT_RGB`` and a non-IR device used for testing, so
    downstream code can rely on the shape unconditionally.
    """
    if frame.ndim == 3:
        channels = frame.shape[2]
        if channels == 3:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        elif channels == 4:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2GRAY)
        elif channels == 1:
            frame = frame[:, :, 0]
        else:  # pragma: no cover - no V4L2 format produces this
            raise CameraReadError(f"unexpected frame with {channels} channels")

    if frame.dtype != np.uint8:
        # A 16-bit IR format (Y16) would otherwise break YuNet, which requires
        # 8-bit input. Scale rather than truncate so contrast is preserved.
        frame = cv2.normalize(frame, None, 0, 255, cv2.NORM_MINMAX, dtype=cv2.CV_8U)

    return frame
