#!/usr/bin/env bash
# Executes the ACTUAL dispatch step extracted from
# .github/workflows/nightly-benchmark.yml against a stubbed `gh`, proving:
#   * a valid workflow_run_id reaches the wait loop and is exported
#   * every invalid/failed dispatch fails BEFORE the wait loop runs
#     (no `gh run view`, no artifact download, no persistence possible)
#
# LOCAL ONLY: `gh` is a shell function; nothing contacts GitHub and no
# benchmark workflow is dispatched.
set -euo pipefail
IFS=$'\n\t'

HERE="$(cd "$(dirname "$0")" && pwd)"
BENCH_DIR="$(cd "$HERE/.." && pwd)"
WORKFLOW="$BENCH_DIR/.github/workflows/nightly-benchmark.yml"

# jq is required to emulate `gh api --jq` (the step's real behaviour).
if ! command -v jq >/dev/null 2>&1 && [[ -x "$BENCH_DIR/.tools/jq.exe" ]]; then
  export PATH="$BENCH_DIR/.tools:$PATH"
fi
command -v jq >/dev/null 2>&1 || { echo "ERROR: jq not found" >&2; exit 2; }
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# --- extract the dispatch step's run block --------------------------------
# Portable interpreter selection: try python3, python, py and keep the first
# that actually runs and has PyYAML. On Windows, `python3` can be a broken
# Microsoft Store alias stub, so availability alone is not enough.
PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
  for cand in python3 python py; do
    cpath="$(command -v "$cand" 2>/dev/null || true)"
    [[ -n "$cpath" ]] || continue
    if "$cpath" -c 'import yaml' >/dev/null 2>&1; then PYTHON="$cpath"; break; fi
  done
fi
[[ -n "$PYTHON" ]] || {
  echo "ERROR: no working python with PyYAML found (tried python3, python, py; set PYTHON=...)" >&2; exit 2
}

WF_IN="$WORKFLOW"; STEP_IN="$TMP/step.sh"
# A Windows build of python cannot open POSIX (/tmp/..., /c/...) paths.
if [[ "$("$PYTHON" -c 'import sys; print(sys.platform)')" == "win32" ]]; then
  WF_IN="$(cygpath -w "$WORKFLOW")"
  STEP_IN="$(cygpath -w "$TMP/step.sh")"
fi
"$PYTHON" - "$WF_IN" "$STEP_IN" <<'PY'
import sys, yaml
doc = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
steps = doc["jobs"]["persist-nightly-package-results"]["steps"]
step = next(s for s in steps if s.get("name", "").startswith("Dispatch benchmark.yml"))
with open(sys.argv[2], "w", encoding="utf-8", newline="\n") as fh:
    fh.write(step["run"])
PY

passed=0
failed=0
total=0

# run_case <name> <fixture> <expected_rc> <expect_wait yes|no> <expected_id|->
#         [expected_guard_substring|-]
# The optional 6th argument pins WHICH guard rejected the input (matched
# against the step's ::error:: output), so a case cannot "pass" by failing
# through an unintended earlier path.
run_case() {
  local name="$1" fixture="$2" expect_rc="$3" expect_wait="$4" expect_id="$5"
  local expect_guard="${6:--}"
  total=$((total + 1))
  local rc=0
  (
    export GITHUB_REPOSITORY="OpenMS/OpenMS-benchmarking"
    export GITHUB_ENV="$TMP/env.$fixture"
    export CALL_LOG="$TMP/calls.$fixture"
    : >"$GITHUB_ENV"
    : >"$CALL_LOG"

    # Stub of the GitHub CLI. Only the two endpoints the step uses exist.
    gh() {
      local sub="$1"; shift
      case "$sub" in
        api)
          # repos/OWNER/REPO/actions/workflows/benchmark.yml/dispatches
          # The stub returns the response BODY like `gh api` does; the step's
          # --jq flag is emulated here by piping the body through jq, exactly
          # what gh does internally (non-JSON body -> non-zero exit).
          if [[ "$*" == *"workflows/benchmark.yml/dispatches"* ]]; then
            local body
            case "$FIXTURE" in
              ok)          body='{"workflow_run_id":123456789}' ;;
              empty)       body='' ;;                     # 204 / no body
              null)        body='{"workflow_run_id":null}' ;;
              missing)     body='{"message":"ok"}' ;;
              nonnumeric)  body='{"workflow_run_id":"abc123"}' ;;
              zero)        body='{"workflow_run_id":0}' ;;
              negative)    body='{"workflow_run_id":-5}' ;;
              huge)        body='{"workflow_run_id":"999999999999999999999"}' ;;
              # Escaped newline inside a JSON string: jq -r turns it into a
              # real newline, which a line-oriented numeric check would pass.
              multiline)   body='{"workflow_run_id":"123\n456"}' ;;
              apifail)     printf '%s\n' 'HTTP 422 Unprocessable Entity' >&2; return 1 ;;
              *) echo "unexpected fixture: $FIXTURE" >&2; return 99 ;;
            esac
            # gh's --jq '.workflow_run_id // empty', via jq/gojq semantics:
            # empty body -> no output rc 0; invalid JSON -> non-zero.
            printf '%s' "$body" | jq -r '.workflow_run_id // empty'
            return $?
          fi
          echo "unexpected gh api call: $*" >&2
          return 99
          ;;
        run)
          # gh run view <id> --json status,conclusion --jq ...
          echo "wait-loop-reached" >>"$CALL_LOG"
          printf '%s\n' "completed success"
          return 0
          ;;
        *)
          echo "unexpected gh subcommand: $sub $*" >&2
          return 99
          ;;
      esac
    }

    FIXTURE="$fixture"
    # Restore the runner's default IFS: the workflow's
    # `read -r STATUS CONCLUSION` relies on space-separated word splitting,
    # and this harness sets a restrictive IFS of its own at the top.
    IFS=$' \t\n'
    set +e
    # shellcheck disable=SC1091
    source "$STEP_UNDER_TEST"
    set -e
  ) >"$TMP/out.$fixture" 2>"$TMP/err.$fixture" || rc=$?

  local ok=1 why=""
  if [[ "$rc" -ne "$expect_rc" ]]; then
    ok=0; why="rc=$rc want=$expect_rc; stderr: $(head -c 300 "$TMP/err.$fixture")"
  fi
  local waited=no
  [[ -s "$TMP/calls.$fixture" ]] && waited=yes
  if [[ "$expect_wait" != "$waited" ]]; then
    ok=0; why="$why; wait-loop reached=$waited want=$expect_wait"
  fi
  if [[ "$expect_id" != "-" ]]; then
    if ! grep -qx "BENCH_RUN_ID=$expect_id" "$TMP/env.$fixture" 2>/dev/null; then
      ok=0; why="$why; GITHUB_ENV missing BENCH_RUN_ID=$expect_id (content: $(tr '\n' ' ' <"$TMP/env.$fixture"))"
    fi
  elif [[ -s "$TMP/env.$fixture" ]]; then
    ok=0; why="$why; GITHUB_ENV should be empty on failure"
  fi
  if [[ "$expect_guard" != "-" ]]; then
    if ! grep -qF -- "$expect_guard" "$TMP/out.$fixture" 2>/dev/null; then
      ok=0; why="$why; expected guard output '$expect_guard' missing (stdout: $(tr '\n' ' ' <"$TMP/out.$fixture"))"
    fi
  fi

  if [[ "$ok" -eq 1 ]]; then
    passed=$((passed + 1)); echo "OK:   $name"
  else
    failed=$((failed + 1)); echo "FAIL: $name --$why" >&2
  fi
}

export STEP_UNDER_TEST="$TMP/step.sh"

echo "== dispatch-step execution tests (stubbed gh, no real dispatch) =="
echo "step under test: $STEP_UNDER_TEST (extracted from $WORKFLOW)"
echo

run_case "valid id: waits and exports BENCH_RUN_ID"        ok          0 yes 123456789
run_case "empty/204 body: fails before wait"               empty       1 no  -
run_case "null workflow_run_id: fails before wait"         null        1 no  -
run_case "missing workflow_run_id key: fails before wait"  missing     1 no  -
run_case "non-numeric id: fails before wait"               nonnumeric  1 no  - "non-numeric or malformed"
run_case "zero id: fails before wait"                      zero        1 no  - "workflow_run_id 0 (invalid)"
run_case "negative id: fails before wait"                  negative    1 no  - "non-numeric or malformed"
run_case "implausibly large id: fails before wait"         huge        1 no  - "implausibly large"
run_case "multi-line string id: fails before wait"         multiline   1 no  - "non-numeric or malformed"
run_case "dispatch API failure: fails before wait"         apifail     1 no  -

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
