"""Decision logic of irisd: authentication, enrolment, lockout and admin ops.

Handlers are called directly with a scripted camera, face engine, liveness
checker and clock, so every accept/reject path is deterministic.  The real
``FaceEngine.select_face``/``match_best``/``compare`` static methods still do
the geometry and cosine maths on the scripted rows and embeddings.
"""

from __future__ import annotations

import json
import math
import os
import socket
import tempfile
import threading
import types
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from unittest import mock

import numpy as np

from iris import daemon as daemon_module
from iris import protocol
from iris.camera import CameraError
from iris.engine import EngineError, FaceEngine
from iris.store import FailureTracker, StoreError

ALICE = np.array([1.0, 0.0, 0.0], dtype=np.float32)
ALICE_GLASSES = np.array([0.9, 0.1, 0.0], dtype=np.float32)
STRANGER = np.array([0.0, 1.0, 0.0], dtype=np.float32)
OTHER_PERSON = np.array([0.0, 0.0, 1.0], dtype=np.float32)

CONFIG = """
[camera]
device = "/dev/never-opened-by-tests"
width = 16
height = 16

[recognition]
threshold = 0.5
required_matches = 3
max_frames = 120

[auth]
enabled = true
timeout = 8.0
max_failures = 3
lockout_seconds = 60

[liveness]
enabled = true
"""


# --------------------------------------------------------------------------- #
# Scripted fakes
# --------------------------------------------------------------------------- #


@dataclass
class Frame:
    """One illuminated frame: who (if anyone) is in it and what liveness says."""

    embedding: np.ndarray | None = None
    live: tuple[bool, str] = (True, "live")
    embed_error: bool = False
    faces: int = 1


def face(embedding: np.ndarray = ALICE, **kwargs: Any) -> Frame:
    return Frame(embedding=embedding, **kwargs)


EMPTY = Frame()  # nobody in front of the camera


class Clock:
    def __init__(self) -> None:
        self.now = 10_000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class ScriptedCamera:
    """Camera stand-in: yields scripted frames, honouring the time budget."""

    script: list[Frame] = []
    step = 0.1
    open_error: CameraError | None = None
    opened = 0
    clock: Clock

    @classmethod
    def from_config(cls, _cfg: dict[str, Any]) -> "ScriptedCamera":
        return cls()

    def __enter__(self) -> "ScriptedCamera":
        if ScriptedCamera.open_error is not None:
            raise ScriptedCamera.open_error
        ScriptedCamera.opened += 1
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def frames(self, budget: float) -> Iterator[Frame]:
        deadline = self.clock.now + budget
        for frame in list(ScriptedCamera.script):
            if self.clock.now >= deadline:
                return
            self.clock.now += ScriptedCamera.step
            yield frame


class ScriptedEngine:
    """FaceEngine stand-in driven by the Frame objects the camera yields."""

    def __init__(self) -> None:
        self.detect_calls: list[float] = []
        self.clock: Clock | None = None

    def detect(self, frame: Frame) -> np.ndarray | None:
        if self.clock is not None:
            self.detect_calls.append(self.clock.now)
        if frame.embedding is None and not frame.embed_error:
            return None
        rows = np.zeros((frame.faces, 15), dtype=np.float32)
        rows[:, 2:4] = 40.0
        rows[:, 14] = 0.9
        return rows

    def embed(self, frame: Frame, _row: np.ndarray) -> np.ndarray:
        if frame.embed_error:
            raise EngineError("bad crop")
        assert frame.embedding is not None
        return frame.embedding.copy()


class ScriptedLiveness:
    """LivenessChecker stand-in with the real streak/confidence semantics."""

    min_consecutive = 1
    enabled = True
    instances: list["ScriptedLiveness"] = []

    def __init__(self, _cfg: dict[str, Any]) -> None:
        self.streak = 0
        self.resets = 0
        ScriptedLiveness.instances.append(self)

    def check(self, frame: Frame, row: np.ndarray | None) -> tuple[bool, str]:
        if not self.enabled:
            return True, "disabled"
        if row is None:
            self.streak = 0
            return False, "no_face"
        ok, reason = frame.live
        self.streak = self.streak + 1 if ok else 0
        return ok, reason

    def is_confident(self) -> bool:
        return not self.enabled or self.streak >= ScriptedLiveness.min_consecutive

    def reset(self) -> None:
        self.streak = 0
        self.resets += 1


@dataclass
class FakeStore:
    templates: dict[str, list[tuple[str, np.ndarray]]] = field(default_factory=dict)
    error: Exception | None = None
    added: list[tuple[str, str, list[np.ndarray]]] = field(default_factory=list)
    add_error: Exception | None = None
    calls: list[str] = field(default_factory=list)
    on_read: Any = None

    def embeddings_for(self, user: str) -> list[tuple[str, np.ndarray]]:
        self.calls.append(f"embeddings_for:{user}")
        if self.on_read is not None:
            self.on_read()
        if self.error is not None:
            raise self.error
        return list(self.templates.get(user, []))

    def add(self, user: str, name: str, samples: list[np.ndarray]) -> None:
        self.calls.append(f"add:{user}")
        if self.add_error is not None:
            raise self.add_error
        self.added.append((user, name, list(samples)))

    def list_faces(self, user: str) -> list[dict[str, Any]]:
        self.calls.append(f"list:{user}")
        return [{"name": n, "created": "", "samples": 1} for n, _ in self.templates.get(user, [])]

    def remove(self, user: str, name: str) -> bool:
        self.calls.append(f"remove:{user}")
        before = self.templates.get(user, [])
        after = [t for t in before if t[0] != name]
        self.templates[user] = after
        return len(after) != len(before)

    def clear(self, user: str) -> None:
        self.calls.append(f"clear:{user}")
        self.templates.pop(user, None)


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


class _DaemonCase(unittest.TestCase):
    config_text = CONFIG

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="iris-daemon-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "state").mkdir(mode=0o700)
        self.config_path = self.root / "config.toml"
        self.config_path.write_text(self.config_text.lstrip(), encoding="utf-8")

        self.clock = Clock()
        ScriptedCamera.script = []
        ScriptedCamera.step = 0.1
        ScriptedCamera.open_error = None
        ScriptedCamera.opened = 0
        ScriptedCamera.clock = self.clock
        ScriptedLiveness.min_consecutive = 1
        ScriptedLiveness.enabled = True
        ScriptedLiveness.instances = []

        fake_time = types.SimpleNamespace(monotonic=self.clock.monotonic, sleep=self.clock.sleep)
        for patcher in (
            mock.patch.object(daemon_module, "time", fake_time),
            mock.patch.object(daemon_module, "Camera", ScriptedCamera),
            mock.patch.object(daemon_module, "LivenessChecker", ScriptedLiveness),
            mock.patch.object(daemon_module.log, "disabled", True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        self.daemon = daemon_module.IrisDaemon(
            socket_path=os.fspath(self.root / "unused.sock"),
            config_path=os.fspath(self.config_path),
            state_dir=os.fspath(self.root / "state"),
            use_tpm=False,
        )
        self.store = FakeStore(templates={"alice": [("default", ALICE)]})
        self.daemon._store = self.store
        self.engine = ScriptedEngine()
        self.engine.clock = self.clock
        self.daemon._ensure_engine = lambda _cfg: self.engine

    def auth(self, user: str = "alice", **extra: Any) -> dict[str, Any]:
        return self.daemon._dispatch({"op": "auth", "user": user, **extra}, None)

    def set_config(self, section: str, **values: Any) -> None:
        self.daemon._op_config_set({"config": {section: values}}, None)


def matching(n: int, embedding: np.ndarray = ALICE) -> list[Frame]:
    return [face(embedding) for _ in range(n)]


# --------------------------------------------------------------------------- #
# Authentication decisions
# --------------------------------------------------------------------------- #


class AuthDecisionTests(_DaemonCase):
    def test_required_consecutive_matches_accept(self) -> None:
        ScriptedCamera.script = matching(3) + matching(10, STRANGER)
        response = self.auth()

        self.assertEqual(response["ok"], True)
        self.assertEqual(response["reason"], protocol.REASON_MATCH)
        self.assertEqual(response["face"], "default")
        self.assertAlmostEqual(response["confidence"], 1.0, places=5)
        self.assertEqual(set(response), {"ok", "confidence", "reason", "face"})

    def test_one_frame_short_of_the_streak_is_not_enough(self) -> None:
        ScriptedCamera.script = matching(2)
        response = self.auth()
        self.assertFalse(response["ok"])
        # Part-way through a streak when frames ran out: that is a timeout,
        # not a rejection of the face.
        self.assertEqual(response["reason"], protocol.REASON_TIMEOUT)

    def test_streak_must_be_consecutive(self) -> None:
        breakers = {
            "stranger": face(STRANGER),
            "empty frame": EMPTY,
            "liveness failure": face(live=(False, "flat_region")),
            "bad crop": Frame(embed_error=True),
        }
        for label, breaker in breakers.items():
            with self.subTest(label):
                ScriptedCamera.script = matching(2) + [breaker] + matching(2)
                self.assertFalse(self.auth()["ok"])
                ScriptedCamera.script = matching(2) + [breaker] + matching(3)
                self.assertTrue(self.auth()["ok"])

    def test_threshold_is_inclusive(self) -> None:
        probe = np.array([1.0, 1.0, 0.0], dtype=np.float32)
        exact = FaceEngine.compare(ALICE, probe)
        self.set_config("recognition", threshold=exact, required_matches=1)
        ScriptedCamera.script = [face(probe)]
        self.assertTrue(self.auth()["ok"])

        self.set_config("recognition", threshold=math.nextafter(exact, 2.0))
        ScriptedCamera.script = [face(probe)]
        self.assertFalse(self.auth()["ok"])

    def test_best_sample_across_named_faces_is_reported(self) -> None:
        self.store.templates["alice"] = [("default", STRANGER), ("glasses", ALICE_GLASSES)]
        ScriptedCamera.script = matching(3)
        response = self.auth()
        self.assertTrue(response["ok"])
        self.assertEqual(response["face"], "glasses")

    def test_first_live_frame_cannot_complete_a_match(self) -> None:
        # Liveness wants two live frames in a row before it is confident, so a
        # single-frame match requirement still needs two frames.
        ScriptedLiveness.min_consecutive = 2
        self.set_config("recognition", required_matches=1)
        ScriptedCamera.script = matching(1)
        self.assertFalse(self.auth()["ok"])
        ScriptedCamera.script = matching(2)
        self.assertTrue(self.auth()["ok"])

    def test_max_frames_caps_the_attempt(self) -> None:
        self.set_config("recognition", max_frames=5)
        ScriptedCamera.script = matching(50, STRANGER)
        response = self.auth()
        self.assertEqual(response["reason"], protocol.REASON_NO_MATCH)
        self.assertEqual(len(self.engine.detect_calls), 5)

    def test_no_match_reports_best_score_without_the_face_name(self) -> None:
        near = np.array([0.4, 0.9, 0.0], dtype=np.float32)
        ScriptedCamera.script = [face(STRANGER), face(near), face(STRANGER)]
        response = self.auth()
        self.assertEqual((response["ok"], response["reason"]), (False, protocol.REASON_NO_MATCH))
        self.assertAlmostEqual(response["confidence"], FaceEngine.compare(ALICE, near), places=5)
        self.assertIsNone(response["face"])

    def test_spoof_is_reported_generically(self) -> None:
        # The client learns "spoof_suspected", never which liveness test fired:
        # that would hand an attacker a tuning signal.
        ScriptedCamera.script = [face(live=(False, "no_ir_return"))] * 10
        response = self.auth()
        self.assertEqual(response["reason"], protocol.REASON_SPOOF_SUSPECTED)
        self.assertNotIn("no_ir_return", json.dumps(response))
        self.assertEqual(set(response), {"ok", "confidence", "reason", "face"})

    def test_positioning_problems_are_no_face_not_spoof(self) -> None:
        for reason in ("face_too_small", "out_of_frame"):
            with self.subTest(reason):
                ScriptedCamera.script = [face(live=(False, reason))] * 10
                self.assertEqual(self.auth()["reason"], protocol.REASON_NO_FACE)

    def test_empty_room_is_no_face(self) -> None:
        ScriptedCamera.script = [EMPTY] * 10
        self.assertEqual(self.auth()["reason"], protocol.REASON_NO_FACE)

    def test_no_illuminated_frames_is_a_camera_error(self) -> None:
        ScriptedCamera.script = []
        self.assertEqual(self.auth()["reason"], protocol.REASON_CAMERA_ERROR)

    def test_largest_face_is_the_one_judged(self) -> None:
        # select_face picks the largest box; every scripted row is the same size
        # here, so this checks a multi-face frame is handled, not crashed on.
        ScriptedCamera.script = [face(faces=3)] * 3
        self.assertTrue(self.auth()["ok"])


class AuthGuardTests(_DaemonCase):
    def test_disabled_answers_without_touching_camera_or_store(self) -> None:
        self.set_config("auth", enabled=False)
        response = self.auth()
        self.assertEqual(response["reason"], protocol.REASON_DISABLED)
        self.assertEqual(ScriptedCamera.opened, 0)
        self.assertEqual(self.store.calls, [])

    def test_not_enrolled_does_not_open_the_camera(self) -> None:
        response = self.auth(user="bob")
        self.assertEqual(response["reason"], protocol.REASON_NOT_ENROLLED)
        self.assertEqual(ScriptedCamera.opened, 0)

    def test_tampered_store_fails_closed_and_says_why(self) -> None:
        self.store.error = StoreError("alice.enc failed authentication")
        response = self.auth()
        self.assertEqual((response["ok"], response["reason"]), (False, protocol.REASON_NOT_ENROLLED))
        self.assertIn("failed authentication", response["error"])
        self.assertEqual(ScriptedCamera.opened, 0)

    def test_unexpected_handler_error_fails_closed_with_an_auth_shape(self) -> None:
        self.store.error = RuntimeError("boom")
        response = self.auth()
        self.assertFalse(response["ok"])
        self.assertIn(response["reason"], protocol.REASONS)
        self.assertIn("internal error", response["error"])

    def test_unsafe_usernames_are_rejected_before_the_store(self) -> None:
        for user in ("../root", "a/b", "", "   ", "-rf", "x" * 40, 7, None):
            with self.subTest(user=user):
                response = self.daemon._dispatch({"op": "auth", "user": user}, None)
                self.assertFalse(response["ok"])
                self.assertEqual(response["reason"], protocol.REASON_NOT_ENROLLED)
        self.assertEqual(self.store.calls, [])

    def test_models_unavailable_is_a_camera_error(self) -> None:
        def broken(_cfg: Any) -> Any:
            raise EngineError("models missing")

        self.daemon._ensure_engine = broken
        self.assertEqual(self.auth()["reason"], protocol.REASON_CAMERA_ERROR)
        self.assertEqual(self.daemon._engine_error, "models missing")

    def test_camera_open_failure_is_a_camera_error(self) -> None:
        ScriptedCamera.open_error = CameraError("cannot open /dev/video2")
        self.assertEqual(self.auth()["reason"], protocol.REASON_CAMERA_ERROR)

    def test_slow_template_read_leaves_no_capture_budget(self) -> None:
        self.store.on_read = lambda: self.clock.sleep(7.8)
        response = self.auth()
        self.assertEqual(response["reason"], protocol.REASON_TIMEOUT)
        self.assertEqual(ScriptedCamera.opened, 0)

    def test_capture_stops_at_the_deadline(self) -> None:
        ScriptedCamera.step = 1.0
        ScriptedCamera.script = matching(100, STRANGER)
        start = self.clock.now
        self.auth(timeout=3.0)
        self.assertLessEqual(self.clock.now - start, 3.0 + ScriptedCamera.step)

    def test_camera_busy_for_the_whole_budget_is_a_timeout(self) -> None:
        ScriptedCamera.script = matching(3)
        with self.daemon._camera_lock:
            response = self.auth(timeout=0.5)  # waits 0.5 s of real time
        self.assertEqual(response["reason"], protocol.REASON_TIMEOUT)
        self.assertEqual(ScriptedCamera.opened, 0)

    def test_shutdown_interrupts_an_attempt(self) -> None:
        ScriptedCamera.script = matching(3)
        self.daemon._stop.set()
        self.assertEqual(self.auth()["reason"], protocol.REASON_TIMEOUT)

    def test_client_timeout_is_clamped(self) -> None:
        cfg = self.daemon.current_config()
        cases = {
            None: 8.0, True: 8.0, "5": 8.0, float("nan"): 8.0, float("inf"): 8.0,
            0: 0.5, -3: 0.5, 2.5: 2.5, 1e9: 60.0,
        }
        for requested, expected in cases.items():
            with self.subTest(requested=requested):
                request = {} if requested is None else {"timeout": requested}
                self.assertEqual(daemon_module.IrisDaemon._auth_timeout(request, cfg), expected)


class LockoutTests(_DaemonCase):
    def _fail(self, frames: list[Frame]) -> dict[str, Any]:
        ScriptedCamera.script = frames
        return self.auth()

    def test_repeated_rejections_lock_the_user_out(self) -> None:
        for _ in range(3):
            self.assertEqual(self._fail(matching(3, STRANGER))["reason"], protocol.REASON_NO_MATCH)
        opened = ScriptedCamera.opened

        ScriptedCamera.script = matching(3)  # even the real user is refused now
        response = self.auth()
        self.assertEqual((response["ok"], response["reason"]), (False, protocol.REASON_LOCKOUT))
        self.assertGreater(response["retry_after"], 0)
        self.assertLessEqual(response["retry_after"], 60)
        self.assertEqual(ScriptedCamera.opened, opened)

    def test_spoof_attempts_count_towards_lockout(self) -> None:
        for _ in range(3):
            self._fail([face(live=(False, "saturated"))] * 5)
        self.assertEqual(self.auth()["reason"], protocol.REASON_LOCKOUT)

    def test_non_attempts_never_lock_the_user_out(self) -> None:
        for _ in range(10):
            self._fail([EMPTY] * 5)                          # no_face
            self._fail([face(live=(False, "out_of_frame"))])  # positioning
            self._fail([])                                    # camera_error
            self._fail(matching(2))                           # timeout mid-streak
        self.store.error = StoreError("tampered")
        for _ in range(10):
            self.auth()                                       # not_enrolled
        self.store.error = None
        ScriptedCamera.script = matching(3)
        self.assertTrue(self.auth()["ok"])

    def test_lockout_is_per_user(self) -> None:
        self.store.templates["bob"] = [("default", STRANGER)]
        for _ in range(3):
            self._fail(matching(3, OTHER_PERSON))
        ScriptedCamera.script = matching(3, STRANGER)
        self.assertTrue(self.auth(user="bob")["ok"])

    def test_success_clears_failure_history(self) -> None:
        for _ in range(2):
            self._fail(matching(3, STRANGER))
        ScriptedCamera.script = matching(3)
        self.assertTrue(self.auth()["ok"])
        for _ in range(2):
            self._fail(matching(3, STRANGER))
        ScriptedCamera.script = matching(3)
        self.assertTrue(self.auth()["ok"])

    def test_admin_changes_clear_a_lockout(self) -> None:
        clear_ops = {
            "remove": {"op": "remove", "user": "alice", "name": "default"},
            "clear": {"op": "clear", "user": "alice"},
        }
        for label, request in clear_ops.items():
            with self.subTest(label):
                self.store.templates["alice"] = [("default", ALICE)]
                self.daemon._failures = FailureTracker()
                for _ in range(3):
                    self._fail(matching(3, STRANGER))
                self.assertEqual(self.auth()["reason"], protocol.REASON_LOCKOUT)
                self.assertTrue(self.daemon._dispatch(request, None)["ok"])
                self.assertEqual(self.daemon._failures.failure_count("alice", 60), 0)


# --------------------------------------------------------------------------- #
# Enrolment
# --------------------------------------------------------------------------- #


class EnrollTests(_DaemonCase):
    def setUp(self) -> None:
        super().setUp()
        self.lines: list[dict[str, Any]] = []
        self.line_times: list[float] = []

        def record(_conn: Any, message: dict[str, Any]) -> None:
            self.lines.append(message)
            self.line_times.append(self.clock.now)

        patcher = mock.patch.object(daemon_module.protocol, "write_message", record)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Above the 0.25 s sample spacing, and chosen so no frame lands exactly
        # on a settle (1.2 s) or pose-timeout (12 s) boundary.
        ScriptedCamera.step = 0.35

    def enroll(self, name: str = "default", user: str = "alice") -> dict[str, Any]:
        return self.daemon._dispatch({"op": "enroll", "user": user, "name": name}, None)

    def test_full_pose_sequence_is_stored(self) -> None:
        ScriptedCamera.script = matching(200)
        response = self.enroll()

        self.assertTrue(response["ok"], response)
        self.assertEqual(response["samples"], daemon_module.ENROLL_TARGET_SAMPLES)
        self.assertEqual(
            response["poses"], {pose: 3 for pose, _ in daemon_module.ENROLL_POSES}
        )
        [(user, name, samples)] = self.store.added
        self.assertEqual((user, name, len(samples)), ("alice", "default", 15))

    def test_progress_stream_shape(self) -> None:
        ScriptedCamera.script = matching(200)
        self.enroll()

        self.assertEqual(self.lines[0]["stage"], "starting")
        self.assertEqual(self.lines[0]["progress"], 0.0)
        progress = [line["progress"] for line in self.lines]
        self.assertEqual(progress, sorted(progress))
        self.assertLess(max(progress), 1.0)  # 1.0 only arrives with the result
        for line in self.lines:
            self.assertEqual(line["total"], daemon_module.ENROLL_TARGET_SAMPLES)
            self.assertNotIn("ok", line)
        announced = [line["pose"] for line in self.lines if line.get("stage") == "pose"]
        self.assertEqual(announced, [pose for pose, _ in daemon_module.ENROLL_POSES])

    def test_frames_while_settling_into_a_pose_are_ignored(self) -> None:
        ScriptedCamera.script = matching(200)
        self.enroll()
        announcements = [
            t for t, line in zip(self.line_times, self.lines) if line.get("stage") == "pose"
        ]
        for announced in announcements:
            settling = [
                t for t in self.engine.detect_calls
                if announced <= t < announced + daemon_module.ENROLL_POSE_SETTLE
            ]
            self.assertEqual(settling, [], f"detected during settle after {announced}")

    def test_samples_are_spaced_out(self) -> None:
        ScriptedCamera.step = 0.1
        ScriptedCamera.script = matching(400)
        self.enroll()
        captured = [t for t, line in zip(self.line_times, self.lines) if line.get("stage") == "captured"]
        gaps = [b - a for a, b in zip(captured, captured[1:])]
        self.assertTrue(all(g >= daemon_module.ENROLL_SAMPLE_INTERVAL - 1e-9 for g in gaps), gaps)

    def test_second_person_is_never_added_to_the_template(self) -> None:
        # Alice first, so she is the reference; then someone else keeps leaning
        # in between her samples.
        mixed = [face(ALICE)] + [face(OTHER_PERSON)] * 3
        ScriptedCamera.script = matching(8) + mixed * 100
        response = self.enroll()
        self.assertTrue(response["ok"], response)
        [(_user, _name, samples)] = self.store.added
        for sample in samples:
            self.assertGreaterEqual(
                FaceEngine.compare(sample, ALICE), daemon_module.ENROLL_IDENTITY_FLOOR
            )
        self.assertTrue(any(l["hint"] == "Keep the same person in frame" for l in self.lines))

    def test_spoof_frames_are_not_enrolled_and_the_user_is_told_why(self) -> None:
        ScriptedCamera.script = [face(live=(False, "no_ir_return"))] * 400
        response = self.enroll()
        self.assertFalse(response["ok"])
        self.assertEqual(response["reason"], protocol.REASON_SPOOF_SUSPECTED)
        self.assertEqual(self.store.added, [])
        # Enrolment *does* explain the problem: the user is present and cooperating.
        self.assertIn(daemon_module._LIVENESS_HINTS["no_ir_return"], {l["hint"] for l in self.lines})

    def test_straight_on_view_is_required(self) -> None:
        # Nobody in frame for the whole centre pose, then a perfect run of the
        # other four poses: 12 samples, but no straight-on view.
        pose_frames = int(daemon_module.ENROLL_POSE_TIMEOUT / ScriptedCamera.step) + 2
        ScriptedCamera.script = [EMPTY] * pose_frames + matching(400)
        response = self.enroll()
        self.assertFalse(response["ok"])
        self.assertEqual(response["samples"], 12)
        self.assertEqual(self.store.added, [])

    def test_too_few_samples_fail_but_a_partial_schedule_succeeds(self) -> None:
        pose_frames = int(daemon_module.ENROLL_POSE_TIMEOUT / ScriptedCamera.step) + 2
        # Centre and left captured (3 settle + 3 sample frames each, plus the
        # frame that advances the pose), then the user leaves: 6 is enough.
        ScriptedCamera.script = matching(13) + [EMPTY] * (pose_frames * 3 + 5)
        response = self.enroll()
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["samples"], daemon_module.ENROLL_MIN_SAMPLES)

        # Centre only: 3 samples is not.
        self.store.added.clear()
        ScriptedCamera.script = matching(6) + [EMPTY] * (pose_frames * 4 + 5)
        response = self.enroll()
        self.assertFalse(response["ok"])
        self.assertEqual(self.store.added, [])

    def test_success_clears_a_lockout(self) -> None:
        for _ in range(3):
            self.daemon._failures.record_failure("alice")
        ScriptedCamera.script = matching(200)
        self.assertTrue(self.enroll()["ok"])
        self.assertEqual(self.daemon._failures.failure_count("alice", 60), 0)

    def test_camera_busy(self) -> None:
        with mock.patch.object(daemon_module, "ENROLL_LOCK_TIMEOUT", 0.05), self.daemon._camera_lock:
            response = self.enroll()
        self.assertEqual((response["ok"], response["reason"]), (False, protocol.REASON_CAMERA_ERROR))
        self.assertEqual(self.store.calls, [])

    def test_camera_and_model_failures_store_nothing(self) -> None:
        ScriptedCamera.open_error = CameraError("unplugged")
        self.assertEqual(self.enroll()["reason"], protocol.REASON_CAMERA_ERROR)
        ScriptedCamera.open_error = None

        def broken(_cfg: Any) -> Any:
            raise EngineError("models missing")

        self.daemon._ensure_engine = broken
        self.assertEqual(self.enroll()["reason"], protocol.REASON_CAMERA_ERROR)
        self.assertEqual(self.store.added, [])
        self.assertFalse(self.daemon._camera_lock.locked())

    def test_store_failure_is_reported(self) -> None:
        self.store.add_error = StoreError("disk full")
        ScriptedCamera.script = matching(200)
        response = self.enroll()
        self.assertEqual((response["ok"], response["error"]), (False, "disk full"))

    def test_invalid_names_are_rejected_before_capture(self) -> None:
        for name in ("", "  ", "x" * 65, "bad\nname", "bad\x7fname", 3, None):
            with self.subTest(name=name):
                response = self.daemon._dispatch({"op": "enroll", "user": "alice", "name": name}, None)
                self.assertFalse(response["ok"])
        self.assertEqual(ScriptedCamera.opened, 0)
        self.assertEqual(self.lines, [])


# --------------------------------------------------------------------------- #
# Failure classification
# --------------------------------------------------------------------------- #


class ClassifyFailureTests(unittest.TestCase):
    def _counters(self, lit=0, faces=0, embedded=0, **reasons: int):
        counters = daemon_module._CaptureCounters(lit, faces, embedded)
        counters.live_reasons.update(reasons)
        return counters

    def test_matrix(self) -> None:
        classify = daemon_module._classify_failure
        cases = [
            (self._counters(), False, protocol.REASON_CAMERA_ERROR),
            (self._counters(lit=5, faces=5, embedded=5), True, protocol.REASON_TIMEOUT),
            (self._counters(lit=5, faces=5, embedded=1), False, protocol.REASON_NO_MATCH),
            (self._counters(lit=5, faces=3, saturated=3), False, protocol.REASON_SPOOF_SUSPECTED),
            (self._counters(lit=5, faces=3, face_too_small=2, saturated=1), False,
             protocol.REASON_NO_FACE),
            (self._counters(lit=5), False, protocol.REASON_NO_FACE),
        ]
        for counters, progressed, expected in cases:
            with self.subTest(expected=expected, progressed=progressed):
                self.assertEqual(classify(counters, progressed=progressed), expected)

    def test_only_presented_faces_count_towards_lockout(self) -> None:
        self.assertEqual(
            daemon_module._COUNTED_FAILURE_REASONS,
            {protocol.REASON_NO_MATCH, protocol.REASON_SPOOF_SUSPECTED},
        )


# --------------------------------------------------------------------------- #
# Admin ops and configuration
# --------------------------------------------------------------------------- #


class AdminOpsTests(_DaemonCase):
    def test_list_returns_metadata_only(self) -> None:
        response = self.daemon._dispatch({"op": "list", "user": "alice"}, None)
        self.assertEqual(response, {"ok": True, "faces": [{"name": "default", "created": "", "samples": 1}]})

    def test_remove_missing_face(self) -> None:
        response = self.daemon._dispatch({"op": "remove", "user": "alice", "name": "nope"}, None)
        self.assertFalse(response["ok"])

    def test_path_traversal_never_reaches_the_store(self) -> None:
        for op in ("list", "remove", "clear", "enroll"):
            with self.subTest(op=op):
                request = {"op": op, "user": "../../etc/passwd", "name": "x"}
                self.assertFalse(self.daemon._dispatch(request, None)["ok"])
        self.assertEqual(self.store.calls, [])

    def test_unknown_op(self) -> None:
        for op in ("shell", None, 3, ["auth"]):
            with self.subTest(op=op):
                self.assertFalse(self.daemon._dispatch({"op": op}, None)["ok"])


class ConfigOpsTests(_DaemonCase):
    def test_partial_update_merges_instead_of_resetting(self) -> None:
        response = self.daemon._dispatch({"op": "config_set", "config": {"auth": {"enabled": False}}}, None)
        self.assertTrue(response["ok"], response)
        cfg = response["config"]
        self.assertFalse(cfg["auth"]["enabled"])
        self.assertEqual(cfg["auth"]["max_failures"], 3)
        self.assertEqual(cfg["recognition"]["threshold"], 0.5)
        self.assertEqual(cfg["camera"]["width"], 16)

    def test_effective_clamped_values_are_reported(self) -> None:
        with self.assertLogs("iris.config", "WARNING"):
            response = self.daemon._dispatch(
                {"op": "config_set", "config": {"auth": {"max_failures": 1000}}}, None
            )
        self.assertEqual(response["config"]["auth"]["max_failures"], FailureTracker._MAX_HISTORY)

    def test_mistyped_values_are_rejected_not_silently_defaulted(self) -> None:
        bad = [
            {"auth": {"enabled": 1}},
            {"auth": {"max_failures": True}},
            {"auth": {"max_failures": 2.5}},
            {"recognition": {"threshold": "0.9"}},
            {"camera": {"device": 5}},
            {"auth": {"custom": [1, 2]}},
            {"auth": "off"},
            {"auth": {"enabled": {"nested": True}}},
        ]
        before = self.config_path.read_text()
        for config in bad:
            with self.subTest(config=config):
                response = self.daemon._dispatch({"op": "config_set", "config": config}, None)
                self.assertFalse(response["ok"])
        self.assertEqual(self.config_path.read_text(), before)
        self.assertFalse(self.daemon._dispatch({"op": "config_set", "config": []}, None)["ok"])

    def test_non_finite_numbers_are_rejected_not_silently_defaulted(self) -> None:
        # The loader drops NaN/infinity and keeps the default, so accepting them
        # here would report success for a write that changed nothing.
        before = self.config_path.read_text()
        for value in (float("nan"), float("inf"), float("-inf")):
            for section, key in (("recognition", "threshold"), ("auth", "timeout")):
                with self.subTest(key=f"{section}.{key}", value=value):
                    response = self.daemon._dispatch(
                        {"op": "config_set", "config": {section: {key: value}}}, None
                    )
                    self.assertFalse(response["ok"])
                    self.assertIn(f"{section}.{key}", response["error"])
        self.assertEqual(self.config_path.read_text(), before)

    def test_int_is_accepted_for_a_float_setting(self) -> None:
        response = self.daemon._dispatch(
            {"op": "config_set", "config": {"auth": {"timeout": 5}}}, None
        )
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["config"]["auth"]["timeout"], 5.0)

    def test_metadata_node_is_refused_as_the_camera(self) -> None:
        cameras = [{"path": "/dev/video3", "is_metadata": True}, {"path": "/dev/video2", "is_metadata": False}]
        with mock.patch.object(daemon_module, "list_cameras", return_value=cameras):
            bad = self.daemon._dispatch({"op": "config_set", "config": {"camera": {"device": "/dev/video3"}}}, None)
            good = self.daemon._dispatch({"op": "config_set", "config": {"camera": {"device": "/dev/video2"}}}, None)
        self.assertFalse(bad["ok"])
        self.assertIn("metadata", bad["error"])
        self.assertTrue(good["ok"], good)

    def test_config_file_edits_are_picked_up_without_restart(self) -> None:
        self.assertEqual(self.daemon.current_config()["recognition"]["threshold"], 0.5)
        self.config_path.write_text(CONFIG.replace("threshold = 0.5", "threshold = 0.9"), encoding="utf-8")
        os.utime(self.config_path, ns=(1, 1))  # guarantee a new stamp even within one tick
        self.assertEqual(self.daemon.current_config()["recognition"]["threshold"], 0.9)

    def test_current_config_is_a_private_copy(self) -> None:
        self.daemon.current_config()["auth"]["enabled"] = False
        self.assertTrue(self.daemon.current_config()["auth"]["enabled"])


class EngineCacheTests(_DaemonCase):
    def test_engine_is_rebuilt_only_when_its_settings_change(self) -> None:
        daemon = daemon_module.IrisDaemon(
            socket_path=os.fspath(self.root / "unused2.sock"),
            config_path=os.fspath(self.config_path),
            state_dir=os.fspath(self.root / "state"),
            use_tpm=False,
        )
        built: list[dict[str, Any]] = []

        class CountingEngine:
            model_dir = "/models"
            threshold = 0.5
            detect_score = 0.7

            def __init__(self, cfg: dict[str, Any]) -> None:
                built.append(cfg)

        with mock.patch.object(daemon_module, "FaceEngine", CountingEngine):
            cfg = daemon.current_config()
            first = daemon._ensure_engine(cfg)
            self.assertIs(daemon._ensure_engine(cfg), first)

            cfg["auth"]["timeout"] = 30.0          # not an engine setting
            cfg["liveness"]["min_variance"] = 99.0  # nor this
            self.assertIs(daemon._ensure_engine(cfg), first)

            cfg["recognition"]["threshold"] = 0.9
            second = daemon._ensure_engine(cfg)
            self.assertIsNot(second, first)

            cfg["camera"]["width"] = 32
            self.assertIsNot(daemon._ensure_engine(cfg), second)
        self.assertEqual(len(built), 3)


# --------------------------------------------------------------------------- #
# Connection and startup safety
# --------------------------------------------------------------------------- #


class ConnectionSafetyTests(_DaemonCase):
    def _serve(self, peer_uid: int, request: dict[str, Any] | None) -> dict[str, Any] | None:
        server, client = socket.socketpair()
        self.addCleanup(client.close)
        creds = daemon_module.PeerCredentials(pid=1, uid=peer_uid, gid=peer_uid)
        with mock.patch.object(daemon_module.IrisDaemon, "_peer_credentials", return_value=creds):
            worker = threading.Thread(target=self.daemon._serve_connection, args=(server,))
            worker.start()
            client.settimeout(2.0)
            if request is not None:
                protocol.write_message(client, request)
            response = protocol.read_message(client)
            client.shutdown(socket.SHUT_WR)
            worker.join(2.0)
        self.assertFalse(worker.is_alive())
        return response

    def test_non_root_peer_is_refused_before_any_request_runs(self) -> None:
        # The refusal is sent on connect, before the client can say anything.
        ScriptedCamera.script = matching(3)
        response = self._serve(1000, None)
        assert response is not None
        self.assertFalse(response["ok"])
        self.assertIn("permission denied", response["error"])
        self.assertEqual(self.store.calls, [])
        self.assertEqual(ScriptedCamera.opened, 0)

    def test_root_peer_is_served(self) -> None:
        response = self._serve(0, {"op": "ping"})
        assert response is not None
        self.assertTrue(response["ok"])

    def test_connection_cap(self) -> None:
        busy = threading.Event()
        threads = {threading.Thread(target=busy.wait) for _ in range(daemon_module.MAX_CONNECTIONS)}
        for thread in threads:
            thread.start()
        self.addCleanup(lambda: (busy.set(), [t.join() for t in threads]))
        self.daemon._threads = set(threads)

        server, client = socket.socketpair()
        self.addCleanup(client.close)
        listener = mock.Mock()
        listener.accept.return_value = (server, None)
        self.daemon._accept_one(listener)

        client.settimeout(2.0)
        response = protocol.read_message(client)
        assert response is not None
        self.assertEqual(response, {"ok": False, "error": "irisd is busy; try again"})
        self.assertEqual(len(self.daemon._threads), daemon_module.MAX_CONNECTIONS)


class StartupSafetyTests(_DaemonCase):
    def _daemon_at(self, socket_path: Path) -> daemon_module.IrisDaemon:
        return daemon_module.IrisDaemon(
            socket_path=os.fspath(socket_path),
            config_path=os.fspath(self.config_path),
            state_dir=os.fspath(self.root / "state"),
            use_tpm=False,
        )

    def test_live_socket_is_never_stolen(self) -> None:
        path = self.root / "live.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(os.fspath(path))
        listener.listen(1)
        with self.assertRaises(daemon_module.DaemonStartupError):
            self._daemon_at(path)._clear_stale_socket()
        self.assertTrue(path.exists())

    def test_stale_socket_is_removed(self) -> None:
        path = self.root / "stale.sock"
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(os.fspath(path))
        stale.close()
        self._daemon_at(path)._clear_stale_socket()
        self.assertFalse(path.exists())

    def test_non_socket_file_is_never_removed(self) -> None:
        path = self.root / "not-a-socket"
        path.write_text("important")
        with self.assertRaises(daemon_module.DaemonStartupError):
            self._daemon_at(path)._clear_stale_socket()
        self.assertEqual(path.read_text(), "important")

    def test_symlinked_runtime_dir_is_refused(self) -> None:
        real = self.root / "elsewhere"
        real.mkdir()
        (self.root / "run").symlink_to(real)
        with self.assertRaisesRegex(daemon_module.DaemonStartupError, "symlink"):
            self._daemon_at(self.root / "run" / "socket")._prepare_runtime_dir()

    def test_refuses_to_run_unprivileged(self) -> None:
        # If the guard ever goes, fail fast here instead of starting a listener.
        started = AssertionError("irisd started without root")
        with mock.patch.object(daemon_module.os, "geteuid", return_value=1000), \
                mock.patch.object(self.daemon, "_prepare_state_dir", side_effect=started):
            with self.assertRaisesRegex(daemon_module.DaemonStartupError, "must run as root"):
                self.daemon.run()


if __name__ == "__main__":
    unittest.main()
