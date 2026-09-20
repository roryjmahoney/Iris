#!/usr/bin/env bash
set -euo pipefail
repo_dir="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)"
for tool in dbus-run-session gdbus gnome-keyring-daemon cc python3; do
    command -v "$tool" >/dev/null || { echo "ERROR: $tool required for GNOME keyring integration test" >&2; exit 1; }
done
[[ -f /usr/lib/x86_64-linux-gnu/security/pam_gnome_keyring.so ]] || {
    echo 'ERROR: libpam-gnome-keyring required for GNOME keyring integration test' >&2; exit 1;
}
test_root="$(mktemp -d /tmp/iris-gnome-keyring.XXXXXX)"
trap 'rm -rf -- "$test_root"' EXIT
mkdir -m 700 "$test_root/home" "$test_root/runtime" "$test_root/data" "$test_root/config" "$test_root/control"
# Isolate the bus activation environment BEFORE starting the D-Bus daemon.
# Do not let any service inherit the desktop's home or keyring socket.
env -u DBUS_STARTER_ADDRESS -u DBUS_STARTER_BUS_TYPE \
    IRIS_TEST_ROOT="$test_root" HOME="$test_root/home" \
    XDG_RUNTIME_DIR="$test_root/runtime" XDG_DATA_HOME="$test_root/data" \
    XDG_CONFIG_HOME="$test_root/config" GNOME_KEYRING_CONTROL="$test_root/control" \
    timeout 30 dbus-run-session -- python3 "$repo_dir/tests/keyring_gnome_integration.py"
