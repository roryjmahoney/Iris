from __future__ import annotations

import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from iris import __version__, protocol
from iris import daemon as daemon_module


class _UnprivilegedTestDaemon(daemon_module.IrisDaemon):
    """Keep the kernel credential lookup real while standing in for root."""

    @staticmethod
    def _peer_credentials(conn: socket.socket) -> daemon_module.PeerCredentials:
        peer = daemon_module.IrisDaemon._peer_credentials(conn)
        return daemon_module.PeerCredentials(peer.pid, 0, 0)


class _EnrolledStore:
    def embeddings_for(self, user: str) -> list[tuple[str, np.ndarray]]:
        if user != "alice":
            return []
        return [("default", np.array([1.0, 0.0], dtype=np.float32))]


class _FakeCamera:
    @classmethod
    def from_config(cls, _cfg: dict[str, object]) -> _FakeCamera:
        return cls()

    def __enter__(self) -> _FakeCamera:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def frames(self, _budget: float):
        yield np.full((16, 16), 80, dtype=np.uint8)


class _FakeFaceEngine:
    def detect(self, _gray: np.ndarray) -> np.ndarray:
        face = np.zeros((1, 15), dtype=np.float32)
        face[0, 2:4] = 8.0
        face[0, 14] = 0.99
        return face

    def embed(self, _gray: np.ndarray, _face: np.ndarray) -> np.ndarray:
        return np.array([1.0, 0.0], dtype=np.float32)


class _FakeLivenessChecker:
    enabled = True

    def __init__(self, _cfg: dict[str, object]) -> None:
        pass

    def check(self, _gray: np.ndarray, _face: np.ndarray | None) -> tuple[bool, str]:
        return True, "live"

    def is_confident(self) -> bool:
        return True


class DaemonProtocolContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="iris-daemon-test-")
        self.root = Path(self._temporary.name)
        state_dir = self.root / "state"
        state_dir.mkdir(mode=0o700)
        config_path = self.root / "config.toml"
        config_path.write_text(
            """
[camera]
device = "/dev/never-opened-by-tests"
width = 16
height = 16
ir_mode = true
min_frame_brightness = 20.0

[recognition]
threshold = 0.5
required_matches = 1
max_frames = 3

[auth]
enabled = true
timeout = 1.0
max_failures = 5
lockout_seconds = 60

[liveness]
enabled = true
min_variance = 1.0
""".lstrip(),
            encoding="utf-8",
        )
        self.daemon = _UnprivilegedTestDaemon(
            socket_path=os.fspath(self.root / "unused.sock"),
            config_path=os.fspath(config_path),
            state_dir=os.fspath(state_dir),
            use_tpm=False,
        )
        self._log_patch = mock.patch.object(daemon_module.log, "disabled", True)
        self._log_patch.start()

    def tearDown(self) -> None:
        self.daemon.stop()
        self._log_patch.stop()
        self._temporary.cleanup()

    def _exchange(
        self,
        request: dict[str, object] | None = None,
        *,
        raw: bytes | None = None,
    ) -> dict[str, object]:
        socket_path = self.root / f"daemon-{threading.get_ident()}-{id(request)}.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(os.fspath(socket_path))
        listener.listen(1)
        listener.settimeout(2.0)
        worker_errors: list[BaseException] = []

        def serve_one() -> None:
            try:
                connection, _ = listener.accept()
                self.daemon._serve_connection(connection)
            except BaseException as exc:  # surfaced in the test thread below
                worker_errors.append(exc)

        worker = threading.Thread(target=serve_one, daemon=True)
        worker.start()
        try:
            if raw is None:
                assert request is not None
                response = protocol.send_request(request, 2.0, os.fspath(socket_path))
            else:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(2.0)
                    client.connect(os.fspath(socket_path))
                    client.sendall(raw)
                    response = protocol.read_message(client)
                    self.assertIsNotNone(response)
        finally:
            listener.close()
            worker.join(2.0)
            socket_path.unlink(missing_ok=True)

        self.assertFalse(worker.is_alive(), "daemon connection worker did not stop")
        if worker_errors:
            raise worker_errors[0]
        assert response is not None
        return response

    def test_ping_round_trips_through_real_unix_socket(self) -> None:
        self.assertEqual(
            self._exchange({"op": "ping"}),
            {"ok": True, "version": __version__},
        )

    def test_auth_success_crosses_protocol_and_daemon_with_boundary_fakes(self) -> None:
        self.daemon._store = _EnrolledStore()
        self.daemon._ensure_engine = lambda _cfg: _FakeFaceEngine()

        with mock.patch.object(daemon_module, "Camera", _FakeCamera), mock.patch.object(
            daemon_module, "LivenessChecker", _FakeLivenessChecker
        ):
            response = self._exchange(
                {"op": "auth", "user": "alice", "timeout": 1.0}
            )

        self.assertEqual(
            response,
            {
                "ok": True,
                "confidence": 1.0,
                "reason": "match",
                "face": "default",
            },
        )

    def test_invalid_auth_request_keeps_fail_closed_response_shape(self) -> None:
        response = self._exchange({"op": "auth"})

        self.assertEqual(response["ok"], False)
        self.assertEqual(response["confidence"], 0.0)
        self.assertEqual(response["reason"], "not_enrolled")
        self.assertIsNone(response["face"])
        self.assertIn("missing or non-string 'user'", response["error"])

    def test_unknown_operation_is_rejected_over_the_socket(self) -> None:
        response = self._exchange({"op": "launch_missiles"})

        self.assertEqual(response["ok"], False)
        self.assertIn("unknown op", response["error"])

    def test_non_object_json_is_rejected_before_dispatch(self) -> None:
        response = self._exchange(raw=b"[]\n")

        self.assertEqual(response["ok"], False)
        self.assertIn("malformed request", response["error"])


if __name__ == "__main__":
    unittest.main()
