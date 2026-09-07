<!-- SPDX-License-Identifier: AGPL-3.0-only -->

# Recover password authentication

Iris is designed to fail back to the existing password stack. The realistic
lockout risk is an incorrect PAM edit—especially a wrong control field—not a
camera or recognition failure.

Before enabling Iris in PAM, open a second terminal, run `sudo -i`, and keep
that root shell open until password authentication has been tested.

## Fastest recovery from an open root shell

Remove only the Iris line from the affected service:

```bash
sed -i '/pam_iris\.so/d' /etc/pam.d/gdm-password
sed -i '/pam_iris\.so/d' /etc/pam.d/sudo
sed -i '/pam_iris\.so/d' /etc/pam.d/polkit-1
```

Run only the command for a service you changed. If the installer created
`polkit-1` from `/etc/pam.d/other`, the normal uninstall path can remove that
generated file and restore its original fallback behavior.

## Recover from a text console

Press `Ctrl+Alt+F3` through `Ctrl+Alt+F6`. A text console normally uses the
`login` PAM service, which the Iris installer does not modify. Sign in with
your password, remove the bad line as shown above, then return to the graphical
session with `Ctrl+Alt+F1` or `Ctrl+Alt+F2`.

## Recover through Ubuntu recovery mode

1. Reboot and open GRUB with `Shift` on BIOS systems or `Esc` on UEFI
   systems.
2. Select **Advanced options for Ubuntu**, then a **recovery mode** entry.
3. Select **root — Drop to root shell prompt**.
4. Remount the root filesystem writable:

   ```bash
   mount -o remount,rw /
   ```

5. Remove the Iris line from each service that was enabled, or restore a backup.

## Restore an installer backup

The installer keeps timestamped originals under `/var/backups/iris/`:

```bash
ls -lt /var/backups/iris/
```

Choose the correct timestamped file and restore it explicitly:

```bash
cp -a /var/backups/iris/gdm-password.YYYYMMDD-HHMMSS \
  /etc/pam.d/gdm-password
```

Repeat only for services that were changed. Avoid bulk edits across
`/etc/pam.d/*`; unrelated services may not share the same structure.

## Disable face attempts without changing PAM

If the PAM control field is already
`[success=done default=ignore]`, either action makes Iris return to the
password path:

```bash
sudo iris config set auth.enabled false
sudo systemctl stop irisd
```

Stopping the daemon is also available from a recovery root shell:

```bash
systemctl stop irisd
```

## Use the supported uninstall

When the checkout is available:

```bash
sudo ./install.sh --uninstall
```

The uninstall path restores PAM backups where available before removing the
module and daemon. It deliberately retains enrolled templates and backup files.

After recovery, verify the PAM state and password path before closing the root
shell:

```bash
grep -rn pam_iris /etc/pam.d/
sudo iris doctor --no-camera
```

For non-lockout failures, continue with [Troubleshooting](TROUBLESHOOTING.md).
