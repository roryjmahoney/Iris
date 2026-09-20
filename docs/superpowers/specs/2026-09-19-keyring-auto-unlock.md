# Optional GNOME Keyring auto-unlock

## User intent
Opt in once, learn the existing password during the next ordinary password login,
and unlock the login keyring on subsequent Iris face logins without an additional
prompt. Login and keyring passwords must match. No conversion to an unencrypted
keyring, password guessing, or reading another application's process memory.

## Boundaries
- Off by default, enabled per local non-root user through `iris keyring enable`.
- Existing face authentication and `[success=done default=ignore]` stay intact.
- Separate optional `pam_iris_keyring.so` captures PAM_AUTHTOK after common-auth;
  only open_session writes it, after a successful login. The helper also verifies
  the token against the current local shadow hash before storing it.
- pam_iris records a per-PAM-handle, per-user face-success marker for gdm-password,
  clearing it before every new attempt. No file or environment-based success flag.
- On session open, the optional module releases a secret only for that marker and
  same user/service, then stashes it as gkr_system_authtok for the installed GNOME
  Keyring session module. This is a version-sensitive GNOME integration contract.
- Root-only helper communicates over private bounded pipes, never argv/env/logs.
  Hard timeout and memory cleanup; failure cannot deny login or prompt by itself.
- TPM-backed existing Iris master key is required; no plaintext-key fallback.
  Keyring ciphertext uses a separate derived AES-GCM key and binds user, UID and
  current password-hash fingerprint as associated data. Password changes invalidate
  old state until the next successful password login. Disable deletes saved state.
- TPM unseal output stays in memory; transient context files go under /run only.
- TPM protection is device-bound, not PCR-bound or a hardware-enforced face check.
  Root remains trusted and can recover the stored password. Face spoofing could
  expose the keyring; this changes the trust boundary and must be documented.
- Strict root ownership, modes, regular files, no symlinks, atomic replacement,
  serialized per-user vault operations; no secrets in status output.
- CLI status distinguishes disabled, pending and ready. Installer opt-in wires only
  gdm-password, backs up metadata, validates known structure, and is idempotent.
  Uninstall removes hooks before binaries and deletes keyring credential state.

## Acceptance
Unit tests exercise encryption, disable, identity/password binding, corruption,
TPM errors, unsafe paths, and no plaintext fallback. Native PAM tests exercise
capture/unlock sequencing, failed auth, user/service changes, repeated handles,
timeouts and prompt-free failure, with isolated fake secrets and temporary PAM
stacks. Existing regressions pass. Installed keyring adapter tested in isolation
where feasible; never claim an actual reboot/login test without observing it.
