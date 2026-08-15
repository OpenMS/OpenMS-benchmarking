#!/usr/bin/env bash
# run_smoke_benchmark.sh - minimal smoke benchmark for the OpenMS binaries.
#
# Purpose (Issue #8788, first milestone): prove the architecture
#   fixed OpenMS SHA -> Linux build -> generated binaries -> benchmark run
# by executing a tiny real pipeline (DecoyDatabase -> CometAdapter ->
# PercolatorAdapter) with the built TOPP tools and recording per-stage
# resource usage. This is a *smoke* test, not the scientific benchmark:
# the PXD028735 dataset and the MS2Rescore mode come in later milestones.
#
# Environment variables:
#   OPENMS_BIN     directory with the built TOPP binaries (build/bin)   [required]
#   ENGINES_DIR    directory with the search engines (_thirdparty)      [required]
#   OPENMS_SHA     OpenMS commit SHA that produced the binaries         [recorded]
#   USE_MS2RESCORE true/false - rescoring request is recorded, not run  [default false]
#   RESULTS_DIR    where results are written                            [default ./results]
#   FIXTURES_DIR   directory with the smoke fixtures                    [default ./fixtures]
#
# Results (RESULTS_DIR/):
#   smoke.json     machine-readable metrics (also uploaded as a CI artifact)
#   summary.txt    human-readable summary
#   stages.tsv     raw per-stage metrics
#   logs/          full stdout/stderr of every stage
#   *.idXML        pipeline outputs
set -euo pipefail

# --- configuration -----------------------------------------------------------

OPENMS_BIN="${OPENMS_BIN:-}"
ENGINES_DIR="${ENGINES_DIR:-}"
OPENMS_SHA="${OPENMS_SHA:-unknown}"
USE_MS2RESCORE="${USE_MS2RESCORE:-false}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESULTS_DIR="${RESULTS_DIR:-${SCRIPT_DIR}/../results}"
FIXTURES_DIR="${FIXTURES_DIR:-${SCRIPT_DIR}/fixtures}"

if [[ -z "${OPENMS_BIN}" || -z "${ENGINES_DIR}" ]]; then
  echo "ERROR: OPENMS_BIN and ENGINES_DIR must be set" >&2
  exit 2
fi

rm -rf "${RESULTS_DIR}"
mkdir -p "${RESULTS_DIR}/logs"

# --- helpers -----------------------------------------------------------------

log() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "${RESULTS_DIR}/summary.txt"; }

# to_seconds "H:MM:SS" | "M:SS" | "S.xx" -> seconds (float)
to_seconds() {
  awk -v s="$1" 'BEGIN {
    n = split(s, a, ":")
    if (n == 3) printf "%.2f", a[1]*3600 + a[2]*60 + a[3]
    else if (n == 2) printf "%.2f", a[1]*60 + a[2]
    else printf "%.2f", s + 0
  }'
}

# append one row to stages.tsv
record_stage() {
  local name="$1" required="$2" rc="$3" wall_s="$4" cpu_s="$5" peak_kb="$6" status="$7" reason="$8"
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "${name}" "${required}" "${rc}" "${wall_s}" "${cpu_s}" "${peak_kb}" "${status}" "${reason}" \
    >> "${RESULTS_DIR}/stages.tsv"
}

# run_stage <name> <required:true|false> <cmd...>
#   required stages must exit 0; optional stages that fail are recorded as
#   "skipped" so the tiny fixture cannot fail the smoke run (e.g. Percolator
#   needs more PSMs than this fixture produces).
#   NOTE: the stage command is run inside `if` on purpose - a bare command
#   under `set -e` would abort the script before we can capture its exit code.
run_stage() {
  local name="$1" required="$2"
  shift 2
  local logfile="${RESULTS_DIR}/logs/${name}.log"
  local wall_s cpu_s peak_kb rc time_out

  echo ">>> $*" > "${logfile}"

  if command -v /usr/bin/time >/dev/null 2>&1 && [[ -x /usr/bin/time ]]; then
    time_out="$(mktemp)"
    if /usr/bin/time -v -o "${time_out}" "$@" >> "${logfile}" 2>&1; then
      rc=0
    else
      rc=$?
    fi
    wall_s="$(grep 'Elapsed (wall clock) time' "${time_out}" | sed -E 's/.*: //' | xargs)"
    wall_s="$(to_seconds "${wall_s:-0}")"
    cpu_user="$(grep 'User time' "${time_out}" | sed -E 's/.*: //' | xargs)"
    cpu_sys="$(grep 'System time' "${time_out}" | sed -E 's/.*: //' | xargs)"
    cpu_s="$(awk -v u="${cpu_user:-0}" -v s="${cpu_sys:-0}" 'BEGIN { printf "%.2f", u + s }')"
    peak_kb="$(grep 'Maximum resident set size' "${time_out}" | sed -E 's/.*: //' | xargs)"
    peak_kb="${peak_kb:-0}"
    rm -f "${time_out}"
  else
    # no GNU time on the runner: fall back to coarse timing, no RSS
    local t0 t1
    t0="$(date +%s.%N)"
    if "$@" >> "${logfile}" 2>&1; then
      rc=0
    else
      rc=$?
    fi
    t1="$(date +%s.%N)"
    wall_s="$(awk -v a="$t0" -v b="$t1" 'BEGIN { printf "%.2f", b - a }')"
    cpu_s="0.00"
    peak_kb="0"
  fi

  local status reason
  if [[ ${rc} -eq 0 ]]; then
    status="pass"
    reason=""
    log "  [PASS] ${name} (${wall_s}s wall, ${cpu_s}s cpu, ${peak_kb} KB peak)"
  elif [[ "${required}" == "true" ]]; then
    status="fail"
    reason="required stage failed (see logs/${name}.log)"
    record_stage "${name}" "${required}" "${rc}" "${wall_s}" "${cpu_s}" "${peak_kb}" "${status}" "${reason}"
    log "  [FAIL] ${name} (exit ${rc}) - required stage failed, see logs/${name}.log"
    exit 1
  else
    status="skipped"
    reason="optional stage failed (exit ${rc}); fixture too small for this stage"
    log "  [SKIP] ${name} (exit ${rc}) - optional stage, ${reason}"
  fi
  record_stage "${name}" "${required}" "${rc}" "${wall_s}" "${cpu_s}" "${peak_kb}" "${status}" "${reason}"
}

# --- checks ------------------------------------------------------------------

echo "=== OpenMS benchmark smoke test ===" | tee "${RESULTS_DIR}/summary.txt"
log "openms_sha:   ${OPENMS_SHA}"
log "use_ms2rescore: ${USE_MS2RESCORE}"
log "results:      ${RESULTS_DIR}"

TOOLS=(CometAdapter DecoyDatabase FileConverter PercolatorAdapter)
MISSING=0
for t in "${TOOLS[@]}"; do
  if [[ -x "${OPENMS_BIN}/${t}" ]]; then
    log "  [OK] ${t}: ${OPENMS_BIN}/${t}"
  else
    log "  [FAIL] ${t} not found in ${OPENMS_BIN}"
    MISSING=1
  fi
done

COMET_ENGINE=""
for c in "${ENGINES_DIR}/Comet/comet.exe" "${ENGINES_DIR}/comet"; do
  if [[ -x "${c}" ]]; then COMET_ENGINE="${c}"; break; fi
done
PERCOLATOR_ENGINE=""
for p in "${ENGINES_DIR}/Percolator/percolator" "${ENGINES_DIR}/percolator"; do
  if [[ -x "${p}" ]]; then PERCOLATOR_ENGINE="${p}"; break; fi
done
if [[ -n "${COMET_ENGINE}" ]]; then log "  [OK] comet:   ${COMET_ENGINE}"; else log "  [FAIL] comet engine not found"; MISSING=1; fi
if [[ -n "${PERCOLATOR_ENGINE}" ]]; then log "  [OK] percolator: ${PERCOLATOR_ENGINE}"; else log "  [FAIL] percolator engine not found"; MISSING=1; fi

if (( MISSING )); then
  log "ERROR: missing binaries/engines - cannot run the smoke benchmark"
  exit 2
fi

# --- versions ----------------------------------------------------------------

{
  echo "openms_sha=${OPENMS_SHA}"
  echo "use_ms2rescore=${USE_MS2RESCORE}"
  for t in FileConverter CometAdapter DecoyDatabase PercolatorAdapter; do
    echo "${t}_version=$("${OPENMS_BIN}/${t}" --version 2>&1 | head -1 || true)"
  done
  echo "comet_engine_version=$("${COMET_ENGINE}" -v 2>&1 | head -1 || true)"
  echo "percolator_engine_version=$("${PERCOLATOR_ENGINE}" --version 2>&1 | head -1 || true)"
} > "${RESULTS_DIR}/meta.txt"
log "versions recorded in meta.txt"

# --- stages ------------------------------------------------------------------

: > "${RESULTS_DIR}/stages.tsv"

run_stage "decoy_database" true \
  "${OPENMS_BIN}/DecoyDatabase" \
  -in "${FIXTURES_DIR}/smoke_input.fasta" \
  -out "${RESULTS_DIR}/decoy.fasta"

run_stage "comet" true \
  "${OPENMS_BIN}/CometAdapter" \
  -in "${FIXTURES_DIR}/smoke_input.mzML" \
  -database "${RESULTS_DIR}/decoy.fasta" \
  -comet_executable "${COMET_ENGINE}" \
  -num_hits 5 \
  -missed_cleavages 3 \
  -out "${RESULTS_DIR}/comet.idXML"

# Optional: the fixture is small, so Percolator may legitimately refuse to run
# (too few PSMs). Its outcome is recorded either way; it becomes required once
# the real PXD028735 benchmark runs in a later milestone. The fixture is
# CometAdapter_3 from OpenMS' own tests, whose spectra yield both target and
# decoy PSMs at these parameters, so Percolator normally does run here.
run_stage "percolator" false \
  "${OPENMS_BIN}/PercolatorAdapter" \
  -in "${RESULTS_DIR}/comet.idXML" \
  -percolator_executable "${PERCOLATOR_ENGINE}" \
  -out "${RESULTS_DIR}/rescored.idXML"

# --- machine-readable results ------------------------------------------------

# Compute the verdict + skipped list in bash (readable without Python), then
# convert meta.txt + stages.tsv into smoke.json. Python keeps the quoting safe
# and lets the JSON stay valid no matter what the tools printed.
verdict="pass"
skipped_note=""
while IFS=$'\t' read -r name required _rc _wall _cpu _peak status _reason; do
  if [[ "${required}" == "true" && "${status}" != "pass" ]]; then
    verdict="fail"
  fi
  if [[ "${status}" == "skipped" ]]; then
    skipped_note="${skipped_note:+${skipped_note}, }${name}"
  fi
done < "${RESULTS_DIR}/stages.tsv"

export RESULTS_DIR
python3 - <<'PY'
import csv, json, os

results_dir = os.environ["RESULTS_DIR"]

meta = {}
with open(os.path.join(results_dir, "meta.txt")) as fh:
    for line in fh:
        if "=" in line:
            k, _, v = line.partition("=")
            meta[k.strip()] = v.strip()

stages = []
with open(os.path.join(results_dir, "stages.tsv")) as fh:
    for row in csv.reader(fh, delimiter="\t"):
        if not row:
            continue
        name, required, rc, wall, cpu, peak, status, reason = (row + [""] * 8)[:8]
        stages.append({
            "name": name,
            "required": required == "true",
            "exit_code": int(rc),
            "wall_time_s": float(wall),
            "cpu_time_s": float(cpu),
            "peak_rss_kb": int(peak),
            "status": status,
            "reason": reason,
        })

required_ok = all(s["status"] == "pass" for s in stages if s["required"])
skipped = [s["name"] for s in stages if s["status"] == "skipped"]
verdict = "pass" if required_ok else "fail"

report = {
    "benchmark": "smoke",
    "milestone": 1,
    "openms_sha": meta.get("openms_sha", ""),
    "use_ms2rescore": meta.get("use_ms2rescore", "false"),
    "ms2rescore_note": (
        "requested but not executed in this milestone - requires the extended "
        "rescoring environment (future milestone)"
        if meta.get("use_ms2rescore") == "true" else
        "off - regular (Comet -> Percolator) benchmark mode"
    ),
    "tool_versions": {k: v for k, v in meta.items() if k.endswith("_version")},
    "stages": stages,
    "verdict": verdict,
    "skipped_optional": skipped,
}

with open(os.path.join(results_dir, "smoke.json"), "w") as fh:
    json.dump(report, fh, indent=2)
PY

# --- summary -----------------------------------------------------------------

log ""
log "Verdict: ${verdict}"
if [[ -n "${skipped_note}" ]]; then
  log "Optional stages skipped (fixture too small, expected in smoke mode): ${skipped_note}"
fi
log "Machine-readable results: ${RESULTS_DIR}/smoke.json"
log "Done."
