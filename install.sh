#!/usr/bin/env bash
#
# Iris installer.
#
# Design rule that governs this whole script: a face-authentication system that
# breaks password authentication has failed catastrophically, so PAM is treated
# as radioactive. Nothing under /etc/pam.d is touched unless you explicitly ask
# for it with a flag; every edit is backed up first; every edit is followed by a
# live proof that your password still works, and rolls itself back if that proof
# fails.
#
#   sudo ./install.sh                     # install everything, wire NO PAM
#   sudo ./install.sh --gdm               # ...and enable face login + lock screen
#   sudo ./install.sh --sudo --polkit     # ...and terminal sudo + polkit dialogs
#   sudo ./install.sh --uninstall         # full removal, restores PAM backups
#
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PREFIX_LIB=/usr/lib/iris
PREFIX_SHARE=/usr/share/iris
CONFIG_DIR=/etc/iris
CONFIG_FILE="$CONFIG_DIR/config.toml"
STATE_DIR=/var/lib/iris
BACKUP_DIR=/var/backups/iris
EXT_UUID="iris@local"
EXT_DIR="/usr/share/gnome-shell/extensions/$EXT_UUID"
PAM_SECURITY_DIR=/usr/lib/x86_64-linux-gnu/security
DOC_DIR=/usr/share/doc/iris
STAMP="$(date +%Y%m%d-%H%M%S)"

WIRE_KEYRING=0; WIRE_GDM=0; WIRE_SUDO=0; WIRE_POLKIT=0; DO_UNINSTALL=0; ASSUME_YES=0

if [[ -t 1 ]]; then
  R=$'\e[31m'; G=$'\e[32m'; Y=$'\e[33m'; B=$'\e[34m'; DIM=$'\e[2m'; BOLD=$'\e[1m'; N=$'\e[0m'
else
  R=''; G=''; Y=''; B=''; DIM=''; BOLD=''; N=''
fi
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$1"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$1"; }
die()  { printf '  %s✗%s %s\n' "$R" "$N" "$1" >&2; exit 1; }
step() { printf '\n%s%s%s\n' "$BOLD" "$1" "$N"; }
note() { printf '    %s%s%s\n' "$DIM" "$1" "$N"; }

usage() {
  sed -n '3,20p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
  cat <<'EOF'

Options:
  --gdm        Wire GDM login and the GNOME lock screen (edits /etc/pam.d/gdm-password)
  --keyring    Add optional keyring hooks to GDM (implies --gdm; enable per user separately)
  --sudo       Wire terminal sudo                       (edits /etc/pam.d/sudo)
  --polkit     Wire polkit / pkexec dialogs             (edits /etc/pam.d/polkit-1)
  --all-pam    All three of the above
  --yes        Skip confirmation (password proof still runs when stdin is a terminal)
  --uninstall  Remove Iris and restore each PAM service to its prior state
  --help       This text
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --gdm) WIRE_GDM=1 ;;
    --keyring) WIRE_KEYRING=1; WIRE_GDM=1 ;;
    --sudo) WIRE_SUDO=1 ;;
    --polkit) WIRE_POLKIT=1 ;;
    --all-pam) WIRE_GDM=1; WIRE_SUDO=1; WIRE_POLKIT=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    --uninstall) DO_UNINSTALL=1 ;;
    --help|-h) usage; exit 0 ;;
    *) die "Unknown option: $1 (try --help)" ;;
  esac
  shift
done

[[ $EUID -eq 0 ]] || die "Run me with sudo: sudo $0 $*"

# The user whose desktop this is; needed to enable the shell extension for them.
TARGET_USER="${SUDO_USER:-}"

strip_iris_pam_service() {
  local f="$1"
  [[ -e "$f" ]] || return 0
  [[ -f "$f" && ! -L "$f" ]] || die "refusing unsafe PAM path: $f"
  grep -Eq 'pam_iris(_keyring)?\.so' "$f" || return 0
  local tmp
  tmp="$(mktemp "$f.iris.remove.XXXXXX")"
  if ! cp --preserve=all "$f" "$tmp" \
      || ! sed -E '/pam_iris(_keyring)?\.so/d' "$f" > "$tmp" \
      || ! mv -f "$tmp" "$f"; then
    rm -f "$tmp"
    die "could not remove Iris hooks from $f; binaries have not been removed"
  fi
}

wire_keyring_service() {
  local f="$PAM_DIR/gdm-password"
  [[ -f "$f" && ! -L "$f" ]] || die "keyring requires a regular gdm-password PAM stack"
  local tmp backup
  tmp="$(mktemp "$f.iris.keyring.XXXXXX")"
  if ! cp --preserve=all "$f" "$tmp"; then
    rm -f "$tmp"
    die "could not preserve GDM PAM metadata"
  fi
  if ! python3 - "$f" "$tmp" <<'PY'
import re
import sys
from pathlib import Path
source, target = map(Path, sys.argv[1:])
original = source.read_text()
lines = original.splitlines(keepends=True)
auth = 'auth optional pam_iris_keyring.so\n'
session = 'session optional pam_iris_keyring.so\n'
existing = [line for line in lines if 'pam_iris_keyring.so' in line]
if existing and (existing.count(auth) != 1 or existing.count(session) != 1 or len(existing) != 2):
    raise SystemExit('unexpected or incomplete keyring hooks; refusing changes')
base = [line for line in lines if line not in (auth, session)]
def exactly_one(pattern):
    matches = [i for i, line in enumerate(base) if re.fullmatch(pattern, line.strip())]
    if len(matches) != 1:
        raise SystemExit('unsupported GDM PAM layout; refusing changes')
    return matches[0]
face = exactly_one(r'auth\s+\[success=done\s+default=ignore\]\s+pam_iris\.so(?:\s+.*)?')
password = exactly_one(r'@include\s+common-auth')
gkr_auth = exactly_one(r'auth\s+optional\s+pam_gnome_keyring\.so')
gkr_session = exactly_one(r'session\s+optional\s+pam_gnome_keyring\.so\s+auto_start')
if not face < password < gkr_auth < gkr_session:
    raise SystemExit('unsupported PAM order; refusing changes')
result = ''
for i, line in enumerate(base):
    if i == gkr_session:
        result += session
    result += line
    if i == password:
        result += auth
if existing and original != result:
    raise SystemExit('keyring hooks have unexpected placement; refusing changes')
target.write_text(result)
PY
  then
    rm -f "$tmp"
    die "GDM keyring configuration was not changed"
  fi
  if cmp -s "$tmp" "$f"; then
    rm -f "$tmp"
    ok "optional GDM keyring hooks already installed"
    return 0
  fi
  backup="$(mktemp "$BACKUP_DIR/gdm-password.keyring.$STAMP.XXXXXX")"
  if ! cp --preserve=all "$f" "$backup" || ! mv -f "$tmp" "$f"; then
    rm -f "$tmp"
    die "could not install optional keyring hooks; backup: $backup"
  fi
  ok "optional GDM keyring hooks installed; enable separately with sudo iris keyring enable"
}

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------
if [[ $DO_UNINSTALL -eq 1 ]]; then
  step "Removing Iris"

  # PAM first, and most carefully: leaving a dangling pam_iris.so reference in a
  # stack after deleting the module is exactly the broken state this machine was
  # found in with its previous face-auth package.
  for svc in gdm-password sudo polkit-1 common-auth; do
    f="/etc/pam.d/$svc"
    # A file Iris created has no "before" to restore — remove it so the service
    # falls back to /etc/pam.d/other again, which is where it started.
    if [[ -f "$f" ]] && grep -q '^# Created by Iris\.' "$f"; then
      rm -f "$f"
      ok "removed $f (Iris created it; service falls back to /etc/pam.d/other)"
      continue
    fi
    if [[ -f "$f" ]] && grep -q 'pam_iris\.so' "$f"; then
      newest="$(find "$BACKUP_DIR" -maxdepth 1 -name "$svc.*" -type f 2>/dev/null | sort | tail -1 || true)"
      if [[ -n "$newest" ]]; then
        if ! _restore_tmp="$(mktemp "$f.iris.restore.XXXXXX")"; then
          die "could not create a restore file for $f; restore $newest manually"
        fi
        if ! cp --preserve=all "$newest" "$_restore_tmp" \
            || ! mv -f "$_restore_tmp" "$f"; then
          rm -f "$_restore_tmp"
          die "could not atomically restore $f; restore $newest manually"
        fi
        ok "restored $f from $(basename "$newest")"
      else
        # No backup: strip our lines rather than leave a reference to a module
        # that is about to be deleted.
        sed -i '/pam_iris\.so/d' "$f"
        ok "stripped pam_iris lines from $f (no backup found)"
      fi
    fi
  done
  # Backups from upgrades can themselves contain Iris hooks. Remove both
  # modules after restoration, before deleting either binary.
  for svc in gdm-password sudo polkit-1 common-auth; do
    strip_iris_pam_service "/etc/pam.d/$svc"
  done
  rm -rf "$STATE_DIR/keyring"
  if [[ -f /usr/share/pam-configs/iris ]]; then
    rm -f /usr/share/pam-configs/iris
    DEBIAN_FRONTEND=noninteractive pam-auth-update --package --remove iris 2>/dev/null || true
    ok "removed pam-auth-update profile"
  fi

  # Greeter dconf: remove only what Iris added.
  rm -f /etc/dconf/db/gdm.d/10-iris
  for _n in gdm gdm-greeter Debian-gdm; do
    _p="/etc/dconf/profile/$_n"
    [[ -f "$_p" ]] || continue
    grep -q "^# Managed by Iris\." "$_p" || continue
    if head -1 "$_p" | grep -q "^# Managed by Iris\."; then
      rm -f "$_p"
      ok "removed $_p (Iris created it)"
    else
      sed -i '/^# Managed by Iris\./,+1d' "$_p"
      ok "removed Iris's system-db line from $_p"
    fi
  done
  dconf update 2>/dev/null || true

  systemctl disable --now irisd.service 2>/dev/null || true
  rm -f /etc/systemd/system/irisd.service /usr/lib/systemd/system/irisd.service
  systemctl daemon-reload 2>/dev/null || true
  ok "daemon stopped and unit removed"

  rm -f "$PAM_SECURITY_DIR/pam_iris.so" /usr/lib/security/pam_iris.so
  rm -f "$PAM_SECURITY_DIR/pam_iris_keyring.so" /usr/lib/security/pam_iris_keyring.so
  rm -f /usr/bin/iris /usr/bin/iris-settings /usr/sbin/irisd
  rm -rf "$PREFIX_LIB" "$PREFIX_SHARE" "$EXT_DIR" "$DOC_DIR"
  rm -f /usr/share/polkit-1/actions/org.iris.policy
  rm -f /usr/share/applications/iris.desktop
  rm -f /usr/share/glib-2.0/schemas/org.gnome.shell.extensions.iris.gschema.xml
  glib-compile-schemas /usr/share/glib-2.0/schemas 2>/dev/null || true
  ok "binaries, models, extension and policies removed"

  echo
  warn "Enrolled face templates in $STATE_DIR were KEPT."
  note "They are encrypted and useless without Iris. To destroy them:  sudo rm -rf $STATE_DIR"
  note "PAM backups kept in $BACKUP_DIR"
  echo
  printf '%sIris removed.%s Password authentication is untouched.\n\n' "$G" "$N"
  exit 0
fi

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
step "Preflight"

if [[ $WIRE_KEYRING -eq 1 ]]; then
  [[ -x /usr/bin/gnome-keyring-daemon ]] || die "install gnome-keyring before using --keyring"
  [[ -f "$PAM_SECURITY_DIR/pam_gnome_keyring.so" ]] || die "install libpam-gnome-keyring before using --keyring"
  [[ -c /dev/tpmrm0 ]] || die "optional keyring auto-unlock requires a TPM"
  for tool in tpm2_load tpm2_unseal tpm2_createprimary; do
    [[ -x "/usr/bin/$tool" ]] || die "install tpm2-tools before using --keyring"
  done
fi

command -v python3 >/dev/null || die "python3 not found"
PYVER="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
ok "python3 $PYVER"

python3 -c 'import cv2, numpy' 2>/dev/null \
  || die "python3-opencv / python3-numpy missing. Install: sudo apt install python3-opencv python3-numpy"
ok "opencv $(python3 -c 'import cv2;print(cv2.__version__)') + numpy $(python3 -c 'import numpy;print(numpy.__version__)')"

python3 -c 'from cryptography.hazmat.primitives.ciphers.aead import AESGCM' 2>/dev/null \
  || die "python3-cryptography missing. Install: sudo apt install python3-cryptography"
ok "cryptography available"

for m in face_detection_yunet_2023mar.onnx face_recognition_sface_2021dec.onnx; do
  [[ -s "$SRC_DIR/models/$m" ]] || die "Model missing: models/$m"
done
ok "ONNX models present"

[[ -e /dev/video0 || -e /dev/video2 ]] || warn "No /dev/video* found — enrolment will fail until a camera exists"
if [[ -e /dev/video2 ]]; then ok "camera nodes present (/dev/video2 expected to be IR)"; fi

command -v gcc >/dev/null || die "gcc missing. Install: sudo apt install build-essential"
[[ -f /usr/include/security/pam_modules.h ]] || die "PAM headers missing. Install: sudo apt install libpam0g-dev"
ok "toolchain for pam_iris.so"

if [[ $((WIRE_GDM + WIRE_SUDO + WIRE_POLKIT)) -gt 0 ]] && ! command -v pamtester >/dev/null; then
  die "pamtester is required to safely verify PAM edits. Install: sudo apt install pamtester"
fi

# ---------------------------------------------------------------------------
# GDM greeter: make the shell extension load on the login screen.
#
# The greeter reads its OWN dconf database, so the user session's
# org.gnome.shell enabled-extensions does not reach it. Two pieces are needed:
#
#   1. /etc/dconf/db/gdm.d/10-iris   — the setting itself
#   2. a "system-db:gdm" line in the gdm dconf PROFILE, or (1) is never read
#
# gdm3 ships /usr/share/dconf/profile/gdm with only user-db + file-db, no
# system-db, so on a clean machine the profile has to be created. Anything Iris
# creates here is marked so uninstall can remove exactly what it added and
# nothing else.
# ---------------------------------------------------------------------------
IRIS_DCONF_MARK='# Managed by Iris.'

setup_greeter_dconf() {
  command -v dconf >/dev/null || { warn "dconf not found — skipping greeter setup"; return 0; }

  install -d -m 0755 /etc/dconf/db/gdm.d
  cat > /etc/dconf/db/gdm.d/10-iris <<EOF
$IRIS_DCONF_MARK Load the Iris shell extension on the GDM login screen.
[org/gnome/shell]
enabled-extensions=['$EXT_UUID']
EOF
  chmod 0644 /etc/dconf/db/gdm.d/10-iris

  # WHICH PROFILE NAME?
  # dconf picks its profile from $DCONF_PROFILE, and GDM sets that to the greeter's
  # user name. That name is not stable across distributions or releases: Debian uses
  # "Debian-gdm" (hence the Debian-gdm -> gdm symlink gdm3 ships), older Ubuntu used
  # "gdm", and Ubuntu 26.04's greeter runs as "gdm-greeter". Guessing wrong means the
  # system database is silently never read and the greeter shows no dial.
  #
  # So write the profile under every name a greeter might ask for. A profile file that
  # nothing requests is inert, which makes the redundancy free.
  local body written=0
  body="$(
    if [[ -f /usr/share/dconf/profile/gdm ]]; then
      grep -vE '^\s*system-db:gdm\s*$' /usr/share/dconf/profile/gdm
    else
      printf 'user-db:user\n'
    fi
    printf 'system-db:gdm\n'
  )"

  install -d -m 0755 /etc/dconf/profile
  local name profile
  for name in gdm gdm-greeter Debian-gdm; do
    profile="/etc/dconf/profile/$name"
    if [[ -f "$profile" ]] && ! grep -q "^# Managed by Iris\." "$profile"; then
      if grep -qE '^\s*system-db:gdm\s*$' "$profile"; then
        continue                       # someone else's profile, already correct
      fi
      cp -a "$profile" "$BACKUP_DIR/dconf-profile-$name.$STAMP"
      printf '%s added system-db:gdm\n' "$IRIS_DCONF_MARK" >> "$profile"
      printf 'system-db:gdm\n' >> "$profile"
      ok "added system-db:gdm to $profile (backup kept)"
      written=$((written + 1))
      continue
    fi
    {
      printf '%s Created so the GDM greeter reads /etc/dconf/db/gdm.d.\n' "$IRIS_DCONF_MARK"
      printf '# GDM names its dconf profile after the greeter user, which varies by\n'
      printf '# release (gdm / gdm-greeter / Debian-gdm), so Iris writes all three.\n'
      printf '%s\n' "$body"
    } > "$profile"
    chmod 0644 "$profile"
    written=$((written + 1))
  done
  ok "greeter dconf profiles written ($written: gdm, gdm-greeter, Debian-gdm)"

  if dconf update 2>/dev/null; then
    ok "greeter dconf updated — extension will load on the login screen"
  else
    warn "dconf update failed — the login screen will not show the Iris dial"
  fi
}

# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------
step "Installing Iris"

install -d -m 0755 "$PREFIX_LIB" "$PREFIX_SHARE" "$PREFIX_SHARE/models" "$CONFIG_DIR" "$DOC_DIR"
install -d -m 0700 "$STATE_DIR"
install -d -m 0700 "$BACKUP_DIR"

rm -rf "${PREFIX_LIB:?}/iris"
cp -r "$SRC_DIR/src/iris" "$PREFIX_LIB/iris"
find "$PREFIX_LIB" -type d -exec chmod 0755 {} +
find "$PREFIX_LIB" -type f -exec chmod 0644 {} +
# Byte-compile so the PAM-adjacent path never pays import compile cost.
python3 -m compileall -q "$PREFIX_LIB/iris" >/dev/null 2>&1 || true
ok "python package -> $PREFIX_LIB/iris"

install -m 0644 "$SRC_DIR"/models/*.onnx "$PREFIX_SHARE/models/"
ok "models -> $PREFIX_SHARE/models"

make_wrapper() {  # $1 = path, $2 = python -m target
  cat > "$1" <<EOF
#!/bin/sh
# Generated by the Iris installer. Do not edit; reinstall instead.
PYTHONPATH="$PREFIX_LIB\${PYTHONPATH:+:\$PYTHONPATH}"
export PYTHONPATH
exec /usr/bin/python3 -m $2 "\$@"
EOF
  chmod 0755 "$1"
}
make_wrapper /usr/bin/iris          iris.cli
make_wrapper /usr/sbin/irisd        iris
make_wrapper /usr/bin/iris-settings iris.gui
ok "/usr/bin/iris, /usr/bin/iris-settings, /usr/sbin/irisd"

if [[ -f "$CONFIG_FILE" ]]; then
  cp -a "$CONFIG_FILE" "$BACKUP_DIR/config.toml.$STAMP"
  warn "kept your existing $CONFIG_FILE (backup: $BACKUP_DIR/config.toml.$STAMP)"
else
  install -m 0644 "$SRC_DIR/data/config.toml" "$CONFIG_FILE"
  ok "config -> $CONFIG_FILE"
fi

install -m 0644 "$SRC_DIR/data/org.iris.policy" /usr/share/polkit-1/actions/org.iris.policy
install -m 0644 "$SRC_DIR/data/iris.desktop"    /usr/share/applications/iris.desktop
install -m 0644 "$SRC_DIR/README.md"            "$DOC_DIR/README.md"
if [[ -f "$SRC_DIR/docs/SECURITY.md" ]]; then
  install -m 0644 "$SRC_DIR/docs/SECURITY.md" "$DOC_DIR/SECURITY.md"
fi
ok "polkit policy, desktop entry, docs"

# GNOME Shell extension (optional — absent if you did not build it)
if [[ -d "$SRC_DIR/extension/$EXT_UUID" ]]; then
  rm -rf "$EXT_DIR"; install -d -m 0755 "$EXT_DIR"
  cp -r "$SRC_DIR/extension/$EXT_UUID/." "$EXT_DIR/"
  find "$EXT_DIR" -type f -exec chmod 0644 {} +
  if [[ -d "$EXT_DIR/schemas" ]]; then
    glib-compile-schemas "$EXT_DIR/schemas" 2>/dev/null \
      && ok "extension -> $EXT_DIR (schemas compiled)" \
      || warn "extension installed but schema compile failed"
  else
    ok "extension -> $EXT_DIR"
  fi
  setup_greeter_dconf
else
  warn "no extension/$EXT_UUID in the source tree — skipping shell extension"
fi


# ---------------------------------------------------------------------------
# PAM module
# ---------------------------------------------------------------------------
step "Building the PAM module"
make -C "$SRC_DIR/pam" clean >/dev/null 2>&1 || true
if ! make -C "$SRC_DIR/pam" >/dev/null; then
  die "pam_iris.so failed to build — refusing to continue"
fi
[[ -f "$SRC_DIR/pam/pam_iris.so" ]] || die "pam_iris.so missing after build"

# A PAM module with unresolved symbols loads and then fails at runtime inside the
# auth stack, which is the worst possible place to discover it.
if command -v ldd >/dev/null; then
  if ldd -r "$SRC_DIR/pam/pam_iris.so" 2>&1 | grep -q 'undefined symbol'; then
    die "pam_iris.so has undefined symbols — refusing to install it"
  fi
fi
install -d -m 0755 "$PAM_SECURITY_DIR"
install -m 0644 "$SRC_DIR/pam/pam_iris.so" "$PAM_SECURITY_DIR/pam_iris.so"
if ldd -r "$SRC_DIR/pam/pam_iris_keyring.so" 2>&1 | grep -q 'undefined symbol'; then
  die "pam_iris_keyring.so has undefined symbols — refusing to install it"
fi
install -m 0644 "$SRC_DIR/pam/pam_iris_keyring.so" "$PAM_SECURITY_DIR/pam_iris_keyring.so"
ok "PAM modules -> $PAM_SECURITY_DIR (no undefined symbols)"

# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------
step "Starting the daemon"
install -m 0644 "$SRC_DIR/data/irisd.service" /usr/lib/systemd/system/irisd.service
systemctl daemon-reload
systemctl enable irisd.service >/dev/null 2>&1
systemctl restart irisd.service

for _ in $(seq 1 40); do
  if iris status >/dev/null 2>&1 || [[ -S /run/irisd/socket ]]; then break; fi
  sleep 0.25
done
if systemctl is-active --quiet irisd.service; then
  ok "irisd running ($(systemctl show -p ActiveState --value irisd.service))"
else
  journalctl -u irisd.service -n 20 --no-pager || true
  die "irisd failed to start — see the log above. Nothing in PAM was touched."
fi

# ---------------------------------------------------------------------------
# PAM wiring — opt-in, backed up, and proven before it is trusted
# ---------------------------------------------------------------------------
IRIS_PAM_LINE='auth	[success=done default=ignore]	pam_iris.so'
# Keep the path separate so isolated tests can exercise wiring without /etc or root.
PAM_DIR=/etc/pam.d

# Prove the module loads and cannot hang, using a throwaway service that can
# never deny anything: iris either succeeds or is ignored, then pam_permit ends it.
selftest_module() {
  local svc=/etc/pam.d/iris-selftest start elapsed
  cat > "$svc" <<EOF
auth	[success=done default=ignore]	pam_iris.so timeout=3 quiet
auth	required			pam_permit.so
account	required			pam_permit.so
EOF
  start=$(date +%s)
  timeout 20 pamtester iris-selftest "${TARGET_USER:-root}" authenticate </dev/null >/dev/null 2>&1
  local rc=$?
  elapsed=$(( $(date +%s) - start ))
  rm -f "$svc"
  if [[ $rc -ne 0 ]]; then
    die "pam_iris.so did not load cleanly (pamtester rc=$rc). PAM left untouched."
  fi
  if [[ $elapsed -gt 12 ]]; then
    die "pam_iris.so took ${elapsed}s to return — it could hang a login. PAM left untouched."
  fi
  ok "module loads, returns in ${elapsed}s, cannot hang"
}

# Edit one service, then require the user to prove password auth still works
# through that exact service. Failure rolls the file back automatically.
wire_service() {
  # NOTE: separate `local` statements on purpose. Bash expands every word on a
  # `local` line before assigning any of them, so `f="$PAM_DIR/$svc"` on the
  # same line would read $svc while it is still unset and abort under `set -u`.
  local svc="$1"
  local desc="$2"
  local f="$PAM_DIR/$svc"
  local backup=''
  local original
  local permission_source
  # Some services (polkit-1 on Ubuntu) have no file of their own and fall back to
  # /etc/pam.d/other. Wiring them means creating the file: `other` verbatim, with
  # our line on top. Functionally identical fallback, plus face auth.
  local created=0
  if [[ ! -f "$f" ]]; then
    [[ -f "$PAM_DIR/other" ]] || {
      warn "$f does not exist and $PAM_DIR/other is missing — skipping $desc"
      return 0
    }
    local fallback_iris_rule_count
    fallback_iris_rule_count="$(grep -Ev '^[[:space:]]*(#|$)' "$PAM_DIR/other" | grep -Ec 'pam_iris\.so([[:space:]]|$)' || true)"
    if [[ $fallback_iris_rule_count -gt 0 ]]; then
      die "$svc: fallback already references pam_iris.so; refusing to copy or compound an unknown PAM configuration. $f service remains absent."
    fi
    created=1
    original="$PAM_DIR/other"
    permission_source="$original"
  else
    local iris_rule_count safe_rule_count
    iris_rule_count="$(grep -Ev '^[[:space:]]*(#|$)' "$f" | grep -Ec 'pam_iris\.so([[:space:]]|$)' || true)"
    if [[ $iris_rule_count -gt 0 ]]; then
      safe_rule_count="$(grep -Ec '^[[:space:]]*auth[[:space:]]+\[success=done[[:space:]]+default=ignore\][[:space:]]+pam_iris\.so([[:space:]]|$)' "$f" || true)"
      if [[ $iris_rule_count -eq 1 && $safe_rule_count -eq 1 ]]; then
        ok "$svc already wired with fail-through control"
        return 0
      fi
      die "$svc has an unexpected or duplicate rule referencing pam_iris.so (including an unexpected control field); refusing to modify it. Review $f and use exactly: $IRIS_PAM_LINE"
    fi

    backup="$BACKUP_DIR/$svc.$STAMP"
    cp -a "$f" "$backup"
    original="$backup"
    permission_source="$f"
  fi

  # Insert as the FIRST auth line so face is tried before password, written to a
  # temp file and moved into place so an interrupt can never leave a half-written
  # PAM stack on disk.
  local tmp; tmp="$(mktemp "$f.iris.XXXXXX")"
  if [[ $created -eq 1 ]]; then
    if ! {
      printf '# Created by Iris. This service previously fell back to /etc/pam.d/other;\n'
      printf '# the body below is that file verbatim. Delete this file to fully revert.\n'
      printf '%s\n' "$IRIS_PAM_LINE"
      cat "$original"
    } > "$tmp"; then
      rm -f "$tmp"
      die "$svc: could not build the PAM stack; service remains absent."
    fi
  else
    if ! {
      printf '# Added by Iris. Remove this line to disable face auth for %s.\n' "$svc"
      printf '%s\n' "$IRIS_PAM_LINE"
      cat "$f"
    } > "$tmp"; then
      rm -f "$tmp"
      die "$svc: could not build the PAM stack; original file is untouched."
    fi
  fi
  if ! cp --attributes-only --preserve=all "$permission_source" "$tmp"; then
    rm -f "$tmp"
    die "$svc: could not preserve PAM file metadata; original state is untouched."
  fi
  if ! mv -f "$tmp" "$f"; then
    rm -f "$tmp"
    die "$svc: could not atomically install the PAM stack; original state is untouched."
  fi

  if [[ $created -eq 1 ]]; then
    ok "$desc: created $f from /etc/pam.d/other + face auth"
    note "uninstall removes this file entirely"
  fi

  rollback_service() {
    if [[ $created -eq 1 ]]; then
      rm -f "$f"
    else
      local rollback_tmp
      if ! rollback_tmp="$(mktemp "$f.iris.rollback.XXXXXX")"; then
        die "$svc: could not create a rollback file; restore $backup manually from the open root shell."
      fi
      if ! cp --preserve=all "$backup" "$rollback_tmp"; then
        rm -f "$rollback_tmp"
        die "$svc: automatic rollback preparation failed; restore $backup manually from the open root shell."
      fi
      if ! mv -f "$rollback_tmp" "$f"; then
        rm -f "$rollback_tmp"
        die "$svc: automatic rollback failed; restore $backup manually from the open root shell."
      fi
    fi
  }

  # The proof must exercise the PASSWORD path specifically. Once a face is
  # enrolled, pam_iris would succeed on its own and the test would pass without
  # ever proving the fallback — which is the only thing that matters here. So
  # face auth is switched off for the duration, and restored no matter how we
  # leave this function.
  local restore_face=0
  if iris config get auth.enabled 2>/dev/null | grep -qi true; then
    if iris config set auth.enabled false >/dev/null 2>&1; then
      restore_face=1
      note "face auth temporarily disabled so this tests the password path itself"
    fi
  fi
  restore_face_auth() {
    if [[ $restore_face -eq 1 ]]; then
      iris config set auth.enabled true >/dev/null 2>&1 || \
        warn "could not re-enable face auth — run: sudo iris config set auth.enabled true"
      restore_face=0
    fi
  }
  trap restore_face_auth RETURN

  # STRUCTURAL PROOF (always runs, needs no password).
  # Our line is prepended with [success=done default=ignore] and nothing else is
  # touched, so the password path is the original stack, unmodified. Verify that
  # literally: every original line must still be present, in order.
  if ! diff <(grep -vE '^\s*#|^\s*$' "$original") \
            <(grep -vE '^\s*#|^\s*$' "$f" | grep -v 'pam_iris\.so') >/dev/null; then
    rollback_service
    restore_face_auth
    if [[ $created -eq 1 ]]; then
      die "$svc: the original stack did not survive the edit — rolled back by deleting $f. Nothing is broken."
    fi
    die "$svc: the original stack did not survive the edit — rolled back. Nothing is broken."
  fi
  if ! grep -q '\[success=done default=ignore\][[:space:]]*pam_iris\.so' "$f"; then
    rollback_service
    restore_face_auth
    if [[ $created -eq 1 ]]; then
      die "$svc: iris line has the wrong control field — rolled back by deleting $f. Nothing is broken."
    fi
    die "$svc: iris line has the wrong control field — rolled back. Nothing is broken."
  fi
  ok "$desc: original stack intact, iris line is fail-through"

  # INTERACTIVE PROOF. Strictly stronger, but needs a human at a terminal.
  if [[ ! -t 0 ]]; then
    restore_face_auth
    warn "$desc wired, but the interactive password proof was SKIPPED (no terminal)"
    note "confirm it yourself now:  sudo pamtester $svc ${TARGET_USER:-root} authenticate"
    if [[ $created -eq 1 ]]; then
      note "rollback if needed:       sudo rm -f $f"
    else
      note "rollback if needed:       sudo cp -a $backup $f"
    fi
    return 0
  fi

  printf '\n  %sPROOF REQUIRED%s for %s — type your password to confirm the fallback still works.\n' \
         "$BOLD$Y" "$N" "$desc"
  note "If you cannot, or it fails, Iris rolls this change back automatically."
  if timeout 120 pamtester "$svc" "${TARGET_USER:-root}" authenticate; then
    restore_face_auth
    ok "$desc wired, password fallback verified"
    if [[ $created -eq 1 ]]; then
      note "rollback:  sudo rm -f $f"
    else
      note "rollback:  sudo cp -a $backup $f"
    fi
  else
    rollback_service
    restore_face_auth
    if [[ $created -eq 1 ]]; then
      die "Password proof FAILED for $svc — rolled back by deleting $f. Nothing is broken."
    fi
    die "Password proof FAILED for $svc — rolled back to $backup. Nothing is broken."
  fi
}

if [[ $((WIRE_GDM + WIRE_SUDO + WIRE_POLKIT)) -gt 0 ]]; then
  step "Wiring PAM"
  warn "This edits authentication. Backups go to $BACKUP_DIR"
  note "Keep a second terminal open with a root shell until you have tested logging in."
  if [[ $ASSUME_YES -eq 0 ]]; then
    read -r -p "  Continue? [y/N] " reply </dev/tty || reply=n
    [[ "$reply" =~ ^[Yy]$ ]] || die "Aborted by user. Nothing in PAM was changed."
  fi
  selftest_module
  if [[ $WIRE_GDM    -eq 1 ]]; then wire_service gdm-password "GDM login + lock screen"; fi
  if [[ $WIRE_KEYRING -eq 1 ]]; then wire_keyring_service; fi
  if [[ $WIRE_SUDO   -eq 1 ]]; then wire_service sudo         "terminal sudo"; fi
  if [[ $WIRE_POLKIT -eq 1 ]]; then wire_service polkit-1     "polkit dialogs"; fi
else
  step "PAM"
  note "Not wired — this run installed the software only. That is the default."
  note "Enrol a face first, test it, then re-run with --gdm (and optionally --sudo --polkit)."
fi

# ---------------------------------------------------------------------------
step "Done"
cat <<EOF

  ${BOLD}Next steps${N}

    1. Enrol your face:        ${B}sudo iris enroll${N}
    2. Check it recognises you:${B} iris test${N}
    3. Full health check:      ${B}iris doctor${N}
    4. Settings app:           ${B}iris-settings${N}
EOF
if [[ -d "$EXT_DIR" ]] && [[ -n "$TARGET_USER" ]]; then
cat <<EOF
    5. Enable the shell UI:    ${B}gnome-extensions enable $EXT_UUID${N}
       (log out and back in first — GNOME only scans for new extensions at start)
EOF
fi
if [[ $((WIRE_GDM + WIRE_SUDO + WIRE_POLKIT)) -eq 0 ]]; then
cat <<EOF

  ${BOLD}Face auth is installed but not yet active anywhere.${N}
  Once enrolment works, turn it on:   ${B}sudo $0 --gdm${N}
EOF
fi
cat <<EOF

  ${DIM}Recovery, if face auth ever misbehaves: it always falls through to your
  password. To remove it entirely:  sudo $0 --uninstall
  PAM backups live in $BACKUP_DIR${N}

EOF
