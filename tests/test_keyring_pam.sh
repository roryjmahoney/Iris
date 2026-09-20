#!/usr/bin/env bash
set -euo pipefail
repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
[[ -f "$repo/pam/pam_iris_keyring.c" ]] || { echo 'FAIL: keyring session adapter missing'; exit 1; }
cp "$repo/tests/keyring_fake_helper.py" "$tmp/helper.py"
${CC:-cc} -Wall -Wextra -Werror -O2 -DIRIS_KEYRING_TEST -DIRIS_KEYRING_HELPER=\"$tmp/helper.py\" -o "$tmp/test" "$repo/tests/test_keyring_native.c" -lpam
"$tmp/test"
${CC:-cc} -Wall -Wextra -Werror -O2 -DIRIS_PAM_TEST_SOCKET=\"$tmp/socket\" -o "$tmp/marker" "$repo/tests/test_keyring_marker.c" -lpam
"$tmp/marker"
