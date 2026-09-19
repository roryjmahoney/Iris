<!-- SPDX-License-Identifier: AGPL-3.0-only -->

# Install Iris

Iris uses an apt-first system installation. Its Python code depends on Ubuntu's
PyGObject, OpenCV, NumPy, and cryptography packages, while authentication
depends on PAM, systemd, polkit, and GNOME components. A virtual environment or
`pip install .` does not provide those operating-system integrations.

The current installer and PAM destination target x86_64 Ubuntu. The project
targets a GNOME Wayland desktop, and the bundled GNOME Shell extension declares
versions 48, 49, and 50. Authentication itself does not depend on the
extension.

## 1. Install dependencies

Review [`requirements-apt.txt`](../requirements-apt.txt), then install it:

```bash
sudo apt update
sed '/^[[:space:]]*#/d; /^[[:space:]]*$/d' requirements-apt.txt \
  | xargs sudo apt install
```

`tpm2-tools` enables optional TPM-backed key sealing. `pamtester` is required
when the installer is asked to modify a PAM service. Test and documentation
asset tools have a separate [development manifest](../requirements-dev-apt.txt).

## 2. Install without enabling face authentication

From the repository root:

```bash
sudo ./install.sh
```

This installs:

- the Python package under `/usr/lib/iris`;
- the CLI, daemon launcher, and settings launcher;
- both ONNX files under `/usr/share/iris/models`;
- `pam_iris.so`, the systemd service, polkit policy, desktop entry, and GNOME
  Shell extension;
- the default configuration at `/etc/iris/config.toml`.

It starts `irisd` but changes no file under `/etc/pam.d`. That is the safe
default.

## 3. Enrol and verify

```bash
sudo iris enroll
sudo iris test
iris doctor
```

Do not enable PAM until `sudo iris test` recognizes you reliably. Use
`iris cameras --all` if the selected camera is wrong, and follow the
[hardware guide](HARDWARE.md) before adjusting thresholds.

The graphical setup is available as:

```bash
iris-settings
```

## 4. Enable selected PAM surfaces

Before changing authentication, open a second terminal, run `sudo -i`, and
leave that root shell open. Then rerun the installer with only the desired
flags:

```bash
sudo ./install.sh --gdm
sudo ./install.sh --sudo
sudo ./install.sh --polkit
```

Flags may be combined. `--all-pam` selects all three, but enabling face
authentication for privilege elevation deserves a separate security decision.

| Flag | PAM file | Authentication surface |
|---|---|---|
| `--gdm` | `/etc/pam.d/gdm-password` | GDM login and GNOME unlock |
| `--sudo` | `/etc/pam.d/sudo` | Terminal `sudo` |
| `--polkit` | `/etc/pam.d/polkit-1` | polkit and `pkexec` dialogs |

For each selected service, the installer:

1. stores a metadata-preserving backup when the service file exists, or builds
   a marked new service from `/etc/pam.d/other` when it does not;
2. prepends exactly
   `auth [success=done default=ignore] pam_iris.so`;
3. verifies that the original stack remains intact;
4. temporarily disables face authentication and asks `pamtester` to prove the
   password path still works;
5. restores the backup—or deletes a newly created service—if that proof fails.

If a service already references `pam_iris.so`, the installer accepts it only
when there is exactly one rule with the expected fail-through control field.
Unexpected or duplicate Iris rules are refused without changing the file.

Run PAM wiring from a terminal. With no terminal on standard input, the
structural proof still runs, but the installer cannot collect a password and
prints an explicit warning plus a manual `pamtester` command.

Do not change the control field to `required` or `requisite`, and do not make
`pam_iris.so` the only authentication module. Iris relies on
`default=ignore` to continue to the existing password stack after any camera,
daemon, model, timeout, or match failure.

The installer does not wire `common-auth`, TTY login, SSH, or arbitrary PAM
services.

## 5. Enable the GNOME Shell presentation

The extension is installed system-wide as `iris@local`. Log out and back in
so GNOME discovers it, then run:

```bash
gnome-extensions enable iris@local
```

The extension shows status and lock-screen scan motion only. It is unprivileged
and cannot reach the daemon's root-only socket or make an authentication
decision.

## Updating

Run the current checkout's installer again with the same PAM flags. Existing
`/etc/iris/config.toml` is preserved and backed up.

## Removing Iris

```bash
sudo ./install.sh --uninstall
```

Uninstall restores PAM backups where available and removes installed programs,
the module, service, policies, extension, and documentation. Enrolled templates
under `/var/lib/iris` and PAM backups under `/var/backups/iris` are retained
deliberately.

To erase enrolled faces while Iris is still installed:

```bash
sudo iris clear --yes
```

For an authentication problem, use the [recovery guide](RECOVERY.md) before
removing files manually.

## Reported validation

On 2026-09-19, the project owner reported having installed, upgraded, and removed
Iris in their own clean Ubuntu environment. This records a manual test report;
the Ubuntu version, Iris revisions, and detailed results were not supplied.
