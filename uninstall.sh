#!/usr/bin/env bash
# Thin wrapper: full removal lives in install.sh so backup/restore logic has one home.
set -euo pipefail
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/install.sh" --uninstall "$@"
