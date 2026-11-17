#!/usr/bin/env bash
# Executes the ACTUAL "Assemble combined results tree" step extracted from
# .github/workflows/pages.yml, proving the report input contract:
#   * both run families are the UNION of main's historical tree
#     (benchmark/results/<family>) and the data branch's generated runs
#   * a run held by only one source is still published (disjoint history)
#   * a duplicated relative path is kept once when byte-identical
#   * a conflicting duplicate fails the build instead of silently winning
#   * main's tools/ reference JSONs are retained
#   * missing data, empty generated history, or missing tools fail clearly
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

# make_repo <with_data> <with_runs> <with_tools> <history> <duplicate>
#   history:   yes = main holds its historical runs, no = empty historical trees
#   duplicate: none | identical | conflict -- a data-branch run that also exists
#              in main's history at the same relative path
make_repo() {
  local with_data="$1" with_runs="$2" with_tools="$3"
  local history="$4" duplicate="$5"
  REPO="$TMP/repo"
  rm -rf "$REPO"
  mkdir -p "$REPO/benchmark/results"
  if [[ "$history" == "yes" ]]; then
    mkdir -p "$REPO/benchmark/results/openms" \
      "$REPO/benchmark/results/openswath_dia/openms"
    # Historical runs committed to main before the storage split: same relative
    # layout as the data branch, under benchmark/results/.
    fixture_result "$REPO/benchmark/results/openms/smoke-42155957466.json" \
      42155957466 smoke
    fixture_result \
      "$REPO/benchmark/results/openswath_dia/openms/openswath_dia-42155957466.json" \
      42155957466 openswath_dia
  fi
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
    case "$duplicate" in
      none) ;;
      identical)
        cp "$REPO/benchmark/results/openms/smoke-42155957466.json" \
           "$REPO/benchmark-data/openms/smoke-42155957466.json"
        cp "$REPO/benchmark/results/openswath_dia/openms/openswath_dia-42155957466.json" \
           "$REPO/benchmark-data/openswath_dia/openms/openswath_dia-42155957466.json"
        ;;
      conflict)
        # Same relative path, different contents: main's history and the data
        # branch disagree about one run.
        fixture_result "$REPO/benchmark-data/openms/smoke-42155957466.json" \
          42155957466 smoke
        "$PYTHON" - "$REPO/benchmark-data/openms/smoke-42155957466.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path, encoding="utf-8"))
data["performance"]["wall_time_s"] = 99.0
json.dump(data, open(path, "w", encoding="utf-8"))
PY
        ;;
      *) echo "unknown duplicate mode: $duplicate" >&2; exit 2 ;;
    esac
  fi
}

passed=0
failed=0
total=0

# run_step <name> <expect_rc> <expect_message|-> [<forbidden_message>]
run_step() {
  local name="$1" expect_rc="$2" expect_message="$3" forbidden="${4:-}"
  total=$((total + 1))
  local rc=0
  ( cd "$REPO" && DATA_DIR=benchmark-data HISTORY_DIR=benchmark/results \
      OUT_DIR=site-results bash "$STEP" ) \
    > "$TMP/out.log" 2> "$TMP/err.log" || rc=$?
  local ok=1 why=""
  if [[ "$rc" -ne "$expect_rc" ]]; then
    ok=0; why="rc=$rc want=$expect_rc; stderr: $(head -c 300 "$TMP/err.log")"
  fi
  if [[ "$expect_message" != "-" ]] \
      && ! grep -qF -- "$expect_message" "$TMP/out.log" "$TMP/err.log"; then
    ok=0; why="$why; missing message '$expect_message'"
  fi
  if [[ -n "$forbidden" ]] && grep -qF -- "$forbidden" "$TMP/out.log" "$TMP/err.log"; then
    ok=0; why="$why; unexpected message '$forbidden'"
  fi
  if [[ "$ok" -eq 1 ]]; then
    passed=$((passed + 1)); echo "OK:   $name"
  else
    failed=$((failed + 1)); echo "FAIL: $name --$why" >&2
  fi
}

# renderer_counts -> "<runs> <tools>" as report_generate.load_results sees it
renderer_counts() {
  "$PYTHON" - "$REPO/site-results" "$BENCH_DIR/benchmark/report" <<'PY'
import sys
sys.path.insert(0, sys.argv[2])
from report_generate import load_results
runs, tools = load_results(sys.argv[1])
print(f"{len(runs)} {len(tools)}")
PY
}

# check <name> <condition-description-when-failing> <test...>
check() {
  local name="$1" why="$2"
  shift 2
  total=$((total + 1))
  if "$@"; then
    passed=$((passed + 1)); echo "OK:   $name"
  else
    failed=$((failed + 1)); echo "FAIL: $name -- $why" >&2
  fi
}

echo "== Pages results-tree assembly tests =="
echo

# --- disjoint history: main's old runs plus the data branch's new runs -------
make_repo yes yes yes yes none
run_step "unions historical and generated runs plus main's tools/" 0 \
  "assembled 3 smoke run(s), 3 openswath run(s), 1 tool reference(s)"
run_step "reports source counts for both sources" 0 \
  "main history=2 file(s), benchmark-results=4 file(s), identical duplicate(s) collapsed=0"
check "historical result from main is published" \
  "main's historical run was dropped from site-results" \
  test -f "$REPO/site-results/openms/smoke-42155957466.json"
check "historical openswath result from main is published" \
  "main's historical openswath run was dropped from site-results" \
  test -f "$REPO/site-results/openswath_dia/openms/openswath_dia-42155957466.json"
check "generated runs from the data branch are published" \
  "the data branch's runs were not assembled" \
  test -f "$REPO/site-results/openms/smoke-42333314804.json"
check "main's tools/ reference result is published" \
  "the tool reference JSON is missing" \
  test -f "$REPO/site-results/tools/proteobench-Exp-1.json"
counts="$(renderer_counts)"
check "renderer sees exactly 6 runs and 1 tool result (no duplicates)" \
  "renderer saw '$counts' (want '6 1')" \
  test "$counts" = "6 1"

# --- identical duplicate: the same run on both sides is kept once -----------
make_repo yes yes yes yes identical
run_step "identical duplicate at the same relative path is collapsed" 0 \
  "duplicate result with identical contents at openms/smoke-42155957466.json"
run_step "identical duplicates are counted in the log" 0 \
  "main history=2 file(s), benchmark-results=6 file(s), identical duplicate(s) collapsed=2"
counts="$(renderer_counts)"
check "identical duplicates do not double-count runs" \
  "renderer saw '$counts' (want '6 1')" \
  test "$counts" = "6 1"

# --- conflicting duplicate: differing bytes fail the build ------------------
make_repo yes yes yes yes conflict
run_step "conflicting duplicate at the same relative path fails loudly" 1 \
  "conflicting result at openms/smoke-42155957466.json"
check "a conflicting duplicate never overwrites main's historical copy" \
  "the conflicting data copy replaced main's historical result" \
  cmp -s "$REPO/site-results/openms/smoke-42155957466.json" \
         "$REPO/benchmark/results/openms/smoke-42155957466.json"

# --- main has no historical runs: the data branch alone is still assembled --
make_repo yes yes yes no none
run_step "empty historical tree on main is reported, not fatal" 0 \
  "holds no historical results on main"
run_step "assembles the data branch's runs without main history" 0 \
  "assembled 2 smoke run(s), 2 openswath run(s), 1 tool reference(s)"

# --- failure modes ----------------------------------------------------------
make_repo no yes yes yes none
run_step "missing data checkout fails clearly" 1 "is missing"
make_repo yes no yes yes none
run_step "empty generated history fails clearly" 1 "no usable history"
make_repo yes yes no yes none
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
run = assemble["run"]
# Both run sources must be assembled: main's history and the data branch.
assert assemble.get("env", {}).get("HISTORY_DIR") == "benchmark/results"
assert 'HISTORY_DIR="$HISTORY_DIR/$family"' in run or "$HISTORY_DIR/$family" in run
assert "$DATA_DIR/$family" in run
assert "$HISTORY_DIR/tools" in run
assert "cmp -s" in run, "duplicate relative paths must be compared, not overwritten"
assert "::error::conflicting result at" in run, "conflicting duplicates must fail"
assert "frozen" not in text, "comments must not claim main's result trees are frozen"
code = [s for s in steps if s.get("name", "").startswith("Checkout code")]
data = [s for s in steps if s.get("name", "").startswith("Checkout generated results")]
assert code and code[0]["with"]["ref"] == "main"
assert data and data[0]["with"]["ref"] == "benchmark-results"
PY
then
  passed=$((passed + 1)); echo "OK:   both run sources, duplicate guard, and checkout refs"
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
