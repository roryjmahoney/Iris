"""IR-native presentation-attack resistance for Iris.

What this is
------------
A set of cheap statistical tests on the **raw** infrared face region, designed
around one physical fact: the laptop's IR camera sees only the light its own
850nm emitter throws back at it. A real face is a diffuse, three-dimensional,
NIR-reflective object, so in a lit strobe frame it comes back bright and richly
textured. A phone or monitor replaying a photo or video emits visible light and
essentially no 850nm energy, so the "face" on it returns almost nothing: the
region reads dark, flat and structureless, or -- if the panel's glass catches
the emitter -- as a blown-out specular hotspot. Either signature is easy to
separate from a real face without any extra hardware.

This is the same class of defence Windows Hello's IR-only mode relies on, and
it is genuinely effective against the attack that actually happens to laptop
users: someone holding up a phone with a photo from social media.

What this is NOT
----------------
Be clear-eyed about the limits; this module is a filter, not a guarantee.

* **It is not certified PAD.** Nothing here has been tested against ISO/IEC
  30107-3. There is no measured APCER/BPCER for this implementation.
* **A high-quality IR-reflective mask defeats it.** A resin or silicone mask
  with skin-like NIR reflectance and real 3-D relief produces bright, textured,
  varying IR returns. These tests cannot tell it from a face. Neither can they
  stop a well-made photograph printed on IR-reflective substrate and shaped to
  the face's contours.
* **No depth check is possible.** This hardware has a single IR sensor and no
  structured-light or time-of-flight projector, so there is no depth map to
  reason about -- the strongest anti-spoof signal available to Face ID-class
  systems is simply not present here.
* **There is no challenge-response.** No blink or head-turn prompt, so a video
  replay on a hypothetical IR-emitting display would pass the texture tests.
* **It cannot stop coercion.** A real face held in front of the camera under
  duress is, by every measure here, live.

Consequently face unlock in Iris is stacked as a *convenience* factor:
``[success=done default=ignore]``, always falling through to the password. It
must never be the only thing between an attacker and the account.

Why raw pixels
--------------
:class:`iris.engine.FaceEngine` applies CLAHE before detection because
recognition wants lighting invariance. Liveness wants the exact opposite. CLAHE
would rescale a near-black screen region up to mid-grey and amplify its sensor
noise into apparent texture -- erasing both signals this module depends on. So
every measurement here is taken on the untouched frame from
:meth:`iris.camera.Camera.frames`.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Sequence

import cv2
import numpy as np

__all__ = ["LivenessChecker", "REASONS"]

_log = logging.getLogger("iris.liveness")

# Reason codes returned by LivenessChecker.check(). "live" and "disabled" are
# the pass cases; every other value is a rejection. The daemon collapses any
# rejection into the protocol's single `reason` value "spoof_suspected" and
# logs the specific code -- the wire format deliberately does not tell a
# potential attacker which test they tripped.
REASONS = (
    "live",             # passed every test
    "disabled",         # liveness checking switched off in config
    "no_face",          # caller passed no detection row
    "face_too_small",   # face region too small to measure meaningfully
    "out_of_frame",     # bounding box mostly outside the sensor area
    "dark_frame",       # whole frame is dark: a strobe-off frame slipped through
    "no_ir_return",     # face region far darker than the scene: emits/reflects no IR
    "saturated",        # blown-out region: specular glare off glass or plastic
    "low_contrast",     # region has almost no tonal range
    "flat_region",      # Laplacian variance below threshold: no real surface texture
    "static_input",     # consecutive frames are pixel-identical: replay or stuck buffer
)


class LivenessChecker:
    """Per-frame IR spoof heuristics with a short multi-frame memory.

    Usage from the daemon: call :meth:`reset` once per authentication attempt,
    then :meth:`check` on each illuminated frame alongside the recognition
    match. Accept only when recognition and liveness agree, and prefer
    :meth:`is_confident` over a single frame's verdict.

    Not thread-safe: the instance carries per-attempt history.
    """

    def __init__(self, cfg: dict) -> None:
        live = dict(cfg.get("liveness") or {})
        cam = dict(cfg.get("camera") or {})

        self.enabled = bool(live.get("enabled", True))

        # Laplacian-variance floor for the face region. Real skin under IR shows
        # pores, nostril and lip edges, eye sockets; a screen shows nothing.
        self.min_variance = float(live.get("min_variance", 12.0))

        # Absolute brightness floor for the face region in a lit frame. Lit
        # frames average ~55-62 on this sensor and a face -- the closest object
        # to the emitter -- is normally brighter than that.
        self.min_face_brightness = float(live.get("min_face_brightness", 25.0))

        # Face brightness as a fraction of whole-frame brightness. This is the
        # core anti-replay test: a screen's "face" is dramatically darker than
        # the real objects around it. Deliberately loose. NIR reflectance of
        # skin varies far less across skin tones than visible-light reflectance
        # does, but it is not identical, and a false rejection here is a
        # fairness problem, so the bar sits well below any plausible live face.
        self.min_face_ratio = float(live.get("min_face_ratio", 0.45))

        # Fraction of the region allowed to be at/near full scale. A glossy
        # phone or photo print can bounce the emitter straight back as a
        # mirror-like hotspot; that is a spoof signature, and it also destroys
        # the texture the recogniser needs.
        self.max_saturated = float(live.get("max_saturated", 0.20))
        self._saturation_level = int(live.get("saturation_level", 250))

        # Standard deviation floor: catches uniformly grey regions that happen
        # to sit above the brightness floor (e.g. a sheet of paper).
        self.min_contrast = float(live.get("min_contrast", 8.0))

        # Consecutive passing frames required before is_confident() is true.
        self.min_consecutive = max(1, int(live.get("min_consecutive", 2)))

        # Frames whose mean falls below this are the emitter's dark strobe
        # phase. Camera.frames() already filters them; this is a backstop for
        # callers using raw_frames(), because a dark frame would make every
        # brightness test meaningless.
        self.min_frame_brightness = float(cam.get("min_frame_brightness", 20.0))

        # Smallest usable face region, in pixels per side. Below roughly this
        # size, Laplacian variance measures sensor noise rather than skin.
        self.min_region_px = int(live.get("min_region_px", 24))

        history = max(2, int(live.get("history", 16)))
        self._history: deque[dict[str, float]] = deque(maxlen=history)
        self._streak = 0
        self.last_reason = "live"

    # ------------------------------------------------------------------
    # state
    # ------------------------------------------------------------------
    def reset(self) -> None:
        """Clear multi-frame state. Call at the start of each auth attempt.

        Without this, the passing streak from a previous successful login would
        carry into the next attempt and satisfy :meth:`is_confident` on frame
        one.
        """
        self._history.clear()
        self._streak = 0
        self.last_reason = "live"

    @property
    def streak(self) -> int:
        """Number of consecutive frames that have passed since the last failure."""
        return self._streak

    @property
    def frames_checked(self) -> int:
        """Frames measured since the last :meth:`reset`."""
        return len(self._history)

    def is_confident(self) -> bool:
        """True once ``min_consecutive`` frames in a row have passed.

        A single frame can pass by luck -- a motion-blurred hand, a reflection
        that briefly looks like texture. Requiring a run makes the daemon's
        accept decision rest on sustained evidence.
        """
        if not self.enabled:
            return True
        return self._streak >= self.min_consecutive

    # ------------------------------------------------------------------
    # measurement
    # ------------------------------------------------------------------
    def measure(
        self, gray: np.ndarray, face_row: Sequence[float] | np.ndarray
    ) -> dict[str, float]:
        """Return the raw statistics :meth:`check` decides on.

        Exposed for the enrolment GUI's diagnostics view and for recalibrating
        thresholds against real hardware; it has no side effects and is not part
        of the module contract in SPEC.md.

        Keys: ``frame_mean``, ``region_mean``, ``region_std``, ``ratio``,
        ``variance`` (Laplacian), ``saturated`` (fraction), ``width``,
        ``height``. An unusable region yields an empty dict.
        """
        arr = self._as_gray(gray)
        roi = self._face_region(arr, face_row)
        if roi is None:
            return {}

        frame_mean = float(arr.mean())
        region_mean = float(roi.mean())
        region_std = float(roi.std())

        # Laplacian variance is the standard focus/texture metric: the operator
        # responds to second-order intensity change, so a surface with real
        # relief and fine detail scores high and a flat panel scores near zero.
        # Measured on the raw region, so sensor noise contributes a small
        # floor -- which is why the brightness and contrast tests below back it
        # up rather than relying on this number alone. It also falls off with
        # distance as the face covers fewer pixels, which is the main source of
        # false rejections; min_region_px keeps the region large enough to mean
        # something.
        variance = float(cv2.Laplacian(roi, cv2.CV_64F, ksize=3).var())

        saturated = float(np.count_nonzero(roi >= self._saturation_level) / roi.size)

        return {
            "frame_mean": frame_mean,
            "region_mean": region_mean,
            "region_std": region_std,
            # Guard the division: a fully black frame is rejected separately.
            "ratio": region_mean / frame_mean if frame_mean > 1e-6 else 0.0,
            "variance": variance,
            "saturated": saturated,
            "width": float(roi.shape[1]),
            "height": float(roi.shape[0]),
        }

    # ------------------------------------------------------------------
    # the check
    # ------------------------------------------------------------------
    def check(
        self, gray: np.ndarray, face_row: Sequence[float] | np.ndarray
    ) -> tuple[bool, str]:
        """Judge one frame.

        Args:
            gray: the **raw** 2-D uint8 IR frame the face was detected in -- not
                a CLAHE-equalised copy (see the module docstring).
            face_row: the 15-column YuNet row from
                :meth:`iris.engine.FaceEngine.detect`; only the bounding box
                (columns 0-3) is used.

        Returns:
            ``(passed, reason)`` where ``reason`` is one of :data:`REASONS`.
            A failure updates the streak, so an attacker cannot alternate a
            spoof frame with a real one to accumulate confidence.
        """
        if not self.enabled:
            # Explicitly opt-out state, surfaced so the daemon can log that
            # liveness was off rather than that it passed.
            self.last_reason = "disabled"
            return True, "disabled"

        if face_row is None:
            return self._fail("no_face")

        stats = self.measure(gray, face_row)
        if not stats:
            # _face_region already logged which geometric condition failed.
            return self._fail(self.last_reason if self.last_reason in REASONS else "no_face")

        # 1. Sanity: a dark strobe frame makes every brightness test nonsense.
        #    Camera.frames() filters these; reaching here means the caller used
        #    an unfiltered source. Fail closed rather than measure garbage.
        if stats["frame_mean"] < self.min_frame_brightness:
            return self._fail("dark_frame", stats)

        # 2. The core IR test: does this "face" send light back? A phone or
        #    monitor showing a face emits no 850nm energy, so the region is far
        #    darker than the genuinely illuminated scene around it. Checked both
        #    absolutely and relative to the frame, because the absolute level
        #    depends on how close the user is sitting.
        if stats["region_mean"] < self.min_face_brightness:
            return self._fail("no_ir_return", stats)
        if stats["ratio"] < self.min_face_ratio:
            return self._fail("no_ir_return", stats)

        # 3. Specular glare: the emitter mirrored off glass or a glossy print.
        #    Checked before the texture test, since a blown-out region can carry
        #    high Laplacian variance at its clipped edges and would otherwise
        #    sail through.
        if stats["saturated"] > self.max_saturated:
            return self._fail("saturated", stats)

        # 4. Tonal range: a uniform surface (paper, a switched-off panel) has
        #    almost none, while a real face spans nose highlight to eye-socket
        #    shadow.
        if stats["region_std"] < self.min_contrast:
            return self._fail("low_contrast", stats)

        # 5. Surface texture: the discriminator between skin and any flat
        #    reproduction of it.
        if stats["variance"] < self.min_variance:
            return self._fail("flat_region", stats)

        # 6. Multi-frame consistency. Two independent 15fps captures of a real
        #    scene never produce byte-identical statistics -- sensor noise alone
        #    guarantees they differ. Identical numbers mean the same buffer was
        #    delivered twice: a stuck V4L2 queue, or frames being injected from
        #    a file rather than the sensor. This does not detect a video replay
        #    (those frames do differ); the IR tests above are what cover that.
        if self._is_duplicate(stats):
            return self._fail("static_input", stats)

        self._history.append(stats)
        self._streak += 1
        self.last_reason = "live"
        return True, "live"

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _fail(self, reason: str, stats: dict[str, float] | None = None) -> tuple[bool, str]:
        """Record a rejection, breaking the confidence streak."""
        if stats:
            self._history.append(stats)
        self._streak = 0
        self.last_reason = reason
        _log.debug("liveness rejected frame: %s (%s)", reason, stats or {})
        return False, reason

    def _is_duplicate(self, stats: dict[str, float]) -> bool:
        """True if this frame's statistics exactly repeat the previous frame's."""
        if not self._history:
            return False
        previous = self._history[-1]
        keys = ("frame_mean", "region_mean", "region_std", "variance")
        return all(abs(stats[k] - previous[k]) < 1e-9 for k in keys)

    @staticmethod
    def _as_gray(gray: np.ndarray) -> np.ndarray:
        """Coerce input to a 2-D uint8 array without altering pixel levels."""
        if gray is None:
            raise ValueError("frame is None")
        arr = np.asarray(gray)
        if arr.ndim == 3:
            # Tolerate the BGR preview stream; note that this is a luminance
            # conversion only -- no equalisation, so absolute levels survive.
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
            arr = np.clip(arr, 0, 255).astype(np.uint8)
        return arr

    def _face_region(
        self, arr: np.ndarray, face_row: Sequence[float] | np.ndarray
    ) -> np.ndarray | None:
        """Crop the central part of the detected face box.

        The box includes forehead, hair and some background at the corners.
        Hair is a poor IR reflector and background is at a different distance
        from the emitter, so both skew the brightness statistics; insetting to
        the middle keeps the measurement on skin -- eyes, nose, mouth -- which
        is exactly the area a spoof has to reproduce.

        Returns ``None`` (and sets :attr:`last_reason`) if the region cannot be
        measured.
        """
        row = np.asarray(face_row, dtype=np.float32).ravel()
        if row.size < 4:
            self.last_reason = "no_face"
            return None

        height, width = arr.shape[:2]
        x, y, w, h = (float(row[0]), float(row[1]), float(row[2]), float(row[3]))
        if w <= 0 or h <= 0:
            self.last_reason = "no_face"
            return None

        inset_x = w * 0.15
        inset_y = h * 0.15
        x0 = int(round(x + inset_x))
        y0 = int(round(y + inset_y))
        x1 = int(round(x + w - inset_x))
        y1 = int(round(y + h - inset_y))

        # Clamp to the sensor. YuNet can return boxes that run off the edge when
        # the user is partly out of frame.
        cx0, cy0 = max(0, x0), max(0, y0)
        cx1, cy1 = min(width, x1), min(height, y1)
        if cx1 <= cx0 or cy1 <= cy0:
            self.last_reason = "out_of_frame"
            return None

        # If clamping ate most of the face we are measuring a sliver at the edge
        # of the sensor, where IR falloff is worst; refuse rather than guess.
        wanted = float((x1 - x0) * (y1 - y0))
        kept = float((cx1 - cx0) * (cy1 - cy0))
        if wanted <= 0 or kept / wanted < 0.6:
            self.last_reason = "out_of_frame"
            return None

        if (cx1 - cx0) < self.min_region_px or (cy1 - cy0) < self.min_region_px:
            self.last_reason = "face_too_small"
            return None

        return arr[cy0:cy1, cx0:cx1]

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (
            f"<LivenessChecker enabled={self.enabled} min_variance={self.min_variance:.1f} "
            f"streak={self._streak}/{self.min_consecutive}>"
        )
