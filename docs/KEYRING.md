<!-- SPDX-License-Identifier: AGPL-3.0-only -->

# Optional GNOME Keyring auto-unlock

Iris can optionally unlock the GNOME **login** keyring after a successful face
login. Setup learns the password during one ordinary GDM password login, without
a separate keyring-password setup prompt. The login and keyring passwords must
match. The feature is off by default and applies only to `gdm-password`.

## Enable

You need working Iris face authentication, GNOME Keyring's PAM module, and an
existing TPM-backed Iris template key. Machines using Iris's plaintext master-key
fallback cannot enable this feature. It does not create or migrate a TPM key.

From the updated checkout:

```bash
sudo apt install gnome-keyring libpam-gnome-keyring tpm2-tools
sudo ./install.sh --keyring
sudo iris keyring enable
sudo iris keyring status
```

`--keyring` also enables Iris's GDM integration. It adds optional hooks and keeps
password fallback. Existing custom or unexpected GDM layouts are refused rather
than guessed. `enable` opts in the invoking user; use `--user USER` for another
local account. The management commands require root and never print passwords.

Initially the state is `pending-password-login`. Log out and use your normal
password for one GDM login; let face authentication time out or cover the camera
if necessary. The optional hook captures the existing PAM password without
asking again. It saves it only when a session opens and the password matches the
local account's current password hash. Then check:

```bash
sudo iris keyring status
```

`ready` means a protected credential has been recorded. It is not proof of an
end-to-end GDM/keyring unlock: verify that with your next real face login. The
helper cannot verify that a separately configured keyring password matches your
login password. If they differ, the standard keyring prompt remains.

## Disable and recovery

```bash
sudo iris keyring disable
```

This deletes the active per-user saved credential and disables capture and
release. Optional PAM hooks can remain installed; they do nothing for disabled
users. Uninstalling Iris removes both hooks and active keyring credential state.
It still retains face templates and the existing PAM backups. Deleting active
state is not a guarantee of erasure from filesystem snapshots or external backups.

Changing your account password invalidates the saved credential. The next normal
password login refreshes it while the feature is enabled. Locked or missing local
accounts cannot release a credential. If the TPM is unavailable, cleared, or its
sealed key is lost, Iris does not fall back to a plaintext key. Face login still
works where otherwise possible; GNOME may ask for the keyring password normally.
No auto-unlock failure turns into a login denial or an Iris-generated prompt.

## What is stored and trusted

The vault lives under root-owned `/var/lib/iris/keyring` (directory mode `0700`,
files `0600`). It stores a pending marker or AES-GCM ciphertext of the login
password, using a separate key derived from Iris's existing TPM-sealed master
key. Ciphertext is bound to the username, numeric UID, and current password-hash
fingerprint. Unsealed key bytes and passwords stay in process memory and private
pipes; they are not written to disk, passed in arguments/environment, or logged.
TPM context files use a private directory under `/run`. Python memory handling
cannot guarantee immediate erasure of every transient copy; the native adapter
explicitly clears its password buffers and the helper disables core dumps.

This is **device-bound storage, not a hardware-enforced face check**. The existing
Iris seal is not bound to measured boot/PCRs. Root on the machine can unseal and
recover the stored password; booting an attacker-controlled OS may also allow
that if the vault is accessible. TPM storage is not a substitute for disk
encryption or protecting boot access. A successful face spoof could expose the
keyring, and the saved credential is also your account password. Enable only if
you accept that additional trust in face authentication and the host.

The daemon never receives or returns the password. `pam_iris` creates a success
marker scoped to the PAM handle and username; the optional session adapter checks
it and the GDM service before requesting an unlock secret through a bounded
root-only helper. Password capture is separate from that face-success path.
The adapter supplies GNOME Keyring's `gkr_system_authtok` session data, a
version-sensitive integration contract tested against the installed GNOME Keyring
50 module in an isolated session.
See [GNOME's PAM implementation](https://github.com/GNOME/gnome-keyring/blob/master/pam/gkr-pam-module.c).

## Validation boundaries

Automated tests use temporary files, synthetic credentials, and substituted TPM
and PAM boundaries. They cover encrypted round trips, failed capture, stale state,
user/service binding, optional failures, installer metadata, and cleanup.
An isolated integration test also verifies that the installed GNOME Keyring PAM
module unlocks a disposable keyring from the adapter token without a prompt.
These checks do not establish face-spoof resistance, prove hardware TPM unsealing,
or prove a reboot/login on your machine.
