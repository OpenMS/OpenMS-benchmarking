#!/usr/bin/env bash
# test_openswath_diagnostics.sh - exercise the runner's failure diagnostics.
#
# The diagnostic path only runs after a failed benchmark, so it cannot be
# covered by running the benchmark itself. This test extracts `log` and
# `report_openswath_failure` from run_openswath_benchmark.sh (so the shipped
# code is the tested code) and drives them with synthetic openswath.json files.
#
# Run:  bash benchmark/test_openswath_diagnostics.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNNER="${HERE}/run_openswath_benchmark.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

# --- interpreter -------------------------------------------------------------
# The runner calls `python3`; on hosts where only `python` works (Git Bash on
# Windows), put a shim first on PATH so the extracted function is unchanged.
if ! python3 -c 'import sys' >/dev/null 2>&1; then
  real_python="$(command -v python || true)"
  [[ -n "${real_python}" ]] || fail "no usable Python interpreter found"
  mkdir -p "${TMP}/bin"
  printf '#!/usr/bin/env bash\nexec "%s" "$@"\n' "${real_python}" > "${TMP}/bin/python3"
  chmod +x "${TMP}/bin/python3"
  PATH="${TMP}/bin:${PATH}"
fi

# --- extract the functions under test ---------------------------------------
extract_fn() {
  awk -v fn="$1" '
    index($0, fn "() {") == 1 {
      print
      if ($0 ~ /}$/) exit
      inside = 1
      next
    }
    inside {
      print
      if ($0 == "}") exit
    }
  ' "${RUNNER}"
}

log_src="$(extract_fn log)"
report_src="$(extract_fn report_openswath_failure)"
[[ -n "${log_src}" ]] || fail "could not extract log() from ${RUNNER}"
[[ -n "${report_src}" ]] || fail "could not extract report_openswath_failure() from ${RUNNER}"
eval "${log_src}"
eval "${report_src}"

# --- fixtures ----------------------------------------------------------------
RESULTS_DIR="${TMP}/results"
SCRIPT_DIR="${HERE}"
mkdir -p "${RESULTS_DIR}"
: > "${RESULTS_DIR}/summary.txt"

write_report() { printf '%s' "$1" > "${RESULTS_DIR}/openswath.json"; }

check() {
  local label="$1" expected="$2" out
  out="$(report_openswath_failure)"
  if ! grep -Fq -- "${expected}" <<< "${out}"; then
    printf '%s\n' "${out}" >&2
    fail "${label}: expected ${expected@Q} in diagnostics"
  fi
  # the same text must reach summary.txt, otherwise the uploaded log loses it
  grep -Fq -- "${expected}" "${RESULTS_DIR}/summary.txt" \
    || fail "${label}: diagnostics were not written to summary.txt"
  echo "PASS: ${label}"
}

# --- valid library_error -----------------------------------------------------
write_report '{"verdict":"fail","stages":[],"correctness":{"library_error":"feature 7 references transition native_id '"'"'grp_Precursor_i0'"'"', which the library does not define"}}'
check "valid library_error" "library_error: feature 7 references transition native_id 'grp_Precursor_i0', which the library does not define"
check "verdict is printed" "verdict: fail"
check "missing featureXML is visible" "artifact output.featureXML: missing"

# --- empty library_error -----------------------------------------------------
write_report '{"verdict":"fail","correctness":{"library_error":"  "}}'
check "empty library_error" "library_error: (empty string)"

# --- absent library_error key ------------------------------------------------
write_report '{"verdict":"fail","stages":[{"name":"openswath_workflow","required":true,"status":"fail","exit_code":1,"reason":"required stage failed (see logs/openswath_workflow.log)"}],"correctness":{}}'
check "absent library_error" "library_error: (key absent"
check "failing stage is named" "stage openswath_workflow: status=fail exit_code=1"

# --- malformed JSON ----------------------------------------------------------
write_report '{"verdict": "fail", "correctness": {'
check "malformed JSON" "not valid JSON"

# --- absent openswath.json ---------------------------------------------------
rm -f "${RESULTS_DIR}/openswath.json"
check "missing openswath.json" "openswath.json is missing"

# --- a broken diagnostics call must be reported, not swallowed --------------
SCRIPT_DIR="${TMP}"   # openswath_diagnostics.py is not here
if report_openswath_failure >/dev/null 2>&1; then
  fail "a failing diagnostic script must return non-zero so the caller can say so"
fi
echo "PASS: a broken diagnostics call is detectable by the caller"

echo
echo "All openswath_diagnostics shell tests passed."
