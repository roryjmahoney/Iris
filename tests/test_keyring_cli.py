"""Public opt-in command dispatch and prompt-free status formatting."""
from contextlib import redirect_stdout
import io
import types
import unittest
from unittest import mock

from iris import cli


class KeyringCliTests(unittest.TestCase):
    def test_enable_requires_installed_hooks_and_calls_vault_for_resolved_user(self):
        vault = types.SimpleNamespace(KeyringError=RuntimeError, enable=mock.Mock(return_value={'user': 'alice', 'state': 'pending-password-login'}))
        output = io.StringIO()
        with mock.patch('iris.keyring', vault, create=True), \
             mock.patch.object(cli, 'require_root'), \
             mock.patch.object(cli, 'resolve_user', return_value='alice'), \
             mock.patch.object(cli, '_require_keyring_hooks') as hooks, redirect_stdout(output):
            self.assertEqual(cli.main(['keyring', 'enable', '--user', 'alice', '--json']), 0)
        hooks.assert_called_once_with()
        vault.enable.assert_called_once_with('alice')
        self.assertIn('pending-password-login', output.getvalue())

    def test_disable_does_not_depend_on_installed_hooks(self):
        vault = types.SimpleNamespace(KeyringError=RuntimeError, disable=mock.Mock(return_value={'user': 'alice', 'state': 'disabled'}))
        with mock.patch('iris.keyring', vault, create=True), \
             mock.patch.object(cli, 'require_root'), \
             mock.patch.object(cli, 'resolve_user', return_value='alice'), \
             mock.patch.object(cli, '_require_keyring_hooks') as hooks, redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['keyring', 'disable']), 0)
        hooks.assert_not_called()
        vault.disable.assert_called_once_with('alice')

    def test_unprivileged_commands_do_not_touch_vault(self):
        with mock.patch.object(cli.os, 'geteuid', return_value=1000), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['keyring', 'status', '--json']), cli.EXIT_PERMISSION)
