<!-- SPDX-License-Identifier: AGPL-3.0-only -->

# Iris documentation

Start with [Installation](INSTALLATION.md), then keep
[Recovery](RECOVERY.md) available before enabling any PAM surface.

## Operator guides

| Document | Use it for |
|---|---|
| [Installation](INSTALLATION.md) | Ubuntu packages, installation, PAM opt-in, shell integration, removal |
| [Configuration](CONFIGURATION.md) | Supported settings, defaults, ranges, and result codes |
| [Hardware](HARDWARE.md) | Selecting an IR capture node and understanding strobing |
| [Troubleshooting](TROUBLESHOOTING.md) | Diagnosing camera, daemon, PAM, extension, TPM, and lockout failures |
| [Recovery](RECOVERY.md) | Restoring password access after an incorrect PAM edit |

## Design and assurance

| Document | Use it for |
|---|---|
| [Security model](SECURITY.md) | Threats, presentation-attack limits, encryption, socket boundary, PAM behavior |
| [Calibration](CALIBRATION.md) | Measurements from the original target hardware and threshold interpretation |
| [Dial motion](ANIMATION.md) | Shared animation states, geometry, timing, and reduced-motion behavior |
| [Interface contract](../SPEC.md) | Internal module boundaries and non-negotiable safety properties |

## Licensing

Iris code and documentation are licensed
`AGPL-3.0-only`; see [`LICENSE`](../LICENSE). The bundled ONNX files retain
their original third-party terms and are not covered by the Iris license grant.
See [`THIRD_PARTY.md`](../THIRD_PARTY.md) for pinned upstream provenance,
artifact hashes, attribution, and the included model-license copies.
