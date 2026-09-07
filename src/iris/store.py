"""Iris encrypted template storage and master-key management.

This module is the *only* place in Iris that touches persistent biometric
material, and it is deliberately paranoid:

* **Never** store raw images.  Only SFace embeddings (128 float32 values per
  sample) are persisted, so a stolen template file cannot be turned back into
  a photograph of the user's face.
* Every user gets their own file, ``/var/lib/iris/<user>.enc``, encrypted with
  AES-256-GCM.  The *username* is fed in as additional authenticated data
  (AAD), so an attacker with write access to the directory cannot rename
  ``mallory.enc`` to ``root.enc`` and log in as root -- the AEAD tag will not
  verify against the new AAD and decryption fails closed.
* The AES key lives in ``/var/lib/iris/master.key`` (0600 root:root).  When a
  TPM resource-manager node is present the key is *sealed* to the TPM instead
  and only the sealed blob is written to disk, so lifting the disk out of the
  machine does not lift the templates with it.  Any tpm2 failure degrades
  cleanly to the plain 0600 key file rather than bricking authentication.
* All writes are atomic (temp file + ``os.replace``) so a power cut mid-enroll
  leaves the previous templates intact rather than a truncated file that would
  lock the user out.

The whole module assumes it runs as root (the daemon does); write operations
assert ``euid == 0`` rather than producing a half-written, wrongly-owned tree.

Also exported is :class:`FailureTracker`, the in-memory rate limiter the daemon
uses to satisfy SAFETY rule 4 (``max_failures`` within ``lockout_seconds``
-> reason ``lockout``).  It lives here rather than in the daemon so that the
policy state has exactly one implementation.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from collections import OrderedDict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Sequence

import numpy as np
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

log = logging.getLogger("iris.store")

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

DEFAULT_ROOT: Final[str] = "/var/lib/iris"

KEY_FILENAME: Final[str] = "master.key"
SEALED_PUB_FILENAME: Final[str] = "master.key.tpm.pub"
SEALED_PRIV_FILENAME: Final[str] = "master.key.tpm.priv"

KEY_BYTES: Final[int] = 32          # AES-256
NONCE_BYTES: Final[int] = 12        # GCM standard nonce; MUST be unique per key
STORE_VERSION: Final[int] = 1

DIR_MODE: Final[int] = 0o700
FILE_MODE: Final[int] = 0o600

# TPM resource manager.  We never open /dev/tpm0 directly: that is the raw
# device, it is single-open, and grabbing it fights with tpm2-abrmd / the
# in-kernel RM that everything else on the system uses.
TPM_RM_DEVICE: Final[str] = "/dev/tpmrm0"
TPM_TCTI: Final[str] = f"device:{TPM_RM_DEVICE}"

# Owner-hierarchy persistent handle for our storage primary key.  Persisting
# the *primary* (not the sealed object) means unsealing costs one TPM2_Load
# instead of a ~1s TPM2_CreatePrimary on every login.  The sealed blob itself
# stays on disk, as required.  Range 0x81000000-0x817FFFFF is the owner range;
# 0x81010F00 avoids the handles commonly squatted by systemd-cryptenroll
# (0x81000001) and clevis (0x81000000).
TPM_PERSISTENT_HANDLE: Final[str] = "0x81010F00"

_TPM2_TOOLS: Final[tuple[str, ...]] = (
    "tpm2_createprimary",
    "tpm2_create",
    "tpm2_load",
    "tpm2_unseal",
)
_TPM_CMD_TIMEOUT: Final[float] = 30.0

# Sanity bounds.  These keep a malicious or buggy caller from turning the
# template file into an unbounded blob that we then have to decrypt in RAM
# inside a PAM module.
MAX_FACES_PER_USER: Final[int] = 16
MAX_EMBEDDINGS_PER_FACE: Final[int] = 64
MAX_EMBEDDING_DIM: Final[int] = 1024
MAX_NAME_LEN: Final[int] = 64
MAX_USER_LEN: Final[int] = 32
MAX_FILE_BYTES: Final[int] = 8 * 1024 * 1024

# POSIX-ish username validation.  This is a *security* control, not cosmetics:
# the username becomes a path component, so "../../etc/shadow" must never get
# through.  Deliberately stricter than useradd's NAME_REGEX.
_USER_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,31}\$?$")


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #


class StoreError(Exception):
    """Base class for every error raised by this module."""


class KeyUnavailableError(StoreError):
    """The master key exists but cannot be recovered (TPM broken/reset).

    Raised instead of silently minting a fresh key: a new key would render
    every existing template permanently undecryptable, which looks to the user
    like "face login mysteriously forgot me" and silently destroys data.
    """


class TemplateCorruptError(StoreError):
    """A template file failed authentication or is not valid JSON.

    Callers must treat this as "authentication impossible" (fail closed), never
    as "no templates enrolled".
    """


# --------------------------------------------------------------------------- #
# Low-level filesystem helpers
# --------------------------------------------------------------------------- #


def _require_root(operation: str) -> None:
    """Abort a mutating operation unless we are uid 0.

    Running enrollment as a normal user would either fail with EACCES halfway
    through or -- worse, if the directory were ever loosened -- create
    root-owned state with the wrong ownership.  Fail fast and loudly instead.
    """
    if os.geteuid() != 0:
        raise PermissionError(
            f"iris.store: {operation} requires root (euid=0), running as euid="
            f"{os.geteuid()}"
        )


def _fsync_dir(path: Path) -> None:
    """fsync a directory so a rename is durable across a power cut.

    os.replace() is atomic but not durable; without this the metadata update
    can be lost and we come back up pointing at the old inode (or none).
    """
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    except OSError as exc:  # some filesystems refuse dir fsync; not fatal
        log.debug("fsync on directory %s failed: %s", path, exc)
    finally:
        os.close(fd)


def _atomic_write(path: Path, data: bytes, mode: int = FILE_MODE) -> None:
    """Write *data* to *path* atomically with the exact permissions requested.

    mkstemp() creates the temp file 0600 in the destination directory (same
    filesystem, so os.replace is a true atomic rename) and never follows a
    pre-existing symlink at the final path -- rename replaces the link itself.
    """
    parent = path.parent
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        if os.geteuid() == 0:
            os.chown(tmp, 0, 0)
        os.replace(tmp, path)
    except BaseException:
        # Never leave a partial .tmp behind holding plaintext-adjacent state.
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
    _fsync_dir(parent)


def _shred(path: Path) -> None:
    """Best-effort overwrite-then-unlink of a short-lived secret file.

    On journalling/CoW filesystems and SSDs this does not guarantee the old
    bytes are gone; it is cheap insurance for the common ext4 case and it keeps
    the plaintext key out of a file that merely got unlinked.
    """
    try:
        size = path.stat().st_size
        with open(path, "r+b", buffering=0) as fh:
            fh.write(os.urandom(size))
            fh.flush()
            os.fsync(fh.fileno())
    except OSError as exc:
        log.debug("shred of %s failed (continuing to unlink): %s", path, exc)
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def ensure_root_dir(root: Path | str = DEFAULT_ROOT) -> Path:
    """Create/repair ``/var/lib/iris`` as root:root 0700 and return it.

    Refuses to operate through a symlink: if an attacker can plant
    ``/var/lib/iris -> /home/mallory``, then every subsequent chmod/chown we do
    lands on their tree instead of ours.
    """
    path = Path(root)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        _require_root(f"creating {path}")
        # Create with the final mode directly; umask can only clear bits, so
        # re-apply with chmod below rather than trusting mkdir's mode.
        path.mkdir(parents=True, mode=DIR_MODE, exist_ok=True)
        st = os.lstat(path)

    if stat.S_ISLNK(st.st_mode):
        raise StoreError(f"{path} is a symlink; refusing to use it for templates")
    if not stat.S_ISDIR(st.st_mode):
        raise StoreError(f"{path} exists and is not a directory")

    if os.geteuid() == 0:
        if stat.S_IMODE(st.st_mode) != DIR_MODE:
            log.warning(
                "tightening permissions on %s from %#o to %#o",
                path,
                stat.S_IMODE(st.st_mode),
                DIR_MODE,
            )
            os.chmod(path, DIR_MODE)
        if (st.st_uid, st.st_gid) != (0, 0):
            log.warning("re-owning %s to root:root (was %d:%d)", path, st.st_uid, st.st_gid)
            os.chown(path, 0, 0)
    elif stat.S_IMODE(st.st_mode) != DIR_MODE:
        # Non-root callers (tests, the read-only side of the CLI) cannot fix it;
        # say so instead of silently continuing with a world-readable directory.
        log.warning(
            "%s has mode %#o (expected %#o) and we are not root; cannot fix",
            path,
            stat.S_IMODE(st.st_mode),
            DIR_MODE,
        )
    return path


def _validate_user(user: str) -> str:
    """Validate a username that is about to become a path component."""
    if not isinstance(user, str):
        raise TypeError(f"user must be str, got {type(user).__name__}")
    user = user.strip()
    if not user or len(user) > MAX_USER_LEN:
        raise ValueError("username must be 1..32 characters")
    if not _USER_RE.match(user):
        raise ValueError(f"refusing unsafe username {user!r}")
    return user


def _validate_name(name: str) -> str:
    """Validate a human-chosen face label (e.g. "glasses", "no glasses")."""
    if not isinstance(name, str):
        raise TypeError(f"name must be str, got {type(name).__name__}")
    name = name.strip()
    if not name:
        raise ValueError("face name must not be empty")
    if len(name) > MAX_NAME_LEN:
        raise ValueError(f"face name must be <= {MAX_NAME_LEN} characters")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
        raise ValueError("face name must not contain control characters")
    return name


# --------------------------------------------------------------------------- #
# TPM sealing
# --------------------------------------------------------------------------- #


def tpm_available() -> bool:
    """True when we have a resource-manager node *and* the tools to drive it."""
    if not os.path.exists(TPM_RM_DEVICE):
        return False
    missing = [tool for tool in _TPM2_TOOLS if shutil.which(tool) is None]
    if missing:
        log.info("TPM present but tpm2-tools missing: %s", ", ".join(missing))
        return False
    return True


def _tpm_error_text(proc: subprocess.CompletedProcess[bytes]) -> str:
    """Collapse tpm2-tools' multi-line stderr into one journal-friendly line.

    tpm2-tools prints a stack of TCTI/ESYS lines; the last one is the useful
    part, and multi-line log records are a nuisance in journald.
    """
    lines = [
        line.strip()
        for line in proc.stderr.decode("utf-8", "replace").splitlines()
        if line.strip()
    ]
    return lines[-1] if lines else f"exit status {proc.returncode}"


def _run_tpm2(args: Sequence[str], *, cwd: Path) -> subprocess.CompletedProcess[bytes]:
    """Run one tpm2-tools command with a hard timeout and a minimal env.

    A wedged TPM must never hang a login: every call is bounded, and every
    failure mode (missing tool, non-zero exit, timeout) is surfaced as a
    :class:`StoreError` so callers have exactly one thing to catch before
    falling back to the key file.
    """
    exe = shutil.which(args[0])
    if exe is None:
        raise StoreError(f"{args[0]} not found on PATH")
    env = {
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "TPM2TOOLS_TCTI": TPM_TCTI,
        # Keep messages deterministic so log greps and the error text we quote
        # do not depend on the caller's locale.
        "LC_ALL": "C",
    }
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell, absolute exe
            [exe, *args[1:]],
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_TPM_CMD_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise StoreError(
            f"{args[0]} timed out after {_TPM_CMD_TIMEOUT:.0f}s (TPM wedged?)"
        ) from exc
    except OSError as exc:
        raise StoreError(f"could not execute {args[0]}: {exc}") from exc
    if proc.returncode != 0:
        log.debug("%s failed rc=%d: %s", args[0], proc.returncode, _tpm_error_text(proc))
    return proc


class KeyManager:
    """Owns the AES-256 master key: creation, TPM sealing and recovery.

    Resolution order in :meth:`load_key`:

    1. sealed blob present and TPM usable -> unseal it;
    2. sealed blob present but unusable -> plain key file if one exists,
       otherwise :class:`KeyUnavailableError` (never mint a replacement);
    3. plain key file present -> use it;
    4. nothing present -> generate 32 random bytes, try to seal them, and only
       write the plain key file if sealing (or its verification) failed.
    """

    def __init__(self, root: Path | str = DEFAULT_ROOT, *, use_tpm: bool = True) -> None:
        self.root = Path(root)
        self.use_tpm = use_tpm
        self.key_path = self.root / KEY_FILENAME
        self.sealed_pub_path = self.root / SEALED_PUB_FILENAME
        self.sealed_priv_path = self.root / SEALED_PRIV_FILENAME
        #: "tpm", "file" or "" until a key has been loaded.  Purely diagnostic.
        self.backend: str = ""
        self._cached: bytes | None = None
        self._lock = threading.Lock()

    # -- public API -------------------------------------------------------- #

    def load_key(self) -> bytes:
        """Return the 32-byte master key, creating it on first use."""
        with self._lock:
            if self._cached is not None:
                return self._cached
            key = self._resolve_key()
            if len(key) != KEY_BYTES:
                raise KeyUnavailableError(
                    f"master key has {len(key)} bytes, expected {KEY_BYTES}"
                )
            self._cached = key
            return key

    def forget(self) -> None:
        """Drop the cached key (used by tests and after a key rotation)."""
        with self._lock:
            self._cached = None

    # -- resolution -------------------------------------------------------- #

    def _resolve_key(self) -> bytes:
        ensure_root_dir(self.root)
        sealed = self.sealed_pub_path.exists() and self.sealed_priv_path.exists()
        plain = self.key_path.exists()

        if sealed and self.use_tpm and tpm_available():
            try:
                key = self._tpm_unseal()
                self.backend = "tpm"
                log.info("master key unsealed from TPM (handle %s)", TPM_PERSISTENT_HANDLE)
                return key
            except StoreError as exc:
                if plain:
                    log.error(
                        "TPM unseal failed (%s); falling back to %s", exc, self.key_path
                    )
                else:
                    raise KeyUnavailableError(
                        f"TPM unseal failed and no fallback key file exists: {exc}. "
                        "The TPM owner hierarchy was probably cleared; existing "
                        "templates cannot be decrypted. Re-enroll after running "
                        "'iris clear' as root."
                    ) from exc
        elif sealed and not plain:
            raise KeyUnavailableError(
                f"{self.sealed_priv_path} exists but the TPM is unavailable; "
                "refusing to generate a new key that would orphan every template"
            )

        if plain:
            key = self._read_key_file()
            self.backend = "file"
            log.info("master key loaded from %s (plain 0600 key file)", self.key_path)
            return key

        return self._create_key()

    def _read_key_file(self) -> bytes:
        # No root assertion here: the file is 0600 root:root, so a non-root
        # reader is stopped by the kernel with EACCES, which is the honest
        # error.  We only need privileges for the repair path below.
        st = os.lstat(self.key_path)
        if stat.S_ISLNK(st.st_mode):
            raise StoreError(f"{self.key_path} is a symlink; refusing to read it")
        if stat.S_IMODE(st.st_mode) != FILE_MODE or (
            os.geteuid() == 0 and (st.st_uid, st.st_gid) != (0, 0)
        ):
            # Repair rather than refuse: wrong permissions mean the key may
            # have leaked, but refusing would lock the user out of their
            # machine.  Shout in the log; the operator decides whether to rotate.
            log.warning(
                "%s had mode %#o owner %d:%d; tightening to 0600 root:root",
                self.key_path,
                stat.S_IMODE(st.st_mode),
                st.st_uid,
                st.st_gid,
            )
            os.chmod(self.key_path, FILE_MODE)
            if os.geteuid() == 0:
                os.chown(self.key_path, 0, 0)
        data = self.key_path.read_bytes()
        if len(data) != KEY_BYTES:
            raise KeyUnavailableError(
                f"{self.key_path} holds {len(data)} bytes, expected {KEY_BYTES}"
            )
        return data

    def _create_key(self) -> bytes:
        _require_root("creating the master key")
        key = os.urandom(KEY_BYTES)

        if self.use_tpm and tpm_available():
            try:
                self._tpm_seal(key)
                # Prove we can get it back *before* discarding the plaintext.
                # A sealed blob we cannot unseal is indistinguishable from data
                # loss, so verify the round trip while we still hold the key.
                recovered = self._tpm_unseal()
                if recovered != key:
                    raise StoreError("TPM unseal returned different bytes than sealed")
                self.backend = "tpm"
                log.info(
                    "master key generated and sealed to the TPM "
                    "(blob=%s, parent handle=%s); no plaintext key on disk",
                    self.sealed_priv_path,
                    TPM_PERSISTENT_HANDLE,
                )
                return key
            except (StoreError, subprocess.TimeoutExpired, OSError) as exc:
                log.warning(
                    "TPM sealing failed (%s); falling back to a plain 0600 key file",
                    exc,
                )
                for path in (self.sealed_pub_path, self.sealed_priv_path):
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
        elif not self.use_tpm:
            log.info("TPM sealing disabled by caller; using a plain 0600 key file")
        else:
            log.info(
                "no usable TPM at %s; master key will be stored as a plain "
                "0600 key file",
                TPM_RM_DEVICE,
            )

        _atomic_write(self.key_path, key, FILE_MODE)
        self.backend = "file"
        log.info("master key generated and written to %s (0600 root:root)", self.key_path)
        return key

    # -- TPM plumbing ------------------------------------------------------ #

    def _tpm_workdir(self) -> tempfile.TemporaryDirectory[str]:
        """Scratch directory for tpm2-tools context files.

        Placed *inside* /var/lib/iris (0700 root) rather than /tmp so the
        transient plaintext key file cannot be read by other users even for the
        microseconds it exists, regardless of how /tmp is mounted.
        """
        ensure_root_dir(self.root)
        return tempfile.TemporaryDirectory(prefix=".tpm-", dir=self.root)

    def _load_sealed_object(self, work: Path) -> None:
        """Load the sealed blob and leave its context at ``work/seal.ctx``.

        Fast path uses the persisted primary; if that handle is empty, holds a
        foreign object, or the TPM was reset, we re-derive the primary from the
        owner hierarchy (deterministic for a fixed template) and retry.
        """
        pub = work / "seal.pub"
        priv = work / "seal.priv"
        pub.write_bytes(self.sealed_pub_path.read_bytes())
        priv.write_bytes(self.sealed_priv_path.read_bytes())

        fast = _run_tpm2(
            ["tpm2_load", "-C", TPM_PERSISTENT_HANDLE,
             "-u", "seal.pub", "-r", "seal.priv", "-c", "seal.ctx"],
            cwd=work,
        )
        if fast.returncode == 0:
            return

        log.debug(
            "persistent handle %s unusable, re-deriving primary", TPM_PERSISTENT_HANDLE
        )
        primary = _run_tpm2(
            ["tpm2_createprimary", "-C", "o", "-g", "sha256", "-G", "ecc",
             "-c", "primary.ctx"],
            cwd=work,
        )
        if primary.returncode != 0:
            raise StoreError(
                "tpm2_createprimary failed: " + _tpm_error_text(primary)
            )
        slow = _run_tpm2(
            ["tpm2_load", "-C", "primary.ctx",
             "-u", "seal.pub", "-r", "seal.priv", "-c", "seal.ctx"],
            cwd=work,
        )
        if slow.returncode != 0:
            raise StoreError(
                "tpm2_load failed: " + _tpm_error_text(slow)
            )

    def _tpm_unseal(self) -> bytes:
        with self._tpm_workdir() as tmpdir:
            work = Path(tmpdir)
            self._load_sealed_object(work)
            out = work / "key.bin"
            proc = _run_tpm2(
                ["tpm2_unseal", "-c", "seal.ctx", "-o", "key.bin"], cwd=work
            )
            if proc.returncode != 0:
                raise StoreError(
                    "tpm2_unseal failed: " + _tpm_error_text(proc)
                )
            try:
                key = out.read_bytes()
            finally:
                _shred(out)
            if len(key) != KEY_BYTES:
                raise StoreError(
                    f"unsealed {len(key)} bytes, expected {KEY_BYTES}"
                )
            return key

    def _tpm_seal(self, key: bytes) -> None:
        """Seal *key* under a TPM owner-hierarchy primary and persist the blob.

        No PCR policy is attached: the goal is "these templates only decrypt on
        this machine", not "only in this boot state".  Binding to PCRs would
        break face login on every kernel/firmware update, which in practice
        gets the whole feature disabled by the user -- a worse outcome.
        """
        _require_root("sealing the master key")
        with self._tpm_workdir() as tmpdir:
            work = Path(tmpdir)

            primary = _run_tpm2(
                ["tpm2_createprimary", "-C", "o", "-g", "sha256", "-G", "ecc",
                 "-c", "primary.ctx"],
                cwd=work,
            )
            if primary.returncode != 0:
                raise StoreError(
                    "tpm2_createprimary failed: " + _tpm_error_text(primary)
                )

            secret = work / "secret.bin"
            fd = os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
            try:
                os.write(fd, key)
            finally:
                os.close(fd)

            try:
                created = _run_tpm2(
                    ["tpm2_create", "-C", "primary.ctx", "-g", "sha256",
                     "-i", "secret.bin", "-u", "seal.pub", "-r", "seal.priv"],
                    cwd=work,
                )
            finally:
                _shred(secret)
            if created.returncode != 0:
                raise StoreError(
                    "tpm2_create (seal) failed: " + _tpm_error_text(created)
                )

            loaded = _run_tpm2(
                ["tpm2_load", "-C", "primary.ctx", "-u", "seal.pub",
                 "-r", "seal.priv", "-c", "seal.ctx"],
                cwd=work,
            )
            if loaded.returncode != 0:
                raise StoreError(
                    "tpm2_load of the freshly sealed object failed: " + _tpm_error_text(loaded)
                )

            # Best effort: persist the primary so later unseals skip the
            # expensive CreatePrimary.  If the handle is taken by another
            # subsystem we simply keep re-deriving the primary each time.
            evict = _run_tpm2(
                ["tpm2_evictcontrol", "-C", "o", "-c", "primary.ctx",
                 TPM_PERSISTENT_HANDLE],
                cwd=work,
            )
            if evict.returncode != 0:
                log.info(
                    "could not persist the TPM primary at %s (%s); unsealing will "
                    "re-derive it each time",
                    TPM_PERSISTENT_HANDLE,
                    _tpm_error_text(evict),
                )

            _atomic_write(self.sealed_pub_path, (work / "seal.pub").read_bytes())
            _atomic_write(self.sealed_priv_path, (work / "seal.priv").read_bytes())

            # The plain key file must not survive alongside a sealed blob: it
            # would defeat the entire point of sealing.
            if self.key_path.exists():
                log.warning(
                    "removing plaintext %s now that the key is TPM-sealed",
                    self.key_path,
                )
                _shred(self.key_path)


# --------------------------------------------------------------------------- #
# Template store
# --------------------------------------------------------------------------- #


def _now_iso() -> str:
    """UTC timestamp, second resolution -- stable and JSON/TOML friendly."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class TemplateStore:
    """Per-user, AES-256-GCM encrypted face template storage (root only).

    On-disk layout of ``/var/lib/iris/<user>.enc``::

        [ 12-byte random nonce ][ AES-256-GCM ciphertext || 16-byte tag ]

    with AAD = the username, and plaintext::

        {"version": 1,
         "faces": [{"name": str, "created": iso8601, "embeddings": [[float,...]]}]}
    """

    def __init__(
        self,
        root: str | os.PathLike[str] = DEFAULT_ROOT,
        key_manager: KeyManager | None = None,
    ) -> None:
        self.root = Path(root)
        self._keys = key_manager if key_manager is not None else KeyManager(self.root)
        self._lock = threading.Lock()

    # -- public API (SPEC) -------------------------------------------------- #

    def list_faces(self, user: str) -> list[dict[str, Any]]:
        """Return face metadata without ever handing out embeddings."""
        doc = self._read(_validate_user(user))
        return [
            {
                "name": face["name"],
                "created": face["created"],
                "samples": len(face["embeddings"]),
            }
            for face in doc["faces"]
        ]

    def add(self, user: str, name: str, embeddings: list[np.ndarray]) -> None:
        """Store (or replace) the named face for *user*.

        Re-enrolling an existing name replaces it outright rather than merging:
        the user's intent when they re-enroll "default" is "use these samples",
        and merging would silently keep stale samples from a haircut ago.
        """
        user = _validate_user(user)
        name = _validate_name(name)
        vectors = self._normalise_embeddings(embeddings)

        with self._lock:
            doc = self._read(user)
            dim = vectors[0].shape[0]
            for face in doc["faces"]:
                for existing in face["embeddings"]:
                    if len(existing) != dim:
                        raise ValueError(
                            f"existing templates for {user} use {len(existing)}-D "
                            f"embeddings but these are {dim}-D; the recognition "
                            "model changed -- clear the enrollment and redo it"
                        )

            faces = [f for f in doc["faces"] if f["name"] != name]
            if len(faces) != len(doc["faces"]):
                log.info("replacing existing face %r for user %s", name, user)
            if len(faces) >= MAX_FACES_PER_USER:
                raise ValueError(
                    f"user {user} already has {len(faces)} faces "
                    f"(limit {MAX_FACES_PER_USER})"
                )

            faces.append(
                {
                    "name": name,
                    "created": _now_iso(),
                    # float(): json cannot serialise numpy scalars.
                    "embeddings": [[float(x) for x in vec] for vec in vectors],
                }
            )
            doc["faces"] = faces
            self._write(user, doc)

        log.info(
            "stored face %r for user %s (%d samples, %d-D)", name, user, len(vectors), dim
        )

    def remove(self, user: str, name: str) -> bool:
        """Delete one named face.  Returns True when something was removed."""
        user = _validate_user(user)
        name = _validate_name(name)
        with self._lock:
            doc = self._read(user)
            kept = [f for f in doc["faces"] if f["name"] != name]
            if len(kept) == len(doc["faces"]):
                return False
            doc["faces"] = kept
            if kept:
                self._write(user, doc)
            else:
                # No faces left: remove the file entirely so `list_faces`
                # reports "not enrolled" instead of an empty encrypted shell.
                self._unlink(user)
        log.info("removed face %r for user %s", name, user)
        return True

    def clear(self, user: str) -> None:
        """Delete every template for *user* (idempotent)."""
        user = _validate_user(user)
        with self._lock:
            self._unlink(user)
        log.info("cleared all face templates for user %s", user)

    def embeddings_for(self, user: str) -> list[tuple[str, np.ndarray]]:
        """Return ``(face_name, embedding)`` pairs for matching.

        One tuple per *sample*, so the daemon can compare against every
        enrolled frame and report which named face matched.
        """
        doc = self._read(_validate_user(user))
        out: list[tuple[str, np.ndarray]] = []
        for face in doc["faces"]:
            for vec in face["embeddings"]:
                out.append((face["name"], np.asarray(vec, dtype=np.float32)))
        return out

    # -- convenience -------------------------------------------------------- #

    def has_user(self, user: str) -> bool:
        """True when *user* has at least one stored template file."""
        return self._path_for(_validate_user(user)).exists()

    def enrolled_users(self) -> list[str]:
        """Every username with a template file, sorted."""
        try:
            names = [p.stem for p in self.root.glob("*.enc") if p.is_file()]
        except OSError as exc:
            log.error("cannot list %s: %s", self.root, exc)
            return []
        return sorted(n for n in names if _USER_RE.match(n))

    @property
    def key_backend(self) -> str:
        """"tpm", "file", or "" if no key has been touched yet."""
        return self._keys.backend

    # -- internals ---------------------------------------------------------- #

    def _path_for(self, user: str) -> Path:
        # user is already validated; assert the invariant that keeps this from
        # ever escaping the store directory.
        path = self.root / f"{user}.enc"
        if path.parent != self.root:
            raise ValueError(f"refusing path traversal for user {user!r}")
        return path

    @staticmethod
    def _normalise_embeddings(embeddings: Sequence[np.ndarray]) -> list[np.ndarray]:
        """Validate and flatten SFace outputs into 1-D float32 vectors.

        SFace returns shape (1, 128); callers may hand us either that or an
        already-flat vector.  Non-finite values would silently poison cosine
        similarity, so they are rejected here rather than at match time.
        """
        if embeddings is None or len(embeddings) == 0:
            raise ValueError("at least one embedding is required")
        if len(embeddings) > MAX_EMBEDDINGS_PER_FACE:
            raise ValueError(
                f"too many embeddings ({len(embeddings)}); "
                f"limit is {MAX_EMBEDDINGS_PER_FACE}"
            )
        out: list[np.ndarray] = []
        dim: int | None = None
        for i, raw in enumerate(embeddings):
            vec = np.asarray(raw, dtype=np.float32).ravel()
            if vec.ndim != 1 or vec.size == 0:
                raise ValueError(f"embedding {i} is empty")
            if vec.size > MAX_EMBEDDING_DIM:
                raise ValueError(
                    f"embedding {i} has {vec.size} dims (limit {MAX_EMBEDDING_DIM})"
                )
            if not np.all(np.isfinite(vec)):
                raise ValueError(f"embedding {i} contains NaN or infinity")
            if dim is None:
                dim = int(vec.size)
            elif vec.size != dim:
                raise ValueError(
                    f"embedding {i} has {vec.size} dims, expected {dim}"
                )
            out.append(vec)
        return out

    def _aesgcm(self) -> AESGCM:
        return AESGCM(self._keys.load_key())

    def _empty_doc(self) -> dict[str, Any]:
        return {"version": STORE_VERSION, "faces": []}

    def _read(self, user: str) -> dict[str, Any]:
        """Decrypt and validate *user*'s template document.

        A missing file is "not enrolled" (empty document).  A file that fails
        the GCM tag is an error, never an empty document: silently treating
        tampering as "no templates" would hide an attack.
        """
        path = self._path_for(user)
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return self._empty_doc()
        if stat.S_ISLNK(st.st_mode):
            raise TemplateCorruptError(f"{path} is a symlink; refusing to read it")
        if st.st_size > MAX_FILE_BYTES:
            raise TemplateCorruptError(
                f"{path} is {st.st_size} bytes (limit {MAX_FILE_BYTES})"
            )
        blob = path.read_bytes()
        if len(blob) <= NONCE_BYTES + 16:  # nonce + at least the GCM tag
            raise TemplateCorruptError(f"{path} is truncated ({len(blob)} bytes)")

        nonce, ciphertext = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
        try:
            # AAD = username: binds the ciphertext to this file name, so a
            # renamed/swapped template fails to authenticate.
            plaintext = self._aesgcm().decrypt(nonce, ciphertext, user.encode("utf-8"))
        except InvalidTag as exc:
            raise TemplateCorruptError(
                f"{path} failed authentication -- wrong master key, or the file "
                "was tampered with or copied from another user"
            ) from exc

        try:
            doc = json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TemplateCorruptError(f"{path} does not contain valid JSON") from exc
        finally:
            del plaintext

        return self._validate_doc(doc, path)

    @staticmethod
    def _validate_doc(doc: Any, path: Path) -> dict[str, Any]:
        """Structurally validate a decrypted document.

        The content is authenticated, so this guards against *our own* older or
        newer formats rather than an attacker -- but the daemon still must not
        crash on a surprise shape.
        """
        if not isinstance(doc, dict):
            raise TemplateCorruptError(f"{path}: top level is not an object")
        version = doc.get("version")
        if version != STORE_VERSION:
            raise TemplateCorruptError(
                f"{path}: unsupported store version {version!r} "
                f"(this build understands {STORE_VERSION})"
            )
        faces = doc.get("faces")
        if not isinstance(faces, list):
            raise TemplateCorruptError(f"{path}: 'faces' is not a list")

        clean: list[dict[str, Any]] = []
        for face in faces:
            if not isinstance(face, dict):
                raise TemplateCorruptError(f"{path}: face entry is not an object")
            name = face.get("name")
            created = face.get("created")
            embeddings = face.get("embeddings")
            if not isinstance(name, str) or not name:
                raise TemplateCorruptError(f"{path}: face has no valid name")
            if not isinstance(created, str):
                created = ""
            if not isinstance(embeddings, list) or not embeddings:
                raise TemplateCorruptError(f"{path}: face {name!r} has no embeddings")
            vectors: list[list[float]] = []
            for vec in embeddings:
                if not isinstance(vec, list) or not vec:
                    raise TemplateCorruptError(
                        f"{path}: face {name!r} has a malformed embedding"
                    )
                if not all(isinstance(x, (int, float)) and not isinstance(x, bool)
                           for x in vec):
                    raise TemplateCorruptError(
                        f"{path}: face {name!r} has a non-numeric embedding"
                    )
                vectors.append([float(x) for x in vec])
            clean.append({"name": name, "created": created, "embeddings": vectors})
        return {"version": STORE_VERSION, "faces": clean}

    def _write(self, user: str, doc: dict[str, Any]) -> None:
        _require_root(f"writing templates for {user}")
        ensure_root_dir(self.root)
        plaintext = json.dumps(doc, separators=(",", ":")).encode("utf-8")
        # A fresh random nonce per write.  Never reuse one under the same key:
        # nonce reuse in GCM leaks the XOR of plaintexts and the auth subkey.
        nonce = os.urandom(NONCE_BYTES)
        ciphertext = self._aesgcm().encrypt(nonce, plaintext, user.encode("utf-8"))
        _atomic_write(self._path_for(user), nonce + ciphertext, FILE_MODE)

    def _unlink(self, user: str) -> None:
        _require_root(f"deleting templates for {user}")
        path = self._path_for(user)
        try:
            path.unlink()
        except FileNotFoundError:
            return
        _fsync_dir(self.root)


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #


class FailureTracker:
    """In-memory, sliding-window failure counter (SAFETY rule 4).

    Deliberately *not* persisted: a reboot clearing the lockout is acceptable
    (an attacker who can reboot the machine has better options), whereas a
    persistent file would be one more root-owned thing to get wrong, and a
    lockout that survives reboots is a great way to brick a laptop.

    Timestamps come from :func:`time.monotonic` so that NTP steps, suspend/
    resume and DST cannot shorten -- or extend forever -- a lockout.

    Thread-safe: the daemon serves several clients and the GUI concurrently.
    """

    #: Per-user history cap.  Only the newest `max_failures` entries can ever
    #: matter, so anything beyond this is dead weight.
    #:
    #: SAFETY: this is also a hard ceiling on an *effective* ``auth.max_failures``.
    #: ``failure_count()`` can never exceed this, so a configured value above it
    #: would make ``is_locked()`` permanently false and disable rate limiting
    #: without any error.  ``cli._RANGES["auth.max_failures"]`` is clamped to the
    #: same number so that configuration is unreachable; raise both together or
    #: neither.
    _MAX_HISTORY: Final[int] = 64

    def __init__(self, max_users: int = 1024) -> None:
        if max_users < 1:
            raise ValueError("max_users must be >= 1")
        self._max_users = max_users
        # OrderedDict as an LRU: an attacker spraying random usernames at the
        # socket must not be able to grow this without bound.
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    def record_failure(self, user: str) -> None:
        """Record one failed authentication attempt for *user*."""
        now = time.monotonic()
        with self._lock:
            history = self._failures.get(user)
            if history is None:
                history = deque(maxlen=self._MAX_HISTORY)
                self._failures[user] = history
            history.append(now)
            self._failures.move_to_end(user)
            while len(self._failures) > self._max_users:
                evicted, _ = self._failures.popitem(last=False)
                log.debug("evicted failure history for %r (LRU)", evicted)

    def reset(self, user: str) -> None:
        """Clear *user*'s history.  Call this on every successful auth."""
        with self._lock:
            self._failures.pop(user, None)

    def is_locked(self, user: str, max_failures: int, lockout_seconds: float) -> bool:
        """True when *user* has >= max_failures failures in the last window.

        ``max_failures <= 0`` disables face auth entirely (always locked);
        ``lockout_seconds <= 0`` disables the lockout (never locked).  Both are
        honoured rather than clamped so config.toml can express either policy.
        """
        if lockout_seconds <= 0:
            return False
        if max_failures <= 0:
            return True
        return self.failure_count(user, lockout_seconds) >= max_failures

    def failure_count(self, user: str, lockout_seconds: float) -> int:
        """Number of failures still inside the sliding window."""
        cutoff = time.monotonic() - max(lockout_seconds, 0.0)
        with self._lock:
            history = self._failures.get(user)
            if history is None:
                return 0
            # Prune on read: keeps memory bounded without a background timer.
            while history and history[0] < cutoff:
                history.popleft()
            if not history:
                del self._failures[user]
                return 0
            return len(history)

    def seconds_until_unlock(
        self, user: str, max_failures: int, lockout_seconds: float
    ) -> float:
        """Seconds until *user* may try again (0.0 when not locked).

        The daemon uses this to put a useful number in the "lockout" reason.
        """
        if not self.is_locked(user, max_failures, lockout_seconds):
            return 0.0
        with self._lock:
            history = self._failures.get(user)
            if not history:
                return 0.0
            # We are locked because the newest `max_failures` entries are all
            # inside the window; the lock lifts when the oldest of those ages
            # out.
            index = max(len(history) - max_failures, 0)
            oldest_relevant = history[index]
        remaining = (oldest_relevant + lockout_seconds) - time.monotonic()
        return max(remaining, 0.0)


__all__ = [
    "DEFAULT_ROOT",
    "FailureTracker",
    "KeyManager",
    "KeyUnavailableError",
    "StoreError",
    "TemplateCorruptError",
    "TemplateStore",
    "ensure_root_dir",
    "tpm_available",
]
