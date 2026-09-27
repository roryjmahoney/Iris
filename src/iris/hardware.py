"""Hardware report: everything a GitHub hardware issue needs, and nothing more.

``iris hardware-report`` exists so that people on laptops nobody on the
project owns can say "does not work here" in a way that can actually be
acted on.  It gathers:

* the machine (DMI vendor/model), OS, kernel and library versions;
* every V4L2 node: capabilities, driver, USB vendor:product id, pixel
  formats and the frame sizes each format offers;
* which node ``camera.device`` (usually ``auto``) resolves to;
* a few seconds of per-frame *mean brightness* from that node, classified as
  a strobing, always-on or silent IR emitter.

Privacy is the constraint that shapes it.  The report is meant to be pasted
into a public issue, so it never captures, keeps or prints an image -- only
one number per frame -- and it never reads serial numbers, MAC addresses,
the user name or the hostname.  DMI fields are read from the world-readable
``/sys/class/dmi/id`` files only; ``product_serial`` and friends are root-only
and deliberately not touched.

Everything is best effort: a field that cannot be read is reported as
unknown, never as an exception, because the people running this are by
definition on hardware where something is already not working.
"""

from __future__ import annotations

import fcntl
import os
import platform
import struct
import sys
import time
from typing import Any, Final, Mapping

from . import __version__
from . import camera as cam

#: World-readable DMI files only.  Serial numbers and UUIDs are root-only on
#: Linux, and are left out on purpose even when running as root.
_DMI_ROOT: str = "/sys/class/dmi/id"
_DMI_FIELDS: Final[tuple[str, ...]] = ("sys_vendor", "product_name", "product_version", "board_name")
_OS_RELEASE: str = "/etc/os-release"

# struct v4l2_frmsizeenum: index pixel_format type union{discrete{w,h} |
# stepwise{min_w,max_w,step_w,min_h,max_h,step_h}} reserved[2]
_FRMSIZE_FMT: Final[str] = "<III6I2I"
_FRMSIZE_SIZE: Final[int] = struct.calcsize(_FRMSIZE_FMT)  # 44
_VIDIOC_ENUM_FRAMESIZES: Final[int] = cam._ioc(cam._IOC_READ | cam._IOC_WRITE, "V", 74, _FRMSIZE_SIZE)
_FRMSIZE_TYPE_DISCRETE: Final[int] = 1
_MAX_FRAME_SIZES: Final[int] = 32

#: Default length of the brightness sample.  Long enough for ~40 frames on a
#: 15 fps sensor, short enough that nobody gives up waiting.
DEFAULT_SAMPLE_SECONDS: Final[float] = 3.0

#: Frames shown in the lit/dark pattern string.
_PATTERN_FRAMES: Final[int] = 24


# --------------------------------------------------------------------------
# small readers
# --------------------------------------------------------------------------

def _read_text(path: str, limit: int = 256) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(limit).strip()
    except OSError:
        return ""


def system_info() -> dict[str, str]:
    """Machine, OS and version information.  Never serials or identities."""
    info = {field: _read_text(os.path.join(_DMI_ROOT, field)) for field in _DMI_FIELDS}

    pretty = ""
    for line in _read_text(_OS_RELEASE, 8192).splitlines():
        if line.startswith("PRETTY_NAME="):
            pretty = line.split("=", 1)[1].strip().strip('"')
            break
    info["os"] = pretty
    info["kernel"] = platform.release()
    info["session"] = " / ".join(
        v for v in (os.environ.get("XDG_CURRENT_DESKTOP", ""), os.environ.get("XDG_SESSION_TYPE", "")) if v
    )
    info["iris"] = __version__
    info["python"] = platform.python_version()
    try:
        import cv2

        info["opencv"] = str(cv2.__version__)
    except Exception:  # noqa: BLE001 - a broken OpenCV is itself worth reporting
        info["opencv"] = "not importable"
    return info


def _fourcc_code(fourcc: str) -> int:
    raw = fourcc.encode("ascii", "replace")[:4].ljust(4, b" ")
    return int.from_bytes(raw, "little")


def frame_sizes(path: str, fourcc: str) -> list[str]:
    """Frame sizes the node offers for *fourcc*, e.g. ``["640x360"]``.

    Discrete sizes are listed individually; a stepwise or continuous range is
    reported as ``"WxH-WxH"``.  Empty if the driver will not say.
    """
    sizes: list[str] = []
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return sizes
    try:
        for index in range(_MAX_FRAME_SIZES):
            buffer = bytearray(_FRMSIZE_SIZE)
            struct.pack_into("<II", buffer, 0, index, _fourcc_code(fourcc))
            try:
                fcntl.ioctl(fd, _VIDIOC_ENUM_FRAMESIZES, buffer)
            except OSError:
                break
            _i, _fmt, kind, a, b, c, d, e, _f, _r1, _r2 = struct.unpack(_FRMSIZE_FMT, buffer)
            if kind == _FRMSIZE_TYPE_DISCRETE:
                sizes.append(f"{a}x{b}")
            else:
                # stepwise/continuous: min_w, max_w, step_w, min_h, max_h, step_h
                sizes.append(f"{a}x{d}-{b}x{e}")
                break
    finally:
        os.close(fd)
    return sizes


def usb_ids(node: str) -> dict[str, str]:
    """USB vendor:product and product strings for ``videoN``, if it is USB.

    ``/sys/class/video4linux/videoN/device`` points at the USB *interface*;
    the ids live on the parent device, so walk up a few levels.
    """
    try:
        here = os.path.realpath(os.path.join(cam._SYSFS_ROOT, node, "device"))
    except OSError:
        return {}
    for _ in range(4):
        vendor = _read_text(os.path.join(here, "idVendor"))
        product = _read_text(os.path.join(here, "idProduct"))
        if vendor and product:
            return {
                "usb_id": f"{vendor}:{product}",
                "usb_product": _read_text(os.path.join(here, "product")),
                "usb_manufacturer": _read_text(os.path.join(here, "manufacturer")),
            }
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    return {}


def _driver(path: str) -> str:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return ""
    try:
        buffer = bytearray(cam._V4L2_CAPABILITY_SIZE)
        cam._ioctl(fd, cam._VIDIOC_QUERYCAP, buffer)
        driver = struct.unpack_from("<16s", buffer)[0]
    except OSError:
        return ""
    finally:
        os.close(fd)
    return driver.split(b"\0", 1)[0].decode("utf-8", "replace")


def describe_nodes() -> list[dict[str, Any]]:
    """Every V4L2 node with the details a hardware issue needs."""
    nodes: list[dict[str, Any]] = []
    try:
        cameras = cam.list_cameras()
    except OSError:
        return nodes
    for entry in cameras:
        path = str(entry["path"])
        node: dict[str, Any] = {
            "path": path,
            "name": str(entry.get("name", "")),
            "kind": "metadata" if entry.get("is_metadata") else ("infrared" if entry.get("is_ir") else "colour"),
            "driver": _driver(path),
            "formats": {
                str(fourcc): frame_sizes(path, str(fourcc)) for fourcc in entry.get("formats") or []
            },
        }
        node.update(usb_ids(os.path.basename(path)))
        nodes.append(node)
    return nodes


# --------------------------------------------------------------------------
# emitter sample
# --------------------------------------------------------------------------

def classify_brightness(means: list[float], threshold: float) -> str:
    """``strobing``, ``always-on``, ``not-firing`` or ``no-frames``."""
    if not means:
        return "no-frames"
    lit = sum(1 for m in means if m >= threshold)
    if lit == 0:
        return "not-firing"
    if lit == len(means):
        return "always-on"
    return "strobing"


def sample_emitter(cfg: Mapping[str, Any], device: str, seconds: float) -> dict[str, Any]:
    """Open *device* and record one mean-brightness number per raw frame.

    No frame is kept past computing its mean.  Failures become part of the
    report (``"error"``) rather than an exception.
    """
    camera_cfg = dict(cfg.get("camera") or {})
    threshold = float(camera_cfg.get("min_frame_brightness", 20.0))
    sample: dict[str, Any] = {"device": device, "seconds": seconds, "threshold": threshold}
    means: list[float] = []
    started = time.monotonic()
    shape = ""
    try:
        with cam.Camera(
            device,
            int(camera_cfg.get("width", 640)),
            int(camera_cfg.get("height", 360)),
            min_brightness=threshold,
            ir_mode=bool(camera_cfg.get("ir_mode", True)),
        ) as camera:
            for frame in camera.raw_frames(seconds):
                if not shape:
                    shape = f"{frame.shape[1]}x{frame.shape[0]}"
                means.append(round(float(frame.mean()), 1))
                del frame
    except cam.CameraError as exc:
        sample["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001 - the report must still print
        sample["error"] = f"{type(exc).__name__}: {exc}"

    elapsed = time.monotonic() - started
    lit = [m for m in means if m >= threshold]
    dark = [m for m in means if m < threshold]
    sample.update({
        "frames": len(means),
        "fps": round(len(means) / elapsed, 1) if elapsed > 0 and means else 0.0,
        "frame_size": shape,
        "emitter": classify_brightness(means, threshold),
        "lit_mean": round(sum(lit) / len(lit), 1) if lit else None,
        "dark_mean": round(sum(dark) / len(dark), 1) if dark else None,
        "min": min(means) if means else None,
        "max": max(means) if means else None,
        "pattern": "".join("L" if m >= threshold else "D" for m in means[:_PATTERN_FRAMES]),
    })
    return sample


# --------------------------------------------------------------------------
# the report
# --------------------------------------------------------------------------

def collect_report(
    cfg: Mapping[str, Any],
    *,
    device: str | None = None,
    capture_seconds: float = DEFAULT_SAMPLE_SECONDS,
) -> dict[str, Any]:
    """Build the whole report as plain data (see :func:`render_markdown`)."""
    configured = str(dict(cfg.get("camera") or {}).get("device", cam.AUTO_DEVICE))
    report: dict[str, Any] = {
        "system": system_info(),
        "nodes": describe_nodes(),
        "configured_device": configured,
    }

    target: str | None = device
    if target is None:
        try:
            resolved = cam.resolve_device(configured)
            target = str(resolved)
        except cam.CameraError as exc:
            report["resolve_error"] = str(exc)
    report["selected_device"] = target

    if target is not None and capture_seconds > 0:
        report["sample"] = sample_emitter(cfg, target, capture_seconds)
    report["verdict"] = verdict(report)
    return report


def verdict(report: Mapping[str, Any]) -> list[str]:
    """Plain-language findings, most important first."""
    notes: list[str] = []
    nodes = report.get("nodes") or []
    if not any(n.get("kind") == "infrared" for n in nodes):
        notes.append(
            "No infrared camera was recognised. If this laptop has one (Windows "
            "Hello), its name and formats above are what's needed to add support."
        )
    sample = report.get("sample")
    if sample is None:
        if report.get("resolve_error"):
            notes.append(str(report["resolve_error"]))
        return notes
    if sample.get("error"):
        notes.append(f"Opening {sample['device']} failed: {sample['error']}")
        return notes
    emitter = sample.get("emitter")
    if emitter == "strobing":
        notes.append("The IR emitter strobes (alternating lit and dark frames). This is the expected pattern.")
    elif emitter == "always-on":
        notes.append("The IR emitter stays on for every frame. This works; the brightness filter just has nothing to drop.")
    elif emitter == "not-firing":
        notes.append(
            "Every frame was dark: the IR emitter never turned on. Many laptops need "
            "it enabled first, e.g. with linux-enable-ir-emitter."
        )
    elif emitter == "no-frames":
        notes.append("The camera opened but sent no frames.")
    return notes


def _value(text: Any) -> str:
    text = "" if text is None else str(text)
    return text.replace("|", "/").replace("\n", " ") or "unknown"


def render_markdown(report: Mapping[str, Any]) -> str:
    """The report as Markdown, ready to paste into a GitHub issue."""
    system = report.get("system") or {}
    lines = [
        "### Iris hardware report",
        "",
        "| | |",
        "|---|---|",
        f"| Laptop | {_value(' '.join(v for v in (system.get('sys_vendor'), system.get('product_name')) if v))} |",
        f"| Model version | {_value(system.get('product_version'))} |",
        f"| OS | {_value(system.get('os'))} |",
        f"| Kernel | {_value(system.get('kernel'))} |",
        f"| Desktop | {_value(system.get('session'))} |",
        f"| Iris / Python / OpenCV | {_value(system.get('iris'))} / {_value(system.get('python'))} / "
        f"{_value(system.get('opencv'))} |",
        f"| camera.device | `{_value(report.get('configured_device'))}` → "
        f"`{_value(report.get('selected_device'))}` |",
        "",
        "#### Video devices",
        "",
        "| Device | Name | Type | USB id | Driver | Formats |",
        "|---|---|---|---|---|---|",
    ]
    for node in report.get("nodes") or []:
        formats = "; ".join(
            f"{fourcc} {', '.join(sizes) if sizes else '?'}" for fourcc, sizes in (node.get("formats") or {}).items()
        ) or "-"
        lines.append(
            f"| `{_value(node.get('path'))}` | {_value(node.get('name'))} | {_value(node.get('kind'))} | "
            f"{_value(node.get('usb_id'))} | {_value(node.get('driver'))} | {_value(formats)} |"
        )
    if not report.get("nodes"):
        lines.append("| none | | | | | |")

    sample = report.get("sample")
    lines += ["", "#### IR emitter sample", ""]
    if sample is None:
        lines.append("Not sampled.")
    elif sample.get("error"):
        lines.append(f"Could not open `{_value(sample.get('device'))}`: {_value(sample.get('error'))}")
    else:
        lines += [
            f"- Emitter: **{sample['emitter']}** ({sample['frames']} frames in {sample['seconds']:g} s, "
            f"{sample['fps']} fps, {_value(sample.get('frame_size'))})",
            f"- Brightness: lit mean {_value(sample.get('lit_mean'))}, dark mean {_value(sample.get('dark_mean'))}, "
            f"range {_value(sample.get('min'))}-{_value(sample.get('max'))}, threshold {sample['threshold']:g}",
            f"- Pattern (L = lit, D = dark): `{sample['pattern'] or '-'}`",
        ]

    findings = report.get("verdict") or []
    if findings:
        lines += ["", "#### Findings", ""]
        lines += [f"- {finding}" for finding in findings]

    lines += [
        "",
        "<sub>Generated by `iris hardware-report`. Contains no images, serial numbers, "
        "user names or host names.</sub>",
    ]
    return "\n".join(lines) + "\n"


__all__ = [
    "DEFAULT_SAMPLE_SECONDS",
    "classify_brightness",
    "collect_report",
    "describe_nodes",
    "frame_sizes",
    "render_markdown",
    "sample_emitter",
    "system_info",
    "usb_ids",
    "verdict",
]

if __name__ == "__main__":  # pragma: no cover
    sys.exit("use: iris hardware-report")
