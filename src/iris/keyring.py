"""Opt-in, root-only TPM-backed login-keyring password vault.

This file deliberately also runs directly under Python -I; it never imports Iris
store.py, whose plaintext fallback is inappropriate for login credentials.
Python cannot guarantee erasure of immutable byte strings from process memory.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import ctypes
import fcntl
import hashlib
import hmac
import json
import os
import resource
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

STATE_DIR = Path('/var/lib/iris/keyring')
MASTER_DIR = Path('/var/lib/iris')
_ROOT_UID = 0
MAX_SECRET = 4096
MAX_RECORD = 16384


class KeyringError(RuntimeError):
    """A generic, non-secret-bearing failure safe for management commands."""


def _fail():
    raise KeyringError('keyring auto-unlock unavailable')


def _require_root():
    if os.geteuid() != 0:
        _fail()


def _parents(path):
    """Reject redirectable or untrusted ancestors, including symlinks."""
    for parent in reversed(path.absolute().parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, _ROOT_UID):
            _fail()
        if info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX):
            _fail()


def _read_file(path, limit=MAX_RECORD, mode=0o600):
    _parents(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != _ROOT_UID
                or info.st_nlink != 1 or info.st_size > limit):
            _fail()
        if mode is not None and stat.S_IMODE(info.st_mode) != mode:
            _fail()
        if mode is None and info.st_mode & 0o022:
            _fail()
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            _fail()
        return data
    finally:
        os.close(fd)


def _local_identity(user):
    if not isinstance(user, str) or not user or any(c in user for c in ':\n\r\x00'):
        _fail()
    passwd = _read_file(Path('/etc/passwd'), 1024 * 1024, None).decode('utf-8')
    accounts = [line.split(':') for line in passwd.splitlines() if line.split(':', 1)[0] == user]
    if len(accounts) != 1 or len(accounts[0]) != 7:
        _fail()
    uid = int(accounts[0][2])
    if uid <= 0:
        _fail()
    return user, uid


def _identity(user):
    user, uid = _local_identity(user)
    shadow = _read_file(Path('/etc/shadow'), 1024 * 1024, None).decode('utf-8')
    entries = [line.split(':') for line in shadow.splitlines() if line.split(':', 1)[0] == user]
    if len(entries) != 1 or len(entries[0]) != 9:
        _fail()
    password_hash = entries[0][1]
    if not password_hash or password_hash.startswith(('!', '*')):
        _fail()
    return user, uid, password_hash


def _verify_password(secret, password_hash):
    # libcrypt accepts bytes, so non-UTF8 PAM tokens do not need lossy decoding.
    library = ctypes.CDLL('libcrypt.so.1', use_errno=True)
    crypt = library.crypt
    crypt.argtypes = (ctypes.c_char_p, ctypes.c_char_p)
    crypt.restype = ctypes.c_char_p
    encoded = password_hash.encode('ascii')
    result = crypt(secret, encoded)
    if not result or result.startswith(b'*') or not hmac.compare_digest(result, encoded):
        _fail()


def _sealed_blobs():
    # Even a dangling symlink or unsafe plaintext fallback is grounds to refuse.
    if os.path.lexists(MASTER_DIR / 'master.key'):
        _fail()
    public = _read_file(MASTER_DIR / 'master.key.tpm.pub')
    private = _read_file(MASTER_DIR / 'master.key.tpm.priv')
    if not public or not private:
        _fail()
    return public, private


def _run(tool, args, work):
    try:
        return subprocess.run(
            ['/usr/bin/' + tool, *args], cwd=work, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=0.8,
            env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C',
                 'TPM2TOOLS_TCTI': 'device:/dev/tpmrm0'}, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        _fail()


def _unseal():
    public, private = _sealed_blobs()
    with tempfile.TemporaryDirectory(prefix='iris-keyring-', dir='/run') as directory:
        work = Path(directory)
        for name, data in (('seal.pub', public), ('seal.priv', private)):
            fd = os.open(work / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
        args = ['-Q', '-C', '0x81010F00', '-u', 'seal.pub', '-r', 'seal.priv', '-c', 'seal.ctx']
        if _run('tpm2_load', args, work).returncode:
            if _run('tpm2_createprimary', ['-Q', '-C', 'o', '-g', 'sha256', '-G', 'ecc',
                                          '-c', 'primary.ctx'], work).returncode:
                _fail()
            args[2] = 'primary.ctx'
            if _run('tpm2_load', args, work).returncode:
                _fail()
        result = _run('tpm2_unseal', ['-c', 'seal.ctx'], work)
        if result.returncode or len(result.stdout) != 32:
            _fail()
        return result.stdout


def _key():
    _sealed_blobs()
    master = _unseal()
    if len(master) != 32:
        _fail()
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                info=b'iris/login-keyring/v1').derive(master)


@contextmanager
def _locked(create=False):
    _parents(STATE_DIR)
    if create:
        try:
            STATE_DIR.mkdir(mode=0o700)
        except FileExistsError:
            pass
    try:
        directory = os.open(STATE_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        yield None
        return
    lock = None
    try:
        info = os.fstat(directory)
        if info.st_uid != _ROOT_UID or stat.S_IMODE(info.st_mode) != 0o700:
            _fail()
        lock = os.open('.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                       0o600, dir_fd=directory)
        info = os.fstat(lock)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != _ROOT_UID
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            _fail()
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield directory
    finally:
        if lock is not None:
            os.close(lock)
        os.close(directory)


def _aad(identity):
    user, uid, password_hash = identity
    return {'version': 1, 'user': user, 'uid': uid,
            'fingerprint': hashlib.sha256(password_hash.encode('utf-8')).hexdigest()}


def _json(data):
    return json.dumps(data, sort_keys=True, separators=(',', ':')).encode('utf-8')


def _read(uid, directory):
    if directory is None:
        return None
    try:
        data = _read_file(STATE_DIR / f'{uid}.json')
    except FileNotFoundError:
        return None
    record = json.loads(data)
    if not isinstance(record, dict) or record.get('version') != 1:
        _fail()
    fields = {'version', 'user', 'uid', 'fingerprint'}
    if set(record) not in (fields, fields | {'nonce', 'ciphertext'}):
        _fail()
    if (type(record['uid']) is not int or record['uid'] != uid
            or not isinstance(record['user'], str)
            or not isinstance(record['fingerprint'], str)
            or len(record['fingerprint']) != 64
            or any(c not in '0123456789abcdef' for c in record['fingerprint'])):
        _fail()
    if 'nonce' in record:
        nonce = base64.b64decode(record['nonce'], validate=True)
        ciphertext = base64.b64decode(record['ciphertext'], validate=True)
        if len(nonce) != 12 or not 17 <= len(ciphertext) <= MAX_SECRET + 16:
            _fail()
    return record


def _write(uid, record, directory):
    name = '.new-' + os.urandom(16).hex()
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(_json(record)); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, f'{uid}.json', src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(name, dir_fd=directory)
        except FileNotFoundError:
            pass


def _state(identity, record):
    aad = _aad(identity)
    state = 'disabled' if record is None else 'pending-password-login'
    if record is not None:
        if record['user'] != identity[0]:
            _fail()
        if 'ciphertext' in record and all(record[k] == v for k, v in aad.items()):
            state = 'ready'
    return {'user': identity[0], 'uid': identity[1], 'state': state}


def _operation(func):
    def wrapped(*args, **kwargs):
        try:
            _require_root()
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            return func(*args, **kwargs)
        except KeyringError:
            raise
        except Exception:
            raise KeyringError('keyring auto-unlock unavailable') from None
    return wrapped


@_operation
def status(user):
    identity = _identity(user)
    with _locked() as directory:
        return _state(identity, _read(identity[1], directory))


@_operation
def enable(user):
    identity = _identity(user)
    with _locked(create=True) as directory:
        record = _read(identity[1], directory)
        _key()  # prove existing TPM state is usable before opting in
        if record is None:
            record = _aad(identity)
            _write(identity[1], record, directory)
        return _state(identity, record)


@_operation
def disable(user):
    # Revocation must remain possible after account locking or record corruption.
    user, uid = _local_identity(user)
    with _locked() as directory:
        if directory is not None:
            name = f'{uid}.json'
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                             dir_fd=directory)
            except FileNotFoundError:
                pass
            else:
                try:
                    info = os.fstat(fd)
                    if (not stat.S_ISREG(info.st_mode) or info.st_uid != _ROOT_UID
                            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                        _fail()
                    os.unlink(name, dir_fd=directory)
                    os.fsync(directory)
                finally:
                    os.close(fd)
    return {'user': user, 'uid': uid, 'state': 'disabled'}


@_operation
def capture(user, secret):
    if not isinstance(secret, bytes) or not 1 <= len(secret) <= MAX_SECRET or b'\x00' in secret:
        _fail()
    identity = _identity(user)
    with _locked() as directory:
        record = _read(identity[1], directory)
        if record is None:
            return
        _state(identity, record)
        _verify_password(secret, identity[2])
        aad = _aad(identity)
        nonce = os.urandom(12)
        encrypted = AESGCM(_key()).encrypt(nonce, secret, _json(aad))
        if _identity(user) != identity:  # don't provision across a password change
            _fail()
        _write(identity[1], dict(aad, nonce=base64.b64encode(nonce).decode('ascii'),
                               ciphertext=base64.b64encode(encrypted).decode('ascii')), directory)


@_operation
def unlock(user):
    identity = _identity(user)
    with _locked() as directory:
        record = _read(identity[1], directory)
        if _state(identity, record)['state'] != 'ready':
            _fail()
        secret = AESGCM(_key()).decrypt(base64.b64decode(record['nonce']),
                                       base64.b64decode(record['ciphertext']), _json(_aad(identity)))
        if not 1 <= len(secret) <= MAX_SECRET or b'\x00' in secret or _identity(user) != identity:
            _fail()
        return secret


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    try:
        _require_root()
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if len(args) != 2 or args[0] not in ('capture', 'unlock'):
            return 1
        if args[0] == 'capture':
            capture(args[1], sys.stdin.buffer.read(MAX_SECRET + 1))
        else:
            sys.stdout.buffer.write(unlock(args[1]))
            sys.stdout.buffer.flush()
        return 0
    except Exception:
        return 1


if __name__ == '__main__':
    sys.exit(main())
