# Keyring Auto-unlock Implementation Plan

**Goal:** Optional prompt-free keyring unlock after one normal password login.
**Spec:** ../specs/2026-09-19-keyring-auto-unlock.md
**Architecture:** Separate native optional PAM adapter and root-only Python vault;
no secret transport in irisd. CLI and installer provide explicit opt-in.
**Tech Stack:** Linux PAM C, Python, cryptography AES-GCM/HKDF, tpm2-tools.

## Tasks
- [x] Storage/helper: src/iris/keyring.py and tests/test_keyring.py. TPM-only existing
  master unseal, encrypted per-user state, enable/disable/status/capture/unlock.
- [x] PAM: pam_iris face-success marker, optional pam_iris_keyring, Makefile, isolated
  native tests. Fixed helper `/usr/bin/python3 -I -B /usr/lib/iris/iris/keyring.py`;
  arguments `capture USER` (stdin secret) or `unlock USER` (stdout secret), <=4096
  bytes, no newline framing. Five-second wall-clock budget; fail optional.
- [x] Integration: `iris keyring {enable,disable,status} [--user USER]`, optional
  installer flag and atomic/idempotent GDM hooks with backups, cleanup on uninstall.
- [x] Documentation, full regression run, independent security/correctness review,
  isolated real TPM and GNOME adapter evidence when safely available.

## Review focus
Never store a token from failed auth; never reuse face success for another user,
service or attempt; no unbounded helper; reject unsafe files and changed passwords;
never fall back to an unsealed key; retain password fallback and uninstall safety.

## Execution rulings
The user approved the feature and one ordinary password login for provisioning.
Proceed with implementation without requesting repeated design approvals.
Do not install live PAM changes until the implementation and isolated gates pass.

## Verification evidence

- Full `./tests/run.sh`: 49 Python tests, 12 existing installer cases, native
  keyring transport/marker tests and existing PAM gates passed.
- `make -C pam check`: both modules pass strict warning-as-error compilation.
- Installed GNOME Keyring 50 PAM module consumes the adapter token and unlocks
  a synthetic keyring in an isolated D-Bus/HOME/data/control environment, with
  a conversation callback that refuses prompts.
- Independent review found no blocking issues, including a second review of
  GNOME test isolation and vault revocation/core-dump fixes.
- No live PAM installation, credential capture, TPM mutation or GDM login was
  performed. Hardware TPM unseal is covered by boundary tests, not a live proof.
