from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from iris import store
from iris.store import (
    FailureTracker,
    KeyManager,
    KeyUnavailableError,
    StoreError,
    TemplateCorruptError,
    TemplateStore,
)


def _vec(seed: int, dim: int = 128) -> np.ndarray:
    return np.random.default_rng(seed).standard_normal(dim).astype(np.float32)


class _RootStoreCase(unittest.TestCase):
    """Run store code as simulated root in a private directory without a TPM.

    CI is unprivileged, so euid is faked to 0 and chown becomes a no-op; the
    permission and ownership logic is still exercised up to the syscall.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "iris"
        for patcher in (
            mock.patch.object(store.os, "geteuid", return_value=0),
            mock.patch.object(store.os, "chown"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.keys = KeyManager(self.root, use_tpm=False)
        self.store = TemplateStore(self.root, key_manager=self.keys)

    def _encrypt_raw(self, user: str, payload: bytes) -> None:
        nonce = os.urandom(store.NONCE_BYTES)
        blob = nonce + AESGCM(self.keys.load_key()).encrypt(nonce, payload, user.encode())
        (self.root / f"{user}.enc").write_bytes(blob)


class KeyManagerTests(_RootStoreCase):
    def test_creates_private_key_file_and_directory(self) -> None:
        key = self.keys.load_key()

        self.assertEqual(len(key), store.KEY_BYTES)
        self.assertEqual(self.keys.backend, "file")
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        key_path = self.root / store.KEY_FILENAME
        self.assertEqual(stat.S_IMODE(key_path.stat().st_mode), 0o600)
        self.assertEqual(key_path.read_bytes(), key)

    def test_existing_key_is_reused_across_instances(self) -> None:
        key = self.keys.load_key()
        self.assertEqual(KeyManager(self.root, use_tpm=False).load_key(), key)

    def test_loose_key_file_permissions_are_tightened(self) -> None:
        self.keys.load_key()
        key_path = self.root / store.KEY_FILENAME
        key_path.chmod(0o644)

        with self.assertLogs("iris.store", "WARNING"):
            KeyManager(self.root, use_tpm=False).load_key()

        self.assertEqual(stat.S_IMODE(key_path.stat().st_mode), 0o600)

    def test_wrong_length_key_file_is_rejected(self) -> None:
        store.ensure_root_dir(self.root)
        key_path = self.root / store.KEY_FILENAME
        key_path.write_bytes(b"short")
        key_path.chmod(0o600)

        with self.assertRaises(KeyUnavailableError):
            self.keys.load_key()

    def test_symlinked_key_file_is_refused(self) -> None:
        store.ensure_root_dir(self.root)
        target = self.root.parent / "elsewhere.key"
        target.write_bytes(os.urandom(store.KEY_BYTES))
        (self.root / store.KEY_FILENAME).symlink_to(target)

        with self.assertRaises(StoreError):
            self.keys.load_key()

    def test_sealed_blob_without_tpm_never_mints_a_new_key(self) -> None:
        store.ensure_root_dir(self.root)
        (self.root / store.SEALED_PUB_FILENAME).write_bytes(b"pub")
        (self.root / store.SEALED_PRIV_FILENAME).write_bytes(b"priv")

        with self.assertRaises(KeyUnavailableError):
            self.keys.load_key()
        self.assertFalse((self.root / store.KEY_FILENAME).exists())

    def test_sealed_blob_without_tpm_falls_back_to_plain_key(self) -> None:
        key = self.keys.load_key()
        (self.root / store.SEALED_PUB_FILENAME).write_bytes(b"pub")
        (self.root / store.SEALED_PRIV_FILENAME).write_bytes(b"priv")

        self.assertEqual(KeyManager(self.root, use_tpm=False).load_key(), key)

    def test_key_creation_requires_root(self) -> None:
        with mock.patch.object(store.os, "geteuid", return_value=1000):
            with self.assertRaises(PermissionError):
                self.keys.load_key()
        self.assertFalse((self.root / store.KEY_FILENAME).exists())

    def test_symlinked_root_directory_is_refused(self) -> None:
        real = self.root.parent / "real"
        real.mkdir(mode=0o700)
        self.root.symlink_to(real)

        with self.assertRaises(StoreError):
            store.ensure_root_dir(self.root)


class TemplateStoreRoundTripTests(_RootStoreCase):
    def test_unenrolled_user_is_empty(self) -> None:
        self.assertEqual(self.store.list_faces("alice"), [])
        self.assertEqual(self.store.embeddings_for("alice"), [])
        self.assertFalse(self.store.has_user("alice"))

    def test_add_round_trips_embeddings_and_metadata(self) -> None:
        vectors = [_vec(1), _vec(2).reshape(1, 128)]
        self.store.add("alice", "default", vectors)

        faces = self.store.list_faces("alice")
        self.assertEqual(len(faces), 1)
        self.assertEqual(faces[0]["name"], "default")
        self.assertEqual(faces[0]["samples"], 2)
        self.assertTrue(faces[0]["created"])
        self.assertNotIn("embeddings", faces[0])

        pairs = self.store.embeddings_for("alice")
        self.assertEqual([name for name, _ in pairs], ["default", "default"])
        for (_, got), want in zip(pairs, vectors):
            self.assertEqual(got.dtype, np.float32)
            np.testing.assert_array_equal(got, np.ravel(want))

        self.assertTrue(self.store.has_user("alice"))
        self.assertEqual(self.store.enrolled_users(), ["alice"])

    def test_file_is_private_and_contains_no_plaintext(self) -> None:
        self.store.add("alice", "glasses-on", [_vec(1)])
        path = self.root / "alice.enc"

        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        blob = path.read_bytes()
        self.assertNotIn(b"glasses-on", blob)
        self.assertNotIn(b"embeddings", blob)
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_every_write_uses_a_fresh_nonce(self) -> None:
        path = self.root / "alice.enc"
        self.store.add("alice", "default", [_vec(1)])
        first = path.read_bytes()[: store.NONCE_BYTES]
        self.store.add("alice", "default", [_vec(1)])
        second = path.read_bytes()[: store.NONCE_BYTES]

        self.assertNotEqual(first, second)

    def test_re_enrolling_a_name_replaces_rather_than_merges(self) -> None:
        self.store.add("alice", "default", [_vec(1), _vec(2)])
        self.store.add("alice", "default", [_vec(3)])

        pairs = self.store.embeddings_for("alice")
        self.assertEqual(len(pairs), 1)
        np.testing.assert_array_equal(pairs[0][1], _vec(3))

    def test_remove_and_clear(self) -> None:
        self.store.add("alice", "a", [_vec(1)])
        self.store.add("alice", "b", [_vec(2)])

        self.assertTrue(self.store.remove("alice", "a"))
        self.assertFalse(self.store.remove("alice", "a"))
        self.assertEqual([f["name"] for f in self.store.list_faces("alice")], ["b"])

        self.assertTrue(self.store.remove("alice", "b"))
        self.assertFalse((self.root / "alice.enc").exists())

        self.store.add("alice", "c", [_vec(3)])
        self.store.clear("alice")
        self.store.clear("alice")
        self.assertFalse(self.store.has_user("alice"))

    def test_users_are_isolated(self) -> None:
        self.store.add("alice", "default", [_vec(1)])
        self.store.add("bob", "default", [_vec(2)])
        self.store.clear("alice")

        self.assertEqual(len(self.store.embeddings_for("bob")), 1)
        self.assertEqual(self.store.enrolled_users(), ["bob"])

    def test_writes_require_root(self) -> None:
        with mock.patch.object(store.os, "geteuid", return_value=1000):
            with self.assertRaises(PermissionError):
                self.store.add("alice", "default", [_vec(1)])

    def test_face_limit_is_enforced(self) -> None:
        for i in range(store.MAX_FACES_PER_USER):
            self.store.add("alice", f"face{i}", [_vec(i)])
        with self.assertRaises(ValueError):
            self.store.add("alice", "one-too-many", [_vec(99)])
        # Replacing an existing name is still allowed at the limit.
        self.store.add("alice", "face0", [_vec(100)])

    def test_dimension_change_against_existing_templates_is_refused(self) -> None:
        self.store.add("alice", "default", [_vec(1, dim=128)])
        with self.assertRaises(ValueError):
            self.store.add("alice", "other", [_vec(2, dim=64)])


class TemplateStoreValidationTests(_RootStoreCase):
    def test_unsafe_usernames_are_rejected(self) -> None:
        for user in ("", "../root", "a/b", "-rf", "x" * 33, ".hidden", "a b"):
            with self.subTest(user=user), self.assertRaises(ValueError):
                self.store.list_faces(user)
        with self.assertRaises(TypeError):
            self.store.list_faces(None)  # type: ignore[arg-type]

    def test_machine_account_style_username_is_accepted(self) -> None:
        self.store.add("host$", "default", [_vec(1)])
        self.assertEqual(len(self.store.embeddings_for("host$")), 1)

    def test_invalid_face_names_are_rejected(self) -> None:
        for name in ("", "   ", "x" * 65, "bad\nname", "bad\x7fname"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.store.add("alice", name, [_vec(1)])

    def test_invalid_embeddings_are_rejected(self) -> None:
        nan = _vec(1)
        nan[5] = np.nan
        inf = _vec(1)
        inf[0] = np.inf
        cases = {
            "none": [],
            "too many": [_vec(i) for i in range(store.MAX_EMBEDDINGS_PER_FACE + 1)],
            "empty vector": [np.array([], dtype=np.float32)],
            "too wide": [np.zeros(store.MAX_EMBEDDING_DIM + 1, dtype=np.float32)],
            "nan": [nan],
            "inf": [inf],
            "mixed dims": [_vec(1, 128), _vec(2, 64)],
        }
        for label, embeddings in cases.items():
            with self.subTest(label), self.assertRaises(ValueError):
                self.store.add("alice", "default", embeddings)
        self.assertFalse(self.store.has_user("alice"))


class TemplateStoreTamperTests(_RootStoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.store.add("alice", "default", [_vec(1)])
        self.path = self.root / "alice.enc"

    def test_flipped_ciphertext_bit_fails_closed(self) -> None:
        blob = bytearray(self.path.read_bytes())
        blob[-1] ^= 0x01
        self.path.write_bytes(bytes(blob))

        with self.assertRaises(TemplateCorruptError):
            self.store.embeddings_for("alice")

    def test_renamed_template_fails_authentication(self) -> None:
        # The AAD binds the ciphertext to the username; a swapped file must not
        # let alice's face unlock root.
        self.path.rename(self.root / "root.enc")

        with self.assertRaises(TemplateCorruptError):
            self.store.embeddings_for("root")

    def test_wrong_master_key_fails_closed(self) -> None:
        other = TemplateStore(self.root, key_manager=KeyManager(self.root, use_tpm=False))
        other._keys._cached = os.urandom(store.KEY_BYTES)

        with self.assertRaises(TemplateCorruptError):
            other.embeddings_for("alice")

    def test_truncated_file_is_corrupt(self) -> None:
        self.path.write_bytes(self.path.read_bytes()[: store.NONCE_BYTES + 16])

        with self.assertRaises(TemplateCorruptError):
            self.store.list_faces("alice")

    def test_oversized_file_is_refused_before_decrypting(self) -> None:
        with self.path.open("r+b") as fh:
            fh.truncate(store.MAX_FILE_BYTES + 1)

        with mock.patch.object(TemplateStore, "_aesgcm") as aesgcm:
            with self.assertRaises(TemplateCorruptError):
                self.store.list_faces("alice")
        aesgcm.assert_not_called()

    def test_symlinked_template_is_refused(self) -> None:
        self.path.rename(self.root / "real.enc")
        self.path.symlink_to(self.root / "real.enc")

        with self.assertRaises(TemplateCorruptError):
            self.store.list_faces("alice")

    def test_authenticated_but_malformed_documents_are_rejected(self) -> None:
        face = {"name": "d", "created": "", "embeddings": [[0.5]]}
        cases = {
            "not json": b"\xff\xfe",
            "not an object": b"[]",
            "future version": json.dumps({"version": 2, "faces": []}).encode(),
            "faces not list": json.dumps({"version": 1, "faces": {}}).encode(),
            "face not object": json.dumps({"version": 1, "faces": [1]}).encode(),
            "no name": json.dumps({"version": 1, "faces": [{**face, "name": ""}]}).encode(),
            "no embeddings": json.dumps(
                {"version": 1, "faces": [{**face, "embeddings": []}]}
            ).encode(),
            "empty vector": json.dumps(
                {"version": 1, "faces": [{**face, "embeddings": [[]]}]}
            ).encode(),
            "bool value": json.dumps(
                {"version": 1, "faces": [{**face, "embeddings": [[True]]}]}
            ).encode(),
            "string value": json.dumps(
                {"version": 1, "faces": [{**face, "embeddings": [["1"]]}]}
            ).encode(),
        }
        for label, payload in cases.items():
            with self.subTest(label):
                self._encrypt_raw("alice", payload)
                with self.assertRaises(TemplateCorruptError):
                    self.store.list_faces("alice")

    def test_missing_created_timestamp_is_tolerated(self) -> None:
        doc = {"version": 1, "faces": [{"name": "d", "embeddings": [[1, 2.5]]}]}
        self._encrypt_raw("alice", json.dumps(doc).encode())

        self.assertEqual(
            self.store.list_faces("alice"), [{"name": "d", "created": "", "samples": 1}]
        )


class FailureTrackerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 1000.0
        patcher = mock.patch.object(store.time, "monotonic", side_effect=lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tracker = FailureTracker()

    def _fail(self, user: str, times: int) -> None:
        for _ in range(times):
            self.tracker.record_failure(user)

    def test_locks_at_threshold_and_unlocks_after_window(self) -> None:
        self._fail("alice", 4)
        self.assertFalse(self.tracker.is_locked("alice", 5, 60))
        self._fail("alice", 1)
        self.assertTrue(self.tracker.is_locked("alice", 5, 60))
        self.assertAlmostEqual(self.tracker.seconds_until_unlock("alice", 5, 60), 60.0)

        self.now += 59.5
        self.assertTrue(self.tracker.is_locked("alice", 5, 60))
        self.assertAlmostEqual(self.tracker.seconds_until_unlock("alice", 5, 60), 0.5)

        self.now += 1.0
        self.assertFalse(self.tracker.is_locked("alice", 5, 60))
        self.assertEqual(self.tracker.seconds_until_unlock("alice", 5, 60), 0.0)
        self.assertEqual(self.tracker.failure_count("alice", 60), 0)

    def test_sliding_window_unlock_tracks_the_oldest_relevant_failure(self) -> None:
        self._fail("alice", 1)
        self.now += 10
        self._fail("alice", 3)

        # Locked at 3; the first failure is outside the 3 newest, so the lock
        # lifts 60s after the second failure, not the first.
        self.assertAlmostEqual(self.tracker.seconds_until_unlock("alice", 3, 60), 60.0)

    def test_reset_and_users_are_independent(self) -> None:
        self._fail("alice", 5)
        self._fail("bob", 1)
        self.tracker.reset("alice")

        self.assertFalse(self.tracker.is_locked("alice", 5, 60))
        self.assertEqual(self.tracker.failure_count("bob", 60), 1)

    def test_policy_edge_values(self) -> None:
        self.assertTrue(self.tracker.is_locked("alice", 0, 60))
        self._fail("alice", 100)
        self.assertFalse(self.tracker.is_locked("alice", 5, 0))

    def test_history_is_capped_at_the_config_ceiling(self) -> None:
        self._fail("alice", 1000)
        self.assertEqual(self.tracker.failure_count("alice", 60), FailureTracker._MAX_HISTORY)
        self.assertTrue(self.tracker.is_locked("alice", FailureTracker._MAX_HISTORY, 60))

    def test_user_table_is_lru_bounded(self) -> None:
        tracker = FailureTracker(max_users=2)
        tracker.record_failure("a")
        tracker.record_failure("b")
        tracker.record_failure("a")
        tracker.record_failure("c")

        self.assertEqual(tracker.failure_count("b", 60), 0)
        self.assertEqual(tracker.failure_count("a", 60), 2)
        self.assertEqual(tracker.failure_count("c", 60), 1)

    def test_max_users_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            FailureTracker(max_users=0)


if __name__ == "__main__":
    unittest.main()
