"""iris hardware-report, and camera.device = "auto" across the CLI and daemon."""

from __future__ import annotations

import builtins
import io
import json
import re
import struct
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

import numpy as np

from iris import camera as cam
from iris import config as config_module
from iris import hardware

LG_NODES = [
    {"path": "/dev/video0", "name": "LGE Camera: LGE FHD Camera", "is_ir": False, "is_metadata": False,
     "formats": ["MJPG", "YUYV"]},
    {"path": "/dev/video1", "name": "LGE Camera: LGE FHD Camera", "is_ir": False, "is_metadata": True,
     "formats": []},
    {"path": "/dev/video2", "name": "LGE Camera: LGE IR-FHD Camera", "is_ir": True, "is_metadata": False,
     "formats": ["GREY"]},
]


class _FakeCamera:
    """Stands in for iris.camera.Camera: yields frames of scripted brightness."""

    levels: list[float] = []
    error: Exception | None = None
    opened: list[str] = []

    def __init__(self, device: str, width: int, height: int, **_kw: Any) -> None:
        self.device = device

    def __enter__(self) -> "_FakeCamera":
        if _FakeCamera.error is not None:
            raise _FakeCamera.error
        _FakeCamera.opened.append(self.device)
        return self

    def __exit__(self, *_a: object) -> None:
        return None

    def raw_frames(self, _seconds: float):
        for level in _FakeCamera.levels:
            yield np.full((36, 64, 3), level, dtype=np.uint8)


class _HardwareCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="iris-hw-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        _FakeCamera.levels = [1, 60, 2, 58] * 5
        _FakeCamera.error = None
        _FakeCamera.opened = []
        for patcher in (
            mock.patch.object(cam, "list_cameras", return_value=[dict(n) for n in LG_NODES]),
            mock.patch.object(cam, "Camera", _FakeCamera),
            mock.patch.object(hardware, "_driver", return_value="uvcvideo"),
            mock.patch.object(hardware, "frame_sizes", side_effect=lambda _p, f: {"GREY": ["640x360"], "MJPG": ["1920x1080", "1280x720"], "YUYV": ["640x480"]}.get(f, [])),
            mock.patch.object(hardware, "usb_ids", return_value={"usb_id": "04f2:b6d9", "usb_product": "LGE Camera", "usb_manufacturer": "Chicony"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)


class SystemInfoTests(unittest.TestCase):
    def test_reads_model_and_os_but_never_serials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dmi = Path(tmp) / "dmi"
            dmi.mkdir()
            for name, value in {"sys_vendor": "LG Electronics", "product_name": "17Z90Q",
                                "product_version": "0.1", "board_name": "17Z90Q",
                                "product_serial": "SECRET-SERIAL", "product_uuid": "SECRET-UUID"}.items():
                (dmi / name).write_text(value + "\n")
            os_release = Path(tmp) / "os-release"
            os_release.write_text('NAME="Ubuntu"\nPRETTY_NAME="Ubuntu 26.04 LTS"\n')

            opened: list[str] = []
            real_open = builtins.open

            def spy(path: Any, *args: Any, **kwargs: Any) -> Any:
                opened.append(str(path))
                return real_open(path, *args, **kwargs)

            with mock.patch.object(hardware, "_DMI_ROOT", str(dmi)), \
                    mock.patch.object(hardware, "_OS_RELEASE", str(os_release)), \
                    mock.patch("builtins.open", spy):
                info = hardware.system_info()

        self.assertEqual((info["sys_vendor"], info["product_name"]), ("LG Electronics", "17Z90Q"))
        self.assertEqual(info["os"], "Ubuntu 26.04 LTS")
        self.assertEqual(info["iris"], hardware.__version__)
        self.assertNotIn("SECRET", json.dumps(info))
        self.assertFalse(any("serial" in p or "uuid" in p for p in opened), opened)

    def test_missing_files_are_blank_not_errors(self) -> None:
        with mock.patch.object(hardware, "_DMI_ROOT", "/nonexistent"), \
                mock.patch.object(hardware, "_OS_RELEASE", "/nonexistent"):
            info = hardware.system_info()
        self.assertEqual((info["product_name"], info["os"]), ("", ""))


class SysfsTests(unittest.TestCase):
    def test_usb_ids_come_from_the_parent_device(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            usb = Path(tmp) / "devices" / "1-5"
            interface = usb / "1-5:1.2"
            interface.mkdir(parents=True)
            (usb / "idVendor").write_text("04f2\n")
            (usb / "idProduct").write_text("b6d9\n")
            (usb / "product").write_text("LGE Camera\n")
            node = Path(tmp) / "class" / "video2"
            node.mkdir(parents=True)
            (node / "device").symlink_to(interface)
            with mock.patch.object(cam, "_SYSFS_ROOT", str(Path(tmp) / "class")):
                ids = hardware.usb_ids("video2")
                self.assertEqual(hardware.usb_ids("video9"), {})
        self.assertEqual(ids["usb_id"], "04f2:b6d9")
        self.assertEqual(ids["usb_product"], "LGE Camera")

    def test_frame_size_enumeration(self) -> None:
        def fake_ioctl(_fd: int, request: int, buffer: bytearray) -> None:
            self.assertEqual(request, hardware._VIDIOC_ENUM_FRAMESIZES)
            index, fourcc = struct.unpack_from("<II", buffer)
            self.assertEqual(fourcc, int.from_bytes(b"GREY", "little"))
            sizes = [(640, 360), (340, 340)]
            if index >= len(sizes):
                raise OSError(22, "EINVAL")
            struct.pack_into("<III", buffer, 8, hardware._FRMSIZE_TYPE_DISCRETE, *sizes[index])

        with mock.patch.object(hardware.os, "open", return_value=99), \
                mock.patch.object(hardware.os, "close"), \
                mock.patch.object(hardware.fcntl, "ioctl", fake_ioctl):
            self.assertEqual(hardware.frame_sizes("/dev/video2", "GREY"), ["640x360", "340x340"])
        self.assertEqual(hardware._FRMSIZE_SIZE, 44)
        self.assertEqual(hardware._VIDIOC_ENUM_FRAMESIZES, 0xC02C564A)

    def test_stepwise_sizes_are_a_range(self) -> None:
        def fake_ioctl(_fd: int, _request: int, buffer: bytearray) -> None:
            struct.pack_into("<I6I", buffer, 8, 3, 160, 1280, 16, 120, 720, 8)

        with mock.patch.object(hardware.os, "open", return_value=99), \
                mock.patch.object(hardware.os, "close"), \
                mock.patch.object(hardware.fcntl, "ioctl", fake_ioctl):
            self.assertEqual(hardware.frame_sizes("/dev/video0", "YUYV"), ["160x120-1280x720"])


class EmitterTests(_HardwareCase):
    def test_classification(self) -> None:
        self.assertEqual(hardware.classify_brightness([1, 60, 2, 58], 20), "strobing")
        self.assertEqual(hardware.classify_brightness([60, 58], 20), "always-on")
        self.assertEqual(hardware.classify_brightness([1, 2], 20), "not-firing")
        self.assertEqual(hardware.classify_brightness([], 20), "no-frames")

    def test_sample_records_numbers_only(self) -> None:
        sample = hardware.sample_emitter({"camera": {"min_frame_brightness": 20.0}}, "/dev/video2", 1.0)
        self.assertEqual(sample["emitter"], "strobing")
        self.assertEqual(sample["frames"], 20)
        self.assertEqual((sample["lit_mean"], sample["dark_mean"]), (59.0, 1.5))
        self.assertEqual(sample["pattern"], "DL" * 10)
        self.assertEqual(sample["frame_size"], "64x36")
        self.assertTrue(all(isinstance(v, (str, int, float, type(None))) for v in sample.values()))

    def test_camera_failure_becomes_part_of_the_report(self) -> None:
        _FakeCamera.error = cam.CameraOpenError("cannot open camera '/dev/video2' (no read permission)")
        sample = hardware.sample_emitter({}, "/dev/video2", 1.0)
        self.assertIn("no read permission", sample["error"])
        self.assertEqual(sample["emitter"], "no-frames")


class ReportTests(_HardwareCase):
    def test_auto_resolves_and_samples_the_infrared_node(self) -> None:
        report = hardware.collect_report({"camera": {"device": "auto"}}, capture_seconds=1.0)
        self.assertEqual((report["configured_device"], report["selected_device"]), ("auto", "/dev/video2"))
        self.assertEqual(_FakeCamera.opened, ["/dev/video2"])
        kinds = {n["path"]: n["kind"] for n in report["nodes"]}
        self.assertEqual(kinds, {"/dev/video0": "colour", "/dev/video1": "metadata", "/dev/video2": "infrared"})
        self.assertEqual(report["nodes"][2]["formats"], {"GREY": ["640x360"]})
        self.assertIn("strobes", report["verdict"][0])

    def test_no_capture_never_opens_the_camera(self) -> None:
        report = hardware.collect_report({"camera": {"device": "auto"}}, capture_seconds=0)
        self.assertNotIn("sample", report)
        self.assertEqual(_FakeCamera.opened, [])

    def test_explicit_device_override(self) -> None:
        hardware.collect_report({"camera": {"device": "auto"}}, device="/dev/video0", capture_seconds=1.0)
        self.assertEqual(_FakeCamera.opened, ["/dev/video0"])

    def test_no_infrared_camera(self) -> None:
        with mock.patch.object(cam, "list_cameras", return_value=[dict(LG_NODES[0])]):
            report = hardware.collect_report({"camera": {"device": "auto"}}, capture_seconds=1.0)
        self.assertIsNone(report["selected_device"])
        self.assertEqual(_FakeCamera.opened, [])
        self.assertTrue(any("No infrared camera" in v for v in report["verdict"]))

    def test_silent_emitter_points_at_the_usual_fix(self) -> None:
        _FakeCamera.levels = [1.0] * 10
        report = hardware.collect_report({"camera": {}}, capture_seconds=1.0)
        self.assertTrue(any("linux-enable-ir-emitter" in v for v in report["verdict"]))

    def test_markdown(self) -> None:
        report = hardware.collect_report({"camera": {"device": "auto"}}, capture_seconds=1.0)
        report["system"]["product_name"] = "Model | with pipe"
        text = hardware.render_markdown(report)
        self.assertIn("### Iris hardware report", text)
        self.assertIn("`auto` → `/dev/video2`", text)
        self.assertIn("| `/dev/video2` | LGE Camera: LGE IR-FHD Camera | infrared | 04f2:b6d9 | uvcvideo | GREY 640x360 |", text)
        self.assertIn("Emitter: **strobing**", text)
        self.assertIn("Model / with pipe", text)  # table cells cannot be broken
        self.assertIn("no images", text)


class HardwareReportCliTests(_HardwareCase):
    def _run(self, *argv: str) -> tuple[int, str, str]:
        from iris import cli

        out, err = io.StringIO(), io.StringIO()
        from iris.cli.output import console

        with redirect_stdout(out), redirect_stderr(err), mock.patch.object(console, "err", err), \
                mock.patch("iris.cli.hardware.load_effective_config",
                           return_value=({"camera": {"device": "auto", "min_frame_brightness": 20.0}}, "test")):
            code = cli.main(["--color", "never", "hardware-report", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_markdown_on_stdout_guidance_on_stderr(self) -> None:
        code, out, err = self._run("--seconds", "1")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("### Iris hardware report"))
        self.assertNotIn("Paste", out)  # `> report.md` captures only the report
        self.assertIn("template=hardware_report.yml", err)

    def test_json(self) -> None:
        code, out, _err = self._run("--json", "--no-capture")
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["selected_device"], "/dev/video2")
        self.assertNotIn("sample", report)


class AutoDeviceAcrossIrisTests(unittest.TestCase):
    def test_default_is_auto(self) -> None:
        self.assertEqual(config_module.DEFAULTS["camera"]["device"], "auto")
        shipped = Path(__file__).resolve().parents[1] / "data" / "config.toml"
        with mock.patch.object(config_module._LOG, "disabled", True):
            self.assertEqual(config_module.load_config(shipped)["camera"]["device"], "auto")

    def test_cli_resolution(self) -> None:
        from iris.cli import settings

        with mock.patch.object(settings, "_safe_list_cameras", return_value=LG_NODES):
            self.assertEqual(settings.resolve_configured_device("auto"), "/dev/video2")
            self.assertEqual(settings.resolve_configured_device("/dev/video0"), "/dev/video0")
            self.assertEqual(settings.semantic_check("camera.device", "auto"), [])
        with mock.patch.object(settings, "_safe_list_cameras", return_value=LG_NODES[:2]):
            self.assertIsNone(settings.resolve_configured_device("auto"))
            [warning] = settings.semantic_check("camera.device", "auto")
            self.assertIn("no infrared camera", warning)

    def test_doctor_without_an_infrared_camera_points_at_the_report(self) -> None:
        from iris.cli import doctor

        cfg = {"camera": {"device": "auto", "min_frame_brightness": 20.0, "width": 640, "height": 360}}
        with mock.patch.object(doctor, "resolve_configured_device", return_value=None):
            camera_check, strobe_check = doctor.check_camera(cfg, 1.0)
        self.assertEqual(camera_check.status, "fail")
        self.assertIn("hardware-report", camera_check.hint)
        self.assertEqual(strobe_check.status, "warn")

    def test_cameras_marks_what_auto_picks(self) -> None:
        from iris import cli
        from iris.cli import cameras

        from iris.cli.output import console

        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()), \
                mock.patch.object(console, "stream", out), mock.patch.object(console, "err", io.StringIO()), \
                mock.patch.object(cameras, "_collect_cameras", return_value=(LG_NODES, "local")), \
                mock.patch.object(cameras, "load_effective_config",
                                  return_value=({"camera": {"device": "auto"}}, "test")), \
                mock.patch.object(cameras, "resolve_configured_device", return_value="/dev/video2"):
            self.assertEqual(cli.main(["--color", "never", "cameras"]), 0)
        text = out.getvalue()
        marked = [line for line in text.splitlines() if re.match(r"\s*\*\s+/dev/", line)]
        self.assertEqual(len(marked), 1, text)
        self.assertIn("/dev/video2", marked[0])
        self.assertIn("camera.device = auto", text)

    def test_daemon_accepts_auto_without_probing_devices(self) -> None:
        from iris import daemon

        with mock.patch.object(daemon, "list_cameras", side_effect=AssertionError("probed")):
            self.assertEqual(daemon.IrisDaemon._validate_config({"camera": {"device": "auto"}}), [])


if __name__ == "__main__":
    unittest.main()
