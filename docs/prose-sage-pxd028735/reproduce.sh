#!/usr/bin/env bash
# Verify that this report is exactly what the frozen outputs produce.
# Read-only with respect to results/, logs/, inputs and executables.
set -uo pipefail
ROOT="${1:-$HOME/prose-sage-pxd028735}"
REP="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMP="$(mktemp -d)"
rc=0

echo "== 1/3 frozen artifacts (13 hashes, read-only)"
if ( cd "$ROOT" && sha256sum -c "$REP/hashes.sha256" ); then
  echo "hashes: OK"
else
  echo "hashes: MISMATCH"
  rc=1
fi

echo
echo "== 2/3 regenerate the results tables from the raw outputs"
if python3 "$REP/emit_tables.py" "$ROOT" "$TMP/tables.md"; then
  :
else
  echo "emit_tables.py: FAILED (consistency check or parse error)"
  rc=1
fi
if diff -u "$REP/tables.md" "$TMP/tables.md"; then
  echo "tables: identical to the ones in the report"
else
  echo "tables: DIFFER"
  rc=1
fi

echo
echo "== 3/3 rebuild the report from template + tables"
python3 "$REP/build_report.py" "$REP/EXPERIMENT_REPORT.template.md" \
  "$TMP/tables.md" "$TMP/report.md" || rc=1
if diff -u "$REP/EXPERIMENT_REPORT.md" "$TMP/report.md"; then
  echo "report: identical"
else
  echo "report: DIFFERS"
  rc=1
fi

rm -rf "$TMP"
echo
if [ "$rc" -eq 0 ]; then
  echo "REPRODUCED: hashes, tables and report all match the frozen outputs."
else
  echo "NOT REPRODUCED: see the diffs above."
fi
exit "$rc"
