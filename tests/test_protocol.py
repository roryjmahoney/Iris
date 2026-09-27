"""Framing, size cap, timeouts and the request helpers in iris.protocol."""

from __future__ import annotations

import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Callable
from unittest import mock

from iris import protocol
from iris.protocol import (
    MAX_MESSAGE_BYTES,
    DaemonUnavailable,
    Disconnected,
    MalformedMessage,
    MessageTooLarge,
    ProtocolError,
    RequestTimeout,
    TransportError,
)


def _pair(timeout: float = 2.0) -> tuple[socket.socket, socket.socket]:
    a, b = socket.socketpair()
    a.settimeout(timeout)
    b.settimeout(timeout)
    return a, b


def _padded(size: int) -> dict[str, str]:
    """An object whose framed line is exactly *size* bytes, newline included."""
    overhead = len(protocol.encode_message({"p": ""}))
    return {"p": "x" * (size - overhead)}


class EncodeDecodeTests(unittest.TestCase):
    def test_compact_utf8_line(self) -> None:
        line = protocol.encode_message({"user": "zoë", "n": 1})
        self.assertEqual(line, '{"user":"zoë","n":1}\n'.encode("utf-8"))
        self.assertEqual(protocol.decode_message(line), {"user": "zoë", "n": 1})

    def test_refuses_what_is_not_a_json_object(self) -> None:
        for bad in ([1, 2], "text", None, {"x": float("nan")}, {"x": float("inf")}, {"x": object()}):
            with self.subTest(bad=bad), self.assertRaises(MalformedMessage):
                protocol.encode_message(bad)  # type: ignore[arg-type]

    def test_size_cap_boundary(self) -> None:
        self.assertEqual(len(protocol.encode_message(_padded(MAX_MESSAGE_BYTES))), MAX_MESSAGE_BYTES)
        with self.assertRaises(MessageTooLarge):
            protocol.encode_message(_padded(MAX_MESSAGE_BYTES + 1))

    def test_decode_rejects_bad_lines(self) -> None:
        for line in (b"\xff\xfe{}\n", b"{not json}\n", b"[1,2]\n", b"42\n", b'"s"\n', b"null\n", b"\n"):
            with self.subTest(line=line), self.assertRaises(MalformedMessage):
                protocol.decode_message(line)

    def test_decode_tolerates_crlf(self) -> None:
        self.assertEqual(protocol.decode_message(b'{"ok":true}\r\n'), {"ok": True})


class FramingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.a, self.b = _pair()
        self.addCleanup(self.a.close)
        self.addCleanup(self.b.close)

    def test_back_to_back_messages_are_read_one_at_a_time(self) -> None:
        self.a.sendall(b'{"n":1}\n{"n":2}\n{"n":3}')
        self.assertEqual(protocol.read_message(self.b), {"n": 1})
        self.assertEqual(protocol.read_message(self.b), {"n": 2})
        # The third, unterminated message is still wholly in the kernel buffer.
        self.assertEqual(self.b.recv(100, socket.MSG_PEEK), b'{"n":3}')

    def test_message_split_across_many_writes(self) -> None:
        line = protocol.encode_message({"hint": "Turn your head" * 100})

        def dribble() -> None:
            for i in range(0, len(line), 97):
                self.a.sendall(line[i:i + 97])
                time.sleep(0.001)

        sender = threading.Thread(target=dribble)
        sender.start()
        self.assertEqual(protocol.read_message(self.b), {"hint": "Turn your head" * 100})
        sender.join()

    def test_message_larger_than_one_peek_chunk(self) -> None:
        message = _padded(protocol._READ_CHUNK * 3 + 17)
        protocol.write_message(self.a, message)
        self.assertEqual(protocol.read_message(self.b), message)

    def test_clean_close_between_messages_is_none(self) -> None:
        protocol.write_message(self.a, {"ok": True})
        self.a.shutdown(socket.SHUT_WR)
        self.assertEqual(protocol.read_message(self.b), {"ok": True})
        self.assertIsNone(protocol.read_message(self.b))

    def test_close_mid_message_is_disconnected(self) -> None:
        self.a.sendall(b'{"ok":tr')
        self.a.shutdown(socket.SHUT_WR)
        with self.assertRaises(Disconnected):
            protocol.read_message(self.b)

    def test_malformed_line_over_the_wire(self) -> None:
        self.a.sendall(b"{oops}\n")
        with self.assertRaises(MalformedMessage):
            protocol.read_message(self.b)

    def test_size_cap_on_read(self) -> None:
        exact = protocol.encode_message(_padded(MAX_MESSAGE_BYTES))
        sender = threading.Thread(target=self.a.sendall, args=(exact,))
        sender.start()
        self.assertEqual(protocol.read_message(self.b), _padded(MAX_MESSAGE_BYTES))
        sender.join()

    def test_oversized_line_is_refused_without_buffering_it(self) -> None:
        # One byte over, newline included (valid JSON plus a trailing space).
        line = protocol.encode_message(_padded(MAX_MESSAGE_BYTES))[:-1] + b" \n"
        self.assertEqual(len(line), MAX_MESSAGE_BYTES + 1)
        sender = threading.Thread(target=self.a.sendall, args=(line,))
        sender.start()
        with self.assertRaises(MessageTooLarge):
            protocol.read_message(self.b)
        # The reader refuses before consuming the chunk that would cross the
        # cap, so it never holds more than MAX_MESSAGE_BYTES of the line.
        leftover = b""
        while sender.is_alive() or not leftover.endswith(b"\n"):
            leftover += self.b.recv(MAX_MESSAGE_BYTES * 2)
        sender.join()
        self.assertGreater(len(leftover), 0)
        self.assertLessEqual(len(line) - len(leftover), MAX_MESSAGE_BYTES)

    def test_endless_line_without_newline_is_refused(self) -> None:
        # Four times the cap with no newline, then the peer hangs up. The cap
        # must fire first; without it this would end as Disconnected instead.
        def flood() -> None:
            try:
                for _ in range(4 * MAX_MESSAGE_BYTES // 4096):
                    self.a.sendall(b"x" * 4096)
                self.a.shutdown(socket.SHUT_WR)
            except OSError:
                pass

        sender = threading.Thread(target=flood, daemon=True)
        sender.start()
        with self.assertRaises(MessageTooLarge):
            protocol.read_message(self.b)
        self.b.close()
        sender.join(5.0)


class TimeoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.a, self.b = _pair(timeout=0.2)
        self.addCleanup(self.a.close)
        self.addCleanup(self.b.close)

    def test_sockets_that_could_block_forever_are_refused(self) -> None:
        # A complete message is waiting, so a missing guard shows up as a
        # successful read rather than as a test that blocks forever.
        self.a.sendall(b'{"ok":true}\n')
        for timeout in (None, 0.0):
            with self.subTest(timeout=timeout):
                self.b.settimeout(timeout)
                with self.assertRaises(ProtocolError):
                    protocol.read_message(self.b)
                with self.assertRaises(ProtocolError):
                    protocol.write_message(self.b, {"ok": True})
        # Nothing was sent while refusing, and nothing was consumed.
        self.a.settimeout(0.05)
        with self.assertRaises(TimeoutError):
            self.a.recv(1)
        self.b.settimeout(1.0)
        self.assertEqual(protocol.read_message(self.b), {"ok": True})

    def test_silent_peer_times_out(self) -> None:
        with self.assertRaises(RequestTimeout):
            protocol.read_message(self.b)

    def test_deadline_wins_over_a_longer_socket_timeout(self) -> None:
        self.b.settimeout(5.0)
        started = time.monotonic()
        with self.assertRaises(RequestTimeout):
            protocol.read_message(self.b, deadline=started + 0.3)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_expired_deadline(self) -> None:
        past = time.monotonic() - 1
        with self.assertRaises(RequestTimeout):
            protocol.read_message(self.b, deadline=past)
        with self.assertRaises(RequestTimeout):
            protocol.write_message(self.b, {"ok": True}, deadline=past)

    def test_slow_drip_cannot_stretch_the_deadline(self) -> None:
        # Each byte arrives well inside the per-syscall timeout, but the overall
        # deadline must still end the read: this is what keeps PAM from hanging.
        self.b.settimeout(5.0)
        stop = threading.Event()

        def drip() -> None:
            while not stop.is_set():
                try:
                    self.a.sendall(b"x")
                except OSError:
                    return
                time.sleep(0.02)

        sender = threading.Thread(target=drip, daemon=True)
        sender.start()
        started = time.monotonic()
        with self.assertRaises(RequestTimeout):
            protocol.read_message(self.b, deadline=started + 0.3)
        elapsed = time.monotonic() - started
        stop.set()
        self.assertLess(elapsed, 1.0)

    def test_write_to_closed_peer(self) -> None:
        self.a.close()
        with self.assertRaises(TransportError):
            for _ in range(10):
                protocol.write_message(self.b, {"ok": True})

    def test_oversized_write_sends_nothing(self) -> None:
        with self.assertRaises(MessageTooLarge):
            protocol.write_message(self.b, _padded(MAX_MESSAGE_BYTES + 1))
        self.a.settimeout(0.05)
        with self.assertRaises(TimeoutError):
            self.a.recv(1)


class _ScriptedServer:
    """A one-connection UNIX-socket server that runs *script* against the client."""

    def __init__(self, path: Path, script: Callable[[socket.socket], None]) -> None:
        self.path = path
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(os.fspath(path))
        self.listener.listen(1)
        self.listener.settimeout(5.0)
        self.requests: list[dict[str, Any] | None] = []
        self.client_closed = threading.Event()
        self.errors: list[BaseException] = []

        def serve() -> None:
            try:
                conn, _ = self.listener.accept()
                with conn:
                    conn.settimeout(5.0)
                    self.requests.append(protocol.read_message(conn))
                    script(conn)
                    try:
                        conn.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    try:
                        if conn.recv(1) == b"":
                            self.client_closed.set()
                    except OSError:
                        self.client_closed.set()
            except BaseException as exc:  # surfaced by the test
                self.errors.append(exc)

        self.thread = threading.Thread(target=serve, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.listener.close()
        self.thread.join(5.0)


class RequestHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="iris-proto-")
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "socket"

    def _server(self, script: Callable[[socket.socket], None]) -> _ScriptedServer:
        server = _ScriptedServer(self.path, script)
        self.addCleanup(server.close)
        return server

    def test_send_request_skips_progress_lines(self) -> None:
        def script(conn: socket.socket) -> None:
            protocol.write_message(conn, {"progress": 0.2, "hint": "left"})
            protocol.write_message(conn, {"progress": 0.9, "hint": "right"})
            protocol.write_message(conn, {"ok": True, "samples": 15})

        server = self._server(script)
        response = protocol.send_request({"op": "enroll", "user": "alice"}, 2.0, os.fspath(self.path))
        self.assertEqual(response, {"ok": True, "samples": 15})
        self.assertEqual(server.requests, [{"op": "enroll", "user": "alice"}])

    def test_stream_request_yields_everything_up_to_the_final_line(self) -> None:
        def script(conn: socket.socket) -> None:
            protocol.write_message(conn, {"progress": 0.5, "hint": "h"})
            protocol.write_message(conn, {"ok": False, "reason": "no_face"})
            protocol.write_message(conn, {"ok": True, "stray": "never read"})

        self._server(script)
        messages = list(protocol.stream_request({"op": "enroll"}, 2.0, os.fspath(self.path)))
        self.assertEqual(messages, [{"progress": 0.5, "hint": "h"}, {"ok": False, "reason": "no_face"}])

    def test_abandoned_stream_closes_the_connection(self) -> None:
        release = threading.Event()

        def script(conn: socket.socket) -> None:
            protocol.write_message(conn, {"progress": 0.1, "hint": "h"})
            release.wait(5.0)

        server = self._server(script)
        opened: list[socket.socket] = []
        real_connect = protocol.connect

        def tracking_connect(*args: Any, **kwargs: Any) -> socket.socket:
            opened.append(real_connect(*args, **kwargs))
            return opened[-1]

        with mock.patch.object(protocol, "connect", tracking_connect):
            stream = protocol.stream_request({"op": "enroll"}, 2.0, os.fspath(self.path))
            self.assertEqual(next(stream), {"progress": 0.1, "hint": "h"})
            stream.close()
        # Closed explicitly, not merely left for the garbage collector.
        self.assertEqual(opened[0].fileno(), -1)
        release.set()
        self.assertTrue(server.client_closed.wait(5.0))

    def test_daemon_closing_without_a_final_answer(self) -> None:
        def script(conn: socket.socket) -> None:
            protocol.write_message(conn, {"progress": 0.5, "hint": "h"})

        self._server(script)
        with self.assertRaises(Disconnected):
            protocol.send_request({"op": "enroll"}, 2.0, os.fspath(self.path))

    def test_timeout_bounds_the_whole_exchange(self) -> None:
        # A daemon that keeps sending progress lines, each well inside any
        # per-read timeout, must still not hold the caller past its budget.
        stop = threading.Event()

        def script(conn: socket.socket) -> None:
            for _ in range(60):  # ~3 s, far past the 0.4 s budget
                if stop.is_set():
                    return
                try:
                    protocol.write_message(conn, {"progress": 0.1, "hint": "still going"})
                except ProtocolError:
                    return
                time.sleep(0.05)

        self._server(script)
        started = time.monotonic()
        with self.assertRaises(RequestTimeout):
            protocol.send_request({"op": "auth", "user": "alice"}, 0.4, os.fspath(self.path))
        stop.set()
        self.assertLess(time.monotonic() - started, 1.5)

    def test_no_daemon(self) -> None:
        with self.assertRaises(DaemonUnavailable):
            protocol.send_request({"op": "ping"}, 1.0, os.fspath(self.path))

        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(os.fspath(self.path))
        stale.close()  # socket file left behind, nobody listening
        with self.assertRaises(DaemonUnavailable):
            protocol.send_request({"op": "ping"}, 1.0, os.fspath(self.path))

    def test_connected_socket_always_has_a_timeout(self) -> None:
        self._server(lambda conn: protocol.write_message(conn, {"ok": True}))
        sock = protocol.connect(os.fspath(self.path), timeout=3.0)
        with sock:
            self.assertIsNotNone(sock.gettimeout())
            self.assertGreater(sock.gettimeout(), 0)
            protocol.write_message(sock, {"op": "ping"})
            self.assertEqual(protocol.read_message(sock), {"ok": True})

    def test_request_must_be_a_mapping(self) -> None:
        with self.assertRaises(TypeError):
            protocol.send_request(["op", "ping"], 1.0, os.fspath(self.path))  # type: ignore[arg-type]


class VocabularyTests(unittest.TestCase):
    def test_progress_detection(self) -> None:
        self.assertTrue(protocol.is_progress({"progress": 0.5, "hint": "x"}))
        self.assertFalse(protocol.is_progress({"progress": 1.0, "ok": True}))
        self.assertFalse(protocol.is_progress({"ok": False}))

    def test_auth_response_shape(self) -> None:
        self.assertEqual(
            protocol.auth_response(1, 1, protocol.REASON_MATCH, "default"),  # type: ignore[arg-type]
            {"ok": True, "confidence": 1.0, "reason": "match", "face": "default"},
        )
        response = protocol.auth_response(False, 0.2, protocol.REASON_NO_MATCH)
        self.assertIs(response["ok"], False)
        self.assertIsInstance(response["confidence"], float)
        self.assertIsNone(response["face"])

    def test_reason_vocabulary_is_closed(self) -> None:
        with self.assertRaises(ValueError):
            protocol.auth_response(False, 0.0, "liveness_no_ir_return")
        with self.assertRaises(ValueError):
            protocol.error_response("oops")
        self.assertEqual(
            protocol.error_response(protocol.REASON_LOCKOUT, retry_after=3.0),
            {"ok": False, "reason": "lockout", "retry_after": 3.0},
        )

    def test_every_reason_has_user_facing_text(self) -> None:
        self.assertEqual(set(protocol.REASON_TEXT), set(protocol.REASONS))
        for reason in protocol.REASONS:
            self.assertTrue(protocol.describe_reason(reason).endswith("."))
        self.assertIn("mystery", protocol.describe_reason("mystery"))

    def test_op_set_is_exactly_the_documented_one(self) -> None:
        # The daemon refuses anything outside OPS; adding an op is a protocol
        # change that PAM, the CLI and the GUI all need to know about.
        self.assertEqual(
            protocol.OPS,
            {"ping", "auth", "list", "cameras", "enroll", "remove", "clear", "config_get", "config_set"},
        )


if __name__ == "__main__":
    unittest.main()
