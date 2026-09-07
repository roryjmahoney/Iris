#!/usr/bin/env bash

set -euo pipefail

repo_dir="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)"

umask 077
export LC_ALL=C
export TZ=UTC
export NO_COLOR=1
export PYTHONDONTWRITEBYTECODE=1
if [[ -n "${PYTHONPATH:-}" ]]; then
    export PYTHONPATH="$repo_dir/src:$PYTHONPATH"
else
    export PYTHONPATH="$repo_dir/src"
fi

cd "$repo_dir"

printf '%s\n' 'Python integration and contract tests'
python3 -m unittest discover -v -s tests -p 'test_*.py'

for shell_test in tests/test_*.sh; do
    printf 'Shell contract test: %s\n' "$shell_test"
    bash "$shell_test"
done

printf '%s\n' 'All Iris tests passed.'
