#!/usr/bin/env bash
# Executes the ACTUAL "Persist results to benchmark-results" step extracted
# from .github/workflows/nightly-benchmark.yml against a stubbed `git`,
# proving the persistence contract:
#   * the push target is exactly HEAD:benchmark-results, never main
#   * only the two expected result files are staged and committed
#   * an identical re-run is a no-op; a differing/incomplete pair fails
#   * a non-race push failure is not retried; a genuine race is retried
#   * a rebase conflict fails before any push
#   * no force-push anywhere
#
# LOCAL ONLY: `git` is a shell function; nothing contacts GitHub.
set -euo pipefail
IFS=$'\n\t'

HERE="$(cd "$(dirname "$0")" && pwd)"
BENCH_DIR="$(cd "$HERE/.." && pwd)"
WORKFLOW="$BENCH_DIR/.github/workflows/nightly-benchmark.yml"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
  for cand in python3 python py; do
    cpath="$(command -v "$cand" 2>/dev/null || true)"
    [[ -n "$cpath" ]] || continue
    if "$cpath" -c 'import yaml' >/dev/null 2>&1; then PYTHON="$cpath"; break; fi
  done
fi
[[ -n "$PYTHON" ]] || {
  echo "ERROR: no working python with PyYAML found (tried python3, python, py; set PYTHON=...)" >&2
  exit 2
}

STEP="$TMP/step.sh"
WF_IN="$WORKFLOW"; STEP_IN="$STEP"
if [[ "$("$PYTHON" -c 'import sys; print(sys.platform)')" == "win32" ]]; then
  WF_IN="$(cygpath -w "$WORKFLOW")"
  STEP_IN="$(cygpath -w "$TMP/step.sh")"
fi
"$PYTHON" - "$WF_IN" "$STEP_IN" <<'PY'
import sys, yaml
doc = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
steps = doc["jobs"]["persist-nightly-package-results"]["steps"]
step = next(s for s in steps if s.get("name", "").startswith("Persist results to"))
with open(sys.argv[2], "w", encoding="utf-8", newline="\n") as fh:
    fh.write(step["run"])
PY

# The pre-persistence package-pair guard lives in the "Extract result JSONs"
# step, so the harness executes THAT step too instead of reimplementing it.
EXTRACT_STEP="$TMP/extract_step.sh"
EXTRACT_IN="$EXTRACT_STEP"
if [[ "$("$PYTHON" -c 'import sys; print(sys.platform)')" == "win32" ]]; then
  EXTRACT_IN="$(cygpath -w "$EXTRACT_STEP")"
fi
"$PYTHON" - "$WF_IN" "$EXTRACT_IN" <<'PY'
import sys, yaml
doc = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
steps = doc["jobs"]["persist-nightly-package-results"]["steps"]
step = next(s for s in steps if s.get("name", "").startswith("Extract result JSONs"))
with open(sys.argv[2], "w", encoding="utf-8", newline="\n") as fh:
    fh.write(step["run"])
PY

BENCH_RUN_ID="42333314804"
SMOKE_REL="openms/smoke-$BENCH_RUN_ID.json"
OW_REL="openswath_dia/openms/openswath_dia-$BENCH_RUN_ID.json"

passed=0
failed=0
total=0

# case_setup <stage:full|missing-openswath> <branch:empty|identical|differs|incomplete|unexpected>
case_setup() {
  local stage="$1" branch="$2"
  WORK="$TMP/work"
  rm -rf "$WORK"
  mkdir -p "$WORK/stage/openms" "$WORK/stage/openswath_dia/openms" "$WORK/branch"
  echo '{"smoke":1}' > "$WORK/stage/$SMOKE_REL"
  if [[ "$stage" == "full" ]]; then
    echo '{"openswath":1}' > "$WORK/stage/$OW_REL"
  fi
  case "$branch" in
    identical)
      mkdir -p "$WORK/branch/openms" "$WORK/branch/openswath_dia/openms"
      cp "$WORK/stage/$SMOKE_REL" "$WORK/branch/$SMOKE_REL"
      cp "$WORK/stage/$OW_REL" "$WORK/branch/$OW_REL"
      ;;
    differs)
      mkdir -p "$WORK/branch/openms" "$WORK/branch/openswath_dia/openms"
      echo '{"other":1}' > "$WORK/branch/$SMOKE_REL"
      echo '{"other":1}' > "$WORK/branch/$OW_REL"
      ;;
    incomplete)
      mkdir -p "$WORK/branch/openms"
      cp "$WORK/stage/$SMOKE_REL" "$WORK/branch/$SMOKE_REL"
      ;;
  esac
}

# run_case <name> <git_fixture> <stage> <branch> <expect_rc> <expect_message|->
#          [unexpected_staged yes|no]
# The persistence step is executed as a child process (see run_case), so the
# git stub must be an executable rather than a shell function. It is placed
# first on PATH and never delegates to the real git.
mkdir -p "$TMP/bin"
cat > "$TMP/bin/git" <<'STUB'
#!/usr/bin/env bash
# Test double for git: logs every invocation, never runs a real git command.
{ printf 'git'; printf ' %s' "$@"; printf '
'; } >> "${CALL_LOG:?CALL_LOG must be set}"
case "$1" in
  config|add|commit|fetch) exit 0 ;;
  diff)
    if [ "${UNEXPECTED_STAGED:-no}" = "yes" ]; then
      printf '%s
' "${SMOKE_REL:?}" "README.md"
    else
      printf '%s
' "${SMOKE_REL:?}" "${OW_REL:?}"
    fi
    exit 0 ;;
  rev-parse)
    case "$*" in
      *--verify*)
        [ "${GIT_FIXTURE:-ok}" = "remote_missing" ] && exit 1
        echo "revA"; exit 0 ;;
    esac
    n=$(( $(cat "${STATE:?}/rev.count" 2>/dev/null || echo 0) + 1 ))
    echo "$n" > "${STATE}/rev.count"
    if [ "${GIT_FIXTURE:-ok}" = "push_fail_then_race" ] && [ "$n" -ge 2 ]; then
      echo "revB"
    else
      echo "revA"
    fi
    exit 0 ;;
  rebase)
    [ "${2:-}" = "--abort" ] && exit 0
    [ "${GIT_FIXTURE:-ok}" = "rebase_conflict" ] && exit 1
    exit 0 ;;
  push)
    p=$(( $(cat "${STATE:?}/push.count" 2>/dev/null || echo 0) + 1 ))
    echo "$p" > "${STATE}/push.count"
    echo "push-target: $*" >> "${STATE}/push.args"
    [ "${GIT_FIXTURE:-ok}" = "push_fail_no_move" ] && exit 1
    if [ "${GIT_FIXTURE:-ok}" = "push_fail_then_race" ] && [ "$p" -eq 1 ]; then
      exit 1
    fi
    exit 0 ;;
  *) exit 0 ;;
esac
STUB
chmod +x "$TMP/bin/git"

# Hermeticity: the child process must resolve `git` to the stub above. If the
# stub were ever bypassed the step would run real git against the fixture
# directory, so this is asserted once, before any case runs.
total=$((total + 1))
resolved_git="$(PATH="$TMP/bin:$PATH" TMP="$TMP" bash -c 'command -v git')"
if [[ ! -x "$TMP/bin/git" || "$resolved_git" != "$TMP/bin/git" ]]; then
  failed=$((failed + 1))
  echo "FAIL: child environment resolves git to '$resolved_git', expected '$TMP/bin/git'" >&2
else
  passed=$((passed + 1))
  echo "OK:   child process resolves git to the test stub"
fi

run_case() {
  local name="$1" fixture="$2" stage="$3" branch="$4" expect_rc="$5"
  local expect_message="$6" expect_pushes="$7" unexpected="${8:-no}"
  total=$((total + 1))
  case_setup "$stage" "$branch"
  local call_log="$WORK/git.calls" push_count_file="$WORK/push.count" rev_file="$WORK/rev.count"
  : > "$call_log"; : > "$push_count_file"; : > "$rev_file"

  local rc=0
  set +e
  (
    export BENCH_RUN_ID STAGE_DIR="$WORK/stage" BRANCH_DIR="$WORK/branch"
    export RESULTS_BRANCH="benchmark-results" MAX_PUSH_ATTEMPTS=3
    export GIT_FIXTURE="$fixture" CALL_LOG="$call_log" UNEXPECTED_STAGED="$unexpected"
    export STATE="$WORK" SMOKE_REL OW_REL
    export PATH="$TMP/bin:$PATH"

    bash -e -o pipefail "$STEP"
  ) > "$WORK/out.log" 2> "$WORK/err.log"
  rc=$?
  set -e

  local ok=1 why="" pushes
  pushes="$(cat "$push_count_file" 2>/dev/null || echo 0)"
  if [[ "$rc" -ne "$expect_rc" ]]; then
    ok=0; why="rc=$rc want=$expect_rc; stderr: $(head -c 300 "$WORK/err.log")"
  fi
  if [[ "$expect_message" != "-" ]] && ! grep -qF -- "$expect_message" "$WORK/out.log" "$WORK/err.log"; then
    ok=0; why="$why; missing message '$expect_message'"
  fi
  if grep -qiE "HEAD:main|push origin main|--force" "$call_log"; then
    ok=0; why="$why; call log contains a main/force push"
  fi
  if [[ "$pushes" -ne "$expect_pushes" ]]; then
    ok=0; why="$why; expected $expect_pushes push(es), got $pushes"
  fi
  if [[ "$expect_pushes" -gt 0 ]] && ! grep -qF -- "push origin HEAD:benchmark-results" "$call_log"; then
    ok=0; why="$why; missing expected push target; calls: $(tr '
' '|' <"$call_log")"
  fi

  if [[ "$ok" -eq 1 ]]; then
    passed=$((passed + 1)); echo "OK:   $name"
  else
    failed=$((failed + 1)); echo "FAIL: $name --$why" >&2
  fi
}

echo "== nightly persistence contract tests (stubbed git, no network) =="
echo

run_case "fresh run: commits the pair and pushes to benchmark-results" \
  ok full empty 0 "pushed benchmark run $BENCH_RUN_ID" 1
total=$((total + 1))
if [[ "$(cat "$TMP/work/branch/$SMOKE_REL")" != '{"smoke":1}' ]]; then
  failed=$((failed + 1)); echo "FAIL: persisted smoke file content mismatch" >&2
else
  passed=$((passed + 1)); echo "OK:   persisted files land at the expected branch paths"
fi

run_case "identical re-run is an idempotent no-op" \
  ok full identical 0 "already persisted byte-identically" 0
total=$((total + 1))
if [[ -s "$TMP/work/git.calls" ]]; then
  failed=$((failed + 1)); echo "FAIL: no-op case invoked git: $(tr '\n' ' ' <"$TMP/work/git.calls")" >&2
else
  passed=$((passed + 1)); echo "OK:   no-op case performs no git operation"
fi

run_case "differing existing pair is refused" \
  ok full differs 1 "refusing to overwrite" 0
run_case "incomplete existing pair is refused" \
  ok full incomplete 1 "incomplete or differs" 0
run_case "missing staged file fails before touching the branch" \
  ok missing-openswath empty 1 "staged result is missing or empty" 0
run_case "unexpected staged path fails" \
  ok full empty 1 "not exactly the two expected" 0 yes
run_case "non-race push failure is not retried" \
  push_fail_no_move full empty 1 "not a race" 1
run_case "genuine race is retried once and succeeds" \
  push_fail_then_race full empty 0 "moved from revA to revB" 2
run_case "rebase conflict fails without pushing" \
  rebase_conflict full empty 1 "hit a conflict" 0
run_case "missing remote branch fails with a clear message" \
  remote_missing full empty 1 "does not exist on the remote" 0

echo
echo "== workflow-level guarantees =="
total=$((total + 1))
if "$PYTHON" - "$WF_IN" <<'PY'
import sys, yaml

doc = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
steps = doc["jobs"]["persist-nightly-package-results"]["steps"]
names = [s.get("name", "") for s in steps]
text = open(sys.argv[1], encoding="utf-8").read()

assert "HEAD:main" not in text, "workflow still pushes to main"
assert "push origin main" not in text, "workflow still pushes to main"
persist_run = next(s for s in steps if s.get("name", "").startswith("Persist results to"))["run"]
assert "--force" not in persist_run, "persist step force-pushes"

pages = [s for s in steps if "gh workflow run pages.yml" in (s.get("run") or "")]
assert len(pages) == 1, f"expected exactly one Pages dispatch, got {[p['name'] for p in pages]}"
assert names.index(pages[0]["name"]) > names.index(
    next(n for n in names if n.startswith("Persist results to"))
), "Pages is dispatched before persistence"

persist = next(s for s in steps if s.get("name", "").startswith("Persist results to"))
assert "gh " not in (persist.get("run") or ""), "persist step must not dispatch workflows"
checkout = [s for s in steps if s.get("name", "").startswith("Checkout benchmark-results")]
assert checkout and checkout[0]["with"]["ref"] == "benchmark-results"
PY
then
  passed=$((passed + 1)); echo "OK:   dispatch order, push target, and no force-push"
else
  failed=$((failed + 1)); echo "FAIL: workflow-level guarantees" >&2
fi

# ---------------------------------------------------------------------------
# Package-pair consistency guard: executes the ACTUAL "Extract result JSONs"
# step (the pre-persistence validation) against generated result JSONs.
# ---------------------------------------------------------------------------
FNAME_A="OpenMS-3.7.0-pre-nightly-2026-11-06-Debian-Linux-x86_64.deb"
SHA_A="3d56f94e9f4209b38e3e61e3bfffad896c55ddee21c5d946e39fdf3bda9e9a1e"
SHA_B="c0664adffdbf4591a796dc323004696b9b7df5164a56fcdb8afb286b7c3c821a"

# pkg <filename> <sha256> <archive_dir> <date> [extra json suffix]
pkg() {
  printf '{"filename":"%s","sha256":"%s","archive_dir":"%s","date":"%s"%s}' \
    "$1" "$2" "$3" "$4" "${5:-}"
}

PKG_OK="$(pkg "$FNAME_A" "$SHA_A" 2026.11.07 2026-11-06)"

# The step invokes `python3`; keep it working even when only `python`/`py` is
# on PATH (the harness already resolved a working interpreter).
mkdir -p "$TMP/bin"
cat > "$TMP/bin/python3" <<EOF
#!/usr/bin/env bash
exec "$PYTHON" "\$@"
EOF
chmod +x "$TMP/bin/python3"

# make_result_json <path> <benchmark> <run_id> <runtime_source> <package literal>
make_result_json() {
  "$PYTHON" - "$1" "$2" "$3" "$4" "$5" <<'PY'
import json, sys
path, bench, run_id, runtime_source, pkg = sys.argv[1:6]
doc = {
    "schema": "openms-benchmarking/report/v2",
    "identity": {
        "benchmark": bench,
        "software": {"name": "OpenMS", "version": "05518e68abaa58b39f66b841e08be7be207cdaa8"},
        "runtime": {"runtime_source": runtime_source, "package": json.loads(pkg)},
    },
    "run": {"run_id": run_id, "run_at": "2026-11-07T03:18:46Z"},
}
with open(path, "w", encoding="utf-8") as fh:
    json.dump(doc, fh)
PY
}

staging_is_empty() {
  local staging="$1/persist-staging"
  [[ ! -e "$staging" ]] && return 0
  [[ -d "$staging" ]] || return 1
  [[ -z "$(find "$staging" -type f -print -quit)" ]]
}

# run_extract_case <name> <smoke_pkg> <ow_pkg> <expect_rc> <expect_message|-> \
#                  [smoke_runtime] [ow_runtime]
run_extract_case() {
  local name="$1" smoke_pkg="$2" ow_pkg="$3" expect_rc="$4" msg="$5"
  local smoke_rt="${6:-package}" ow_rt="${7:-package}"
  total=$((total + 1))
  local WORKX="$TMP/extract-work"
  rm -rf "$WORKX"
  mkdir -p "$WORKX/downloaded/openms" "$WORKX/downloaded/openswath_dia/openms"
  make_result_json "$WORKX/downloaded/smoke-$BENCH_RUN_ID.json" \
    smoke "$BENCH_RUN_ID" "$smoke_rt" "$smoke_pkg"
  make_result_json "$WORKX/downloaded/openswath_dia/openms/openswath_dia-$BENCH_RUN_ID.json" \
    openswath_dia "$BENCH_RUN_ID" "$ow_rt" "$ow_pkg"

  # Run the step the way GitHub Actions does (bash -e -o pipefail) rather than
  # sourcing it: errexit is suppressed inside a subshell that is part of an
  # `||` list, which would hide a non-zero exit from the step's python guard.
  set +e
  (
    cd "$WORKX"
    export BENCH_RUN_ID
    export PATH="$TMP/bin:$PATH"
    bash -e -o pipefail "$EXTRACT_STEP"
  ) > "$WORKX/out.log" 2> "$WORKX/err.log"
  local rc=$?
  set -e

  local ok=1 why=""
  if [[ "$rc" -ne "$expect_rc" ]]; then
    ok=0; why="rc=$rc want=$expect_rc; stderr: $(head -c 300 "$WORKX/err.log")"
  fi
  if [[ "$msg" != "-" ]] && ! grep -qF -- "$msg" "$WORKX/out.log" "$WORKX/err.log"; then
    ok=0; why="$why; missing message '$msg'; stderr: $(head -c 300 "$WORKX/err.log")"
  fi
  if [[ "$expect_rc" -ne 0 ]]; then
    if ! staging_is_empty "$WORKX"; then
      ok=0; why="$why; persist-staging was populated despite the failure"
    fi
  else
    if [[ ! -s "$WORKX/persist-staging/openms/smoke-$BENCH_RUN_ID.json" ]] \
       || [[ ! -s "$WORKX/persist-staging/openswath_dia/openms/openswath_dia-$BENCH_RUN_ID.json" ]]; then
      ok=0; why="$why; expected staged pair is missing"
    fi
  fi

  if [[ "$ok" -eq 1 ]]; then
    passed=$((passed + 1)); echo "OK:   $name"
  else
    failed=$((failed + 1)); echo "FAIL: $name --$why" >&2
  fi
}

echo "== package-pair consistency guard (actual Extract result JSONs step) =="
echo

run_extract_case "matching package metadata stages both JSONs" \
  "$PKG_OK" "$PKG_OK" 0 "package-pair identity verified"

run_extract_case "differing sha256 fails" \
  "$PKG_OK" "$(pkg "$FNAME_A" "$SHA_B" 2026.11.07 2026-11-06)" \
  1 "package identity mismatch for run $BENCH_RUN_ID: sha256"

run_extract_case "differing filename fails" \
  "$PKG_OK" "$(pkg OpenMS-3.7.0-pre-nightly-2026-11-10-Debian-Linux-x86_64.deb "$SHA_A" 2026.11.07 2026-11-06)" \
  1 "package identity mismatch for run $BENCH_RUN_ID: filename"

run_extract_case "missing package object on one side fails" \
  "$PKG_OK" null 1 "identity.runtime.package is missing or not an object"

run_extract_case "null package objects fail" \
  null null 1 "identity.runtime.package is missing or not an object"

run_extract_case "empty required metadata fails" \
  "$PKG_OK" "$(pkg "$FNAME_A" "" 2026.11.07 2026-11-06)" \
  1 "missing required metadata: sha256"

run_extract_case "unknown additional package field is ignored" \
  "$PKG_OK" "$(pkg "$FNAME_A" "$SHA_A" 2026.11.07 2026-11-06 ',"bytes":12345')" \
  0 "package-pair identity verified"

run_extract_case "differing date fails" \
  "$PKG_OK" "$(pkg "$FNAME_A" "$SHA_A" 2026.11.07 2026-11-10)" \
  1 "package identity mismatch for run $BENCH_RUN_ID: date"

# A truthy non-string (here: boolean true) must be rejected, not compared.
run_extract_case "non-string required metadata fails" \
  "$PKG_OK" "{\"filename\":\"$FNAME_A\",\"sha256\":\"$SHA_A\",\"archive_dir\":\"2026.11.07\",\"date\":true}" \
  1 "identity.runtime.package is missing required metadata: date (empty or not a string)"

run_extract_case "runtime_source source still fails with the existing diagnostic" \
  "$PKG_OK" "$PKG_OK" 1 "identity.runtime.runtime_source is 'source'" package source

echo
echo "== summary =="
echo "total:  $total"
echo "passed: $passed"
echo "failed: $failed"
if [[ "$failed" -gt 0 ]]; then
  echo "RESULT: FAIL" >&2
  exit 1
fi
echo "RESULT: PASS"
