#!/usr/bin/env bash
# run_openswath_benchmark.sh - DIA benchmark for the OpenMS OpenSwathWorkflow.
#
# Purpose: benchmark the OpenSwath/DIA workflow, complementing the DDA smoke
# benchmark and the PXD028735/ProteoBench benchmark.
#
# Environment variables:
#   OPENMS_BIN     directory with the built TOPP binaries (build/bin)   [required]
#   OPENMS_SHA     OpenMS commit SHA that produced the binaries         [recorded]
#   RESULTS_DIR    where results are written                            [default ./results]
#   FIXTURES_DIR   directory with the OpenSwath fixtures                [default ./fixtures]
#
# Results (RESULTS_DIR/):
#   openswath.json     machine-readable metrics
#   summary.txt        human-readable summary
#   stages.tsv         raw per-stage metrics
#   logs/              full stdout/stderr of every stage
#   *.featureXML      pipeline outputs
#   *.chrom.mzML       extracted chromatograms
#   *.json             QC metrics
set -euo pipefail

# --- configuration -----------------------------------------------------------

OPENMS_BIN="${OPENMS_BIN:-}"
OPENMS_SHA="${OPENMS_SHA:-unknown}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESULTS_DIR="${RESULTS_DIR:-${SCRIPT_DIR}/../results}"
FIXTURES_DIR="${FIXTURES_DIR:-${SCRIPT_DIR}/fixtures}"

if [[ -z "${OPENMS_BIN}" ]]; then
  echo "ERROR: OPENMS_BIN must be set" >&2
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
#   "skipped".
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
    reason="optional stage failed (exit ${rc})"
    log "  [SKIP] ${name} (exit ${rc}) - optional stage, ${reason}"
  fi
  record_stage "${name}" "${required}" "${rc}" "${wall_s}" "${cpu_s}" "${peak_kb}" "${status}" "${reason}"
}

# --- checks ------------------------------------------------------------------

echo "=== OpenMS benchmark: OpenSwath DIA ===" | tee "${RESULTS_DIR}/summary.txt"
log "openms_sha:   ${OPENMS_SHA}"
log "results:      ${RESULTS_DIR}"

# Check required tool
if [[ -x "${OPENMS_BIN}/OpenSwathWorkflow" ]]; then
  log "  [OK] OpenSwathWorkflow: ${OPENMS_BIN}/OpenSwathWorkflow"
else
  log "  [FAIL] OpenSwathWorkflow not found in ${OPENMS_BIN}"
  exit 2
fi

# --- versions ----------------------------------------------------------------

{
  echo "openms_sha=${OPENMS_SHA}"
  echo "openswath_version=$("${OPENMS_BIN}/OpenSwathWorkflow" --version 2>&1 | head -1 || true)"
} > "${RESULTS_DIR}/meta.txt"
log "versions recorded in meta.txt"

# --- stages ------------------------------------------------------------------

: > "${RESULTS_DIR}/stages.tsv"

# Stage 1: Run OpenSwathWorkflow
run_stage "openswath_workflow" true \
  "${OPENMS_BIN}/OpenSwathWorkflow" \
  -in "${FIXTURES_DIR}/OpenSwathWorkflow_1_input.mzML" \
  -tr "${FIXTURES_DIR}/OpenSwathWorkflow_1_input.TraML" \
  -Calibration:rt_norm "${FIXTURES_DIR}/OpenSwathWorkflow_1_input.trafoXML" \
  -out_chrom "${RESULTS_DIR}/output.chrom.mzML" \
  -out_features "${RESULTS_DIR}/output.featureXML" \
  -out_qc "${RESULTS_DIR}/output.json"

# --- machine-readable results ------------------------------------------------

verdict="pass"
while IFS=$'\t' read -r name required _rc _wall _cpu _peak status _reason; do
  if [[ "${required}" == "true" && "${status}" != "pass" ]]; then
    verdict="fail"
  fi
done < "${RESULTS_DIR}/stages.tsv"

export RESULTS_DIR
python3 - <<'PY'
import csv, json, os
import xml.etree.ElementTree as ET

results_dir = os.environ["RESULTS_DIR"]

# Read meta
meta = {}
with open(os.path.join(results_dir, "meta.txt")) as fh:
    for line in fh:
        if "=" in line:
            k, _, v = line.partition("=")
            meta[k.strip()] = v.strip()

# Read stages
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

# Parse featureXML to extract correctness metrics
feature_count = 0
overall_qualities = []
intensities = []
featurexml_path = os.path.join(results_dir, "output.featureXML")
if os.path.exists(featurexml_path):
    tree = ET.parse(featurexml_path)
    root = tree.getroot()
    # Count top-level features
    feature_list = root.find("featureList")
    if feature_list is not None:
        features = feature_list.findall("feature")
        feature_count = len(features)
        for feat in features:
            oq = feat.find("overallquality")
            if oq is not None and oq.text:
                try:
                    overall_qualities.append(float(oq.text))
                except ValueError:
                    pass
            intensity = feat.find("intensity")
            if intensity is not None and intensity.text:
                try:
                    intensities.append(float(intensity.text))
                except ValueError:
                    pass

# Parse QC JSON
qc_metrics = {}
qc_path = os.path.join(results_dir, "output.json")
if os.path.exists(qc_path):
    with open(qc_path) as fh:
        qc_metrics = json.load(fh)

required_ok = all(s["status"] == "pass" for s in stages if s["required"])
verdict = "pass" if required_ok else "fail"

report = {
    "schema": "openms-benchmarking/report/v1",
    "source": "openms",
    "benchmark": "openswath_dia",
    "milestone": 2,
    "run_id": os.environ.get("GITHUB_RUN_ID", "local"),
    "run_at": __import__("datetime").datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    "openms_sha": meta.get("openms_sha", ""),
    "cache": "none",
    "dataset": "OpenSwathWorkflow_1 (DIA, 7 peptides, 5 SWATH windows)",
    "stages": stages,
    "verdict": verdict,
    "correctness": {
        "expected_features": 6,
        "actual_features": feature_count,
        "features_match": feature_count == 6,
        "overall_quality_sum": round(sum(overall_qualities), 6) if overall_qualities else 0,
        "total_intensity": round(sum(intensities), 2) if intensities else 0,
        "qc_charge_distribution": qc_metrics.get("ChargeDistributionMS1", []),
    },
    "tool_versions": {k: v for k, v in meta.items() if k.endswith("_version")},
}

with open(os.path.join(results_dir, "openswath.json"), "w") as fh:
    json.dump(report, fh, indent=2)
PY

# --- summary -----------------------------------------------------------------

log ""
log "Verdict: ${verdict}"
log "Machine-readable results: ${RESULTS_DIR}/openswath.json"
log "Done."
