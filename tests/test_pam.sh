#!/usr/bin/env bash

set -euo pipefail

repo_dir="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)"
compiler="$(command -v "${CC:-cc}" || true)"

if [[ -z "$compiler" ]]; then
    printf '%s\n' 'ERROR: C compiler unavailable for PAM checks' >&2
    exit 1
fi
if [[ ! -f /usr/include/security/pam_modules.h ]]; then
    printf '%s\n' 'ERROR: PAM development headers unavailable' >&2
    exit 1
fi
if ! command -v ldd >/dev/null 2>&1; then
    printf '%s\n' 'ERROR: ldd unavailable for PAM relocation check' >&2
    exit 1
fi

temporary_dir="$(mktemp -d "${TMPDIR:-/tmp}/iris-pam-test.XXXXXX")"
trap 'rm -rf -- "$temporary_dir"' EXIT HUP INT TERM

module="$temporary_dir/pam_iris.so"
harness="$temporary_dir/pam-contract"
config_dir="$temporary_dir/pam.d"
mkdir "$config_dir"

"$compiler" \
    -fsyntax-only -Werror -Wall -Wextra -Wshadow -Wconversion \
    -Wsign-conversion -Wcast-qual -Wformat=2 -Wstrict-prototypes \
    -Wmissing-prototypes -Wvla \
    "$repo_dir/pam/pam_iris.c"
printf '%s\n' 'PASS: pam_iris.c strict compile check'

"$compiler" \
    -Wall -Wextra -Werror -O2 -fPIC -shared -D_FORTIFY_SOURCE=2 \
    -fstack-protector-strong -Wl,--no-undefined -Wl,-z,relro -Wl,-z,now \
    -o "$module" "$repo_dir/pam/pam_iris.c" -lpam

relocations="$(LD_BIND_NOW=1 ldd -r "$module" 2>&1)" || {
    printf '%s\n' "$relocations" >&2
    exit 1
}
if [[ "$relocations" == *"undefined symbol"* ]]; then
    printf '%s\n' "$relocations" >&2
    exit 1
fi
printf '%s\n' 'PASS: temporary pam_iris.so links with no unresolved symbols'

"$compiler" -Wall -Wextra -Werror -Wpedantic \
    -o "$harness" "$repo_dir/tests/test_pam_harness.c" -lpam

permit_module=""
for candidate in \
    /usr/lib/*/security/pam_permit.so \
    /lib/*/security/pam_permit.so \
    /usr/lib/security/pam_permit.so \
    /lib/security/pam_permit.so; do
    if [[ -f "$candidate" ]]; then
        permit_module="$candidate"
        break
    fi
done
if [[ -z "$permit_module" ]]; then
    printf '%s\n' 'SKIP: pam_permit.so unavailable; fail-through runtime check skipped'
    exit 0
fi
if ! command -v timeout >/dev/null 2>&1; then
    printf '%s\n' 'SKIP: timeout unavailable; fail-through runtime check skipped'
    exit 0
fi

printf 'auth [success=done default=ignore] %s quiet timeout=0.5\n' "$module" \
    > "$config_dir/fail-through"
printf 'auth required %s\n' "$permit_module" >> "$config_dir/fail-through"

printf 'auth required %s quiet timeout=0.5\n' "$module" \
    > "$config_dir/blocking"
printf 'auth required %s\n' "$permit_module" >> "$config_dir/blocking"

timeout 5 "$harness" fail-through "$config_dir" success
timeout 5 "$harness" blocking "$config_dir" auth-error
printf '%s\n' 'PASS: PAM default=ignore fails through; required control blocks'
