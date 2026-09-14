#!/usr/bin/env bash
# prepare_pxd028735_inputs.sh - generate the Phase-2 ProSE entrapment inputs.
#
# Dataset: PXD028735 (Van Puyvelde et al. 2022, Sci Data; ProteoBench LFQ-DDA
# Q Exactive HF-X mixed-species HYE module). License: CC0 (PRIDE v3 API).
# The two validated Phase-2a pilot runs (Condition A and B, replicate Alpha_01)
# are downloaded from the OpenMS-hosted benchmark server; the combined
# target + entrapment FASTA is assembled deterministically.
#
# Creates (deterministically; nothing is vendored or committed):
#   <out>/LFQ_Orbitrap_DDA_Condition_{A,B}_Sample_Alpha_01.mzML
#   <out>/entrapment_hye.fasta     HYE targets + ENTRAPMENT_-prefixed real
#                                  S. cellulosum So ce56 targets, via
#                                  make_hye_entrapment_fasta.py (the source
#                                  must be decoy-free - ProSE generates decoys
#                                  itself via -Search:decoys auto)
#   <out>/PROVENANCE.txt           dataset accession, source URLs, sizes,
#                                  sha256 of every input/output, parameters
#
# Usage: prepare_pxd028735_inputs.sh <openms-checkout> [out-dir] [runs]
#   <openms-checkout>  source tree at the pinned commit (for the SoCe FASTA)
#   [out-dir]          default: benchmark/generated/prose-pxd028735 (ignored)
#   [runs]             space-separated run letters, default "A B"
#
# Integrity: each downloaded file is verified against a pinned byte size and
# (for the mzML runs) a pinned sha256; any mismatch aborts before any
# benchmark can consume the inputs. Determinism: the FASTA generator is
# order-deterministic and downloads are content-pinned.
set -euo pipefail

OPENMS_ROOT="${1:?usage: prepare_pxd028735_inputs.sh <openms-checkout> [out-dir] [runs]}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${2:-${HERE}/generated/prose-pxd028735}"
RUNS="${3:-A B}"

BASE_URL="https://archive.openms.de/openms/benchmarks/pride-benchmarks/lfq/QExactiveHF/ProteoBench_Module_2"
HYE_URL="${BASE_URL}/ProteoBenchFASTA_MixedSpecies_HYE.fasta"
HYE_SHA256="d9ac434d88492c10c8e9a587ee7dbc9480fa0995fa07a6ba35a7da8abf39aa25"
HYE_SIZE=16670994
SOCE="doc/pyopenms/src/data/18Protein_SoCe_Tr_detergents_trace.fasta"
SOCE_SHA256="714d53edaf768c5162715cae974cb3fa040477879bd999a45c28c66712a04ca8"

# Pinned mzML runs: sha256 (from the Phase-2a pilot) + byte size (Content-Length).
RUN_SHA_A="d044284fbb07ea14c3f2aa28b2f94f932d76cabba59f9a6238968f3fcda1d083"
RUN_SIZE_A=478111386
RUN_SHA_B="ba2a1ba0ba0571ceb94bfe7bf8d5f43f6dd5fb5b508fcd6f4c1c52fbe18dfacf"
RUN_SIZE_B=484687461

command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 not found" >&2; exit 2; }
command -v curl >/dev/null 2>&1 || { echo "ERROR: curl not found" >&2; exit 2; }
command -v gunzip >/dev/null 2>&1 || { echo "ERROR: gunzip not found" >&2; exit 2; }

SOCE_PATH="${OPENMS_ROOT}/${SOCE}"
[[ -f "${SOCE_PATH}" ]] || {
  echo "ERROR: missing S. cellulosum FASTA: ${SOCE_PATH}" >&2
  echo "       (expected in the OpenMS checkout at the pinned commit)" >&2
  exit 2
}

mkdir -p "${OUT_DIR}"

fetch() { # fetch <url> <out-path> <expected-size> [expected-sha256]
  local url="$1" out="$2" size="$3" sha="${4:-}"
  if [[ -f "${out}" ]]; then
    local have
    have=$(stat -c%s "${out}")
    if [[ "${have}" == "${size}" && -z "${sha}" ]]; then
      echo "  [cached] $(basename "${out}") (${have} bytes)"
      return 0
    fi
    if [[ "${have}" == "${size}" && -n "${sha}" ]] \
       && echo "${sha}  ${out}" | sha256sum -c - >/dev/null 2>&1; then
      echo "  [cached, verified] $(basename "${out}")"
      return 0
    fi
    echo "  [re-downloading: size/hash mismatch] $(basename "${out}")" >&2
    rm -f "${out}"
  fi
  echo "  [download] $(basename "${out}") (${size} bytes)"
  curl -sS --retry 3 --retry-delay 5 --fail -o "${out}" "${url}"
  local have
  have=$(stat -c%s "${out}")
  [[ "${have}" == "${size}" ]] || {
    echo "ERROR: $(basename "${out}"): size ${have} != pinned ${size}" >&2
    rm -f "${out}"
    exit 3
  }
  if [[ -n "${sha}" ]]; then
    echo "${sha}  ${out}" | sha256sum -c - || {
      echo "ERROR: $(basename "${out}"): sha256 mismatch" >&2
      rm -f "${out}"
      exit 3
    }
  fi
}

echo "=== PXD028735 inputs -> ${OUT_DIR} ==="

# 1. HYE target FASTA
fetch "${HYE_URL}" "${OUT_DIR}/ProteoBenchFASTA_MixedSpecies_HYE.fasta" \
  "${HYE_SIZE}" "${HYE_SHA256}"

# 2. selected mzML runs (download .gz, verify, decompress; keep the .gz)
declare -A RUN_SHA=( [A]=${RUN_SHA_A} [B]=${RUN_SHA_B} )
declare -A RUN_SIZE=( [A]=${RUN_SIZE_A} [B]=${RUN_SIZE_B} )
for R in ${RUNS}; do
  case "${R}" in
    A|B) : ;;
    *) echo "ERROR: unknown run '${R}' (valid: A B)" >&2; exit 2 ;;
  esac
  GZ="LFQ_Orbitrap_DDA_Condition_${R}_Sample_Alpha_01.mzML.gz"
  MZML="${GZ%.gz}"
  fetch "${BASE_URL}/${GZ}" "${OUT_DIR}/${GZ}" "${RUN_SIZE[${R}]}" "${RUN_SHA[${R}]}"
  if [[ ! -f "${OUT_DIR}/${MZML}" ]] || [[ "${OUT_DIR}/${GZ}" -nt "${OUT_DIR}/${MZML}" ]]; then
    echo "  [gunzip] ${MZML}"
    gunzip -kc "${OUT_DIR}/${GZ}" > "${OUT_DIR}/${MZML}"
  fi
done

# 3. combined target + entrapment FASTA (fails closed on decoy-marked sources)
echo "  [fasta] entrapment_hye.fasta"
python3 "${HERE}/make_hye_entrapment_fasta.py" \
  --hye "${OUT_DIR}/ProteoBenchFASTA_MixedSpecies_HYE.fasta" \
  --soce "${SOCE_PATH}" \
  --out "${OUT_DIR}/entrapment_hye.fasta" --force

# 4. provenance record
{
  echo "# generated by benchmark/prepare_pxd028735_inputs.sh (temporary, not vendored)"
  echo "dataset=PXD028735 (ProteoBench LFQ-DDA Q Exactive HF-X mixed-species HYE module)"
  echo "publication=Van Puyvelde et al. 2022, Scientific Data, doi:10.1038/s41597-022-01216-6"
  echo "license=CC0 (Creative Commons Public Domain; PRIDE v3 API, https://www.ebi.ac.uk/pride/ws/archive/v3/projects/PXD028735)"
  echo "runs=${RUNS}"
  echo "openms_commit=${OPENMS_SHA_OVERRIDE:-$(git -C "${OPENMS_ROOT}" rev-parse HEAD 2>/dev/null || echo unknown)}"
  echo "hye_fasta_url=${HYE_URL}"
  echo "hye_fasta_sha256=${HYE_SHA256}"
  echo "soce_fasta=${SOCE}"
  echo "soce_fasta_sha256=${SOCE_SHA256}"
  echo "base_url=${BASE_URL}"
  for R in ${RUNS}; do
    GZ="LFQ_Orbitrap_DDA_Condition_${R}_Sample_Alpha_01.mzML.gz"
    echo "run_${R}_url=${BASE_URL}/${GZ}"
    echo "run_${R}_sha256=${RUN_SHA[${R}]}"
    echo "run_${R}_size=${RUN_SIZE[${R}]}"
  done
  echo "# all files below, sha256:"
  ( cd "${OUT_DIR}" && sha256sum ProteoBenchFASTA_MixedSpecies_HYE.fasta entrapment_hye.fasta \
      LFQ_Orbitrap_DDA_Condition_*_Sample_Alpha_01.mzML.gz \
      LFQ_Orbitrap_DDA_Condition_*_Sample_Alpha_01.mzML )
  echo "# search parameters (applied identically to both arms by run_prose_benchmark.sh):"
  echo "search_params=precursor 10 ppm (ProSE default, published); fragment 0.02 Da (published; passed via PROSE_EXTRA_SEARCH_ARGS); closed search; decoys auto"
  echo "entrapment_design=absent-organism S. cellulosum So ce56 targets, ENTRAPMENT_-prefixed; shared tryptic peptides count as entrapment (conservative)"
  echo "generated_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
} > "${OUT_DIR}/PROVENANCE.txt"

echo "generated inputs in ${OUT_DIR}:"
ls -la "${OUT_DIR}"
