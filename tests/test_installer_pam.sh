#!/usr/bin/env bash
set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALLER="$TEST_DIR/../install.sh"
ORIGINAL_PATH="$PATH"
FAILURES=0

fail() {
  printf '    %s\n' "$1" >&2
  return 1
}

assert_contains() {
  local haystack="$1"
  local needle="$2"
  [[ "$haystack" == *"$needle"* ]] || fail "expected output to contain: $needle"
}

assert_no_service_artifacts() {
  local service="$1"
  [[ ! -e "$PAM_DIR/$service" ]] || fail "$PAM_DIR/$service should be absent"
  local leftover
  leftover="$(find "$PAM_DIR" -maxdepth 1 -name "$service.iris.*" -print -quit)"
  [[ -z "$leftover" ]] || fail "temporary PAM file was left behind: $leftover"
}

load_wire_service() {
  local extracted="$CASE_ROOT/wire_service.sh"

  # Exercise the real installer function while redirecting its hard-coded PAM
  # path on revisions that predate the explicit PAM_DIR test seam.
  sed -n '/^wire_service()[[:space:]]*{/,/^}/p' "$INSTALLER" > "$extracted"
  if ! grep -q 'local f="$PAM_DIR/$svc"' "$extracted"; then
    sed -i 's|/etc/pam.d|${PAM_DIR}|g' "$extracted"
  fi
  # shellcheck disable=SC1090
  source "$extracted"
  declare -F wire_service >/dev/null || fail "could not load wire_service from install.sh"
}

setup_case() {
  CASE_ROOT="$(mktemp -d)"
  trap 'rm -rf "$CASE_ROOT"' EXIT
  PAM_DIR="$CASE_ROOT/pam.d"
  BACKUP_DIR="$CASE_ROOT/backups"
  BIN_DIR="$CASE_ROOT/bin"
  MV_LOG="$CASE_ROOT/mv.log"
  MV_MUTATED="$CASE_ROOT/mv-mutated"
  PAMTESTER_LOG="$CASE_ROOT/pamtester.log"
  STAMP=20260902-000000
  TARGET_USER=test-user
  IRIS_PAM_LINE=$'auth\t[success=done default=ignore]\tpam_iris.so'
  BOLD=''; Y=''; N=''
  MV_MUTATION=''
  PAMTESTER_RC=0

  mkdir -p "$PAM_DIR" "$BACKUP_DIR" "$BIN_DIR"
  REAL_MV="$(command -v mv)"
  REAL_SED="$(command -v sed)"
  SCRIPT_BIN="$(command -v script)" \
    || { fail "script(1) is required for PTY-backed PAM tests"; return 1; }

  cat > "$BIN_DIR/mv" <<'EOF'
#!/usr/bin/env bash
set -eu
src=''; dst=''
for arg in "$@"; do
  case "$arg" in
    -*) ;;
    *)
      if [[ -z "$src" ]]; then src="$arg"; else dst="$arg"; fi
      ;;
  esac
done
"$REAL_MV" "$@"
printf '%s\n%s\n' "$src" "$dst" >> "$MV_LOG"
if [[ ! -e "$MV_MUTATED" ]]; then
  case "${MV_MUTATION:-}" in
    drop-fallback) "$REAL_SED" -i '/pam_unix\.so/d' "$dst"; : > "$MV_MUTATED" ;;
    corrupt-control) "$REAL_SED" -i 's/success=done/success=broken/' "$dst"; : > "$MV_MUTATED" ;;
  esac
fi
EOF

  cat > "$BIN_DIR/iris" <<'EOF'
#!/usr/bin/env bash
if [[ "${*:-}" == 'config get auth.enabled' ]]; then
  printf 'false\n'
fi
exit 0
EOF

  cat > "$BIN_DIR/pamtester" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$PAMTESTER_LOG"
exit "$PAMTESTER_RC"
EOF

  chmod +x "$BIN_DIR/mv" "$BIN_DIR/iris" "$BIN_DIR/pamtester"
  PATH="$BIN_DIR:$ORIGINAL_PATH"
  export PATH PAM_DIR BACKUP_DIR STAMP TARGET_USER IRIS_PAM_LINE BOLD Y N
  export MV_LOG MV_MUTATED PAMTESTER_LOG MV_MUTATION PAMTESTER_RC REAL_MV REAL_SED

  ok()   { printf 'OK %s\n' "$1"; }
  warn() { printf 'WARN %s\n' "$1"; }
  note() { printf 'NOTE %s\n' "$1"; }
  die()  { printf 'DIE %s\n' "$1" >&2; exit 1; }

  load_wire_service
}

write_fallback() {
  printf '%s\n' \
    '# fallback comment must survive verbatim' \
    $'auth\trequired\tpam_unix.so nullok' \
    '' \
    $'account\trequired\tpam_unix.so' > "$PAM_DIR/other"
  chmod 0644 "$PAM_DIR/other"
}

run_wire_with_tty() {
  local service="$1"
  local description="$2"
  local runner="$CASE_ROOT/run-wire-with-tty.sh"
  local command_line

  TEST_SERVICE="$service"
  TEST_DESCRIPTION="$description"
  export TEST_SERVICE TEST_DESCRIPTION
  export -f wire_service ok warn note die

  printf '%s\n' \
    '#!/usr/bin/env bash' \
    'set -euo pipefail' \
    '[[ -t 0 ]] || { printf "PTY missing\n" >&2; exit 99; }' \
    'wire_service "$TEST_SERVICE" "$TEST_DESCRIPTION"' > "$runner"
  chmod +x "$runner"
  printf -v command_line 'bash %q' "$runner"
  "$SCRIPT_BIN" -q -e -c "$command_line" /dev/null </dev/null
}

test_created_service_is_atomic_and_preserves_fallback() {
  setup_case || return 1
  write_fallback

  local output target source destination fallback_copy
  target="$PAM_DIR/missing-service"
  fallback_copy="$CASE_ROOT/fallback.expected"
  cp "$PAM_DIR/other" "$fallback_copy"
  output="$(wire_service missing-service 'missing service' </dev/null 2>&1)" \
    || fail "creation failed unexpectedly: $output" || return 1

  [[ -f "$MV_LOG" ]] || fail "created service was not installed with an atomic rename" || return 1
  mapfile -t moved < "$MV_LOG"
  [[ ${#moved[@]} -eq 2 ]] || fail "expected one atomic rename, saw ${#moved[@]} logged arguments" || return 1
  source="${moved[0]}"; destination="${moved[1]}"
  [[ "$(dirname "$source")" == "$PAM_DIR" ]] \
    || fail "atomic temporary file was not in the PAM directory: $source" || return 1
  [[ "$(basename "$source")" == missing-service.iris.* ]] \
    || fail "unexpected atomic temporary filename: $source" || return 1
  [[ "$destination" == "$target" ]] \
    || fail "atomic rename targeted $destination instead of $target" || return 1
  [[ -f "$target" ]] || fail "created service file is absent" || return 1
  [[ "$(sed -n '3p' "$target")" == $'auth\t[success=done default=ignore]\tpam_iris.so' ]] \
    || fail "created service lacks the validated fail-through Iris control field" || return 1
  tail -n +4 "$target" | cmp -s - "$fallback_copy" \
    || fail "pam.d/other was not preserved verbatim after the Iris header" || return 1
  [[ -z "$(find "$PAM_DIR" -maxdepth 1 -name 'missing-service.iris.*' -print -quit)" ]] \
    || fail "atomic temporary PAM file remains after success" || return 1
}

test_created_service_reports_noninteractive_proof_skip() {
  setup_case || return 1
  write_fallback

  local output
  output="$(wire_service missing-service 'missing service' </dev/null 2>&1)" \
    || fail "noninteractive creation failed unexpectedly: $output" || return 1

  assert_contains "$output" 'original stack intact, iris line is fail-through' || return 1
  assert_contains "$output" 'interactive password proof was SKIPPED (no terminal)' || return 1
  assert_contains "$output" "rollback if needed:       sudo rm -f $PAM_DIR/missing-service" || return 1
  [[ ! -e "$PAMTESTER_LOG" ]] \
    || fail "pamtester must not run without a terminal" || return 1
}

test_created_service_rolls_back_to_absent_on_password_proof_failure() {
  setup_case || return 1
  write_fallback
  PAMTESTER_RC=1
  export PAMTESTER_RC

  local output rc
  output="$(run_wire_with_tty missing-service 'missing service' 2>&1)"
  rc=$?

  [[ $rc -ne 0 ]] || fail "forced password-proof failure returned success" || return 1
  [[ -f "$PAMTESTER_LOG" ]] || fail "interactive password proof did not run" || return 1
  assert_contains "$(<"$PAMTESTER_LOG")" 'missing-service test-user authenticate' || return 1
  assert_no_service_artifacts missing-service || return 1
  assert_contains "$output" 'Password proof FAILED for missing-service' || return 1
  assert_contains "$output" 'rolled back' || return 1
}

test_created_service_rolls_back_to_absent_on_structural_proof_failure() {
  setup_case || return 1
  write_fallback
  MV_MUTATION=drop-fallback
  export MV_MUTATION

  local output rc
  output="$(wire_service missing-service 'missing service' </dev/null 2>&1)"
  rc=$?

  [[ $rc -ne 0 ]] || fail "corrupted fallback stack passed structural proof" || return 1
  assert_no_service_artifacts missing-service || return 1
  assert_contains "$output" 'original stack did not survive the edit' || return 1
  assert_contains "$output" 'rolled back' || return 1
}

test_created_service_rolls_back_to_absent_on_control_proof_failure() {
  setup_case || return 1
  write_fallback
  MV_MUTATION=corrupt-control
  export MV_MUTATION

  local output rc
  output="$(wire_service missing-service 'missing service' </dev/null 2>&1)"
  rc=$?

  [[ $rc -ne 0 ]] || fail "invalid Iris control field passed proof" || return 1
  assert_no_service_artifacts missing-service || return 1
  assert_contains "$output" 'iris line has the wrong control field' || return 1
  assert_contains "$output" 'rolled back' || return 1
}

test_missing_service_refuses_iris_rule_in_fallback_before_editing() {
  setup_case || return 1
  write_fallback
  printf '%s\n' $'auth\trequired\tpam_iris.so' >> "$PAM_DIR/other"

  local output rc
  output="$(wire_service missing-service 'missing service' </dev/null 2>&1)"
  rc=$?

  [[ $rc -ne 0 ]] || fail "Iris-bearing fallback was accepted" || return 1
  assert_no_service_artifacts missing-service || return 1
  [[ ! -e "$MV_LOG" ]] \
    || fail "service was edited before the unsafe fallback was refused" || return 1
  assert_contains "$output" 'fallback already references pam_iris.so' || return 1
  assert_contains "$output" 'service remains absent' || return 1
}

test_existing_service_password_failure_still_restores_original() {
  setup_case || return 1
  write_fallback
  printf '%s\n' \
    '# existing service' \
    $'auth\trequired\tpam_unix.so' \
    $'account\trequired\tpam_unix.so' > "$PAM_DIR/existing-service"
  chmod 0640 "$PAM_DIR/existing-service"
  local has_xattr=0
  if python3 - "$PAM_DIR/existing-service" <<'PY'
import os
import sys

try:
    os.setxattr(sys.argv[1], b"user.iris-rollback", b"preserved")
except OSError:
    raise SystemExit(1)
PY
  then
    has_xattr=1
  fi
  cp "$PAM_DIR/existing-service" "$CASE_ROOT/existing.expected"
  PAMTESTER_RC=1
  export PAMTESTER_RC

  local output rc
  output="$(run_wire_with_tty existing-service 'existing service' 2>&1)"
  rc=$?

  [[ $rc -ne 0 ]] || fail "existing service accepted a forced password-proof failure" || return 1
  cmp -s "$PAM_DIR/existing-service" "$CASE_ROOT/existing.expected" \
    || fail "existing service was not restored byte-for-byte" || return 1
  [[ "$(stat -c '%a' "$PAM_DIR/existing-service")" == 640 ]] \
    || fail "existing service mode 0640 was not restored" || return 1
  [[ -f "$BACKUP_DIR/existing-service.$STAMP" ]] \
    || fail "existing service backup is missing" || return 1
  [[ "$(stat -c '%a' "$BACKUP_DIR/existing-service.$STAMP")" == 640 ]] \
    || fail "backup did not retain the original mode 0640" || return 1
  if [[ $has_xattr -eq 1 ]]; then
    local restored_xattr backup_xattr
    restored_xattr="$(python3 -c 'import os,sys; print(os.getxattr(sys.argv[1], b"user.iris-rollback").decode())' "$PAM_DIR/existing-service")" \
      || fail "rollback lost the original extended attribute" || return 1
    backup_xattr="$(python3 -c 'import os,sys; print(os.getxattr(sys.argv[1], b"user.iris-rollback").decode())' "$BACKUP_DIR/existing-service.$STAMP")" \
      || fail "backup lost the original extended attribute" || return 1
    [[ "$restored_xattr" == preserved && "$backup_xattr" == preserved ]] \
      || fail "extended attribute changed during backup or rollback" || return 1
  fi
  mapfile -t moved < "$MV_LOG"
  [[ ${#moved[@]} -eq 4 ]] \
    || fail "rollback was not installed with a second atomic rename" || return 1
  [[ "$(basename "${moved[2]}")" == existing-service.iris.rollback.* ]] \
    || fail "unexpected rollback temporary filename: ${moved[2]}" || return 1
  [[ "${moved[3]}" == "$PAM_DIR/existing-service" ]] \
    || fail "atomic rollback targeted ${moved[3]}" || return 1
  [[ -z "$(find "$PAM_DIR" -maxdepth 1 -name 'existing-service.iris.*' -print -quit)" ]] \
    || fail "rollback left a temporary PAM file behind" || return 1
  assert_contains "$output" "rolled back to $BACKUP_DIR/existing-service.$STAMP" || return 1
}

test_existing_service_structural_failure_keeps_prior_rollback_behavior() {
  setup_case || return 1
  write_fallback
  printf '%s\n' \
    '# existing service' \
    $'auth\trequired\tpam_unix.so' \
    $'account\trequired\tpam_unix.so' > "$PAM_DIR/existing-service"
  chmod 0644 "$PAM_DIR/existing-service"
  cp "$PAM_DIR/existing-service" "$CASE_ROOT/existing.expected"
  MV_MUTATION=drop-fallback
  export MV_MUTATION

  local output rc
  output="$(wire_service existing-service 'existing service' </dev/null 2>&1)"
  rc=$?

  [[ $rc -ne 0 ]] || fail "existing service corruption passed structural proof" || return 1
  cmp -s "$PAM_DIR/existing-service" "$CASE_ROOT/existing.expected" \
    || fail "existing service stack was not restored byte-for-byte" || return 1
  [[ "$(stat -c '%a' "$PAM_DIR/existing-service")" == 644 ]] \
    || fail "existing structural rollback no longer restores mode 0644" || return 1
  [[ ! -e "$PAMTESTER_LOG" ]] \
    || fail "password proof ran after structural validation failed" || return 1
  assert_contains "$output" 'original stack did not survive the edit' || return 1
}

test_existing_unsafe_iris_rule_is_refused_unchanged() {
  setup_case || return 1
  write_fallback
  printf '%s\n' \
    '# unsafe prior configuration' \
    $'auth\trequired\tpam_iris.so' \
    $'auth\trequired\tpam_unix.so' > "$PAM_DIR/existing-service"
  cp "$PAM_DIR/existing-service" "$CASE_ROOT/existing.expected"

  local output rc
  output="$(wire_service existing-service 'existing service' </dev/null 2>&1)"
  rc=$?

  [[ $rc -ne 0 ]] || fail "unsafe existing Iris rule was accepted" || return 1
  cmp -s "$PAM_DIR/existing-service" "$CASE_ROOT/existing.expected" \
    || fail "unsafe existing service was modified" || return 1
  [[ -z "$(find "$BACKUP_DIR" -maxdepth 1 -type f -print -quit)" ]] \
    || fail "refused service unexpectedly created a backup" || return 1
  assert_contains "$output" 'unexpected control field' || return 1
  assert_contains "$output" 'refusing to modify it' || return 1
}

test_existing_safe_iris_rule_is_left_alone() {
  setup_case || return 1
  printf '%s\n' \
    "$IRIS_PAM_LINE" \
    $'auth\trequired\tpam_unix.so' > "$PAM_DIR/existing-service"
  cp "$PAM_DIR/existing-service" "$CASE_ROOT/existing.expected"

  local output
  output="$(wire_service existing-service 'existing service' </dev/null 2>&1)" \
    || fail "safe existing Iris rule was refused: $output" || return 1

  cmp -s "$PAM_DIR/existing-service" "$CASE_ROOT/existing.expected" \
    || fail "safe existing service was modified" || return 1
  [[ -z "$(find "$BACKUP_DIR" -maxdepth 1 -type f -print -quit)" ]] \
    || fail "unchanged service unexpectedly created a backup" || return 1
  [[ ! -e "$PAMTESTER_LOG" ]] \
    || fail "already-safe service unexpectedly ran pamtester" || return 1
  assert_contains "$output" 'already wired with fail-through control' || return 1
}

test_existing_non_auth_iris_rule_is_refused_unchanged() {
  setup_case || return 1
  printf '%s\n' \
    $'account\trequired\tpam_iris.so' \
    $'auth\trequired\tpam_unix.so' > "$PAM_DIR/existing-service"
  cp "$PAM_DIR/existing-service" "$CASE_ROOT/existing.expected"

  local output rc
  output="$(wire_service existing-service 'existing service' </dev/null 2>&1)"
  rc=$?

  [[ $rc -ne 0 ]] || fail "non-auth Iris rule was accepted" || return 1
  cmp -s "$PAM_DIR/existing-service" "$CASE_ROOT/existing.expected" \
    || fail "service with a non-auth Iris rule was modified" || return 1
  assert_contains "$output" 'unexpected or duplicate rule' || return 1
}

test_existing_service_preserves_extended_attributes() {
  setup_case || return 1
  printf '%s\n' $'auth\trequired\tpam_unix.so' > "$PAM_DIR/existing-service"
  local expected_acl=''
  if command -v setfacl >/dev/null && command -v getfacl >/dev/null \
      && setfacl -m u:nobody:r-- "$PAM_DIR/existing-service" 2>/dev/null; then
    expected_acl="$(getfacl -cp "$PAM_DIR/existing-service")"
  fi
  if ! python3 - "$PAM_DIR/existing-service" <<'PY'
import os
import sys

try:
    os.setxattr(sys.argv[1], b"user.iris-test", b"preserved")
except OSError:
    raise SystemExit(1)
PY
  then
    printf 'SKIP: test filesystem has no user xattr support\n'
    return 0
  fi

  local output value
  output="$(wire_service existing-service 'existing service' </dev/null 2>&1)" \
    || fail "wiring failed unexpectedly: $output" || return 1
  value="$(python3 - "$PAM_DIR/existing-service" <<'PY'
import os
import sys

try:
    print(os.getxattr(sys.argv[1], b"user.iris-test").decode())
except OSError:
    raise SystemExit(1)
PY
)" || fail "extended attribute was lost" || return 1
  [[ "$value" == preserved ]] || fail "extended attribute changed to: $value" || return 1
  if [[ -n "$expected_acl" ]]; then
    [[ "$(getfacl -cp "$PAM_DIR/existing-service")" == "$expected_acl" ]] \
      || fail "access ACL changed during atomic replacement" || return 1
  fi
}

run_test() {
  local name="$1"
  local output
  if output="$("$name" 2>&1)"; then
    printf 'ok - %s\n' "$name"
  else
    printf 'not ok - %s\n%s\n' "$name" "$output"
    FAILURES=$((FAILURES + 1))
  fi
}

run_test test_created_service_is_atomic_and_preserves_fallback
run_test test_created_service_reports_noninteractive_proof_skip
run_test test_created_service_rolls_back_to_absent_on_password_proof_failure
run_test test_created_service_rolls_back_to_absent_on_structural_proof_failure
run_test test_created_service_rolls_back_to_absent_on_control_proof_failure
run_test test_missing_service_refuses_iris_rule_in_fallback_before_editing
run_test test_existing_service_password_failure_still_restores_original
run_test test_existing_service_structural_failure_keeps_prior_rollback_behavior
run_test test_existing_unsafe_iris_rule_is_refused_unchanged
run_test test_existing_safe_iris_rule_is_left_alone
run_test test_existing_non_auth_iris_rule_is_refused_unchanged
run_test test_existing_service_preserves_extended_attributes

if [[ $FAILURES -ne 0 ]]; then
  printf '%d test(s) failed\n' "$FAILURES" >&2
  exit 1
fi

printf 'all installer PAM tests passed\n'
