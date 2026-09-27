"""iris.gui.backend: CLI invocation, error wording, face listing and enrolment.

The real functions run real subprocesses; only the programs are fakes: a shell
script stands in for ``iris`` and another for ``pkexec``.  GTK is not needed:
a minimal ``gi.repository`` stand-in queues ``GLib.idle_add`` callbacks so the
tests can run the "main loop" by hand.
"""

from __future__ import annotations

import importlib
import json
import os
import stat
import sys
import tempfile
import textwrap
import time
import types
import unittest
from pathlib import Path
from typing import Any, Callable
from unittest import mock


class _FakeGLib:
    PRIORITY_DEFAULT = 0
    SOURCE_REMOVE = False
    SOURCE_CONTINUE = True

    def __init__(self) -> None:
        self.queue: list[tuple[Callable[..., Any], tuple[Any, ...]]] = []
        self.timeouts: dict[int, Callable[[], bool]] = {}
        self._next = 1

    def idle_add(self, fn: Callable[..., Any], *args: Any, priority: int = 0) -> int:
        self.queue.append((fn, args))
        return 0

    def timeout_add(self, _ms: int, fn: Callable[[], bool]) -> int:
        self._next += 1
        self.timeouts[self._next] = fn
        return self._next

    def source_remove(self, source: int) -> None:
        self.timeouts.pop(source, None)

    def run_pending(self) -> None:
        while self.queue:
            fn, args = self.queue.pop(0)
            fn(*args)


def _import_backend() -> tuple[types.ModuleType, _FakeGLib]:
    glib = _FakeGLib()
    repository = types.SimpleNamespace(GLib=glib, Gdk=types.SimpleNamespace())
    gi = types.ModuleType("gi")
    gi.require_version = lambda _name, _version: None  # type: ignore[attr-defined]
    fakes = {"gi": gi, "gi.repository": repository}
    saved = {name: sys.modules.get(name) for name in (*fakes, "iris.gui", "iris.gui.backend")}
    sys.modules.update(fakes)  # type: ignore[arg-type]
    sys.modules.pop("iris.gui", None)
    sys.modules.pop("iris.gui.backend", None)
    try:
        module = importlib.import_module("iris.gui.backend")
    finally:
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
    return module, glib


backend, GLIB = _import_backend()


class _FakeCli(unittest.TestCase):
    """Temp dir with a scriptable fake ``iris`` and a pass-through ``pkexec``."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="iris-gui-")
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.calls = self.dir / "calls.log"
        self.iris = self.dir / "iris"
        self.pkexec = self.dir / "pkexec"
        self.write_script(self.pkexec, 'echo "pkexec" >> "$CALLS"\nexec "$@"\n')
        self.write_cli('echo \'{"ok": true}\'')
        for patcher in (
            mock.patch.object(backend, "iris_binary", return_value=str(self.iris)),
            mock.patch.object(backend, "pkexec_binary", return_value=str(self.pkexec)),
            mock.patch.dict(os.environ, {"CALLS": str(self.calls)}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        GLIB.queue.clear()
        GLIB.timeouts.clear()

    def write_script(self, path: Path, body: str) -> None:
        path.write_text("#!/bin/sh\n" + textwrap.dedent(body), encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def write_cli(self, body: str) -> None:
        """The fake iris logs its argv (and stdin, if any) then runs *body*."""
        self.write_script(
            self.iris,
            'echo "iris $*" >> "$CALLS"\n' + textwrap.dedent(body) + "\n",
        )

    def logged(self) -> list[str]:
        if not self.calls.exists():
            return []
        return self.calls.read_text(encoding="utf-8").splitlines()


# --------------------------------------------------------------------------- #
# Value objects and small helpers
# --------------------------------------------------------------------------- #


class FaceTests(unittest.TestCase):
    def test_display_text(self) -> None:
        face = backend.Face.from_dict({"name": "default", "created": "2026-09-02T10:00:00Z", "samples": 12})
        self.assertEqual(face.title, "Your face")
        self.assertIn("2026", face.subtitle)
        self.assertTrue(face.subtitle.endswith("12 samples"))

        self.assertEqual(backend.Face("glasses", "", 1).subtitle, "1 sample")
        self.assertEqual(backend.Face("glasses", "", 0).subtitle, "Enrolled")
        self.assertEqual(backend.Face("glasses", "", 0).title, "glasses")

    def test_bad_or_missing_fields_degrade(self) -> None:
        face = backend.Face.from_dict({"name": 7, "created": "not a date", "samples": None})
        self.assertEqual((face.name, face.samples, face.created_display), ("7", 0, ""))
        self.assertEqual(backend.Face.from_dict({}).samples, 0)
        self.assertEqual(backend.Face.from_dict({"samples": "3"}).samples, 3)


class CameraListTests(unittest.TestCase):
    def test_metadata_nodes_dropped_and_infrared_first(self) -> None:
        cameras = [
            {"path": "/dev/video0", "name": "FHD", "is_ir": False, "is_metadata": False, "formats": ["MJPG"]},
            {"path": "/dev/video1", "name": "FHD", "is_ir": False, "is_metadata": True, "formats": []},
            {"path": "/dev/video4", "name": "IR 2", "is_ir": True, "is_metadata": False, "formats": ["GREY"]},
            {"path": "/dev/video2", "name": "IR", "is_ir": True, "is_metadata": False, "formats": ["GREY"]},
        ]
        with mock.patch("iris.camera.list_cameras", return_value=cameras):
            listed = backend.list_capture_cameras()
        self.assertEqual([c.path for c in listed], ["/dev/video2", "/dev/video4", "/dev/video0"])
        self.assertEqual(listed[0].subtitle, "Infrared · /dev/video2 · GREY")
        self.assertEqual(backend.CameraInfo("/dev/x", "", False, ()).title, "/dev/x")


class ParsingHelperTests(unittest.TestCase):
    def test_progress_lines(self) -> None:
        parse = backend._parse_progress_line
        self.assertEqual(parse('{"progress": 0.5, "hint": "left"}'), {"progress": 0.5, "hint": "left"})
        self.assertIsNone(parse("{not json"))
        self.assertIsNone(parse("[1, 2]"))
        self.assertIsNone(parse("Loading models"))
        self.assertEqual(parse("Capturing... 40%"), {"progress": 0.4, "hint": "Capturing"})
        self.assertEqual(parse("12.5 %")["progress"], 0.125)

    def test_clamp_and_int(self) -> None:
        for raw, want in ((0.5, 0.5), (-1, 0.0), (7, 1.0), ("0.25", 0.25), ("x", 0.0), (None, 0.0), (float("nan"), 0.0)):
            with self.subTest(raw=raw):
                self.assertEqual(backend._clamp01(raw), want)
        for raw, want in ((3, 3), ("4", 4), (2.9, 2), (None, None), ("x", None), (float("inf"), None), (float("nan"), None)):
            with self.subTest(raw=raw):
                self.assertEqual(backend._as_int(raw), want)

    def test_current_user_comes_from_the_uid_not_the_environment(self) -> None:
        import pwd

        spoofed = {"USER": "not-a-real-account", "LOGNAME": "not-a-real-account"}
        with mock.patch.dict(os.environ, spoofed):
            self.assertEqual(backend.current_user(), pwd.getpwuid(os.getuid()).pw_name)


# --------------------------------------------------------------------------- #
# Running the CLI
# --------------------------------------------------------------------------- #


class RunCliTests(_FakeCli):
    def test_helper_missing(self) -> None:
        with mock.patch.object(backend, "iris_binary", return_value=None):
            with self.assertRaises(backend.HelperMissing) as caught:
                backend._run_cli([["list"]], [], privileged=False)
        self.assertFalse(caught.exception.retryable)

    def test_privileged_needs_pkexec(self) -> None:
        with mock.patch.object(backend, "pkexec_binary", return_value=None):
            with self.assertRaises(backend.BackendError) as caught:
                backend._run_cli([["clear"]], [], privileged=True)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(self.logged(), [])

    def test_privileged_goes_through_pkexec_unprivileged_does_not(self) -> None:
        backend._run_cli([["list"]], ["--json"], privileged=False)
        backend._run_cli([["clear"]], ["--json"], privileged=True)
        self.assertEqual(self.logged(), ["iris list --json", "pkexec", "iris clear --json"])

    def test_usage_error_falls_through_to_the_next_spelling(self) -> None:
        self.write_cli('''
            if [ "$1" = "config-set" ]; then echo "usage: iris ... invalid choice: 'config-set'" >&2; exit 2; fi
            cat > "$CALLS.stdin"; echo '{"ok": true}'
        ''')
        backend.save_config({"auth": {"enabled": False}})
        self.assertEqual(
            [line for line in self.logged() if line != "pkexec"],
            ["iris config-set --json", "iris config set --json"],
        )
        sent = json.loads(Path(f"{self.calls}.stdin").read_text(encoding="utf-8"))
        self.assertEqual(sent, {"auth": {"enabled": False}})

    def test_a_real_failure_is_not_retried(self) -> None:
        # Retrying would mean a second password prompt for the same failure.
        self.write_cli('echo "boom" >&2; exit 1')
        with self.assertRaises(backend.BackendError):
            backend.save_config({})
        self.assertEqual([line for line in self.logged() if line.startswith("iris")], ["iris config-set --json"])

    def test_exit_2_without_a_usage_message_is_not_retried(self) -> None:
        self.write_cli('echo "disk full" >&2; exit 2')
        with self.assertRaises(backend.BackendError):
            backend.save_config({})
        self.assertEqual(len([line for line in self.logged() if line.startswith("iris")]), 1)

    def test_timeout(self) -> None:
        self.write_cli("sleep 5")
        started = time.monotonic()
        with self.assertRaises(backend.BackendError) as caught:
            backend._run_cli([["list"]], [], privileged=False, timeout=0.3)
        self.assertLess(time.monotonic() - started, 3)
        self.assertIn("did not respond in time", caught.exception.message)

    def test_stderr_is_capped(self) -> None:
        self.write_cli("head -c 100000 /dev/zero | tr '\\0' x >&2; exit 1")
        result = backend._run_cli([["list"]], [], privileged=False)
        self.assertEqual(len(result.stderr), backend._MAX_STDERR_BYTES)

    def test_binary_that_vanished(self) -> None:
        self.iris.unlink()
        with self.assertRaises(backend.HelperMissing):
            backend._run_cli([["list"]], [], privileged=False)


class FinalJsonTests(unittest.TestCase):
    def result(self, code: int, stdout: str = "", stderr: str = "") -> Any:
        return backend._Result(code, stdout, stderr, ["pkexec", "iris", "clear"])

    def test_last_object_is_the_verdict(self) -> None:
        stdout = 'noise\n{"progress": 0.5}\n{broken\n{"ok": true, "samples": 12}\n'
        self.assertEqual(backend._final_json(self.result(0, stdout)), {"ok": True, "samples": 12})

    def test_failure_messages_are_written_for_people(self) -> None:
        cases = [
            (self.result(126), backend.AuthorisationCancelled, "Administrator approval"),
            (self.result(127, stderr="nope"), backend.BackendError, "not allowed"),
            (self.result(1, '{"ok": false, "reason": "no_face"}'), backend.BackendError, "No face was visible"),
            (self.result(1, '{"ok": false, "error": "disk full"}'), backend.BackendError, "disk full"),
            (self.result(3, stderr="socket gone"), backend.BackendError, "Nothing was changed"),
            (self.result(0, ""), backend.BackendError, "Nothing was changed"),
            (self.result(0, '{"ok": false}'), backend.BackendError, "Nothing was changed"),
        ]
        for result, kind, text in cases:
            with self.subTest(code=result.returncode, stdout=result.stdout):
                with self.assertRaises(kind) as caught:
                    backend._final_json(result)
                self.assertIn(text, caught.exception.message)

    def test_not_authorised_is_not_retryable_and_names_the_command(self) -> None:
        with self.assertRaises(backend.BackendError) as caught:
            backend._final_json(self.result(127, stderr="Not authorized"))
        self.assertFalse(caught.exception.retryable)
        self.assertIn("pkexec iris clear", caught.exception.detail)

    def test_cancelled_prompt_with_a_json_error_reports_the_error(self) -> None:
        with self.assertRaises(backend.BackendError) as caught:
            backend._final_json(self.result(126, '{"ok": false, "error": "no such user"}'))
        self.assertNotIsInstance(caught.exception, backend.AuthorisationCancelled)


# --------------------------------------------------------------------------- #
# Face management
# --------------------------------------------------------------------------- #


class FetchFacesTests(_FakeCli):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(backend, "_daemon_request", return_value=None)
        self.daemon = patcher.start()
        self.addCleanup(patcher.stop)

    def test_daemon_answer_wins_without_running_anything(self) -> None:
        self.daemon.return_value = {"ok": True, "faces": [{"name": "default", "samples": 3}, "junk"]}
        faces = backend.fetch_faces("alice", allow_prompt=False)
        self.assertEqual([f.name for f in faces], ["default"])
        self.assertEqual(self.logged(), [])

    def test_unprivileged_cli_is_tried_before_any_prompt(self) -> None:
        self.write_cli('echo \'{"ok": true, "faces": [{"name": "glasses", "samples": 5}]}\'')
        faces = backend.fetch_faces("alice", allow_prompt=True)
        self.assertEqual([(f.name, f.samples) for f in faces], [("glasses", 5)])
        self.assertEqual(self.logged(), ["iris list --user alice --json"])

    def test_opening_the_panel_never_prompts(self) -> None:
        self.write_cli('echo "permission denied" >&2; exit 4')
        with self.assertRaises(backend.BackendError) as caught:
            backend.fetch_faces("alice", allow_prompt=False)
        self.assertIn("needs administrator approval", caught.exception.message)
        self.assertNotIn("pkexec", self.logged())

    def test_unlock_escalates_to_pkexec(self) -> None:
        self.write_cli('''
            if [ -z "$ESCALATED" ]; then exit 4; fi
            echo '{"ok": true, "faces": []}'
        ''')
        self.write_script(self.pkexec, 'echo "pkexec" >> "$CALLS"\nESCALATED=1 exec "$@"\n')
        self.assertEqual(backend.fetch_faces("alice", allow_prompt=True), [])
        self.assertEqual(self.logged(), ["iris list --user alice --json", "pkexec", "iris list --user alice --json"])

    def test_a_zero_exit_without_a_verdict_still_escalates(self) -> None:
        self.write_cli('if [ -z "$ESCALATED" ]; then echo "garbage"; exit 0; fi; echo \'{"ok": true, "faces": []}\'')
        self.write_script(self.pkexec, 'echo "pkexec" >> "$CALLS"\nESCALATED=1 exec "$@"\n')
        self.assertEqual(backend.fetch_faces("alice", allow_prompt=True), [])
        self.assertIn("pkexec", self.logged())

    def test_remove_and_clear(self) -> None:
        backend.remove_face("alice", "glasses")
        backend.clear_faces("alice")
        self.assertEqual(
            self.logged(),
            ["pkexec", "iris remove --user alice --name glasses --json", "pkexec", "iris clear --user alice --json"],
        )

    def test_dismissed_prompt(self) -> None:
        self.write_script(self.pkexec, "exit 126\n")
        with self.assertRaises(backend.AuthorisationCancelled):
            backend.clear_faces("alice")


class RunAsyncTests(unittest.TestCase):
    def setUp(self) -> None:
        GLIB.queue.clear()

    def _run(self, work: Callable[[], Any]) -> list[tuple[Any, Any]]:
        delivered: list[tuple[Any, Any]] = []
        backend.run_async(work, lambda value, error: delivered.append((value, error)))
        deadline = time.monotonic() + 5
        while not GLIB.queue and time.monotonic() < deadline:
            time.sleep(0.01)
        GLIB.run_pending()
        return delivered

    def test_value_and_errors_arrive_on_the_main_loop(self) -> None:
        self.assertEqual(self._run(lambda: 42), [(42, None)])

        def fails() -> None:
            raise backend.BackendError("camera unplugged")

        [(value, error)] = self._run(fails)
        self.assertIsNone(value)
        self.assertEqual(error.message, "camera unplugged")

    def test_a_crashing_worker_still_reports(self) -> None:
        with self.assertLogs("iris.gui.backend", "ERROR"):
            [(value, error)] = self._run(lambda: 1 / 0)
        self.assertIsNone(value)
        self.assertIn("Something went wrong", error.message)
        self.assertIn("ZeroDivisionError", error.detail)

    def test_a_raising_callback_does_not_escape(self) -> None:
        def boom(_value: Any, _error: Any) -> None:
            raise RuntimeError("widget gone")

        with self.assertLogs("iris.gui.backend", "ERROR"):
            self.assertIs(backend._deliver(boom, 1, None), GLIB.SOURCE_REMOVE)


# --------------------------------------------------------------------------- #
# Enrolment
# --------------------------------------------------------------------------- #


class EnrollProcessTests(_FakeCli):
    def enroll(self, **callbacks: Any) -> tuple[Any, list[Any], list[Any]]:
        progress: list[Any] = []
        finished: list[Any] = []
        proc = backend.EnrollProcess(
            "alice", "default",
            on_progress=progress.append,
            on_finished=lambda final, error: finished.append((final, error)),
            **callbacks,
        )
        proc.start()
        self.addCleanup(self._close_pipes, proc)
        deadline = time.monotonic() + 10
        while not finished and time.monotonic() < deadline:
            GLIB.run_pending()
            time.sleep(0.01)
        GLIB.run_pending()
        return proc, progress, finished

    @staticmethod
    def _close_pipes(proc: Any) -> None:
        if proc._proc is not None:
            if proc._proc.poll() is None:  # a failed test must not leave it blocked
                proc._proc.kill()
                proc._proc.wait()
            for stream in (proc._proc.stdout, proc._proc.stderr):
                if stream is not None:
                    stream.close()

    def test_non_finite_counts_do_not_kill_the_reader(self) -> None:
        self.write_cli('''
            echo '{"progress": 0.5, "hint": "h", "samples": Infinity, "total": NaN}'
            echo '{"ok": true, "samples": 15}'
        ''')
        _proc, progress, finished = self.enroll()
        self.assertEqual([(p.samples, p.total) for p in progress], [(None, None)])
        self.assertEqual(finished, [({"ok": True, "samples": 15}, None)])

    def test_progress_then_success(self) -> None:
        self.write_cli('''
            echo '{"progress": 0.0, "hint": "Preparing", "total": 15}'
            echo 'Loading models'
            echo '{"progress": 0.4, "hint": "Turn left", "samples": 6, "total": 15}'
            echo '{"progress": 7, "hint": "clamped"}'
            echo '{"ok": true, "samples": 15}'
        ''')
        proc, progress, finished = self.enroll()
        self.assertEqual(finished, [({"ok": True, "samples": 15}, None)])
        self.assertEqual([(p.fraction, p.hint, p.samples) for p in progress],
                         [(0.0, "Preparing", None), (0.4, "Turn left", 6), (1.0, "clamped", None)])
        self.assertEqual(progress[1].total, 15)
        self.assertEqual(self.logged()[:2], ["pkexec", "iris enroll --user alice --name default --json"])
        self.assertFalse(proc.running)
        self.assertEqual(GLIB.timeouts, {})  # the stall timer is cleaned up

    def test_failure_is_described_from_the_final_line(self) -> None:
        self.write_cli('echo \'{"ok": false, "reason": "spoof_suspected", "error": "only 2 samples"}\'; exit 1')
        _proc, _progress, finished = self.enroll()
        [(final, error)] = finished
        self.assertIsNone(final)
        self.assertIn("did not look like a live face", error.message)

    def test_crash_without_a_verdict_names_the_command(self) -> None:
        self.write_cli('echo "Traceback: boom" >&2; exit 1')
        _proc, _progress, finished = self.enroll()
        [(final, error)] = finished
        self.assertIsNone(final)
        self.assertIn("iris enroll --user alice --name default --json", error.detail)
        self.assertIn("Traceback: boom", error.detail)

    def test_dismissed_prompt(self) -> None:
        self.write_script(self.pkexec, "exit 126\n")
        _proc, _progress, finished = self.enroll()
        self.assertIsInstance(finished[0][1], backend.AuthorisationCancelled)

    def test_chatty_stderr_cannot_deadlock_the_helper(self) -> None:
        self.write_cli("head -c 1000000 /dev/zero | tr '\\0' 'x' | fold -w 100 >&2; echo '{\"ok\": true}'")
        _proc, _progress, finished = self.enroll()
        self.assertEqual(finished, [({"ok": True}, None)])

    def test_cancel_stops_callbacks(self) -> None:
        self.write_cli('echo \'{"progress": 0.1, "hint": "h"}\'; exec sleep 5')
        progress: list[Any] = []
        finished: list[Any] = []
        proc = backend.EnrollProcess("alice", "default", on_progress=progress.append,
                                     on_finished=lambda *a: finished.append(a))
        proc.start()
        self.addCleanup(self._close_pipes, proc)
        time.sleep(0.3)
        proc.cancel()
        deadline = time.monotonic() + 5
        while proc.running and time.monotonic() < deadline:
            time.sleep(0.02)
        GLIB.run_pending()
        self.assertFalse(proc.running)
        self.assertEqual((progress, finished), ([], []))

    def test_stall_is_reported_once_per_interval(self) -> None:
        stalls: list[None] = []
        proc = backend.EnrollProcess("alice", "default", on_stalled=lambda: stalls.append(None))
        proc._proc = mock.Mock()
        proc._last_progress_at = time.monotonic() - backend._STALL_SECONDS - 1
        self.assertIs(proc._check_stall(), GLIB.SOURCE_CONTINUE)
        self.assertIs(proc._check_stall(), GLIB.SOURCE_CONTINUE)
        self.assertEqual(len(stalls), 1)
        proc._finished.set()
        self.assertIs(proc._check_stall(), GLIB.SOURCE_REMOVE)

    def test_missing_helper(self) -> None:
        with mock.patch.object(backend, "iris_binary", return_value=None):
            with self.assertRaises(backend.HelperMissing):
                backend.EnrollProcess("alice", "default").start()


if __name__ == "__main__":
    unittest.main()
