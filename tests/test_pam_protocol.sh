#!/usr/bin/env bash
# Runtime contract of pam_iris.c against a scripted fake irisd (see the .c).

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

temporary_dir="$(mktemp -d "${TMPDIR:-/tmp}/iris-pam-proto.XXXXXX")"
trap 'rm -rf -- "$temporary_dir"' EXIT HUP INT TERM

# AddressSanitizer and UndefinedBehaviorSanitizer turn any out-of-bounds read
# or write in the reply parser, or other undefined behaviour, into a failure.
"$compiler" -Wall -Wextra -Werror -O1 -g \
    -fsanitize=address,undefined,float-cast-overflow -fno-sanitize-recover=all -fno-omit-frame-pointer \
    -DIRIS_PAM_TEST_SOCKET="\"$temporary_dir/socket\"" \
    -o "$temporary_dir/pam-protocol" "$repo_dir/tests/test_pam_protocol.c" -lpam

timeout 120 "$temporary_dir/pam-protocol"
