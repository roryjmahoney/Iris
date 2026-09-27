from __future__ import annotations

import unittest

import cv2
import numpy as np

from iris import daemon
from iris.liveness import REASONS, LivenessChecker

HEIGHT, WIDTH = 240, 320
# A YuNet-shaped row: x, y, w, h followed by landmarks and a score that the
# checker must ignore.  The measured region is the central 70% of this box.
FACE_ROW = np.array([100, 60, 120, 120] + [0.0] * 10 + [0.9], dtype=np.float32)
REGION = (slice(78, 162), slice(118, 202))


def _checker(**liveness: object) -> LivenessChecker:
    return LivenessChecker({"liveness": liveness, "camera": {}})


def _scene(seed: int, face: np.ndarray | None = None, background: int = 60) -> np.ndarray:
    """A lit IR frame: noisy background plus whatever occupies the face box."""
    rng = np.random.default_rng(seed)
    frame = np.clip(rng.normal(background, 4, (HEIGHT, WIDTH)), 0, 255).astype(np.uint8)
    region = frame[REGION]
    if face is None:
        face = rng.normal(130, 30, region.shape)
    frame[REGION] = np.clip(face, 0, 255).astype(np.uint8)
    return frame


def _live(seed: int) -> np.ndarray:
    """A bright, textured, IR-reflective face: every test should pass."""
    return _scene(seed)


class LiveFaceTests(unittest.TestCase):
    def test_real_face_passes_and_builds_confidence(self) -> None:
        checker = _checker()
        self.assertFalse(checker.is_confident())

        self.assertEqual(checker.check(_live(1), FACE_ROW), (True, "live"))
        self.assertFalse(checker.is_confident())
        self.assertEqual(checker.check(_live(2), FACE_ROW), (True, "live"))

        self.assertTrue(checker.is_confident())
        self.assertEqual(checker.streak, 2)
        self.assertEqual(checker.frames_checked, 2)

    def test_min_consecutive_is_configurable_and_floored_at_one(self) -> None:
        checker = _checker(min_consecutive=3)
        for seed in range(2):
            checker.check(_live(seed), FACE_ROW)
        self.assertFalse(checker.is_confident())
        checker.check(_live(9), FACE_ROW)
        self.assertTrue(checker.is_confident())

        single = _checker(min_consecutive=0)
        single.check(_live(1), FACE_ROW)
        self.assertTrue(single.is_confident())

    def test_one_spoof_frame_breaks_the_streak(self) -> None:
        # An attacker must not be able to interleave spoof frames with real
        # ones and still accumulate confidence.
        checker = _checker()
        checker.check(_live(1), FACE_ROW)
        checker.check(_live(2), FACE_ROW)
        self.assertTrue(checker.is_confident())

        dark_face = _scene(3, face=np.full((84, 84), 10.0))
        self.assertFalse(checker.check(dark_face, FACE_ROW)[0])
        self.assertEqual(checker.streak, 0)
        self.assertFalse(checker.is_confident())

        checker.check(_live(4), FACE_ROW)
        self.assertFalse(checker.is_confident())

    def test_reset_clears_confidence_from_a_previous_attempt(self) -> None:
        checker = _checker()
        checker.check(_live(1), FACE_ROW)
        checker.check(_live(2), FACE_ROW)
        checker.reset()

        self.assertFalse(checker.is_confident())
        self.assertEqual(checker.frames_checked, 0)
        self.assertEqual(checker.last_reason, "live")
        # History was cleared too, so a frame identical to the last one before
        # the reset is not treated as a duplicate.
        self.assertEqual(checker.check(_live(2), FACE_ROW), (True, "live"))

    def test_disabled_checker_passes_everything_but_says_so(self) -> None:
        checker = _checker(enabled=False)
        black = np.zeros((HEIGHT, WIDTH), dtype=np.uint8)

        self.assertEqual(checker.check(black, None), (True, "disabled"))
        self.assertEqual(checker.last_reason, "disabled")
        self.assertTrue(checker.is_confident())


class SpoofRejectionTests(unittest.TestCase):
    def assertRejects(self, frame: np.ndarray, reason: str, row=FACE_ROW, **cfg) -> None:
        checker = _checker(**cfg)
        self.assertEqual(checker.check(frame, row), (False, reason))
        self.assertEqual(checker.last_reason, reason)
        self.assertEqual(checker.streak, 0)
        self.assertIn(reason, REASONS)

    def test_dark_strobe_frame(self) -> None:
        self.assertRejects(_scene(1, background=5, face=np.full((84, 84), 5.0)), "dark_frame")

    def test_phone_screen_returns_no_infrared(self) -> None:
        # A screen emits no 850nm light: the "face" is textured but near black.
        rng = np.random.default_rng(1)
        self.assertRejects(_scene(1, face=rng.normal(12, 5, (84, 84))), "no_ir_return")

    def test_face_much_darker_than_the_scene_is_rejected_relatively(self) -> None:
        # Above the absolute floor, but far darker than a brightly lit room.
        rng = np.random.default_rng(1)
        frame = _scene(1, face=rng.normal(40, 15, (84, 84)), background=150)
        self.assertRejects(frame, "no_ir_return")

    def test_specular_glare_from_glass(self) -> None:
        face = np.random.default_rng(1).normal(130, 30, (84, 84))
        face[:, :30] = 255  # a blown-out hotspot over ~36% of the region
        self.assertRejects(_scene(1, face=face), "saturated")

    def test_uniform_surface_like_paper(self) -> None:
        face = np.random.default_rng(1).normal(120, 2, (84, 84))
        self.assertRejects(_scene(1, face=face), "low_contrast")

    def test_smooth_gradient_has_range_but_no_texture(self) -> None:
        # A photo of a face has tonal range without real surface relief; a
        # linear ramp is the extreme case -- high std, zero Laplacian.
        ramp = np.tile(np.linspace(80, 180, 84), (84, 1))
        self.assertRejects(_scene(1, face=ramp), "flat_region")

    def test_repeated_buffer_is_static_input(self) -> None:
        checker = _checker()
        frame = _live(1)
        self.assertEqual(checker.check(frame, FACE_ROW), (True, "live"))
        self.assertEqual(checker.check(frame.copy(), FACE_ROW), (False, "static_input"))
        self.assertFalse(checker.is_confident())

    def test_glare_is_reported_before_missing_texture(self) -> None:
        # A smooth ramp that clips to white is both glare and textureless; the
        # documented order reports glare, which gives the user the right hint.
        ramp = np.tile(np.linspace(150, 300, 84), (84, 1))
        self.assertRejects(_scene(1, face=ramp), "saturated")

    def test_thresholds_come_from_config(self) -> None:
        frame = _live(1)
        self.assertRejects(frame, "flat_region", min_variance=1e9)
        self.assertRejects(frame, "low_contrast", min_contrast=1e9)
        self.assertRejects(frame, "no_ir_return", min_face_brightness=250)
        self.assertRejects(frame, "saturated", max_saturated=-1)

        checker = LivenessChecker(
            {"liveness": {}, "camera": {"min_frame_brightness": 200}}
        )
        self.assertEqual(checker.check(frame, FACE_ROW), (False, "dark_frame"))


class GeometryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.checker = _checker()
        self.frame = _live(1)

    def assertGeometry(self, row, reason: str) -> None:
        self.assertEqual(self.checker.check(self.frame, row), (False, reason))
        self.assertEqual(self.checker.measure(self.frame, row), {})

    def test_missing_or_degenerate_detection(self) -> None:
        self.assertEqual(self.checker.check(self.frame, None), (False, "no_face"))
        self.assertGeometry([1.0, 2.0, 3.0], "no_face")
        self.assertGeometry([100, 60, 0, 120], "no_face")
        self.assertGeometry([100, 60, 120, -5], "no_face")

    def test_face_too_small(self) -> None:
        self.assertGeometry([100, 60, 30, 30], "face_too_small")

    def test_face_mostly_off_sensor(self) -> None:
        self.assertGeometry([WIDTH - 20, 60, 120, 120], "out_of_frame")
        self.assertGeometry([WIDTH + 10, 60, 120, 120], "out_of_frame")
        self.assertGeometry([-100, -100, 90, 90], "out_of_frame")

    def test_slightly_clipped_face_is_still_measured(self) -> None:
        # Inset region spans x=-22..62: 84 columns wanted, 62 (74%) on sensor.
        row = [-40, 60, 120, 120]
        stats = self.checker.measure(self.frame, row)
        self.assertEqual(stats["width"], 62.0)

    def test_geometric_rejections_map_to_no_face_in_the_daemon(self) -> None:
        self.assertEqual(
            daemon._GEOMETRIC_LIVENESS_REASONS, {"no_face", "face_too_small", "out_of_frame"}
        )


class MeasurementTests(unittest.TestCase):
    def test_measure_reports_region_statistics_without_side_effects(self) -> None:
        checker = _checker()
        frame = _live(1)
        stats = checker.measure(frame, FACE_ROW)

        region = frame[REGION]
        self.assertEqual((stats["width"], stats["height"]), (84.0, 84.0))
        self.assertAlmostEqual(stats["region_mean"], float(region.mean()))
        self.assertAlmostEqual(stats["frame_mean"], float(frame.mean()))
        self.assertAlmostEqual(stats["ratio"], stats["region_mean"] / stats["frame_mean"])
        self.assertEqual(checker.frames_checked, 0)
        self.assertEqual(checker.streak, 0)

    def test_black_frame_ratio_does_not_divide_by_zero(self) -> None:
        black = np.zeros((HEIGHT, WIDTH), dtype=np.uint8)
        self.assertEqual(_checker().measure(black, FACE_ROW)["ratio"], 0.0)

    def test_colour_preview_frames_keep_their_levels(self) -> None:
        gray = _live(1)
        expected = _checker().measure(gray, FACE_ROW)
        for code in (cv2.COLOR_GRAY2BGR, cv2.COLOR_GRAY2BGRA):
            with self.subTest(code=code):
                converted = cv2.cvtColor(gray, code)
                self.assertEqual(_checker().measure(converted, FACE_ROW), expected)

    def test_wide_dtypes_are_clipped_not_rescaled(self) -> None:
        frame = _live(1).astype(np.float64)
        frame[REGION][0, 0] = 1000.0
        stats = _checker().measure(frame, FACE_ROW)
        self.assertGreater(stats["saturated"], 0.0)

    def test_malformed_frames_raise(self) -> None:
        checker = _checker()
        for frame in (
            None,
            np.zeros((HEIGHT, WIDTH, 2), dtype=np.uint8),
            np.zeros((4, HEIGHT, WIDTH, 1), dtype=np.uint8),
            np.zeros((0, 0), dtype=np.uint8),
        ):
            with self.subTest(shape=getattr(frame, "shape", None)):
                with self.assertRaises(ValueError):
                    checker.check(frame, FACE_ROW)


class ReasonContractTests(unittest.TestCase):
    def test_every_rejection_reason_has_enrolment_guidance(self) -> None:
        rejections = set(REASONS) - {"live", "disabled"}
        self.assertEqual(set(daemon._LIVENESS_HINTS), rejections)


if __name__ == "__main__":
    unittest.main()
