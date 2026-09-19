<!-- SPDX-License-Identifier: AGPL-3.0-only -->

<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/iris-dial.gif">
  <source media="(prefers-color-scheme: light)" srcset="docs/assets/iris-dial-light.gif">
  <img src="docs/assets/iris-dial.gif" width="288" alt="The Iris authentication dial moving from idle through scanning to a success checkmark">
</picture>

# Iris

**Local infrared face authentication for Ubuntu, GNOME, and Wayland.**

Look at your laptop. That is the whole interaction.

[![License](https://img.shields.io/badge/license-AGPL--3.0--only-blue)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Ubuntu%2026.04-E95420)](docs/INSTALLATION.md)
[![Desktop](https://img.shields.io/badge/GNOME-50%20%C2%B7%20Wayland-4A86CF)](docs/INSTALLATION.md)
[![Storage](https://img.shields.io/badge/templates-AES--256--GCM%20%C2%B7%20TPM%202.0-2EA043)](docs/SECURITY.md)
[![Offline](https://img.shields.io/badge/network-none-555555)](docs/SECURITY.md)

[Website](https://roryjmahoney.github.io/Iris/) · [Install](#quick-install) · [Security model](docs/SECURITY.md) · [Recovery](docs/RECOVERY.md) · [Documentation](docs/README.md)

</div>

---

Iris connects an infrared camera to Linux authentication through a small PAM
module and a root-owned daemon. It can authenticate GDM login and unlock,
`sudo`, and polkit prompts while keeping password authentication available as
the fallback.

> [!WARNING]
> Face authentication is a convenience factor, not a replacement for a strong
> password or passkey. Iris has no depth sensor, has not been certified against
> ISO/IEC 30107-3, cannot resist coercion, and may be defeated by a sufficiently
> capable physical replica. Keep the exact PAM fallback configuration installed
> by Iris and read the [security model](docs/SECURITY.md) before enabling it for
> privilege elevation.

## What Iris provides

- Infrared-first V4L2 capture with strobe-aware frame selection.
- OpenCV YuNet face detection and SFace embeddings from local ONNX models.
- Multi-frame matching and IR presentation-attack heuristics.
- AES-256-GCM-encrypted templates, with optional TPM 2.0 key sealing.
- A GTK4/libadwaita settings app, CLI diagnostics, and a presentation-only
  GNOME Shell extension.
- An intentionally narrow PAM client: image processing, models, camera access,
  and biometric storage remain in the daemon.

Iris stores face embeddings, not captured images. Templates and key material
remain on the machine under `/var/lib/iris`; see
[Security: template storage](docs/SECURITY.md#4-template-storage-encryption-and-tpm-sealing)
for the exact format and limitations.

## Quick install

The supported installation is apt-first. PyGObject, OpenCV, PAM, polkit, and
systemd integration are operating-system dependencies; `pip install .` is not
an installation path for Iris.

```bash
sed '/^[[:space:]]*#/d; /^[[:space:]]*$/d' requirements-apt.txt \
  | xargs sudo apt install
sudo ./install.sh
```

The first command installs the reviewed Ubuntu package set. The installer then
installs and starts Iris but deliberately enables no PAM service.

```bash
sudo iris enroll
sudo iris test
iris doctor
```

Only after recognition works reliably, opt in to the authentication surface you
want. Start with GDM:

```bash
sudo ./install.sh --gdm
```

For an existing PAM service, the installer keeps a metadata-preserving backup;
for a missing service, it builds from `/etc/pam.d/other` and rolls back by
deletion. It then verifies the fail-through control field and interactively
proves that password authentication still works. Keep a second root shell open
while changing authentication. Full prerequisites, setup, and removal are in
the [installation guide](docs/INSTALLATION.md).

## Supported authentication surfaces

| Surface | Installer flag | PAM service | Scope |
|---|---|---|---|
| GDM login | `--gdm` | `gdm-password` | Graphical sign-in |
| GNOME unlock | `--gdm` | `gdm-password` | Lock-screen unlock |
| Terminal elevation | `--sudo` | `sudo` | `sudo` authentication |
| Desktop authorization | `--polkit` | `polkit-1` | polkit and `pkexec` prompts |

The GNOME Shell extension displays status and scan motion; it does not make the
authentication decision. TTY, SSH, and arbitrary PAM services are not wired by
the installer.

## Architecture

```text
Management                                      Authentication

iris-settings ── pkexec ──┐                 GDM / sudo / polkit
iris CLI ─────────────────┤                          │
                          │                    pam_iris.so
                          │                          │
                          └──────────┬───────────────┘
                                     │
                            /run/irisd/socket
                              root:root 0600
                                     │
                                  irisd
                    ┌────────────────┼────────────────┐
                    │                │                │
               IR camera       ONNX inference   encrypted store
               V4L2/GREY       YuNet + SFace    /var/lib/iris

GNOME Shell extension ── presentation only; no daemon or template access
```

The daemon is the only component that opens the camera, loads models, or reads
templates. The PAM module sends a bounded authentication request over the local
Unix socket. A response containing top-level `"ok": true` is its only success
path; every transport, timeout, parse, camera, liveness, or match failure
returns control to the remaining PAM stack.

## Everyday use

```bash
iris-settings                         # graphical setup and settings
sudo iris enroll glasses              # add another appearance
sudo iris list                        # labels, dates, and sample counts
sudo iris test                        # exercise the real authentication path
iris cameras --all                    # inspect capture and metadata nodes
iris doctor                           # installation and hardware diagnostics
sudo iris config set auth.enabled false
```

Run `iris --help` or `iris <command> --help` for the complete CLI. See the
[configuration reference](docs/CONFIGURATION.md) for defaults and safety bounds.

## Development and tests

Install the additional test and documentation-tool dependencies from
[`requirements-dev-apt.txt`](requirements-dev-apt.txt), then run:

```bash
./tests/run.sh
```

The [Iris tests workflow](.github/workflows/tests.yml) runs this full suite on
every pull request and push to `main`, using Ubuntu 26.04. It checks the Python
contracts, GNOME Shell resources, installer rollback, and native PAM fallback;
skipped tests fail CI. These headless checks do not validate camera recognition
or a live graphical login.

Regenerate the README animation from the shipping `DialModel` and its Cairo
painter with:

```bash
python3 tools/render_readme_gif.py --both
python3 tools/render_readme_gif.py --site --both
```

The first command writes the README pair under `docs/assets/`; the second writes
the website pair under `site/assets/` with backgrounds matched to the website's
authentication card.
The README selects between them with `<picture>` and `prefers-color-scheme`, so
the hero animation stays legible on GitHub's light and dark themes. Pass
`--light` or omit it to render a single variant.

Generated review frames under `tools/frames/` are intentionally ignored; the
curated README asset under `docs/assets/` is tracked.

## Documentation

| Guide | Contents |
|---|---|
| [Installation](docs/INSTALLATION.md) | Dependencies, safe setup, PAM opt-in, extension, uninstall |
| [Configuration](docs/CONFIGURATION.md) | Settings, defaults, bounds, advanced keys, reason codes |
| [Hardware](docs/HARDWARE.md) | Camera discovery, IR strobing, measured target notes |
| [Troubleshooting](docs/TROUBLESHOOTING.md) | Camera, daemon, PAM, shell, TPM, and lockout diagnosis |
| [Recovery](docs/RECOVERY.md) | Password fallback and recovery from a bad PAM edit |
| [Security](docs/SECURITY.md) | Threat model, liveness limits, storage, privilege boundaries |
| [Calibration](docs/CALIBRATION.md) | Target-hardware measurements and threshold interpretation |
| [Dial motion](docs/ANIMATION.md) | Rendering states, timing, color, and reduced motion |

The [documentation index](docs/README.md) includes the interface contract and
third-party notices.

## License

Iris source code and documentation are licensed under the
[GNU Affero General Public License v3.0 only](LICENSE) (`AGPL-3.0-only`).
The ONNX model artifacts under `models/` are excluded from that grant and
remain under their original MIT and Apache-2.0 terms. Their pinned provenance,
hashes, attribution, and license copies are recorded in
[`THIRD_PARTY.md`](THIRD_PARTY.md).
