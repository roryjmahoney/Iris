from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

from iris import engine
from iris.engine import EMBEDDING_DIM, EngineError, FaceEngine

MODELS = Path(__file__).resolve().parents[1] / "models"

# A plausible YuNet row for a face in the middle of a 640x360 frame: bbox,
# five landmarks (eyes, nose, mouth corners) and a score.
FACE_ROW = np.array(
    [270, 110, 100, 120, 295, 150, 345, 150, 320, 180, 300, 205, 340, 205, 0.9],
    dtype=np.float32,
)


def _frame(seed: int, shape: tuple[int, ...] = (360, 640)) -> np.ndarray:
    return np.random.default_rng(seed).integers(0, 256, shape, dtype=np.uint8)


def _row(x: float, y: float, w: float, h: float, score: float) -> list[float]:
    return [x, y, w, h] + [0.0] * 10 + [score]


class _EngineCase(unittest.TestCase):
    engine: FaceEngine

    @classmethod
    def setUpClass(cls) -> None:
        # Loading SFace costs ~0.5s; share one real engine across the class.
        cls.engine = FaceEngine({"recognition": {"model_dir": str(MODELS)}})


class CompareTests(unittest.TestCase):
    def test_cosine_similarity_basics(self) -> None:
        a = np.array([1.0, 0.0, 0.0])
        self.assertAlmostEqual(FaceEngine.compare(a, a), 1.0)
        self.assertAlmostEqual(FaceEngine.compare(a, 5 * a), 1.0)
        self.assertAlmostEqual(FaceEngine.compare(a, -a), -1.0)
        self.assertAlmostEqual(FaceEngine.compare(a, np.array([0.0, 2.0, 0.0])), 0.0)
        self.assertAlmostEqual(FaceEngine.compare(a, np.array([1.0, 1.0, 0.0])), 2**-0.5)

    def test_shapes_are_flattened(self) -> None:
        v = np.random.default_rng(1).standard_normal(EMBEDDING_DIM).astype(np.float32)
        self.assertAlmostEqual(FaceEngine.compare(v.reshape(1, -1), v.tolist()), 1.0)

    def test_result_is_always_a_valid_cosine(self) -> None:
        rng = np.random.default_rng(2)
        for _ in range(200):
            v = rng.standard_normal(EMBEDDING_DIM).astype(np.float32) * 1e3
            self.assertLessEqual(FaceEngine.compare(v, v), 1.0)
            self.assertGreaterEqual(FaceEngine.compare(v, -v), -1.0)

    def test_zero_vector_fails_closed(self) -> None:
        zero = np.zeros(EMBEDDING_DIM)
        with self.assertLogs("iris.engine", "WARNING"):
            self.assertEqual(FaceEngine.compare(zero, np.ones(EMBEDDING_DIM)), 0.0)

    def test_malformed_pairs_raise(self) -> None:
        with self.assertRaises(ValueError):
            FaceEngine.compare(np.array([]), np.array([]))
        with self.assertRaises(ValueError):
            FaceEngine.compare(np.ones(128), np.ones(64))


class MatchBestTests(unittest.TestCase):
    def test_best_single_sample_wins(self) -> None:
        live = np.array([1.0, 0.0])
        candidates = [
            ("glasses", np.array([0.0, 1.0])),
            ("default", np.array([1.0, 0.2])),
            ("glasses", np.array([1.0, 1.0])),
        ]
        name, score = FaceEngine.match_best(live, candidates)
        self.assertEqual(name, "default")
        self.assertAlmostEqual(score, FaceEngine.compare(live, candidates[1][1]))

    def test_nothing_enrolled_can_never_match(self) -> None:
        self.assertEqual(FaceEngine.match_best(np.ones(4), []), (None, -1.0))

    def test_stale_templates_are_skipped_not_fatal(self) -> None:
        live = np.ones(128)
        candidates = [("old-model", np.ones(64)), ("default", np.ones(128))]
        with self.assertLogs("iris.engine", "WARNING"):
            name, score = FaceEngine.match_best(live, candidates)
        self.assertEqual(name, "default")
        self.assertAlmostEqual(score, 1.0)

    def test_only_stale_templates_means_no_match(self) -> None:
        with self.assertLogs("iris.engine", "WARNING"):
            self.assertEqual(
                FaceEngine.match_best(np.ones(128), [("old", np.ones(64))]), (None, -1.0)
            )

    def test_accepts_a_generator(self) -> None:
        pairs = ((str(i), np.eye(3)[i]) for i in range(3))
        self.assertEqual(FaceEngine.match_best(np.eye(3)[2], pairs), ("2", 1.0))


class SelectFaceTests(unittest.TestCase):
    def test_largest_face_wins_over_highest_score(self) -> None:
        # A bystander far back may score higher but must not steer the match.
        faces = np.array(
            [_row(0, 0, 40, 40, 0.99), _row(200, 50, 150, 160, 0.75), _row(500, 0, 60, 60, 0.9)],
            dtype=np.float32,
        )
        np.testing.assert_array_equal(FaceEngine.select_face(faces), faces[1])

    def test_single_row_and_empty_inputs(self) -> None:
        row = np.array(_row(1, 2, 3, 4, 0.9), dtype=np.float32)
        np.testing.assert_array_equal(FaceEngine.select_face(row), row)
        self.assertIsNone(FaceEngine.select_face(None))
        self.assertIsNone(FaceEngine.select_face(np.empty((0, 15), dtype=np.float32)))


class ThresholdTests(_EngineCase):
    def test_default_threshold_is_the_sface_operating_point(self) -> None:
        self.assertEqual(self.engine.threshold, 0.363)

    def test_threshold_boundary_is_inclusive(self) -> None:
        t = self.engine.threshold
        self.assertTrue(self.engine.is_match(t))
        self.assertTrue(self.engine.is_match(1.0))
        self.assertFalse(self.engine.is_match(np.nextafter(t, -1.0)))
        self.assertFalse(self.engine.is_match(-1.0))

    def test_threshold_comes_from_config(self) -> None:
        strict = FaceEngine(
            {"recognition": {"model_dir": str(MODELS), "threshold": 0.9, "detect_score": 0.5}}
        )
        self.assertEqual((strict.threshold, strict.detect_score), (0.9, 0.5))
        self.assertFalse(strict.is_match(0.89))


class ModelResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def test_configured_directory_wins(self) -> None:
        for name in (engine.DETECTOR_MODEL, engine.RECOGNIZER_MODEL):
            (self.tmp / name).write_bytes(b"")
        self.assertEqual(FaceEngine._resolve_model_dir(str(self.tmp)), self.tmp)

    def test_non_root_falls_back_to_the_source_checkout(self) -> None:
        with mock.patch.object(engine.os, "geteuid", return_value=1000):
            with self.assertLogs("iris.engine", "WARNING"):
                resolved = FaceEngine._resolve_model_dir(str(self.tmp / "missing"))
        self.assertEqual(resolved, MODELS)

    def test_root_never_loads_models_from_the_checkout(self) -> None:
        # The checkout is user-writable; a root daemon loading it would let an
        # unprivileged user swap in a recogniser that matches any face.
        with mock.patch.object(engine.os, "geteuid", return_value=0):
            with self.assertRaises(EngineError):
                FaceEngine._resolve_model_dir(str(self.tmp / "missing"))

    def test_incomplete_directory_is_not_used(self) -> None:
        (self.tmp / engine.DETECTOR_MODEL).write_bytes(b"")
        with mock.patch.object(engine.os, "geteuid", return_value=0):
            with self.assertRaises(EngineError):
                FaceEngine._resolve_model_dir(str(self.tmp))

    def test_corrupt_model_raises_engine_error(self) -> None:
        shutil.copy(MODELS / engine.DETECTOR_MODEL, self.tmp)
        (self.tmp / engine.RECOGNIZER_MODEL).write_bytes(b"not an onnx graph")
        with self.assertRaises(EngineError):
            FaceEngine({"recognition": {"model_dir": str(self.tmp)}})


class DetectTests(_EngineCase):
    def test_empty_scene_has_no_face(self) -> None:
        for frame in (np.full((360, 640), 60, np.uint8), np.zeros((360, 640), np.uint8)):
            self.assertIsNone(self.engine.detect(frame))

    def test_frame_size_changes_are_pushed_to_the_detector(self) -> None:
        eng = FaceEngine({"recognition": {"model_dir": str(MODELS)}})
        with mock.patch.object(eng, "_detector", wraps=eng._detector) as detector:
            eng.detect(np.zeros((360, 640), np.uint8))
            detector.setInputSize.assert_not_called()

            eng.detect(np.zeros((240, 320), np.uint8))
            eng.detect(np.zeros((240, 320), np.uint8))
            detector.setInputSize.assert_called_once_with((320, 240))

    def test_rows_are_sorted_by_descending_score(self) -> None:
        eng = FaceEngine({"recognition": {"model_dir": str(MODELS)}})
        raw = np.array([_row(0, 0, 9, 9, 0.71), _row(1, 1, 9, 9, 0.98), _row(2, 2, 9, 9, 0.85)])
        with mock.patch.object(eng, "_detector") as detector:
            detector.detect.return_value = (1, raw)
            faces = eng.detect(np.zeros((360, 640), np.uint8))
        assert faces is not None
        self.assertEqual(faces.dtype, np.float32)
        np.testing.assert_allclose(faces[:, 14], [0.98, 0.85, 0.71])

    def test_detector_failure_becomes_engine_error(self) -> None:
        eng = FaceEngine({"recognition": {"model_dir": str(MODELS)}})
        with mock.patch.object(eng, "_detector") as detector:
            detector.detect.side_effect = cv2.error("boom")
            with self.assertRaises(EngineError):
                eng.detect(np.zeros((360, 640), np.uint8))


class EmbedTests(_EngineCase):
    def test_embedding_shape_and_determinism(self) -> None:
        frame = _frame(1)
        first = self.engine.embed(frame, FACE_ROW)

        self.assertEqual(first.shape, (EMBEDDING_DIM,))
        self.assertEqual(first.dtype, np.float32)
        self.assertTrue(np.all(np.isfinite(first)))
        np.testing.assert_array_equal(self.engine.embed(frame, FACE_ROW), first)
        self.assertAlmostEqual(FaceEngine.compare(first, first), 1.0, places=6)

    def test_embedding_is_an_independent_copy(self) -> None:
        frame = _frame(1)
        first = self.engine.embed(frame, FACE_ROW)
        snapshot = first.copy()
        self.engine.embed(_frame(2), FACE_ROW)
        np.testing.assert_array_equal(first, snapshot)

    def test_embedding_depends_on_the_pixels_under_the_face(self) -> None:
        a = self.engine.embed(_frame(1), FACE_ROW)
        b = self.engine.embed(_frame(2), FACE_ROW)
        self.assertLess(FaceEngine.compare(a, b), 0.999)

    def test_row_without_landmarks_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.engine.embed(_frame(1), FACE_ROW[:4])

    def test_preprocessing_is_shared_by_detect_and_embed(self) -> None:
        # Enrolment and authentication must see identical pixels: any frame the
        # engine accepts goes through the same _prepare() path.
        with mock.patch.object(self.engine, "_prepare", wraps=self.engine._prepare) as prep:
            frame = _frame(1)
            self.engine.detect(frame)
            self.engine.embed(frame, FACE_ROW)
        self.assertEqual(prep.call_count, 2)
        for call in prep.call_args_list:
            self.assertIs(call.args[0], frame)


class PrepareTests(_EngineCase):
    def test_output_is_equalised_three_channel_bgr(self) -> None:
        # A dim, low-contrast frame: CLAHE must stretch it.
        rng = np.random.default_rng(1)
        dim = np.clip(rng.normal(40, 3, (360, 640)), 0, 255).astype(np.uint8)
        out = self.engine._prepare(dim)

        self.assertEqual((out.shape, out.dtype), ((360, 640, 3), np.uint8))
        np.testing.assert_array_equal(out[..., 0], out[..., 1])
        np.testing.assert_array_equal(out[..., 1], out[..., 2])
        self.assertGreater(out[..., 0].std(), dim.std())

    def test_colour_and_sliced_inputs_match_grayscale(self) -> None:
        gray = _frame(1)
        expected = self.engine._prepare(gray)
        np.testing.assert_array_equal(
            self.engine._prepare(cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)), expected
        )
        np.testing.assert_array_equal(
            self.engine._prepare(cv2.cvtColor(gray, cv2.COLOR_GRAY2BGRA)), expected
        )

        wide = np.zeros((360, 1280), np.uint8)
        wide[:, ::2] = gray
        np.testing.assert_array_equal(self.engine._prepare(wide[:, ::2]), expected)

    def test_wide_dtypes_are_clipped_not_rescaled(self) -> None:
        gray = _frame(1)
        wide = gray.astype(np.float64)
        wide[0, 0] = 1000.0
        wide[0, 1] = -50.0
        clipped = gray.copy()
        clipped[0, 0], clipped[0, 1] = 255, 0
        np.testing.assert_array_equal(
            self.engine._prepare(wide), self.engine._prepare(clipped)
        )

    def test_malformed_frames_raise(self) -> None:
        for frame in (
            None,
            np.zeros((360, 640, 2), np.uint8),
            np.zeros((2, 360, 640, 1), np.uint8),
            np.zeros((0, 0), np.uint8),
        ):
            with self.subTest(shape=getattr(frame, "shape", None)):
                with self.assertRaises(ValueError):
                    self.engine.detect(frame)


if __name__ == "__main__":
    unittest.main()
