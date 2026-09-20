"""Exercise optional keyring PAM wiring against temporary stacks only."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
STACK = '''#%PAM-1.0
auth [success=done default=ignore] pam_iris.so
auth requisite pam_nologin.so
@include common-auth
auth optional pam_gnome_keyring.so
@include common-account
session required pam_loginuid.so
@include common-session
session optional pam_gnome_keyring.so auto_start
@include common-password
'''
AUTH = 'auth optional pam_iris_keyring.so\n'
SESSION = 'session optional pam_iris_keyring.so\n'


class KeyringInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pam = self.root / 'pam.d'
        self.backups = self.root / 'backups'
        self.pam.mkdir()
        self.backups.mkdir()
        self.stack = self.pam / 'gdm-password'
        self.stack.write_text(STACK)
        self.stack.chmod(0o640)

    def run_function(self, name, *args):
        source = (ROOT / 'install.sh').read_text()
        start = source.index(name + '() {')
        end = source.index('\n}', start) + 2
        script = 'set -euo pipefail\ndie() { echo "$*" >&2; exit 1; }\nok() { :; }\n'
        script += source[start:end] + '\n' + name + ' "$@"\n'
        return subprocess.run(['bash', '-c', script, 'test', *map(str, args)],
                              env={**os.environ, 'PAM_DIR': str(self.pam),
                                   'BACKUP_DIR': str(self.backups), 'STAMP': 'test'},
                              capture_output=True, text=True, timeout=10)

    def test_wiring_order_preserves_original_and_metadata_and_is_idempotent(self):
        os.setxattr(self.stack, b'user.iris-test', b'preserved')
        result = self.run_function('wire_keyring_service')
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = self.stack.read_text()
        self.assertEqual(actual.replace(AUTH, '').replace(SESSION, ''), STACK)
        self.assertIn('@include common-auth\n' + AUTH, actual)
        self.assertIn(SESSION + 'session optional pam_gnome_keyring.so auto_start', actual)
        self.assertEqual(self.stack.stat().st_mode & 0o777, 0o640)
        self.assertEqual(os.getxattr(self.stack, b'user.iris-test'), b'preserved')
        backups = list(self.backups.iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), STACK)
        result = self.run_function('wire_keyring_service')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(self.backups.iterdir()), backups)

    def test_unknown_duplicate_and_misordered_stacks_refused_unchanged(self):
        for content in (STACK.replace('@include common-auth', '@include other-auth'),
                        STACK.replace('auto_start', ''),
                        STACK + AUTH, STACK + AUTH + AUTH,
                        STACK.replace('auth optional pam_gnome_keyring.so\n', '') ,
                        AUTH + STACK + SESSION):
            with self.subTest(content=content):
                self.stack.write_text(content)
                result = self.run_function('wire_keyring_service')
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.stack.read_text(), content)
                self.assertEqual(list(self.backups.iterdir()), [])

    def test_symlink_refused(self):
        self.stack.unlink()
        target = self.root / 'target'
        target.write_text(STACK)
        self.stack.symlink_to(target)
        self.assertNotEqual(self.run_function('wire_keyring_service').returncode, 0)
        self.assertEqual(target.read_text(), STACK)

    def test_cleanup_removes_both_modules_even_from_reinstalled_backup(self):
        self.stack.write_text(STACK + AUTH + SESSION)
        result = self.run_function('strip_iris_pam_service', self.stack)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('pam_iris', self.stack.read_text())
        self.assertIn('@include common-auth', self.stack.read_text())
        self.assertIn('session optional pam_gnome_keyring.so auto_start', self.stack.read_text())
        self.assertEqual(self.stack.stat().st_mode & 0o777, 0o640)
