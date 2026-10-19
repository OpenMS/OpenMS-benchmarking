#!/usr/bin/env bash
# Contract tests for the workflow_run_id validation logic used by
# .github/workflows/nightly-benchmark.yml (OpenMS/OpenMS-benchmarking#7).
#
# run_validation() mirrors, statement for statement, the validation block in
# the workflow's dispatch step (jq extraction + empty/numeric/length/zero
# checks). This is LOCAL validation only: it never contacts GitHub and never
# dispatches a workflow.
set -euo pipefail
IFS=$'\n\t'

HERE="$(cd "$(dirname "$0")" && pwd)"
BENCH_DIR="$(cd "$HERE/.." && pwd)"
WORKFLOW="$BENCH_DIR/.github/workflows/nightly-benchmark.yml"

if [[ ! -f "$WORKFLOW" ]]; then
  echo "ERROR: cannot find $WORKFLOW" >&2
  exit 2
fi

# jq mirrors `gh api --jq`; it must exist for these tests to be meaningful.
if ! command -v jq >/dev/null 2>&1 && [[ -x "$BENCH_DIR/.tools/jq.exe" ]]; then
  export PATH="$BENCH_DIR/.tools:$PATH"
fi
if ! command -v jq >/dev/null 2>&1; then
  echo "ERROR: jq not found on PATH (required to emulate gh --jq)" >&2
  exit 2
fi

error_annotation() { echo "::error::$1"; }

run_validation() {
  local json="$1"
  local RAW_ID DISPATCH_RESP
  DISPATCH_RESP="$json"
  RAW_ID="$(printf "%s" "$DISPATCH_RESP" | jq -r ".workflow_run_id // empty" 2>/dev/null)" || {
    error_annotation "dispatch response is not the expected JSON shape (missing or unparseable workflow_run_id)"
    return 1
  }
  if [[ -z "$RAW_ID" ]]; then
    error_annotation "dispatch returned no workflow_run_id (empty or null response)"
    return 1
  fi
  if [[ ! "$RAW_ID" =~ ^[0-9]+$ ]]; then
    error_annotation "dispatch returned a non-numeric or malformed workflow_run_id: '$RAW_ID'"
    return 1
  fi
  if [[ "${#RAW_ID}" -gt 18 ]]; then
    error_annotation "dispatch returned an implausibly large workflow_run_id: '$RAW_ID'"
    return 1
  fi
  if [[ "$RAW_ID" -eq 0 ]]; then
    error_annotation "dispatch returned workflow_run_id 0 (invalid)"
    return 1
  fi
  printf "%s" "$RAW_ID"
}

passed=0
failed=0
total=0

test_case() {
  local name="$1"
  local expect_rc="$2"
  local expect_stdout="${3:-}"
  shift 3
  total=$((total + 1))
  local stderr_log stdout_log rc stdout
  stderr_log="$(mktemp)"
  stdout_log="$(mktemp)"
  set +e
  stdout="$("$@" >"$stdout_log" 2>"$stderr_log")"
  rc=$?
  set -e
  if [[ "$rc" -ne "$expect_rc" ]]; then
    failed=$((failed + 1))
    echo "FAIL: $name (expected rc=$expect_rc, got rc=$rc)" >&2
    echo "  stdout: $(tr -s ' \n' ' ' <"$stdout_log" | head -c 400)" >&2
    echo "  stderr: $(tr -s ' \n' ' ' <"$stderr_log" | head -c 400)" >&2
  elif [[ "$expect_rc" -eq 0 && -z "$(cat "$stdout_log")" ]]; then
    failed=$((failed + 1))
    echo "FAIL: $name (expected success with an id on stdout, got empty stdout)" >&2
  elif [[ "$expect_rc" -eq 0 && "$(cat "$stdout_log")" != "$expect_stdout" ]]; then
    failed=$((failed + 1))
    echo "FAIL: $name (expected id '$expect_stdout', got '$(cat "$stdout_log")')" >&2
  else
    passed=$((passed + 1))
    echo "OK:   $name"
  fi
  rm -f "$stderr_log" "$stdout_log"
}

echo "== ID validation contract tests =="
echo "workload: $WORKFLOW"
echo

# --- required cases -------------------------------------------------------
test_case "valid positive numeric ID (123456789)" 0 "123456789" run_validation '{"workflow_run_id":123456789}'
test_case "missing workflow_run_id key" 1 "" run_validation '{"other":"value"}'
test_case "empty string workflow_run_id" 1 "" run_validation '{"workflow_run_id":""}'
test_case "null workflow_run_id" 1 "" run_validation '{"workflow_run_id":null}'
test_case "non-numeric workflow_run_id" 1 "" run_validation '{"workflow_run_id":"abc123"}'
test_case "zero workflow_run_id" 1 "" run_validation '{"workflow_run_id":0}'
test_case "negative workflow_run_id" 1 "" run_validation '{"workflow_run_id":-5}'
test_case "API command failure (non-JSON payload)" 1 "" run_validation "not json{{"
test_case "malformed shape: workflow_run_id is a huge number string" 1 "" run_validation '{"workflow_run_id":"999999999999999999999"}'
test_case "float workflow_run_id" 1 "" run_validation '{"workflow_run_id":12.5}'
# Malformed-but-well-formed-JSON: jq -r emits a REAL newline for an escaped
# \n, and a line-oriented grep would accept it (one line is all digits),
# corrupting GITHUB_ENV. The workflow must reject the whole value instead.
test_case "multi-line string workflow_run_id (embedded newline)" 1 "" run_validation '{"workflow_run_id":"123\n456"}'
test_case "embedded carriage-return workflow_run_id" 1 "" run_validation '{"workflow_run_id":"123\r456"}'

# The oversized case must fail via the LENGTH guard specifically. A broken
# fixture (unterminated JSON string) once made this case "pass" through the
# unparseable-JSON path instead, so assert the rejection reason explicitly.
total=$((total + 1))
if huge_out="$(run_validation '{"workflow_run_id":"999999999999999999999"}')"; then
  failed=$((failed + 1))
  echo "FAIL: oversized id accepted by validation" >&2
elif [[ "$huge_out" == *"implausibly large"* ]]; then
  passed=$((passed + 1))
  echo "OK:   oversized id rejected by the length guard (not merely unparseable)"
else
  failed=$((failed + 1))
  echo "FAIL: oversized id rejected for the wrong reason: $huge_out" >&2
fi

# --- every rejection happens before any downstream effect -----------------
# The validation runs before the wait loop in the workflow; here we assert
# the validation function itself rejects before returning any id.
for payload in '{"workflow_run_id":null}' '{"workflow_run_id":"abc"}' '{"workflow_run_id":0}' '{"workflow_run_id":-5}'; do
  total=$((total + 1))
  if out="$(run_validation "$payload")"; then
    failed=$((failed + 1))
    echo "FAIL: rejection path returned success for $payload" >&2
  elif [[ "$out" =~ ^[0-9]+$ ]]; then
    failed=$((failed + 1))
    echo "FAIL: rejection path produced an id for $payload: $out" >&2
  else
    passed=$((passed + 1))
    echo "OK:   rejects without producing an id: $payload"
  fi
done

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
