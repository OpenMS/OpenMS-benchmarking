#!/usr/bin/env bash
# Executes the ACTUAL "Assemble combined results tree" step extracted from
# .github/workflows/pages.yml, proving the report input contract:
#   * both generated run families come from the data branch
#   * main's tools/ reference JSONs are retained
#   * main's frozen benchmark/results/openms tree is NOT copied
#   * missing data, empty history, or missing tools fail with a clear message
#   * the assembled tree is exactly what the renderer expects (no duplicates)
#
# LOCAL ONLY: no network, no GH_TOKEN.
set -euo pipefail
IFS=$'\n\t'

HERE="$(cd "$(dirname "$0")" && pwd)"
BENCH_DIR="$(cd "$HERE/.." && pwd)"
PAGES="$BENCH_DIR/.github/workflows/pages.yml"
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
WF_IN="$PAGES"; STEP_IN="$STEP"
if [[ "$("$PYTHON" -c 'import sys; print(sys.platform)')" == "win32" ]]; then
  WF_IN="$(cygpath -w "$PAGES")"
  STEP_IN="$(cygpath -w "$STEP")"
fi
"$PYTHON" - "$WF_IN" "$STEP_IN" <<'PY'
import sys, yaml
doc = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
steps = doc["jobs"]["build"]["steps"]
step = next(s for s in steps if s.get("name", "").startswith("Assemble combined results tree"))
with open(sys.argv[2], "w", encoding="utf-8", newline="\n") as fh:
    fh.write(step["run"])
PY

# fixture_result <path> <run_id> <benchmark>
fixture_result() {
  "$PYTHON" - "$1" "$2" "$3" <<'PY'
import json, sys
path, run_id, benchmark = sys.argv[1], sys.argv[2], sys.argv[3]
json.dump({
    "schema": "openms-benchmarking/report/v2",
    "identity": {
        "benchmark": benchmark,
        "benchmark_version": 1,
        "dataset": "fixture",
        "software": {"name": "OpenMS", "version": "abc123"},
        "configuration": {},
        "runtime": {"runtime_source": "package"},
    },
    "run": {"run_id": run_id, "run_at": "2026-11-07T03:17:21Z", "cache": "none"},
    "performance": {"wall_time_s": 1.0, "cpu_time_s": 0.1, "peak_rss_kb": 1024,
                    "build": {"wall_time_s": 0.0, "artifact_bytes": 1}, "stages": []},
    "metrics": {"verdict": "pass"},
}, open(path, "w", encoding="utf-8"))
PY
}

# make_repo <with_data yes|no> <with_runs yes|no> <with_tools yes|no>
make_repo() {
  local with_data="$1" with_runs="$2" with_tools="$3"
  REPO="$TMP/repo"
  rm -rf "$REPO"
  mkdir -p "$REPO/benchmark/results/openms"
  # Frozen pre-split history on main: must never be copied into site-results.
  fixture_result "$REPO/benchmark/results/openms/smoke-42155957466.json" 42155957466 smoke
  if [[ "$with_tools" == "yes" ]]; then
    mkdir -p "$REPO/benchmark/results/tools"
    fixture_result "$REPO/benchmark/results/tools/proteobench-Exp-1.json" Exp-1 proteobench
  fi
  if [[ "$with_data" == "yes" ]]; then
    mkdir -p "$REPO/benchmark-data/openms" "$REPO/benchmark-data/openswath_dia/openms"
    if [[ "$with_runs" == "yes" ]]; then
      fixture_result "$REPO/benchmark-data/openms/smoke-42333314804.json" 42333314804 smoke
      fixture_result "$REPO/benchmark-data/openms/smoke-42951989482.json" 42951989482 smoke
      fixture_result \
        "$REPO/benchmark-data/openswath_dia/openms/openswath_dia-42333314804.json" \
        42333314804 openswath_dia
      fixture_result \
        "$REPO/benchmark-data/openswath_dia/openms/openswath_dia-42951989482.json" \
        42951989482 openswath_dia
    fi
  fi
}

passed=0
failed=0
total=0

# run_step <name> <expect_rc> <expect_message|->
run_step() {
  local name="$1" expect_rc="$2" expect_message="$3"
  total=$((total + 1))
  local rc=0
  ( cd "$REPO" && DATA_DIR=benchmark-data OUT_DIR=site-results bash "$STEP" ) \
    > "$TMP/out.log" 2> "$TMP/err.log" || rc=$?
  local ok=1 why=""
  if [[ "$rc" -ne "$expect_rc" ]]; then
    ok=0; why="rc=$rc want=$expect_rc; stderr: $(head -c 300 "$TMP/err.log")"
  fi
  if [[ "$expect_message" != "-" ]] && ! grep -qF -- "$expect_message" "$TMP/out.log" "$TMP/err.log"; then
    ok=0; why="$why; missing message '$expect_message'"
  fi
  if [[ "$ok" -eq 1 ]]; then
    passed=$((passed + 1)); echo "OK:   $name"
  else
    failed=$((failed + 1)); echo "FAIL: $name --$why" >&2
  fi
}

echo "== Pages results-tree assembly tests =="
echo

make_repo yes yes yes
run_step "assembles both run families plus main's tools/" 0 "assembled 2 smoke run(s), 2 openswath run(s), 1 tool reference(s)"
total=$((total + 1))
if [[ -f "$REPO/site-results/openms/smoke-42333314804.json" \
      && -f "$REPO/site-results/openswath_dia/openms/openswath_dia-42333314804.json" \
      && -f "$REPO/site-results/tools/proteobench-Exp-1.json" ]]; then
  passed=$((passed + 1)); echo "OK:   expected files land under site-results/"
else
  failed=$((failed + 1)); echo "FAIL: assembled tree is missing expected files" >&2
fi
total=$((total + 1))
if [[ -e "$REPO/site-results/openms/smoke-42155957466.json" ]]; then
  failed=$((failed + 1)); echo "FAIL: frozen main history was copied into site-results" >&2
else
  passed=$((passed + 1)); echo "OK:   frozen main history is not copied"
fi
total=$((total + 1))
counts="$("$PYTHON" - "$REPO/site-results" "$BENCH_DIR/benchmark/report" <<'PY'
import sys
sys.path.insert(0, sys.argv[2])
from report_generate import load_results
runs, tools = load_results(sys.argv[1])
print(f"{len(runs)} {len(tools)}")
PY
)"
if [[ "$counts" == "4 1" ]]; then
  passed=$((passed + 1)); echo "OK:   renderer sees exactly 4 runs and 1 tool result (no duplicates)"
else
  failed=$((failed + 1)); echo "FAIL: renderer saw '$counts' (want '4 1')" >&2
fi

make_repo no yes yes
run_step "missing data checkout fails clearly" 1 "is missing"
make_repo yes no yes
run_step "empty run history fails clearly" 1 "no usable history"
make_repo yes yes no
run_step "missing tools/ on main fails clearly" 1 "tools is missing"

echo
echo "== workflow-level guarantees =="
total=$((total + 1))
if "$PYTHON" - "$WF_IN" <<'PY'
import sys, yaml

doc = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
steps = doc["jobs"]["build"]["steps"]
text = open(sys.argv[1], encoding="utf-8").read()

assert "--results-dir site-results" in text
assert "--results-dir benchmark/results" not in text
assemble = next(
    s for s in steps if s.get("name", "").startswith("Assemble combined results tree")
)
assemble_run = assemble["run"]
assert "benchmark/results/openms" not in assemble_run, "assembly must not copy the frozen run history"
assert "benchmark/results/tools" in assemble_run
code = [s for s in steps if s.get("name", "").startswith("Checkout code")]
data = [s for s in steps if s.get("name", "").startswith("Checkout generated results")]
assert code and code[0]["with"]["ref"] == "main"
assert data and data[0]["with"]["ref"] == "benchmark-results"
PY
then
  passed=$((passed + 1)); echo "OK:   data source, render argument, and no frozen-history copy"
else
  failed=$((failed + 1)); echo "FAIL: workflow-level guarantees" >&2
fi

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
