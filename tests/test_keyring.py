"""Optional keyring vault tests; no real accounts or TPM are accessed."""
import ctypes
import base64
import fcntl
import io
import subprocess
from types import SimpleNamespace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from iris import keyring as k


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.vault = self.base / 'keyring'
        self.account = ('alice', 1001, '$6$fakehash')
        for name, value in [('STATE_DIR', self.vault), ('MASTER_DIR', self.base),
                            ('_ROOT_UID', os.getuid())]:
            p = patch.object(k, name, value); p.start(); self.addCleanup(p.stop)
        for name, value in [('_require_root', None), ('_local_identity', self.account[:2]), ('_identity', self.account),
                            ('_unseal', b'k' * 32), ('_verify_password', None)]:
            p = patch.object(k, name, return_value=value)
            setattr(self, name, p.start()); self.addCleanup(p.stop)
        for suffix in ('tpm.pub', 'tpm.priv'):
            path = self.base / ('master.key.' + suffix)
            path.write_bytes(b'sealed'); path.chmod(0o600)

    def test_lifecycle_and_ciphertext(self):
        self.assertEqual(k.status('alice')['state'], 'disabled')
        self.assertEqual(k.enable('alice')['state'], 'pending-password-login')
        k.capture('alice', b'correct password')
        self.assertEqual(k.status('alice')['state'], 'ready')
        self.assertEqual(k.enable('alice')['state'], 'ready')
        self.assertEqual(k.unlock('alice'), b'correct password')
        data = (self.vault / '1001.json').read_bytes()
        self.assertNotIn(b'correct password', data)
        self.assertEqual((self.vault / '1001.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.vault.stat().st_mode & 0o777, 0o700)
        self.assertEqual(k.disable('alice')['state'], 'disabled')
        with self.assertRaises(k.KeyringError): k.unlock('alice')

    def test_capture_never_enables(self):
        k.capture('alice', b'secret')
        self.assertEqual(k.status('alice')['state'], 'disabled')
        self._verify_password.assert_not_called()

    def test_password_change_invalidates_and_refreshes(self):
        k.enable('alice'); k.capture('alice', b'old')
        self._identity.return_value = ('alice', 1001, '$6$newhash')
        self.assertEqual(k.status('alice')['state'], 'pending-password-login')
        with self.assertRaises(k.KeyringError): k.unlock('alice')
        k.capture('alice', b'new')
        self.assertEqual(k.unlock('alice'), b'new')

    def test_wrong_user_and_tamper_fail_closed(self):
        k.enable('alice'); k.capture('alice', b'secret')
        self._identity.return_value = ('bob', 1001, '$6$fakehash')
        with self.assertRaises(k.KeyringError): k.unlock('bob')
        self._identity.return_value = self.account
        path = self.vault / '1001.json'
        data = json.loads(path.read_text()); data['ciphertext'] = 'AAAA'
        path.write_text(json.dumps(data))
        with self.assertRaises(k.KeyringError): k.unlock('alice')

    def test_unsafe_modes_symlinks_and_corruption(self):
        k.enable('alice')
        path = self.vault / '1001.json'
        for mode in (0o644, 0o660, 0o400):
            path.chmod(mode)
            with self.assertRaises(k.KeyringError): k.status('alice')
        path.chmod(0o600)
        for content in (b'not json', b'{}', b'x' * 17000):
            path.write_bytes(content)
            with self.assertRaises(k.KeyringError): k.status('alice')
        path.unlink(); path.symlink_to(self.base / 'master.key.tpm.pub')
        with self.assertRaises(k.KeyringError): k.enable('alice')
        path.unlink(); self.vault.chmod(0o755)
        with self.assertRaises(k.KeyringError): k.status('alice')

    def test_missing_tpm_and_plaintext_rejected(self):
        plain = self.base / 'master.key'; plain.write_bytes(b'x' * 32)
        plain.chmod(0o600)
        with self.assertRaises(k.KeyringError): k.enable('alice')
        plain.unlink(); (self.base / 'master.key.tpm.pub').unlink()
        with self.assertRaises(k.KeyringError): k.enable('alice')
        self.assertEqual(k.status('alice')['state'], 'disabled')

    def test_invalid_and_wrong_secrets_preserve_state(self):
        k.enable('alice')
        for secret in (b'', b'x' * 4097, b'a\x00b'):
            with self.assertRaises(k.KeyringError): k.capture('alice', secret)
        self._verify_password.side_effect = k.KeyringError('unavailable')
        with self.assertRaises(k.KeyringError): k.capture('alice', b'wrong')
        self.assertEqual(k.status('alice')['state'], 'pending-password-login')

    def test_unseal_failure_does_not_enable(self):
        self._unseal.side_effect = k.KeyringError('unavailable')
        with self.assertRaises(k.KeyringError): k.enable('alice')
        self.assertEqual(k.status('alice')['state'], 'disabled')


    def test_authenticated_tamper_and_wrong_master_rejected(self):
        k.enable('alice'); k.capture('alice', b'secret')
        self._unseal.return_value = b'z' * 32
        with self.assertRaises(k.KeyringError): k.unlock('alice')
        self._unseal.return_value = b'k' * 32
        path = self.vault / '1001.json'
        data = json.loads(path.read_text())
        ciphertext = bytearray(base64.b64decode(data['ciphertext'])); ciphertext[-1] ^= 1
        data['ciphertext'] = base64.b64encode(ciphertext).decode('ascii')
        path.write_text(json.dumps(data))
        with self.assertRaises(k.KeyringError): k.unlock('alice')

    def test_directory_symlink_hardlinks_and_lock_contention(self):
        self.vault.symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(k.KeyringError): k.enable('alice')
        self.vault.unlink(); k.enable('alice')
        path = self.vault / '1001.json'
        os.link(path, self.base / 'hardlink')
        with self.assertRaises(k.KeyringError): k.status('alice')
        (self.base / 'hardlink').unlink()
        with (self.vault / '.lock').open('rb') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(k.KeyringError): k.capture('alice', b'secret')
        self.assertEqual(k.status('alice')['state'], 'pending-password-login')
        (self.vault / '.lock').chmod(0o644)
        with self.assertRaises(k.KeyringError): k.status('alice')

    def test_atomic_replace_failure_preserves_existing_credential(self):
        k.enable('alice'); k.capture('alice', b'first')
        with patch.object(k.os, 'replace', side_effect=OSError('disk failure')):
            with self.assertRaises(k.KeyringError): k.capture('alice', b'second')
        self.assertEqual(k.unlock('alice'), b'first')
        self.assertFalse(list(self.vault.glob('.new-*')))


    def test_disable_locked_local_account_and_corrupt_record(self):
        k.enable('alice')
        self._identity.side_effect = k.KeyringError('locked')
        with patch.object(k, '_local_identity', return_value=('alice', 1001)):
            for content in (b'not json', b'x' * 20000):
                path = self.vault / '1001.json'
                path.write_bytes(content); path.chmod(0o600)
                result = k.disable('alice')
                self.assertEqual(result, {'user': 'alice', 'uid': 1001, 'state': 'disabled'})
                self.assertFalse(path.exists())

    def test_disable_refuses_unsafe_record_even_when_corrupt(self):
        k.enable('alice')
        path = self.vault / '1001.json'; path.write_bytes(b'not json')
        with patch.object(k, '_local_identity', return_value=('alice', 1001)):
            path.chmod(0o644)
            with self.assertRaises(k.KeyringError): k.disable('alice')
            path.unlink(); path.symlink_to(self.base / 'master.key.tpm.pub')
            with self.assertRaises(k.KeyringError): k.disable('alice')


class BoundaryTests(unittest.TestCase):
    def test_core_dumps_disabled_before_secret_io(self):
        with patch.object(k, '_require_root'), patch.object(k.resource, 'setrlimit') as limit, \
             patch.object(k, '_identity', side_effect=k.KeyringError('account')):
            with self.assertRaises(k.KeyringError): k.capture('alice', b'secret')
            limit.assert_called_once_with(k.resource.RLIMIT_CORE, (0, 0))
        with patch.object(k, '_require_root'), patch.object(k.resource, 'setrlimit') as limit, \
             patch.object(k, 'unlock', side_effect=k.KeyringError('account')):
            self.assertEqual(k.main(['unlock', 'alice']), 1)
            limit.assert_called_once_with(k.resource.RLIMIT_CORE, (0, 0))

    def test_root_required(self):
        with patch.object(k.os, 'geteuid', return_value=1000):
            for operation in (k.enable, k.disable, k.status, k.unlock):
                with self.assertRaises(k.KeyringError): operation('alice')

    def test_local_identity_rejects_root_locked_missing(self):
        passwd = b'alice:x:1001:1001::/home/alice:/bin/bash\nroot:x:0:0::/root:/bin/bash\n'
        for user, shadow in [('alice', b'alice:!:1:0:99999:7:::\n'),
                             ('alice', b'alice:*:1:0:99999:7:::\n'),
                             ('alice', b'alice::1:0:99999:7:::\n'),
                             ('alice', b''), ('root', b'root:$6$hash:1:0:99999:7:::\n'),
                             ('missing', b'')]:
            with self.subTest(user=user, shadow=shadow):
                with patch.object(k, '_read_file', side_effect=[passwd, shadow]):
                    with self.assertRaises(k.KeyringError): k._identity(user)
        with patch.object(k, '_read_file', side_effect=[passwd, b'alice:$6$hash:1:0:99999:7:::\n']):
            self.assertEqual(k._identity('alice'), ('alice', 1001, '$6$hash'))

    def test_libcrypt_verifies_real_synthetic_hash(self):
        library = ctypes.CDLL('libcrypt.so.1')
        library.crypt.argtypes = (ctypes.c_char_p, ctypes.c_char_p)
        library.crypt.restype = ctypes.c_char_p
        password_hash = library.crypt(b'test-only-password', b'$6$testsalt$').decode('ascii')
        k._verify_password(b'test-only-password', password_hash)
        with self.assertRaises(k.KeyringError): k._verify_password(b'wrong', password_hash)

    def test_tpm_fast_and_fallback_never_write_unsealed_key(self):
        for fallback in (False, True):
            with self.subTest(fallback=fallback), tempfile.TemporaryDirectory() as tmp:
                original_tempdir = tempfile.TemporaryDirectory
                def private_directory(**kwargs):
                    self.assertEqual(kwargs['dir'], '/run')
                    return original_tempdir(prefix=kwargs['prefix'], dir=tmp)
                calls = []
                def run(tool, args, work):
                    calls.append((tool, list(args)))
                    self.assertEqual(work.stat().st_mode & 0o777, 0o700)
                    self.assertEqual(sorted(p.name for p in work.iterdir()), ['seal.priv', 'seal.pub'])
                    self.assertNotIn('-o', args)
                    if tool == 'tpm2_unseal':
                        return SimpleNamespace(returncode=0, stdout=b'k' * 32)
                    return SimpleNamespace(returncode=int(fallback and len(calls) == 1), stdout=b'')
                with patch.object(k, '_sealed_blobs', return_value=(b'pub', b'priv')), \
                     patch.object(k.tempfile, 'TemporaryDirectory', side_effect=private_directory), \
                     patch.object(k, '_run', side_effect=run):
                    self.assertEqual(k._unseal(), b'k' * 32)
                self.assertIn('0x81010F00', calls[0][1])
                self.assertEqual(len(calls), 4 if fallback else 2)
                if fallback:
                    self.assertEqual(calls[1][0], 'tpm2_createprimary')
                    self.assertIn('ecc', calls[1][1])
                    self.assertIn('sha256', calls[1][1])
                self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_tpm_fixed_environment_timeout_and_errors(self):
        with patch.object(k.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            k._run('tpm2_unseal', ['-c', 'seal.ctx'], Path('/run/test'))
        args, kwargs = run.call_args
        self.assertEqual(args[0][0], '/usr/bin/tpm2_unseal')
        self.assertLessEqual(kwargs['timeout'], 1)
        self.assertEqual(set(kwargs['env']), {'PATH', 'LC_ALL', 'TPM2TOOLS_TCTI'})
        for error in (FileNotFoundError(), subprocess.TimeoutExpired('tool', 0.8)):
            with patch.object(k.subprocess, 'run', side_effect=error):
                with self.assertRaises(k.KeyringError): k._run('tpm2_unseal', [], Path('/run'))

    def test_helper_raw_output_and_silent_failure(self):
        output = SimpleNamespace(buffer=io.BytesIO())
        with patch.object(k, '_require_root'), patch.object(k, 'unlock', return_value=b'raw-secret'), \
             patch.object(k.sys, 'stdout', output):
            self.assertEqual(k.main(['unlock', 'alice']), 0)
            self.assertEqual(output.buffer.getvalue(), b'raw-secret')
        with patch.object(k, '_require_root'), patch.object(k, 'unlock', side_effect=k.KeyringError('private')), \
             patch.object(k.sys, 'stdout', SimpleNamespace(buffer=io.BytesIO())) as out, \
             patch.object(k.sys, 'stderr', io.StringIO()) as err:
            self.assertEqual(k.main(['unlock', 'alice']), 1)
            self.assertEqual(out.buffer.getvalue(), b'')
            self.assertEqual(err.getvalue(), '')
        inp = SimpleNamespace(buffer=io.BytesIO(b'x' * 9000))
        with patch.object(k, '_require_root'), patch.object(k, 'capture') as capture, patch.object(k.sys, 'stdin', inp):
            self.assertEqual(k.main(['capture', 'alice']), 0)
            self.assertEqual(len(capture.call_args.args[1]), 4097)

    def test_isolated_standalone_nonroot_fails_silently(self):
        if os.geteuid() == 0:
            self.skipTest('requires nonroot test process')
        result = subprocess.run(['/usr/bin/python3', '-I', '-B', k.__file__, 'unlock', 'alice'],
                                capture_output=True, timeout=3)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b'')
        self.assertEqual(result.stderr, b'')


if __name__ == '__main__': unittest.main()
