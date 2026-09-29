#!/usr/bin/env bash
# run_prose_benchmark.sh - ProSE + PeptDeep entrapment benchmark (PR #9975).
#
# Runs two arms over the SAME mzML/FASTA/configuration and compares the
# headline outcome metrics:
#
#   baseline   ProSE + Percolator (peptdeep:enable=false)
#   peptdeep   identical, plus PeptDeep MS2+RT prediction features
#              (peptdeep:enable=true, ms2_model/rt_model/instrument)
#
# The fixture (mzML with entrapment proteins in the FASTA) is NOT vendored
# yet: Satyam is still picking the minimal entrapment dataset, so every
# input is configured via environment variables below.
#
# Environment variables:
#   OPENMS_BIN       directory with the built TOPP binaries (build/bin)  [required]
#   PROSE_INPUT      mzML searched in both arms                          [required]
#   PROSE_FASTA      target+entrapment FASTA                             [required]
#   PROSE_ENTRAPMENT_PREFIX  accession prefix marking entrapment proteins
#                    [default ENTRAPMENT_]
#   ENGINES_DIR      directory with the search engines (_thirdparty)     [optional]
#   PERCOLATOR_EXE   Percolator binary; default: ENGINES_DIR lookup      [optional]
#   PROSE_EXE        ProSE binary; default: ${OPENMS_BIN}/ProSE          [optional]
#   PEPTDEEP_MS2_MODEL  PeptDeep MS2 ONNX model                          [default
#                    models/peptdeep_ms2_dynamic.onnx - ProSE resolves a
#                    relative name against <exe>/../share/OpenMS, i.e. the
#                    WITH_ONNX=ON build-tree copy of share/OpenMS/models]
#   PEPTDEEP_RT_MODEL   PeptDeep RT ONNX model
#                    [default models/peptdeep_rt_dynamic.onnx]
#   PEPTDEEP_INSTRUMENT instrument class for the MS2 model [default QE]
#   PROSE_THREADS    threads, identical in both arms                     [default 4]
#   OPENMS_SHA       OpenMS commit SHA that produced the binaries        [recorded]
#   RESULTS_DIR      where results are written                           [default ./results]
#
# Results (RESULTS_DIR/):
#   prose.json       machine-readable metrics (per-arm headline counts +
#                    per-stage wall/cpu/peak-RSS under /usr/bin/time -v)
#   summary.txt      human-readable log
#   stages.tsv       raw per-stage metrics
#   logs/            full stdout/stderr of every stage
#   baseline.idXML / peptdeep.idXML     per-arm outputs
#   baseline_summary.yaml / peptdeep_summary.yaml  ProSE -summary_out
#                   diagnostics (NOT used for headline metrics: no RSS,
#                   Percolator/ONNX coverage unverified)
set -euo pipefail

# --- configuration -----------------------------------------------------------

OPENMS_BIN="${OPENMS_BIN:-}"
PROSE_INPUT="${PROSE_INPUT:-}"
PROSE_FASTA="${PROSE_FASTA:-}"
PROSE_ENTRAPMENT_PREFIX="${PROSE_ENTRAPMENT_PREFIX:-ENTRAPMENT_}"
ENGINES_DIR="${ENGINES_DIR:-}"
PERCOLATOR_EXE="${PERCOLATOR_EXE:-}"
PROSE_EXE="${PROSE_EXE:-${OPENMS_BIN}/ProSE}"
PEPTDEEP_MS2_MODEL="${PEPTDEEP_MS2_MODEL:-models/peptdeep_ms2_dynamic.onnx}"
PEPTDEEP_RT_MODEL="${PEPTDEEP_RT_MODEL:-models/peptdeep_rt_dynamic.onnx}"
PEPTDEEP_INSTRUMENT="${PEPTDEEP_INSTRUMENT:-QE}"
PROSE_THREADS="${PROSE_THREADS:-4}"
OPENMS_SHA="${OPENMS_SHA:-unknown}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESULTS_DIR="${RESULTS_DIR:-${SCRIPT_DIR}/../results}"

if [[ -z "${OPENMS_BIN}" || -z "${PROSE_INPUT}" || -z "${PROSE_FASTA}" ]]; then
  echo "ERROR: OPENMS_BIN, PROSE_INPUT and PROSE_FASTA must be set" >&2
  exit 2
fi
for f in "${PROSE_INPUT}" "${PROSE_FASTA}" "${PROSE_EXE}"; do
  if [[ ! -e "${f}" ]]; then
    echo "ERROR: not found: ${f}" >&2
    exit 2
  fi
done

# Percolator executable: explicit, or the same ENGINES_DIR lookup the other
# runners use. ProSE only needs the argument to trigger its PercolatorAdapter
# rescoring (in-process backend), but it must be a real path.
if [[ -z "${PERCOLATOR_EXE}" && -n "${ENGINES_DIR}" ]]; then
  for p in "${ENGINES_DIR}/Percolator/percolator" "${ENGINES_DIR}/percolator"; do
    if [[ -x "${p}" ]]; then PERCOLATOR_EXE="${p}"; break; fi
  done
fi
if [[ -z "${PERCOLATOR_EXE}" || ! -e "${PERCOLATOR_EXE}" ]]; then
  echo "ERROR: PERCOLATOR_EXE not set/found (set it or pass ENGINES_DIR)" >&2
  exit 2
fi

rm -rf "${RESULTS_DIR}"
mkdir -p "${RESULTS_DIR}/logs"

# --- helpers (same conventions as run_openswath_benchmark.sh) -----------------

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
    wall_s="$(awk -v a="${t0}" -v b="${t1}" 'BEGIN { printf "%.2f", b - a }')"
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

echo "=== OpenMS benchmark: ProSE + PeptDeep entrapment ===" | tee "${RESULTS_DIR}/summary.txt"
log "openms_sha:      ${OPENMS_SHA}"
log "input:           ${PROSE_INPUT}"
log "fasta:           ${PROSE_FASTA}"
log "entrapment:      accessions starting with '${PROSE_ENTRAPMENT_PREFIX}'"
log "peptdeep models: ${PEPTDEEP_MS2_MODEL} / ${PEPTDEEP_RT_MODEL} (${PEPTDEEP_INSTRUMENT})"
log "threads:         ${PROSE_THREADS}"
log "search args:     ${PROSE_EXTRA_SEARCH_ARGS:-<none>}"
log "results:         ${RESULTS_DIR}"

if [[ ! -x "${PROSE_EXE}" ]]; then
  log "  [FAIL] ProSE not found/executable: ${PROSE_EXE}"
  exit 2
fi
log "  [OK] ProSE: ${PROSE_EXE}"
log "  [OK] Percolator: ${PERCOLATOR_EXE}"

# --- versions ----------------------------------------------------------------

{
  echo "openms_sha=${OPENMS_SHA}"
  echo "prose_input=${PROSE_INPUT}"
  echo "prose_fasta=${PROSE_FASTA}"
  echo "entrapment_prefix=${PROSE_ENTRAPMENT_PREFIX}"
  echo "peptdeep_ms2_model=${PEPTDEEP_MS2_MODEL}"
  echo "peptdeep_rt_model=${PEPTDEEP_RT_MODEL}"
  echo "peptdeep_instrument=${PEPTDEEP_INSTRUMENT}"
  echo "threads=${PROSE_THREADS}"
  echo "prose_extra_search_args=${PROSE_EXTRA_SEARCH_ARGS:-}"
  echo "prose_version=$("${PROSE_EXE}" --version 2>&1 | head -1 || true)"
} > "${RESULTS_DIR}/meta.txt"
log "versions recorded in meta.txt"

# --- stages ------------------------------------------------------------------

: > "${RESULTS_DIR}/stages.tsv"

# Shared search configuration. Both arms get the exact same arguments apart
# from the four peptdeep:* flags; search-parameter tuning is the caller's job
# (via PROSE_EXTRA_SEARCH_ARGS) and applies to both arms identically.
COMMON_ARGS=(
  -in "${PROSE_INPUT}"
  -database "${PROSE_FASTA}"
  -percolator_executable "${PERCOLATOR_EXE}"
  -threads "${PROSE_THREADS}"
  -no_summary
  ${PROSE_EXTRA_SEARCH_ARGS:-}
)

# Arm 1: baseline (Percolator only). peptdeep:enable is false by default;
# simply omit the flag (its presence on the command line would mean "true":
# OpenMS maps a string param defaulting to "false" with valid_strings
# {true,false} to a no-value CLI flag, see TOPPBase::paramEntryToParameterInformation_).
run_stage "prose_baseline" true \
  "${PROSE_EXE}" "${COMMON_ARGS[@]}" \
  -out_idxml "${RESULTS_DIR}/baseline.idXML" \
  -summary_out "${RESULTS_DIR}/baseline_summary.yaml"

# Arm 2: same search + PeptDeep MS2/RT prediction features. A relative model
# name is resolved by ProSE against <exe>/../share/OpenMS (see design 2).
run_stage "prose_peptdeep" true \
  "${PROSE_EXE}" "${COMMON_ARGS[@]}" \
  -out_idxml "${RESULTS_DIR}/peptdeep.idXML" \
  -summary_out "${RESULTS_DIR}/peptdeep_summary.yaml" \
  -Search:peptdeep:enable \
  -Search:peptdeep:ms2_model "${PEPTDEEP_MS2_MODEL}" \
  -Search:peptdeep:rt_model "${PEPTDEEP_RT_MODEL}" \
  -Search:peptdeep:instrument "${PEPTDEEP_INSTRUMENT}"

# --- machine-readable results ------------------------------------------------

export RESULTS_DIR
export PROSE_ENTRAPMENT_PREFIX
SCRIPT_DIR_EXPORT="${SCRIPT_DIR}"
export SCRIPT_DIR_EXPORT
python3 - <<'PY'
import csv, datetime, json, os, sys

results_dir = os.environ["RESULTS_DIR"]
sys.path.insert(0, os.path.join(os.environ["SCRIPT_DIR_EXPORT"]))
import prose_metrics as pm

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

prefix = os.environ["PROSE_ENTRAPMENT_PREFIX"]

def arm_metrics(name, idxml):
    try:
        parsed = pm.parse_idxml(idxml, prefix)
        counts = pm.count_psms(parsed)
    except pm.ProseMetricsError as e:
        # An empty/structurally unusable idXML (e.g. the search found no PSMs
        # at all) is a benchmark failure, not a crash: record it, fail the
        # verdict below, and keep the runner's outputs complete.
        counts = {
            "error": str(e),
            "q_threshold": pm.Q_THRESHOLD,
            "psms_total": 0,
            "target_psms_at_1pct_fdr": 0,
            "target_peptides_at_1pct_fdr": 0,
            "entrapment_psms_at_1pct_fdr": 0,
            "entrapment_peptides_at_1pct_fdr": 0,
        }
    counts["wall_time_s"] = 0.0   # filled from the arm's stage below
    counts["peak_rss_mb"] = 0.0
    return counts

arms = {}
for name, idxml, stage in (
    ("baseline", "baseline.idXML", "prose_baseline"),
    ("peptdeep", "peptdeep.idXML", "prose_peptdeep"),
):
    path = os.path.join(results_dir, idxml)
    counts = arm_metrics(name, path)
    st = next(s for s in stages if s["name"] == stage)
    counts["wall_time_s"] = st["wall_time_s"]
    counts["peak_rss_mb"] = round(st["peak_rss_kb"] / 1024.0, 1)
    # Guard: the peptdeep arm must really have run PeptDeep. A build without
    # ONNX would otherwise silently produce a baseline-equivalent result.
    if name == "peptdeep":
        missing = pm.check_peptdeep_features(path)
        counts["peptdeep_features_missing"] = missing
    arms[name] = counts

required_ok = all(s["status"] == "pass" for s in stages if s["required"])
arms_usable = all("error" not in arms[a] for a in ("baseline", "peptdeep"))
peptdeep_ran = not arms["peptdeep"].get("peptdeep_features_missing")
verdict = "pass" if required_ok and arms_usable and peptdeep_ran else "fail"

report = {
    "schema": "openms-benchmarking/report/v2",
    "benchmark": "prose_peptdeep",
    "arms": arms,
    "stages": stages,
    "verdict": verdict,
    "openms_sha": meta.get("openms_sha", ""),
    "configuration": {
        "input": meta.get("prose_input", ""),
        "fasta": meta.get("prose_fasta", ""),
        "entrapment_prefix": meta.get("entrapment_prefix", ""),
        "peptdeep_ms2_model": meta.get("peptdeep_ms2_model", ""),
        "peptdeep_rt_model": meta.get("peptdeep_rt_model", ""),
        "peptdeep_instrument": meta.get("peptdeep_instrument", ""),
        "threads": meta.get("threads", ""),
        "prose_extra_search_args": meta.get("prose_extra_search_args", ""),
    },
    "run_id": os.environ.get("GITHUB_RUN_ID", "local"),
    "run_at": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    "tool_versions": {k: v for k, v in meta.items() if k.endswith("_version")},
}

with open(os.path.join(results_dir, "prose.json"), "w") as fh:
    json.dump(report, fh, indent=2)
PY

# --- summary -----------------------------------------------------------------

verdict="pass"
while IFS=$'\t' read -r name required _rc _wall _cpu _peak status _reason; do
  if [[ "${required}" == "true" && "${status}" != "pass" ]]; then
    verdict="fail"
  fi
done < "${RESULTS_DIR}/stages.tsv"

if python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1]))["verdict"] == "pass" else 1)' "${RESULTS_DIR}/prose.json" 2>/dev/null; then
  : # prose.json verdict already combines stage + peptdeep-feature outcomes
else
  verdict="fail"
fi

log ""
log "Verdict: ${verdict}"
log "Machine-readable results: ${RESULTS_DIR}/prose.json"
if [[ "${verdict}" == "pass" ]]; then
  log "Done."
else
  log "Done with failures - see logs/ and prose.json."
  exit 1
fi
