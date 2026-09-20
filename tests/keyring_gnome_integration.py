"""Verify the adapter's private PAM contract with a disposable GNOME keyring.

Invoked by test_keyring_gnome.sh inside a new D-Bus session. Only synthetic
credentials and a temporary HOME/runtime/data directory are used. The real
GNOME PAM module is called WITHOUT auto_start, so it cannot start a fallback
daemon with the real account home if the test control socket is unavailable.
"""
from pathlib import Path
from contextlib import nullcontext
import os
import pwd
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
SECRET = b'iris synthetic integration credential'
GKR = Path('/usr/lib/x86_64-linux-gnu/security/pam_gnome_keyring.so')
PERMIT = GKR.with_name('pam_permit.so')


def run(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=10, **kwargs).stdout


def dbus(path, method, *args):
    return run('gdbus', 'call', '--session', '--dest', 'org.freedesktop.secrets',
               '--object-path', path, '--method', method, *args)


with nullcontext(os.environ['IRIS_TEST_ROOT']) as temporary:
    work = Path(temporary)
    for child in ('home', 'runtime', 'data', 'config', 'control', 'pam'):
        (work / child).mkdir(mode=0o700, exist_ok=True)
    os.environ.update(HOME=str(work / 'home'), XDG_RUNTIME_DIR=str(work / 'runtime'),
                      XDG_DATA_HOME=str(work / 'data'), XDG_CONFIG_HOME=str(work / 'config'),
                      GNOME_KEYRING_CONTROL=str(work / 'control'))
    helper = work / 'helper.py'
    helper.write_text('import sys\nsys.stdout.buffer.write(' + repr(SECRET) + ')\n')
    run('cc', '-Werror', '-Wall', '-Wextra', '-fPIC', '-shared', '-DIRIS_KEYRING_TEST',
        '-DIRIS_KEYRING_HELPER="' + str(helper) + '"', '-o', str(work / 'adapter.so'),
        str(ROOT / 'pam/pam_iris_keyring.c'), '-lpam')
    (work / 'marker.c').write_text(r'''
#define _GNU_SOURCE
#include <security/pam_modules.h>
#include <stdlib.h>
#include <string.h>
static void cleanup(pam_handle_t *p, void *data, int status) {
    (void)p; (void)status; free(data);
}
int pam_sm_authenticate(pam_handle_t *p, int flags, int argc, const char **argv) {
    const void *user = NULL;
    (void)flags; (void)argc; (void)argv;
    if (pam_get_item(p, PAM_USER, &user) || !user) return PAM_AUTH_ERR;
    char *value = strdup(user);
    if (!value) return PAM_BUF_ERR;
    int result = pam_set_data(p, "iris.face_authenticated.v1", value, cleanup);
    if (result) free(value);
    return result;
}
int pam_sm_setcred(pam_handle_t *p, int flags, int argc, const char **argv) {
    (void)p; (void)flags; (void)argc; (void)argv; return PAM_IGNORE;
}
''')
    run('cc', '-Werror', '-Wall', '-Wextra', '-fPIC', '-shared', '-o', str(work / 'marker.so'),
        str(work / 'marker.c'), '-lpam')
    (work / 'harness.c').write_text(r'''
#include <security/pam_appl.h>
#include <stdio.h>
static int no_prompt(int n, const struct pam_message **m, struct pam_response **r, void *d) {
    (void)n; (void)m; (void)r; (void)d; return PAM_CONV_ERR;
}
int main(int argc, char **argv) {
    if (argc != 4) return 2;
    pam_handle_t *p = NULL;
    struct pam_conv conv = {no_prompt, NULL};
    int result = pam_start_confdir("gdm-password", argv[1], &conv, argv[2], &p);
    if (!result) result = pam_putenv(p, argv[3]);
    if (!result) result = pam_authenticate(p, PAM_SILENT);
    if (!result) result = pam_open_session(p, PAM_SILENT);
    if (p) pam_end(p, result);
    return result ? 1 : 0;
}
''')
    run('cc', '-Werror', '-Wall', '-Wextra', '-o', str(work / 'harness'),
        str(work / 'harness.c'), '-lpam')
    (work / 'pam/gdm-password').write_text(
        f'auth required {work}/marker.so\n'
        f'session required {PERMIT}\n'
        f'session optional {work}/adapter.so\n'
        f'session optional {GKR}\n')
    with (work / 'daemon.log').open('w') as log:
        daemon = subprocess.Popen(['gnome-keyring-daemon', '--foreground', '--unlock',
                                   '--components=secrets', '--control-directory=' + str(work / 'control')],
                                  stdin=subprocess.PIPE, stdout=log, stderr=log)
        try:
            daemon.stdin.write(SECRET)
            daemon.stdin.close()
            collection = '/org/freedesktop/secrets/collection/login'
            deadline = time.monotonic() + 5
            while True:
                try:
                    # Check ownership without auto-activating another daemon.
                    owned = run('gdbus', 'call', '--session', '--dest', 'org.freedesktop.DBus',
                                '--object-path', '/org/freedesktop/DBus', '--method',
                                'org.freedesktop.DBus.NameHasOwner', 'org.freedesktop.secrets')
                    assert 'true' in owned, 'our temporary daemon is not ready'
                    result = dbus(collection, 'org.freedesktop.DBus.Properties.Get',
                                  'org.freedesktop.Secret.Collection', 'Locked')
                    assert '<false>' in result, result
                    break
                except (subprocess.CalledProcessError, AssertionError):
                    if time.monotonic() >= deadline:
                        raise AssertionError('isolated GNOME daemon did not become ready')
                    time.sleep(0.1)
            dbus('/org/freedesktop/secrets', 'org.freedesktop.Secret.Service.Lock', "['" + collection + "']")
            assert '<true>' in dbus(collection, 'org.freedesktop.DBus.Properties.Get',
                                    'org.freedesktop.Secret.Collection', 'Locked')
            run(str(work / 'harness'), pwd.getpwuid(os.getuid()).pw_name, str(work / 'pam'),
                'GNOME_KEYRING_CONTROL=' + str(work / 'control'))
            assert '<false>' in dbus(collection, 'org.freedesktop.DBus.Properties.Get',
                                     'org.freedesktop.Secret.Collection', 'Locked'), 'GNOME did not consume the PAM token'
            assert list((work / 'data/keyrings').glob('*.keyring')), 'keyring must be inside isolated data directory'
            print('PASS: installed GNOME PAM module unlocks an isolated keyring from the Iris session adapter without prompts')
        finally:
            daemon.terminate()
            try:
                daemon.wait(timeout=3)
            except subprocess.TimeoutExpired:
                daemon.kill()
                daemon.wait(timeout=3)
