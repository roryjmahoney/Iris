"""Face detection and recognition for Iris.

This module owns the two ONNX networks that turn an infrared frame into a
comparable embedding:

* **YuNet** (``face_detection_yunet_2023mar.onnx``) -- a small CNN detector that
  returns a bounding box, five landmarks and a confidence score per face.
* **SFace** (``face_recognition_sface_2021dec.onnx``) -- a 128-dimensional
  embedding network; two crops of the same person land close together under
  cosine similarity.

Both ship with OpenCV's ``cv2.FaceDetectorYN`` / ``cv2.FaceRecognizerSF``
wrappers, so no third-party runtime is needed.

Input contract
--------------
Frames come from :mod:`iris.camera`, which yields **2-D uint8 grayscale**
images from the IR sensor (``/dev/video2`` is GREY 8-bit 640x360 only) and has
already dropped the dark half of the emitter's strobe cycle. Two conversions
happen here, on every frame, for both enrolment and authentication:

1. **CLAHE** (contrast-limited adaptive histogram equalisation). The IR emitter
   is a point source next to the lens, so illumination falls off sharply from
   the centre of the frame and the overall level swings with how far the user
   is sitting from the laptop. A global brightness shift moves an SFace
   embedding just as much as a change of identity does, which is how a system
   like this ends up "working at night but not in the morning". CLAHE
   normalises *local* contrast, so a face enrolled at 40cm in a bright room and
   the same face at 70cm in the dark land in the same region of embedding
   space. It is applied inside this module precisely so that enrolment and
   authentication can never disagree about the preprocessing.
2. **GRAY -> BGR**. Both networks were trained on 3-channel colour input and
   their input blobs are 3-channel; OpenCV will not accept a single-channel
   Mat. Replicating the gray plane three times is the standard adaptation and
   is what the OpenCV Zoo IR examples do.

Cost, measured on the target machine over 40 frames at 640x360 (each figure is
end-to-end and already includes the CLAHE + BGR conversion above, which
together account for ~0.2ms): ``detect()`` ~5-6ms, ``embed()`` ~7-8ms. A full
detect-plus-embed frame lands near 13ms including the liveness pass, comfortably
inside the ~50ms/frame budget at 15fps. The CNN forward passes dominate, so a
frame that actually contains a face costs no more than these synthetic ones.

Everything in here is pure computation: no camera, no filesystem beyond
loading the models, no persistence. Templates live in :mod:`iris.store`.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Iterable, Sequence

import cv2
import numpy as np

__all__ = ["FaceEngine", "EngineError", "DETECTOR_MODEL", "RECOGNIZER_MODEL"]

_log = logging.getLogger("iris.engine")

# Exact filenames as installed by the packaging step (see SPEC.md "Paths").
DETECTOR_MODEL = "face_detection_yunet_2023mar.onnx"
RECOGNIZER_MODEL = "face_recognition_sface_2021dec.onnx"

# YuNet emits one row of 15 floats per face:
#   0-1 bbox x,y   2-3 bbox w,h
#   4-5 right eye  6-7 left eye  8-9 nose tip
#   10-11 right mouth corner  12-13 left mouth corner
#   14 detection score
# SFace's alignCrop() consumes columns 4..13 (the landmarks) to build the
# similarity transform, so a bbox-only row is not enough to embed.
FACE_ROW_LEN = 15
_LANDMARK_COLS = 14

# SFace produces a 128-D descriptor.
EMBEDDING_DIM = 128


class EngineError(RuntimeError):
    """Raised when the recognition models cannot be loaded or run.

    Subclasses :class:`RuntimeError` so callers that only care that "the engine
    is unusable" can catch broadly; the daemon maps this to the protocol
    ``camera_error``/failure paths and PAM turns it into an auth denial.
    """


class FaceEngine:
    """Detect faces and turn them into comparable embeddings.

    Construction loads both ONNX graphs, which takes on the order of a hundred
    milliseconds and ~40MB of RSS. Build one instance per process and reuse it;
    the daemon holds a single engine for the lifetime of the service.

    Not thread-safe: ``cv2.FaceDetectorYN``/``FaceRecognizerSF`` hold mutable
    internal state (notably the detector's input size), so serialise calls or
    give each thread its own engine.
    """

    def __init__(self, cfg: dict) -> None:
        rec = dict(cfg.get("recognition") or {})
        cam = dict(cfg.get("camera") or {})

        self.threshold = float(rec.get("threshold", 0.363))
        self.detect_score = float(rec.get("detect_score", 0.7))

        model_dir = self._resolve_model_dir(str(rec.get("model_dir", "/usr/share/iris/models")))
        self.model_dir = model_dir
        detector_path = model_dir / DETECTOR_MODEL
        recognizer_path = model_dir / RECOGNIZER_MODEL

        # The detector needs an input size at construction time, but it is reset
        # on every frame whose dimensions differ (see detect()). Seed it with the
        # configured capture size so the common case never re-allocates.
        width = int(cam.get("width", 640))
        height = int(cam.get("height", 360))
        self._input_size: tuple[int, int] = (width, height)

        # top_k caps the candidate boxes kept before NMS. The stock examples use
        # 5000, which is sized for crowd photos; a laptop login frame holds a
        # handful of faces at most, and a smaller cap trims NMS work.
        nms_threshold = float(rec.get("nms_threshold", 0.3))
        top_k = int(rec.get("top_k", 50))

        try:
            self._detector = cv2.FaceDetectorYN.create(
                str(detector_path),
                "",  # no separate config file: the ONNX carries the graph
                self._input_size,
                self.detect_score,
                nms_threshold,
                top_k,
            )
            self._recognizer = cv2.FaceRecognizerSF.create(str(recognizer_path), "")
        except cv2.error as exc:  # corrupt/truncated model, unsupported opset...
            raise EngineError(
                f"failed to initialise recognition models from {model_dir}: {exc}"
            ) from exc

        # One CLAHE object, reused: creating it per frame is wasteful and the
        # object is stateless between apply() calls.
        clip = float(rec.get("clahe_clip", 2.0))
        grid = int(rec.get("clahe_grid", 8))
        self._clahe = cv2.createCLAHE(clipLimit=clip, tileGridSize=(grid, grid))

        _log.debug(
            "FaceEngine ready (models=%s, threshold=%.3f, detect_score=%.2f, input=%dx%d)",
            model_dir,
            self.threshold,
            self.detect_score,
            width,
            height,
        )

    # ------------------------------------------------------------------
    # model discovery
    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_model_dir(configured: str) -> Path:
        """Return the directory that actually holds both ONNX files.

        The configured directory (``/usr/share/iris/models`` once installed)
        always wins. A fallback to the source checkout lets the enrolment GUI
        and the test suite run before ``install.sh`` has copied anything into
        /usr/share.

        That fallback is refused when running as root. The daemon runs as root
        and the checkout lives in a user-writable home directory, so allowing it
        there would mean that a broken or partial install silently causes a root
        process to load an ONNX graph an unprivileged user can rewrite -- both a
        parser attack surface and a way to swap in a recogniser that matches any
        face. Root gets the configured path or a hard failure.
        """
        primary = Path(configured)

        def complete(path: Path) -> bool:
            return (path / DETECTOR_MODEL).is_file() and (path / RECOGNIZER_MODEL).is_file()

        if complete(primary):
            return primary

        if os.geteuid() != 0:
            # <repo>/src/iris/engine.py -> <repo>/models
            checkout = Path(__file__).resolve().parents[2] / "models"
            if complete(checkout):
                _log.warning(
                    "models not found in configured dir %s; falling back to source "
                    "checkout at %s -- run the installer for a production system",
                    configured,
                    checkout,
                )
                return checkout

        raise EngineError(
            f"recognition models not found: expected {DETECTOR_MODEL} and "
            f"{RECOGNIZER_MODEL} in {configured}"
        )

    # ------------------------------------------------------------------
    # preprocessing
    # ------------------------------------------------------------------
    def _prepare(self, gray: np.ndarray) -> np.ndarray:
        """Grayscale IR frame -> CLAHE-equalised 3-channel BGR the nets accept.

        Both :meth:`detect` and :meth:`embed` funnel through here so a face row
        produced by the detector always indexes the exact same pixels the
        recogniser will crop.
        """
        if gray is None:
            raise ValueError("frame is None")

        arr = np.asarray(gray)
        if arr.ndim == 3:
            # Defensive: the preview path (Camera.raw_frames) hands out BGR. Fold
            # it back to one channel so CLAHE sees luminance rather than being
            # applied per-channel, which would tint the image.
            if arr.shape[2] == 3:
                arr = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
            elif arr.shape[2] == 4:
                arr = cv2.cvtColor(arr, cv2.COLOR_BGRA2GRAY)
            else:
                raise ValueError(f"unsupported frame shape {arr.shape}")
        if arr.ndim != 2:
            raise ValueError(f"expected a 2-D grayscale frame, got shape {arr.shape}")
        if arr.size == 0:
            raise ValueError("empty frame")

        if arr.dtype != np.uint8:
            # The IR node is GREY 8-bit, so this only fires if a caller hands us
            # a float/16-bit buffer. Clip rather than rescale: rescaling would
            # make the absolute brightness of the frame depend on its own
            # content, which breaks comparability between enrolment and auth.
            _log.debug("frame dtype %s coerced to uint8", arr.dtype)
            arr = np.clip(arr, 0, 255).astype(np.uint8)

        # OpenCV needs a contiguous buffer; slices of a larger capture buffer
        # (e.g. a cropped preview) are not.
        arr = np.ascontiguousarray(arr)
        equalised = self._clahe.apply(arr)
        return cv2.cvtColor(equalised, cv2.COLOR_GRAY2BGR)

    # ------------------------------------------------------------------
    # detection
    # ------------------------------------------------------------------
    def detect(self, gray: np.ndarray) -> np.ndarray | None:
        """Detect faces in a grayscale IR frame.

        Args:
            gray: 2-D uint8 frame from :meth:`iris.camera.Camera.frames`.

        Returns:
            An ``(N, 15)`` float32 array of YuNet rows sorted by descending
            detection score, or ``None`` when no face scored above
            ``recognition.detect_score``. ``None`` (rather than an empty array)
            keeps the daemon's "no_face" branch unambiguous.
        """
        bgr = self._prepare(gray)
        height, width = bgr.shape[:2]

        # YuNet allocates its input blob for a fixed size and raises if the frame
        # does not match, so the size must be pushed before detect() whenever it
        # changes. Guarded by a comparison because setInputSize() reshapes the
        # network and is far from free.
        if (width, height) != self._input_size:
            self._detector.setInputSize((width, height))
            self._input_size = (width, height)

        try:
            _retval, faces = self._detector.detect(bgr)
        except cv2.error as exc:
            raise EngineError(f"face detection failed: {exc}") from exc

        # OpenCV returns None (not an empty Mat) when nothing is detected.
        if faces is None or len(faces) == 0:
            return None

        rows = np.asarray(faces, dtype=np.float32)
        if rows.ndim == 1:
            rows = rows.reshape(1, -1)
        # YuNet already applies NMS but does not guarantee score ordering.
        order = np.argsort(-rows[:, 14], kind="stable")
        return rows[order]

    @staticmethod
    def select_face(faces: np.ndarray | None) -> np.ndarray | None:
        """Pick the face to authenticate against: the largest by bbox area.

        With a laptop camera the subject is the closest and therefore biggest
        face; a bystander further back must not be able to steer the match.
        """
        if faces is None or len(faces) == 0:
            return None
        rows = np.asarray(faces, dtype=np.float32)
        if rows.ndim == 1:
            rows = rows.reshape(1, -1)
        areas = rows[:, 2] * rows[:, 3]
        return rows[int(np.argmax(areas))]

    # ------------------------------------------------------------------
    # embedding
    # ------------------------------------------------------------------
    def embed(self, gray: np.ndarray, face_row: Sequence[float] | np.ndarray) -> np.ndarray:
        """Compute the SFace embedding for one detected face.

        Args:
            gray: the same frame that was passed to :meth:`detect`; the row's
                coordinates are meaningless against any other frame.
            face_row: one 15-column row from :meth:`detect`.

        Returns:
            A 1-D float32 array of length 128. Flat (rather than SFace's native
            ``(1, 128)``) because it round-trips through JSON in
            :mod:`iris.store` and back via ``np.asarray`` unchanged.
        """
        row = np.asarray(face_row, dtype=np.float32).reshape(1, -1)
        if row.shape[1] < _LANDMARK_COLS:
            # alignCrop reads the five landmarks, not just the box; a truncated
            # row would silently read past the end of the buffer in older
            # OpenCV builds.
            raise ValueError(
                f"face_row needs at least {_LANDMARK_COLS} columns (bbox + 5 landmarks), "
                f"got {row.shape[1]}"
            )

        bgr = self._prepare(gray)
        try:
            # alignCrop warps the face to the canonical 112x112 pose SFace was
            # trained on, using the eye/nose/mouth landmarks. Skipping it and
            # feeding a raw bbox crop costs a large chunk of accuracy, because
            # the network has no rotation invariance of its own.
            aligned = self._recognizer.alignCrop(bgr, row)
            feature = self._recognizer.feature(aligned)
        except cv2.error as exc:
            raise EngineError(f"embedding failed: {exc}") from exc

        # copy(): ravel() can return a view onto OpenCV's output Mat, and the
        # embedding outlives this call (it goes into the template store).
        vector = np.asarray(feature, dtype=np.float32).ravel().copy()
        if vector.size != EMBEDDING_DIM:
            raise EngineError(
                f"unexpected embedding size {vector.size} (expected {EMBEDDING_DIM}); "
                "wrong SFace model?"
            )
        return vector

    # ------------------------------------------------------------------
    # comparison
    # ------------------------------------------------------------------
    @staticmethod
    def compare(a: np.ndarray, b: np.ndarray) -> float:
        """Cosine similarity of two embeddings, in [-1, 1]; higher is closer.

        This is a manual normalised dot product rather than
        ``FaceRecognizerSF.match(..., FR_COSINE)``. The two are the same
        arithmetic -- verified against OpenCV on this machine over 200 random
        pairs, worst-case disagreement 1.4e-8, i.e. float32 round-off and eight
        orders of magnitude below the 0.363 decision threshold -- but the manual
        form is a true ``@staticmethod`` as the contract
        requires, works on embeddings deserialised from the template store
        without a loaded 40MB model, and never raises on shape quirks the way
        the OpenCV wrapper does.

        Matching rule for callers: ``compare(a, b) >= cfg["recognition"]["threshold"]``
        (SFace's published cosine operating point is 0.363).
        """
        va = np.asarray(a, dtype=np.float64).ravel()
        vb = np.asarray(b, dtype=np.float64).ravel()
        if va.size == 0 or vb.size == 0:
            raise ValueError("cannot compare empty embeddings")
        if va.size != vb.size:
            raise ValueError(f"embedding size mismatch: {va.size} vs {vb.size}")

        norm_a = float(np.linalg.norm(va))
        norm_b = float(np.linalg.norm(vb))
        if norm_a == 0.0 or norm_b == 0.0:
            # A zero vector has no direction, so no meaningful similarity.
            # Return the "as dissimilar as an unrelated face" value rather than
            # raising: a corrupt stored template must fail closed, not crash the
            # authentication attempt.
            _log.warning("zero-norm embedding in comparison; treating as no match")
            return 0.0

        # Floating point can push a self-comparison a hair past 1.0; clip so the
        # confidence reported over the protocol is always a valid cosine.
        return float(np.clip(np.dot(va, vb) / (norm_a * norm_b), -1.0, 1.0))

    def is_match(self, score: float) -> bool:
        """Apply the configured threshold to a similarity score."""
        return score >= self.threshold

    @staticmethod
    def match_best(
        embedding: np.ndarray,
        candidates: Iterable[tuple[str, np.ndarray]],
    ) -> tuple[str | None, float]:
        """Find the closest enrolled template.

        Args:
            embedding: the live embedding from :meth:`embed`.
            candidates: ``(name, embedding)`` pairs exactly as
                :meth:`iris.store.TemplateStore.embeddings_for` returns them. A
                name appears once per enrolled sample; the best single sample
                wins, since a user's samples deliberately span several poses and
                averaging them would blur the identity.

        Returns:
            ``(name, score)`` for the highest cosine similarity, or
            ``(None, -1.0)`` when there is nothing to compare against (-1.0 is
            the floor of the cosine range, so it can never pass a threshold).
            The caller applies :meth:`is_match`; this returns the best candidate
            regardless of threshold so the daemon can log near-misses.
        """
        best_name: str | None = None
        best_score = -1.0
        skipped = 0

        for name, candidate in candidates:
            try:
                score = FaceEngine.compare(embedding, candidate)
            except ValueError:
                # A template stored by a different model version has the wrong
                # dimensionality. Skip it instead of failing the whole attempt,
                # so one stale entry cannot lock a user out.
                skipped += 1
                continue
            if score > best_score:
                best_score = score
                best_name = name

        if skipped:
            _log.warning("%d stored template(s) skipped: embedding size mismatch", skipped)
        return best_name, best_score

    # ------------------------------------------------------------------
    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (
            f"<FaceEngine models={os.fspath(self.model_dir)!r} "
            f"threshold={self.threshold:.3f} detect_score={self.detect_score:.2f}>"
        )
