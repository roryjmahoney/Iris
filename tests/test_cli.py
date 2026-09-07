from __future__ import annotations

import json
import os
import pwd
import socket
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, Sequence

from iris import protocol


REPOSITORY = Path(__file__).resolve().parents[1]
CLI = REPOSITORY / "bin" / "iris"

CONFIG_FIXTURE = {
    "camera": {
        "device": "/dev/video2",
        "width": 640,
        "height": 360,
        "ir_mode": True,
        "min_frame_brightness": 20.0,
    },
    "recognition": {
        "threshold": 0.363,
        "detect_score": 0.7,
        "model_dir": "/usr/share/iris/models",
        "required_matches": 3,
        "max_frames": 120,
    },
    "auth": {
        "enabled": True,
        "timeout": 8.0,
        "max_failures": 5,
        "lockout_seconds": 60,
    },
    "liveness": {"enabled": True, "min_variance": 12.0},
}


class _ScriptedUnixDaemon:
    def __init__(self, path: Path, responses: Sequence[dict[str, Any]]) -> None:
        self.path = path
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.errors: list[BaseException] = []
        self._stop = threading.Event()
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(os.fspath(path))
        self._listener.listen(len(self.responses) or 1)
        self._listener.settimeout(0.1)
        self._worker = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> _ScriptedUnixDaemon:
        self._worker.start()
        return self

    def __exit__(self, exc_type: object, _exc: object, _tb: object) -> None:
        self._stop.set()
        self._listener.close()
        self._worker.join(2.0)
        self.path.unlink(missing_ok=True)
        if exc_type is None:
            if self._worker.is_alive():
                raise AssertionError("scripted daemon did not stop")
            if self.errors:
                raise self.errors[0]
            if len(self.requests) != len(self.responses):
                raise AssertionError(
                    f"expected {len(self.responses)} request(s), got {len(self.requests)}"
                )

    def _serve(self) -> None:
        index = 0
        try:
            while index < len(self.responses) and not self._stop.is_set():
                try:
                    connection, _ = self._listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    if self._stop.is_set():
                        return
                    raise
                with connection:
                    connection.settimeout(2.0)
                    request = protocol.read_message(connection)
                    if request is None:
                        raise AssertionError("CLI closed without sending a request")
                    self.requests.append(request)
                    protocol.write_message(connection, self.responses[index])
                    index += 1
        except BaseException as exc:
            self.errors.append(exc)


class CliJsonContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="iris-cli-test-")
        self.root = Path(self._temporary.name)
        self.user = pwd.getpwuid(os.getuid()).pw_name

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "LC_ALL": "C",
                "NO_COLOR": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        environment.pop("SUDO_USER", None)
        return subprocess.run(
            [os.fspath(CLI), "--color", "never", *arguments],
            cwd=REPOSITORY,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )

    def test_list_json_is_one_document_with_stable_shape(self) -> None:
        socket_path = self.root / "list.sock"
        faces = [
            {
                "name": "default",
                "created": "2026-01-02T03:04:05+00:00",
                "samples": 15,
            }
        ]
        with _ScriptedUnixDaemon(
            socket_path, [{"ok": True, "faces": faces}]
        ) as daemon:
            completed = self._run(
                "--socket",
                os.fspath(socket_path),
                "list",
                "--user",
                self.user,
                "--json",
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(len(completed.stdout.splitlines()), 1)
        self.assertEqual(
            json.loads(completed.stdout),
            {"ok": True, "user": self.user, "faces": faces, "source": "daemon"},
        )
        self.assertEqual(daemon.requests, [{"op": "list", "user": self.user}])

    def test_auth_rejection_remains_json_and_uses_distinct_exit_code(self) -> None:
        socket_path = self.root / "auth.sock"
        responses = [
            {"ok": True, "config": CONFIG_FIXTURE},
            {
                "ok": False,
                "confidence": 0.21,
                "reason": "no_match",
                "face": None,
            },
        ]
        with _ScriptedUnixDaemon(socket_path, responses) as daemon:
            completed = self._run(
                "--socket",
                os.fspath(socket_path),
                "test",
                "--user",
                self.user,
                "--timeout",
                "0.5",
                "--json",
            )

        self.assertEqual(completed.returncode, 5, completed.stderr)
        self.assertEqual(completed.stderr, "")
        payload = json.loads(completed.stdout)
        elapsed = payload.pop("elapsed_seconds")
        self.assertIsInstance(elapsed, (int, float))
        self.assertGreaterEqual(elapsed, 0.0)
        self.assertLess(elapsed, 2.0)
        self.assertEqual(
            payload,
            {
                "user": self.user,
                "ok": False,
                "reason": "no_match",
                "confidence": 0.21,
                "face": None,
                "threshold": 0.363,
                "retry_after": None,
            },
        )
        self.assertEqual(
            daemon.requests,
            [
                {"op": "config_get"},
                {"op": "auth", "user": self.user, "timeout": 0.5},
            ],
        )

    def test_status_json_composes_multiple_daemon_contracts(self) -> None:
        socket_path = self.root / "status.sock"
        faces = [
            {
                "name": "glasses",
                "created": "2026-02-03T04:05:06+00:00",
                "samples": 12,
            }
        ]
        responses = [
            {"ok": True, "config": CONFIG_FIXTURE},
            {"ok": True, "version": "daemon-test-version"},
            {"ok": True, "faces": faces},
        ]
        with _ScriptedUnixDaemon(socket_path, responses) as daemon:
            completed = self._run(
                "--socket",
                os.fspath(socket_path),
                "status",
                "--user",
                self.user,
                "--json",
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        payload = json.loads(completed.stdout)
        self.assertIsInstance(payload.pop("version"), str)
        self.assertEqual(
            payload,
            {
                "daemon": {
                    "running": True,
                    "version": "daemon-test-version",
                    "socket": os.fspath(socket_path),
                },
                "config_source": "/etc/iris/config.toml (as loaded by irisd)",
                "config": CONFIG_FIXTURE,
                "user": self.user,
                "faces": faces,
            },
        )
        self.assertEqual(
            daemon.requests,
            [
                {"op": "config_get"},
                {"op": "ping"},
                {"op": "list", "user": self.user},
            ],
        )

    def test_transport_error_is_machine_readable_on_stdout(self) -> None:
        socket_path = self.root / "missing.sock"
        completed = self._run(
            "--socket",
            os.fspath(socket_path),
            "list",
            "--user",
            self.user,
            "--json",
        )

        self.assertEqual(completed.returncode, 3)
        self.assertEqual(len(completed.stdout.splitlines()), 1)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["ok"], False)
        self.assertIn(os.fspath(socket_path), payload["error"])
        self.assertIn("irisd", payload["hint"])
        self.assertIn("error:", completed.stderr)


if __name__ == "__main__":
    unittest.main()
