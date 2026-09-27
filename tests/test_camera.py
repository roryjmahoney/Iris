from __future__ import annotations

import struct
import unittest
from typing import Any
from unittest import mock

import cv2
import numpy as np

from iris import camera
from iris.camera import Camera, CameraOpenError, CameraReadError

CAPTURE_CAPS = 0x04200001
META_CAPS = 0x04A00000
PHYSICAL_UNION = 0x84A00001  # `capabilities`: union over every node, meta bit set


class FakeClock:
    """Deterministic stand-in for time.monotonic/time.sleep."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeCapture:
    """cv2.VideoCapture replacement driven by a script of read() results.

    Each read advances the fake clock by one 15fps frame period.
    """

    instances: list["FakeCapture"] = []
    opens: dict[Any, bool] = {}
    script: list[Any] = []
    negotiated: tuple[int, int] | None = None
    clock: FakeClock

    def __init__(self, device: Any, backend: int) -> None:
        self.device = device
        self.backend = backend
        self.props: dict[int, float] = {}
        self.released = False
        self.reads = 0
        FakeCapture.instances.append(self)

    def isOpened(self) -> bool:
        return FakeCapture.opens.get(self.device, True)

    def set(self, prop: int, value: float) -> bool:
        self.props[prop] = value
        return True

    def get(self, prop: int) -> float:
        if FakeCapture.negotiated and prop == cv2.CAP_PROP_FRAME_WIDTH:
            return FakeCapture.negotiated[0]
        if FakeCapture.negotiated and prop == cv2.CAP_PROP_FRAME_HEIGHT:
            return FakeCapture.negotiated[1]
        return self.props.get(prop, 0.0)

    def read(self) -> tuple[bool, np.ndarray | None]:
        FakeCapture.clock.now += 1 / 15
        self.reads += 1
        if not FakeCapture.script:
            return False, None
        item = FakeCapture.script.pop(0)
        if item is None:
            return False, None
        return True, item

    def release(self) -> None:
        self.released = True


def _gray(level: float, shape: tuple[int, ...] = (36, 64)) -> np.ndarray:
    return np.full(shape, level, dtype=np.uint8)


class _CaptureCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        FakeCapture.instances = []
        FakeCapture.opens = {}
        FakeCapture.script = []
        FakeCapture.negotiated = None
        FakeCapture.clock = self.clock
        for patcher in (
            mock.patch.object(camera.cv2, "VideoCapture", FakeCapture),
            mock.patch.object(camera.time, "monotonic", self.clock.monotonic),
            mock.patch.object(camera.time, "sleep", self.clock.sleep),
            # Path devices are classified via QUERYCAP; default to a capture node.
            mock.patch.object(camera, "_is_metadata_node", return_value=False),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _camera(self, **kwargs: Any) -> Camera:
        cam = Camera(kwargs.pop("device", "/dev/video2"), 64, 36, **kwargs)
        self.addCleanup(cam.release)
        return cam


class StrobeFilteringTests(_CaptureCase):
    def test_dark_strobe_frames_are_dropped(self) -> None:
        FakeCapture.script = [_gray(1), _gray(55), _gray(2), _gray(60), _gray(1)]
        cam = self._camera()

        frames = list(cam.frames(timeout=1.0))

        self.assertEqual([int(f.mean()) for f in frames], [55, 60])
        self.assertEqual(cam.frame_stats, {"lit": 2, "dark": 3})
        for frame in frames:
            self.assertEqual((frame.ndim, frame.dtype), (2, np.uint8))

    def test_threshold_is_exclusive_below_min_brightness(self) -> None:
        FakeCapture.script = [_gray(19), _gray(20), _gray(21)]
        frames = list(self._camera(min_brightness=20).frames(timeout=0.5))
        self.assertEqual([int(f.mean()) for f in frames], [20, 21])

    def test_non_ir_mode_keeps_every_frame(self) -> None:
        FakeCapture.script = [_gray(1), _gray(60)]
        cam = self._camera(ir_mode=False)
        self.assertEqual(len(list(cam.frames(timeout=0.5))), 2)
        self.assertEqual(cam.frame_stats, {"lit": 2, "dark": 0})

    def test_emitter_not_firing_is_logged(self) -> None:
        FakeCapture.script = [_gray(1), _gray(2), _gray(3)]
        cam = self._camera()
        with self.assertLogs("iris.camera", "WARNING") as logs:
            self.assertEqual(list(cam.frames(timeout=0.25)), [])
        self.assertIn("emitter may not be firing", logs.output[0])
        self.assertEqual(cam.last_frame_mean, 3.0)

    def test_colour_and_wide_frames_are_normalised_to_gray_uint8(self) -> None:
        bgr = cv2.cvtColor(_gray(80), cv2.COLOR_GRAY2BGR)
        bgra = cv2.cvtColor(_gray(90), cv2.COLOR_GRAY2BGRA)
        single = _gray(70)[:, :, None]
        y16 = np.tile(np.linspace(0, 65535, 64), (36, 1)).astype(np.uint16)
        FakeCapture.script = [bgr, bgra, single, y16]

        frames = list(self._camera(ir_mode=False).frames(timeout=0.3))

        self.assertEqual(len(frames), 4)
        for frame in frames:
            self.assertEqual((frame.shape, frame.dtype), ((36, 64), np.uint8))
        self.assertEqual([int(f.mean()) for f in frames[:3]], [80, 90, 70])
        # 16-bit IR is scaled into the full 8-bit range, not truncated: the
        # left-to-right ramp must stay monotonic from black to white.
        row = frames[3][0].astype(int)
        self.assertEqual((row[0], row[-1]), (0, 255))
        self.assertTrue(np.all(np.diff(row) >= 0))


class DeadlineAndFailureTests(_CaptureCase):
    def test_frames_stop_at_the_deadline(self) -> None:
        FakeCapture.script = [_gray(60)] * 1000
        start = self.clock.now

        frames = list(self._camera().frames(timeout=2.0))

        # Deadline is checked before each read, so at most one frame overruns.
        self.assertLessEqual(self.clock.now - start, 2.0 + 1 / 15 + 1e-9)
        self.assertEqual(len(frames), 30)

    def test_zero_or_negative_timeout_reads_nothing(self) -> None:
        FakeCapture.script = [_gray(60)]
        for timeout in (0.0, -5.0):
            with self.subTest(timeout=timeout):
                cam = self._camera()
                self.assertEqual(list(cam.frames(timeout=timeout)), [])
        self.assertEqual(sum(c.reads for c in FakeCapture.instances), 0)

    def test_lazy_open_is_charged_to_the_timeout(self) -> None:
        FakeCapture.script = [_gray(60)] * 100
        cam = self._camera()
        real_open = cam.open

        def slow_open() -> None:
            self.clock.now += 0.5  # format negotiation on real hardware
            real_open()

        with mock.patch.object(cam, "open", slow_open):
            start = self.clock.now
            list(cam.frames(timeout=1.0))
        self.assertLessEqual(self.clock.now - start, 1.0 + 1 / 15 + 1e-9)

    def test_sustained_read_failure_raises(self) -> None:
        FakeCapture.script = [None] * 100
        with self.assertRaises(CameraReadError):
            list(self._camera().frames(timeout=8.0))
        self.assertEqual(FakeCapture.instances[0].reads, camera._MAX_CONSECUTIVE_READ_FAILURES)

    def test_empty_frames_count_as_failures(self) -> None:
        FakeCapture.script = [np.empty((0, 0), np.uint8)] * 100
        with self.assertRaises(CameraReadError):
            list(self._camera().frames(timeout=8.0))

    def test_intermittent_failures_reset_the_counter(self) -> None:
        limit = camera._MAX_CONSECUTIVE_READ_FAILURES
        FakeCapture.script = ([None] * (limit - 1) + [_gray(60)]) * 3
        frames = list(self._camera().frames(timeout=8.0))
        self.assertEqual(len(frames), 3)

    def test_failed_reads_back_off_instead_of_spinning(self) -> None:
        FakeCapture.script = [None] * 5 + [_gray(60)]
        with mock.patch.object(camera.time, "sleep", wraps=self.clock.sleep) as sleep:
            list(self._camera().frames(timeout=0.8))
        self.assertGreaterEqual(sleep.call_count, 5)
        sleep.assert_called_with(camera._READ_RETRY_SLEEP)


class OpenAndConfigureTests(_CaptureCase):
    def test_ir_mode_requests_native_grey_at_the_configured_size(self) -> None:
        cam = self._camera()
        cam.open()

        cap = FakeCapture.instances[0]
        self.assertEqual(cap.backend, cv2.CAP_V4L2)
        self.assertEqual(cap.props[cv2.CAP_PROP_FOURCC], cv2.VideoWriter_fourcc(*"GREY"))
        self.assertEqual(cap.props[cv2.CAP_PROP_CONVERT_RGB], 0)
        self.assertEqual(cap.props[cv2.CAP_PROP_FRAME_WIDTH], 64)
        self.assertEqual(cap.props[cv2.CAP_PROP_FRAME_HEIGHT], 36)

    def test_colour_mode_leaves_the_format_alone(self) -> None:
        self._camera(ir_mode=False).open()
        cap = FakeCapture.instances[0]
        self.assertNotIn(cv2.CAP_PROP_FOURCC, cap.props)
        self.assertNotIn(cv2.CAP_PROP_CONVERT_RGB, cap.props)

    def test_negotiated_size_mismatch_is_logged_not_fatal(self) -> None:
        FakeCapture.negotiated = (640, 480)
        cam = self._camera()
        with self.assertLogs("iris.camera", "WARNING"):
            cam.open()
        self.assertTrue(cam.is_open)

    def test_path_falls_back_to_numeric_index(self) -> None:
        FakeCapture.opens = {"/dev/video2": False}
        cam = self._camera()
        cam.open()
        self.assertEqual([c.device for c in FakeCapture.instances], ["/dev/video2", 2])
        self.assertTrue(FakeCapture.instances[0].released)
        self.assertFalse(FakeCapture.instances[1].released)

    def test_unopenable_device_raises_with_a_hint(self) -> None:
        FakeCapture.opens = {"/dev/video7": False, 7: False}
        cam = self._camera(device="/dev/video7")
        with mock.patch.object(camera.os.path, "exists", return_value=False):
            with self.assertRaisesRegex(CameraOpenError, "does not exist"):
                cam.open()
        self.assertTrue(all(c.released for c in FakeCapture.instances))
        self.assertFalse(cam.is_open)

    def test_permission_hint(self) -> None:
        FakeCapture.opens = {"/dev/video7": False, 7: False}
        cam = self._camera(device="/dev/video7")
        with mock.patch.object(camera.os.path, "exists", return_value=True), \
                mock.patch.object(camera.os, "access", return_value=False):
            with self.assertRaisesRegex(CameraOpenError, "no read permission"):
                cam.open()

    def test_metadata_node_is_refused_before_opening(self) -> None:
        with mock.patch.object(camera, "_is_metadata_node", return_value=True):
            with self.assertRaisesRegex(CameraOpenError, "metadata node"):
                self._camera(device="/dev/video3").open()
        self.assertEqual(FakeCapture.instances, [])

    def test_unclassifiable_node_is_left_to_opencv(self) -> None:
        with mock.patch.object(camera, "_is_metadata_node", return_value=None):
            cam = self._camera()
            cam.open()
        self.assertTrue(cam.is_open)

    def test_configure_failure_releases_the_capture(self) -> None:
        cam = self._camera()
        with mock.patch.object(cam, "_configure", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                cam.open()
        self.assertTrue(FakeCapture.instances[0].released)
        self.assertFalse(cam.is_open)

    def test_open_is_idempotent_and_release_is_safe_twice(self) -> None:
        cam = self._camera()
        cam.open()
        cam.open()
        self.assertEqual(len(FakeCapture.instances), 1)
        cam.release()
        cam.close()
        self.assertTrue(FakeCapture.instances[0].released)
        self.assertFalse(cam.is_open)

    def test_context_manager_releases_even_on_error(self) -> None:
        with self.assertRaises(ValueError):
            with self._camera() as cam:
                self.assertTrue(cam.is_open)
                raise ValueError
        self.assertTrue(FakeCapture.instances[0].released)

    def test_from_config(self) -> None:
        cam = Camera.from_config({
            "camera": {
                "device": "/dev/video4",
                "width": 320,
                "height": 240,
                "min_frame_brightness": 33.0,
                "ir_mode": False,
            }
        })
        self.assertEqual(
            (cam.device, cam.width, cam.height, cam.min_brightness, cam.ir_mode),
            ("/dev/video4", 320, 240, 33.0, False),
        )
        default = Camera.from_config({})
        self.assertEqual((default.device, default.ir_mode), ("/dev/video2", True))


class RawFramesTests(_CaptureCase):
    def test_raw_frames_keep_dark_frames_as_bgr(self) -> None:
        FakeCapture.script = [
            _gray(1),
            _gray(60),
            cv2.cvtColor(_gray(70), cv2.COLOR_GRAY2BGRA),
            cv2.cvtColor(_gray(80), cv2.COLOR_GRAY2BGR),
        ]
        cam = self._camera()
        frames = list(cam.raw_frames(timeout=0.3))

        self.assertEqual(len(frames), 4)
        for frame in frames:
            self.assertEqual(frame.shape, (36, 64, 3))
        self.assertEqual([int(f.mean()) for f in frames], [1, 60, 70, 80])
        self.assertEqual(cam.last_frame_mean, 60.0)


def _capability(caps: int, device_caps: int, card: bytes) -> bytes:
    return struct.pack(
        camera._V4L2_CAPABILITY_FMT, b"uvcvideo", card, b"usb-0000:00:14.0-5", 0,
        caps, device_caps, b"",
    )


class QueryCapabilitiesTests(unittest.TestCase):
    def _query(self, payload: bytes) -> tuple[int, str]:
        def fill(_fd: int, request: int, buffer: bytearray) -> None:
            self.assertEqual(request, camera._VIDIOC_QUERYCAP)
            buffer[:] = payload

        with mock.patch.object(camera.os, "open", return_value=99), \
                mock.patch.object(camera.os, "close") as close, \
                mock.patch.object(camera, "_ioctl", fill):
            result = camera._query_capabilities("/dev/video2")
        close.assert_called_once_with(99)
        return result

    def test_ioctl_numbers_match_the_kernel_headers(self) -> None:
        self.assertEqual(camera._V4L2_CAPABILITY_SIZE, 104)
        self.assertEqual(camera._V4L2_FMTDESC_SIZE, 64)
        self.assertEqual(camera._VIDIOC_QUERYCAP, 0x80685600)
        self.assertEqual(camera._VIDIOC_ENUM_FMT, 0xC0405602)

    def test_device_caps_are_used_when_advertised(self) -> None:
        # `capabilities` has the metadata bit on every node of the device; only
        # device_caps tells the IR capture node from its metadata sibling.
        caps, name = self._query(
            _capability(PHYSICAL_UNION, CAPTURE_CAPS, b"LGE Camera: LGE IR-FHD Camera\0junk")
        )
        self.assertEqual((caps, name), (CAPTURE_CAPS, "LGE Camera: LGE IR-FHD Camera"))

    def test_legacy_driver_without_device_caps(self) -> None:
        caps, _ = self._query(_capability(0x04000001, 0xDEAD, b"old"))
        self.assertEqual(caps, 0x04000001)

    def test_close_happens_even_if_the_ioctl_fails(self) -> None:
        with mock.patch.object(camera.os, "open", return_value=99), \
                mock.patch.object(camera.os, "close") as close, \
                mock.patch.object(camera, "_ioctl", side_effect=OSError(25, "ENOTTY")):
            with self.assertRaises(OSError):
                camera._query_capabilities("/dev/video2")
        close.assert_called_once_with(99)


class EnumerateFormatsTests(unittest.TestCase):
    def _enumerate(self, fourccs: list[str]) -> list[str]:
        def fill(_fd: int, request: int, buffer: bytearray) -> None:
            self.assertEqual(request, camera._VIDIOC_ENUM_FMT)
            index, buf_type = struct.unpack_from("<II", buffer)
            self.assertEqual(buf_type, camera._V4L2_BUF_TYPE_VIDEO_CAPTURE)
            if index >= len(fourccs):
                raise OSError(22, "EINVAL")
            code = fourccs[index].ljust(4, " ").encode()
            struct.pack_into("<I", buffer, 44, int.from_bytes(code, "little"))

        with mock.patch.object(camera.os, "open", return_value=99), \
                mock.patch.object(camera.os, "close"), \
                mock.patch.object(camera, "_ioctl", fill):
            return camera._enumerate_formats("/dev/video0")

    def test_formats_are_decoded_until_einval(self) -> None:
        self.assertEqual(self._enumerate(["MJPG", "YUYV"]), ["MJPG", "YUYV"])
        self.assertEqual(self._enumerate(["Y8"]), ["Y8"])
        self.assertEqual(self._enumerate([]), [])

    def test_enumeration_is_bounded(self) -> None:
        self.assertEqual(len(self._enumerate(["GREY"] * 500)), camera._MAX_FORMATS)

    def test_unopenable_node_has_no_formats(self) -> None:
        with mock.patch.object(camera.os, "open", side_effect=PermissionError(13, "EACCES")):
            self.assertEqual(camera._enumerate_formats("/dev/video0"), [])


class ListCamerasTests(unittest.TestCase):
    """The observed four-node LG laptop, plus edge cases."""

    NODES: dict[str, dict[str, Any]] = {
        "video0": {"name": "LGE Camera: LGE FHD Camera", "caps": CAPTURE_CAPS,
                   "formats": ["MJPG", "YUYV"]},
        "video1": {"name": "LGE Camera: LGE FHD Camera", "caps": META_CAPS, "formats": []},
        "video2": {"name": "LGE Camera: LGE IR-FHD Camera", "caps": CAPTURE_CAPS,
                   "formats": ["GREY"]},
        "video3": {"name": "LGE Camera: LGE IR-FHD Camera", "caps": META_CAPS, "formats": []},
    }

    def _list(self, nodes: dict[str, dict[str, Any]], missing: tuple[str, ...] = ()) -> list:
        root = camera._SYSFS_ROOT

        def query(path: str) -> tuple[int, str]:
            node = nodes[path.rsplit("/", 1)[1]]
            if node.get("error"):
                raise OSError(5, "EIO")
            return node["caps"], node.get("card", "")

        enumerate_formats = mock.Mock(
            side_effect=lambda path: list(nodes[path.rsplit("/", 1)[1]]["formats"])
        )
        with mock.patch.object(camera.glob, "glob",
                               return_value=[f"{root}/{n}" for n in reversed(list(nodes))]), \
                mock.patch.object(camera.os.path, "exists",
                                  side_effect=lambda p: p.rsplit("/", 1)[1] not in missing), \
                mock.patch.object(camera, "_read_sysfs_name",
                                  side_effect=lambda d: nodes[d.rsplit("/", 1)[1]]["name"]), \
                mock.patch.object(camera, "_query_capabilities", side_effect=query), \
                mock.patch.object(camera, "_enumerate_formats", enumerate_formats):
            result = camera.list_cameras()
            self.format_queries = [c.args[0] for c in enumerate_formats.call_args_list]
            return result

    def test_real_laptop_layout(self) -> None:
        cams = self._list(self.NODES)

        self.assertEqual([c["path"] for c in cams], [f"/dev/video{i}" for i in range(4)])
        self.assertEqual([c["is_metadata"] for c in cams], [False, True, False, True])
        self.assertEqual([c["is_ir"] for c in cams], [False, False, True, False])
        self.assertEqual(cams[2]["formats"], ["GREY"])
        # Metadata nodes are never opened to enumerate formats.
        self.assertEqual(self.format_queries, ["/dev/video0", "/dev/video2"])

    def test_default_ir_camera_picks_the_capture_node(self) -> None:
        with mock.patch.object(camera, "list_cameras", return_value=self._list(self.NODES)):
            self.assertEqual(camera.default_ir_camera()["path"], "/dev/video2")
        with mock.patch.object(camera, "list_cameras", return_value=[]):
            self.assertIsNone(camera.default_ir_camera())

    def test_ir_detection_heuristics(self) -> None:
        cases = {
            "Integrated Camera": (["GREY"], True),          # mono-only formats
            "Integrated Camera ": (["GREY", "YUYV"], False),  # has a colour format
            "Infrared Camera": (["MJPG"], True),
            "IR Camera": (["MJPG"], True),
            "IRIS Webcam": (["MJPG"], False),                # not a standalone token
            "Kirin ir cam": (["MJPG"], False),               # lowercase "ir"
            "Nothing": ([], False),
        }
        nodes = {
            f"video{i}": {"name": name, "caps": CAPTURE_CAPS, "formats": formats}
            for i, (name, (formats, _)) in enumerate(cases.items())
        }
        cams = self._list(nodes)
        for cam, (name, (_, expected)) in zip(cams, cases.items()):
            with self.subTest(name=name):
                self.assertEqual(cam["is_ir"], expected)

    def test_node_without_video_capture_counts_as_metadata(self) -> None:
        cams = self._list({"video0": {"name": "IR out", "caps": 0x04000002, "formats": []}})
        self.assertEqual((cams[0]["is_metadata"], cams[0]["is_ir"]), (True, False))

    def test_numeric_sort_and_skipped_nodes(self) -> None:
        nodes = {
            "video10": {"name": "ten", "caps": CAPTURE_CAPS, "formats": ["YUYV"]},
            "video9": {"name": "nine", "caps": CAPTURE_CAPS, "formats": ["YUYV"]},
            "video2": {"name": "broken", "error": True, "formats": []},
            "video1": {"name": "gone", "caps": CAPTURE_CAPS, "formats": []},
        }
        with self.assertLogs("iris.camera", "WARNING"):
            cams = self._list(nodes, missing=("video1",))
        self.assertEqual([c["path"] for c in cams], ["/dev/video9", "/dev/video10"])

    def test_name_falls_back_to_card_then_node(self) -> None:
        nodes = {
            "video0": {"name": "", "card": "Card Name", "caps": CAPTURE_CAPS, "formats": []},
            "video1": {"name": "", "caps": CAPTURE_CAPS, "formats": []},
        }
        self.assertEqual([c["name"] for c in self._list(nodes)], ["Card Name", "video1"])


class MetadataNodeTests(unittest.TestCase):
    def test_classification(self) -> None:
        with mock.patch.object(camera, "_query_capabilities", return_value=(META_CAPS, "")):
            self.assertTrue(camera._is_metadata_node("/dev/video1"))
        with mock.patch.object(camera, "_query_capabilities", return_value=(CAPTURE_CAPS, "")):
            self.assertFalse(camera._is_metadata_node("/dev/video2"))
        with mock.patch.object(camera, "_query_capabilities", side_effect=OSError(2, "ENOENT")):
            self.assertIsNone(camera._is_metadata_node("/dev/video9"))


if __name__ == "__main__":
    unittest.main()
