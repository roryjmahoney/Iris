<!-- SPDX-License-Identifier: AGPL-3.0-only -->

# Troubleshoot Iris

Start with the health summary and daemon log:

```bash
iris doctor
sudo iris status
sudo systemctl status irisd
sudo journalctl -u irisd -n 50 --no-pager
```

`doctor` distinguishes failures from advisory states such as an absent TPM or
a user who has not enrolled yet.

## Every frame is dark or authentication reports `no_face`

Run `iris doctor` and inspect its IR-strobe result. Check that the lid is open,
the camera is uncovered, and another application is not holding the capture
node. If all sampled frames are below the configured threshold, use the
brightest value reported by `doctor` to choose a lower
`camera.min_frame_brightness`.

Confirm that the configured node is a real capture device:

```bash
iris cameras --all
iris config get camera.device
```

See [Camera and hardware notes](HARDWARE.md) before changing formats or
disabling IR mode.

## The camera works but a face is not recognized

- Sit approximately 40–70 cm from the display and face the camera.
- Temporarily lower `recognition.detect_score` from 0.7 to 0.6 only to
  diagnose detection at distance.
- Enrol a second appearance for material changes such as glasses:
  `sudo iris enroll glasses`.
- Run `sudo iris test` and distinguish `no_face`, `no_match`, and
  `spoof_suspected`.

The largest detected face is selected. A small face or one clipped by the frame
may be rejected before matching.

## The daemon does not start

Check:

```bash
sudo systemctl status irisd
sudo journalctl -u irisd -n 100 --no-pager
ls -l /usr/share/iris/models/
```

Common causes are missing models, an unavailable camera, a stale socket after
an abnormal stop, incorrect permissions under `/run/irisd` or
`/var/lib/iris`, or attempting to run `irisd` without root privileges. The
daemon refuses symlinked state and runtime directories.

To observe a foreground run:

```bash
sudo /usr/sbin/irisd --log-level DEBUG
```

## The CLI reports permission denied on the socket

This is expected for daemon operations run without elevation.
`/run/irisd/socket` is `root:root 0600` inside a `0700` directory, and the
daemon independently checks `SO_PEERCRED`. Use `sudo` for enrolment, template
management, and live authentication requests.

## The GNOME Shell extension is missing

The extension is installed as `iris@local`. GNOME discovers a newly installed
system extension at session start:

```bash
gnome-extensions list | grep iris@local
gnome-extensions enable iris@local
```

Log out and back in before running those commands. The extension is
presentation only; face authentication can work without it.

## PAM never invokes Iris

Inspect each layer:

```bash
grep -rn pam_iris /etc/pam.d/
ls -l /usr/lib/x86_64-linux-gnu/security/pam_iris.so
sudo iris status
sudo iris doctor --no-camera
```

The Iris line must be above the existing password path and use exactly:

```text
auth    [success=done default=ignore]    pam_iris.so
```

`gdm-password` covers GDM and unlock, `sudo` covers terminal elevation, and
`polkit-1` covers desktop authorization. Editing one does not enable the
others. A `quiet` module argument suppresses user-facing prompts; a `debug`
argument adds journal diagnostics.

If password authentication is unavailable, stop here and use
[Recovery](RECOVERY.md).

## Authentication stopped after a TPM or firmware event

A cleared TPM owner hierarchy can make an existing sealed key unavailable.
Iris does not silently generate a replacement, because that would strand every
encrypted template.

```bash
sudo journalctl -u irisd | grep -i unseal
sudo iris clear --user "$USER" --yes
sudo iris enroll
```

This discards the old templates and creates fresh key material. Password
authentication remains independent.

## `Too many attempts`

The default policy locks face authentication after five failures within 60
seconds. The counter is in memory; a successful authentication, fresh
enrolment, or daemon restart clears it. The Iris lockout does not lock the
password path.

Review the exact controls in [Configuration](CONFIGURATION.md) and the broader
limitations in [Security](SECURITY.md).
