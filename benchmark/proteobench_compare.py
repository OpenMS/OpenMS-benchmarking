#!/usr/bin/env python3
"""
ProteoBench external-context comparison for OpenMS-benchmarking.

Consumes the vendored, byte-verbatim snapshot of ProteoBench
`quant_lfq_DDA_ion_QExactive` submissions stored in

    benchmark/results/proteobench/external/   (see PROVENANCE.md there)

plus the existing local ProteoBench-scorer result JSONs (Exp-1 / Exp-2), and
produces a machine-readable summary and/or a standalone HTML comparison.

Design constraints (deliberate):
  - stdlib only; no network access; no ProteoBench API/client.
  - The upstream submission JSONs are read as-is and never rewritten; no
    derived per-submission files are created.
  - This tool is independent of the reporting layer's schema conventions: it
    emits ordinary descriptive data with no schema identifier.
  - Descriptive only: no ranking, winner, best, tier, or leaderboard semantics;
    no runtime/memory/hardware comparison.

Data flow:

    external/*.json (46, pinned commit) --\
                                           +--> summary JSON / HTML
    results/tools/proteobench-Exp-*.json --/   (local rows labeled N=6)

Usage:

    python benchmark/proteobench_compare.py \
        [--external-dir DIR] [--local PATH ...]
        [--json-out PATH] [--html-out PATH]
"""

import argparse
import glob
import html
import json
import math
import os
import re
import sys

# ---------------------------------------------------------------------------
# Pinned provenance
# ---------------------------------------------------------------------------

UPSTREAM_REPO = "Proteobench/Results_quant_ion_DDA"
SOURCE_COMMIT = "206f2410bec7c3343471d8fb0fc7f24e516fd196"
MODULE_ID = "quant_lfq_DDA_ion_QExactive"

# ---------------------------------------------------------------------------
# Parameter compatibility filter (agreed criteria)
# ---------------------------------------------------------------------------

FILTER = {
    "precursor_tolerance_ppm_max": 10.0,
    "fragment_tolerance_da_max": 0.05,
    "fragment_tolerance_ppm_max": 50.0,
    "enzymes": ["Trypsin", "Trypsin/P"],
    "max_missed_cleavages": 2,
    "psm_fdr_target": 0.01,
    "psm_fdr_tolerance": 0.001,  # "approximately 0.01"
}

COMPATIBLE = "compatible"
INCOMPATIBLE = "incompatible"
UNKNOWN = "unknown"  # missing/NaN/malformed metadata -> never treated as compatible

# Upstream metric keys exposed per N; values keep upstream names.
N_METRIC_KEYS = (
    "nr_feature",
    "median_abs_epsilon_global",
    "mean_abs_epsilon_global",
    "CV_median",
    "roc_auc",
)

_N_RANGE_RE = re.compile(
    r"^\[\s*(-?[\d.eE+]+)\s*(ppm|Da)\s*,\s*(-?[\d.eE+]+)\s*(ppm|Da)\s*\]$",
    re.IGNORECASE,
)
_N_SINGLE_RE = re.compile(r"^(-?[\d.eE+]+)\s*(ppm|Da)$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Pure parsing helpers
# ---------------------------------------------------------------------------

def usable_number(value):
    """Return value as float if it is a usable finite number, else None.

    None, non-numeric strings, NaN and infinities all map to None so that
    unusable metadata can never silently pass a numeric criterion.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def parse_tolerance(value):
    """Parse an upstream mass-tolerance string.

    Handles the shapes actually present in the snapshot:
      '[-4.5 ppm, 4.5 ppm]'  '[-0.02 Da, 0.02 Da]'  '10.00ppm'  '5 ppm'

    Returns (max_abs_value, unit_lowercase) or None when the value is
    missing, NaN, or malformed. Mixed units in one range -> None.
    """
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() == "nan":
        return None
    match = _N_RANGE_RE.match(text)
    if match:
        low, unit_low, high, unit_high = (
            match.group(1),
            match.group(2).lower(),
            match.group(3),
            match.group(4).lower(),
        )
        if unit_low != unit_high:
            return None
        try:
            low_f, high_f = float(low), float(high)
        except ValueError:
            return None
        return (max(abs(low_f), abs(high_f)), unit_low)
    match = _N_SINGLE_RE.match(text)
    if match:
        try:
            number = float(match.group(1))
        except ValueError:
            return None
        return (abs(number), match.group(2).lower())
    return None


def classify_submission(data):
    """Apply the agreed parameter filter to one upstream submission dict.

    Returns (status, failed_criteria) where status is COMPATIBLE,
    INCOMPATIBLE, or UNKNOWN. Any required metadata that is missing,
    malformed, or NaN yields UNKNOWN regardless of the other fields;
    a submission only reaches COMPATIBLE when every criterion passes.
    """
    precursor = parse_tolerance(data.get("precursor_mass_tolerance"))
    fragment = parse_tolerance(data.get("fragment_mass_tolerance"))
    enzyme = data.get("enzyme")
    enzyme = enzyme.strip() if isinstance(enzyme, str) else None
    miscleavages = usable_number(data.get("allowed_miscleavages"))
    fdr = usable_number(data.get("ident_fdr_psm"))

    # Any required metadata that is missing, malformed, or NaN makes the
    # whole classification UNKNOWN - never silently compatible/incompatible.
    if (
        precursor is None
        or fragment is None
        or enzyme is None
        or miscleavages is None
        or fdr is None
    ):
        return UNKNOWN, ["missing_or_unusable_metadata"]

    failed = []
    if precursor[1] != "ppm" or precursor[0] > FILTER["precursor_tolerance_ppm_max"]:
        failed.append("precursor_tolerance")
    if not (
        (fragment[1] == "da" and fragment[0] <= FILTER["fragment_tolerance_da_max"])
        or (fragment[1] == "ppm" and fragment[0] <= FILTER["fragment_tolerance_ppm_max"])
    ):
        failed.append("fragment_tolerance")
    if enzyme not in FILTER["enzymes"]:
        failed.append("enzyme")
    if miscleavages > FILTER["max_missed_cleavages"]:
        failed.append("missed_cleavages")
    if abs(fdr - FILTER["psm_fdr_target"]) > FILTER["psm_fdr_tolerance"]:
        failed.append("psm_fdr")

    if failed:
        return INCOMPATIBLE, failed
    return COMPATIBLE, []


def flatten_n_metrics(data, n):
    """Extract the exposed metric set from results[str(n)].

    Returns a dict keyed by the exact upstream field names; entries whose
    value is missing/NaN are omitted. Returns an empty dict when the N key
    itself is absent.
    """
    results = data.get("results")
    if not isinstance(results, dict):
        return {}
    bucket = results.get(str(n))
    if not isinstance(bucket, dict):
        return {}
    out = {}
    for key in N_METRIC_KEYS:
        value = usable_number(bucket.get(key))
        if value is not None:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_external_submissions(external_dir):
    """Load every vendored submission JSON.

    Filename/content consistency is enforced: the 40-hex filename stem must
    equal the JSON's `intermediate_hash`. A malformed JSON file becomes an
    UNKNOWN record with reason 'malformed_json' (deterministic handling,
    sorted by filename); it never crashes the run.
    """
    records = []
    paths = sorted(glob.glob(os.path.join(external_dir, "*.json")))
    for path in paths:
        name = os.path.basename(path)
        stem = name[: -len(".json")]
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            records.append(
                {
                    "filename": name,
                    "intermediate_hash": None,
                    "status": UNKNOWN,
                    "failed_criteria": ["malformed_json"],
                    "software": None,
                    "software_version": None,
                    "search_engine": None,
                    "search_engine_version": None,
                    "metrics": {},
                }
            )
            continue
        recorded = data.get("intermediate_hash")
        if not isinstance(recorded, str) or recorded != stem:
            raise ValueError(
                f"filename/intermediate_hash mismatch: {name} "
                f"(expected stem == intermediate_hash, got {recorded!r})"
            )
        status, failed = classify_submission(data)
        records.append(
            {
                "filename": name,
                "intermediate_hash": recorded,
                "status": status,
                "failed_criteria": failed,
                "software": data.get("software_name"),
                "software_version": _string_or_none(data.get("software_version")),
                "search_engine": _string_or_none(data.get("search_engine")),
                "search_engine_version": _string_or_none(data.get("search_engine_version")),
                "metrics": {
                    "1": flatten_n_metrics(data, 1),
                    "3": flatten_n_metrics(data, 3),
                    "6": flatten_n_metrics(data, 6),
                },
            }
        )
    records.sort(key=lambda record: record["filename"])
    return records


def _string_or_none(value):
    if isinstance(value, str):
        return value
    return None


def load_local_results(paths):
    """Load local ProteoBench-scorer result JSONs by their actual field names.

    The local files carry metrics.quantified_precursors, which corresponds to
    ProteoBench nr_feature at N=6 for these six-run results. They are NOT
    historical ProteoBench submissions and are kept separate from the
    external counts.
    """
    rows = []
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        metrics = data.get("metrics") or {}
        tool = data.get("tool") or {}
        pb = data.get("proteobench") or {}
        quantified = usable_number(metrics.get("quantified_precursors"))
        rows.append(
            {
                "label": data.get("label") or data.get("run_id") or os.path.basename(path),
                "dataset": data.get("dataset"),
                "software": tool.get("name"),
                "software_version": tool.get("version"),
                "search_engine": tool.get("search_engine"),
                "search_engine_version": tool.get("search_engine_version"),
                "scorer_version": pb.get("version"),
                "module": pb.get("module"),
                "intermediate_hash": pb.get("intermediate_hash"),
                # quantified_precursors == nr_feature at N=6 for six-run results
                "nr_feature_n6": quantified,
                "median_abs_epsilon": usable_number(metrics.get("median_abs_epsilon")),
                "mean_abs_epsilon": usable_number(metrics.get("mean_abs_epsilon")),
                "cv_median": usable_number(metrics.get("cv_median")),
                "roc_auc": usable_number(metrics.get("roc_auc")),
            }
        )
    rows.sort(key=lambda row: row["label"])
    return rows


# ---------------------------------------------------------------------------
# Summary (deterministic)
# ---------------------------------------------------------------------------

def build_summary(external, local):
    counts = {COMPATIBLE: 0, INCOMPATIBLE: 0, UNKNOWN: 0}
    for record in external:
        counts[record["status"]] += 1
    return {
        "description": (
            "External-context comparison of ProteoBench quant_lfq_DDA_ion_QExactive "
            "submissions with local OpenMS ProteoBench-scorer results. "
            "Descriptive only: no ranking is implied."
        ),
        "source": {
            "upstream_repository": UPSTREAM_REPO,
            "source_commit": SOURCE_COMMIT,
            "module": MODULE_ID,
            "provenance_file": "benchmark/results/proteobench/external/PROVENANCE.md",
        },
        "filter": dict(FILTER),
        "counts": {
            "total": len(external),
            "compatible": counts[COMPATIBLE],
            "incompatible": counts[INCOMPATIBLE],
            "unknown_insufficient_metadata": counts[UNKNOWN],
        },
        "submissions": [
            {
                "filename": record["filename"],
                "intermediate_hash": record["intermediate_hash"],
                "software": record["software"],
                "software_version": record["software_version"],
                "search_engine": record["search_engine"],
                "search_engine_version": record["search_engine_version"],
                "status": record["status"],
                "failed_criteria": record["failed_criteria"],
                "metrics_by_n": record["metrics"],
            }
            for record in external
        ],
        "local_results": [
            {
                "label": row["label"],
                "dataset": row["dataset"],
                "software": row["software"],
                "software_version": row["software_version"],
                "search_engine": row["search_engine"],
                "search_engine_version": row["search_engine_version"],
                "scorer_version": row["scorer_version"],
                "module": row["module"],
                "intermediate_hash": row["intermediate_hash"],
                "kind": "local_proteobench_scorer",
                "note": (
                    "quantified_precursors corresponds to ProteoBench nr_feature "
                    "at N=6 for these six-run results; produced locally with the "
                    "ProteoBench scorer, not a historical ProteoBench submission"
                ),
                "metrics": {
                    "nr_feature_n6": row["nr_feature_n6"],
                    "median_abs_epsilon": row["median_abs_epsilon"],
                    "mean_abs_epsilon": row["mean_abs_epsilon"],
                    "cv_median": row["cv_median"],
                    "roc_auc": row["roc_auc"],
                },
            }
            for row in local
        ],
    }


# ---------------------------------------------------------------------------
# HTML rendering (standalone, deterministic, no ranking semantics)
# ---------------------------------------------------------------------------

_PROVENANCE_FOOTER = f"""
<p><strong>Provenance.</strong> Submissions: {UPSTREAM_REPO} @
<code>{SOURCE_COMMIT}</code> (pinned; 46 byte-verbatim JSONs, see
<code>benchmark/results/proteobench/external/PROVENANCE.md</code> for per-file
SHA256 hashes). Module: <code>{MODULE_ID}</code>.</p>
<p><strong>Caveats.</strong>
(1) Historical submission JSONs contain no database/FASTA provenance, so exact
historical FASTA identity cannot be proven; parameter-compatibility does not
imply database identity.
(2) ProteoBench's canonical HYE FASTA
(<code>ProteoBenchFASTA_MixedSpecies_HYE</code>, member SHA256
<code>d9ac434d&hellip;bf39aa25</code>, 31,889 entries, no decoys) is
byte-identical to the HYE target FASTA used by our pipeline, but our
ProSE/ProSE+PeptDeep workflow additionally appends a SoCe entrapment database
and uses a different identification workflow, so its results are
<strong>contextual, not controlled</strong>, relative to these submissions.
(3) The local rows are produced by our local ProteoBench scorer (not
submissions); their <code>quantified_precursors</code> corresponds to
<code>nr_feature</code> at N=6.
(4) Alphabetical ordering only &mdash; this report makes no comparison of
relative quality and identifies no preferred submission.</p>
"""


def _esc(value):
    return html.escape(str(value)) if value is not None else "&mdash;"


def _num(value, digits=3):
    if value is None:
        return "&mdash;"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return f"{value:,}"


def render_html(summary):
    counts = summary["counts"]
    filt = summary["filter"]

    # Alphabetical by software name then filename; no ranking semantics.
    def sort_key(record):
        return (
            (record.get("software") or "~").lower(),
            record.get("filename") or "",
        )

    compatible = sorted(
        (r for r in summary["submissions"] if r["status"] == COMPATIBLE), key=sort_key
    )
    rest = sorted(
        (r for r in summary["submissions"] if r["status"] != COMPATIBLE), key=sort_key
    )

    parts = []
    parts.append(
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<title>ProteoBench External-Context Comparison</title>\n"
        "<style>"
        "body{font-family:sans-serif;margin:2rem;max-width:75rem;}"
        "table{border-collapse:collapse;margin:1rem 0;width:100%;}"
        "th,td{border:1px solid #ccc;padding:0.3rem 0.5rem;text-align:left;}"
        "th.num,td.num{text-align:right;}"
        "caption{caption-side:top;text-align:left;font-weight:bold;padding:0.4rem 0;}"
        ".muted{color:#555;}"
        "h1{font-size:1.4rem;} h2{font-size:1.1rem;margin-top:1.6rem;}"
        "</style>\n</head>\n<body>\n"
    )
    parts.append("<h1>ProteoBench External-Context Comparison</h1>")
    parts.append(
        f"<p>Module <code>{_esc(MODULE_ID)}</code> &middot; "
        f"PXD028735 (ProteoBench QE HF-X HYE ion-LFQ). "
        f"N=3 is the ProteoBench headline view; N=1 and N=6 are secondary context.</p>"
    )
    parts.append(
        f"<p><strong>Filter summary:</strong> "
        f"{counts['total']} total &middot; {counts['compatible']} compatible &middot; "
        f"{counts['incompatible']} incompatible &middot; "
        f"{counts['unknown_insufficient_metadata']} unknown / insufficient metadata.</p>"
    )
    parts.append(
        "<p class=\"muted\">Filter criteria: precursor &le; "
        f"{_num(filt['precursor_tolerance_ppm_max'], 1)} ppm; fragment &le; "
        f"{_num(filt['fragment_tolerance_da_max'], 2)} Da or &le; "
        f"{_num(filt['fragment_tolerance_ppm_max'], 1)} ppm; enzyme "
        f"{' or '.join(filt['enzymes'])}; missed cleavages &le; "
        f"{filt['max_missed_cleavages']}; PSM FDR &asymp; "
        f"{filt['psm_fdr_target']} (&plusmn;{filt['psm_fdr_tolerance']}). "
        "Missing, malformed, or NaN metadata is classified as unknown, never compatible.</p>"
    )

    # N=3 headline table (compatible submissions only)
    parts.append("<h2>Parameter-compatible submissions &mdash; N=3 headline</h2>")
    parts.append(
        "<table><caption>N=3: quantified in at least 3 of the 6 raw files. "
        "Alphabetical by software.</caption>"
        "<tr><th>Software</th><th>Version</th><th>Search engine</th>"
        "<th class=\"num\">nr_feature (N=3)</th>"
        "<th class=\"num\">median |&epsilon;| (N=3)</th>"
        "<th class=\"num\">mean |&epsilon;| (N=3)</th>"
        "<th class=\"num\">CV median (N=3)</th>"
        "<th class=\"num\">ROC AUC (N=3)</th></tr>"
    )
    for record in compatible:
        metrics = record["metrics_by_n"].get("3", {})
        parts.append(
            f"<tr><td>{_esc(record['software'])}</td>"
            f"<td>{_esc(record['software_version'])}</td>"
            f"<td>{_esc(record['search_engine'])}</td>"
            f"<td class=\"num\">{_num(metrics.get('nr_feature'))}</td>"
            f"<td class=\"num\">{_num(metrics.get('median_abs_epsilon_global'))}</td>"
            f"<td class=\"num\">{_num(metrics.get('mean_abs_epsilon_global'))}</td>"
            f"<td class=\"num\">{_num(metrics.get('CV_median'))}</td>"
            f"<td class=\"num\">{_num(metrics.get('roc_auc'))}</td></tr>"
        )
    # Local rows appended, clearly separated and labeled (N=6 context).
    for row in summary["local_results"]:
        metrics = row["metrics"]
        parts.append(
            f"<tr><td>{_esc(row['software'])} "
            f"<em>(local ProteoBench scorer v{_esc(row['scorer_version'])})</em></td>"
            f"<td>{_esc(row['software_version'])}</td>"
            f"<td>{_esc(row['search_engine'])}</td>"
            f"<td class=\"num\">{_num(metrics.get('nr_feature_n6'))}</td>"
            f"<td class=\"num\">{_num(metrics.get('median_abs_epsilon'))}</td>"
            f"<td class=\"num\">{_num(metrics.get('mean_abs_epsilon'))}</td>"
            f"<td class=\"num\">{_num(metrics.get('cv_median'))}</td>"
            f"<td class=\"num\">{_num(metrics.get('roc_auc'))}</td></tr>"
        )
    parts.append("</table>")
    if summary["local_results"]:
        parts.append(
            "<p class=\"muted\"><em>Rows marked &ldquo;local ProteoBench scorer&rdquo; "
            "are our own six-run OpenMS/quantms results scored locally "
            "(scorer v0.18.2); their nr_feature value is at N=6, not N=3, and they "
            "are not historical ProteoBench submissions.</em></p>"
        )

    # Secondary context: N=1 and N=6 for compatible submissions
    parts.append("<h2>Secondary context &mdash; N=1 and N=6 (compatible submissions)</h2>")
    parts.append(
        "<table><caption>nr_feature at N=1 (quantified in &ge;1 file) and N=6 "
        "(quantified in all 6 files).</caption>"
        "<tr><th>Software</th><th>Version</th>"
        "<th class=\"num\">nr_feature (N=1)</th>"
        "<th class=\"num\">nr_feature (N=6)</th>"
        "<th class=\"num\">median |&epsilon;| (N=6)</th>"
        "<th class=\"num\">ROC AUC (N=6)</th></tr>"
    )
    for record in compatible:
        m1 = record["metrics_by_n"].get("1", {})
        m6 = record["metrics_by_n"].get("6", {})
        parts.append(
            f"<tr><td>{_esc(record['software'])}</td>"
            f"<td>{_esc(record['software_version'])}</td>"
            f"<td class=\"num\">{_num(m1.get('nr_feature'))}</td>"
            f"<td class=\"num\">{_num(m6.get('nr_feature'))}</td>"
            f"<td class=\"num\">{_num(m6.get('median_abs_epsilon_global'))}</td>"
            f"<td class=\"num\">{_num(m6.get('roc_auc'))}</td></tr>"
        )
    parts.append("</table>")

    # Non-compatible submissions with reasons (transparent exclusion)
    if rest:
        parts.append("<h2>Excluded from the compatible view</h2>")
        parts.append(
            "<table><caption>Submissions not shown in the tables above, with the "
            "criterion that failed (or why metadata was unusable).</caption>"
            "<tr><th>Software</th><th>Version</th><th>Status</th><th>Reason</th></tr>"
        )
        for record in rest:
            reason = ", ".join(record["failed_criteria"]) or "&mdash;"
            parts.append(
                f"<tr><td>{_esc(record['software'])}</td>"
                f"<td>{_esc(record['software_version'])}</td>"
                f"<td>{_esc(record['status'])}</td><td>{reason}</td></tr>"
            )
        parts.append("</table>")

    parts.append(_PROVENANCE_FOOTER)
    parts.append("</body>\n</html>\n")
    return "".join(parts)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_EXTERNAL = os.path.join(_SCRIPT_DIR, "results", "proteobench", "external")
_DEFAULT_LOCAL = [
    os.path.join(
        _SCRIPT_DIR, "results", "tools", "proteobench-Exp-1-Comet--Percolator.json"
    ),
    os.path.join(
        _SCRIPT_DIR, "results", "tools", "proteobench-Exp-2-Comet--MS2Rescore--Percolator.json"
    ),
]


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="External-context ProteoBench comparison (descriptive, no ranking)."
    )
    parser.add_argument("--external-dir", default=_DEFAULT_EXTERNAL,
                        help="Directory with the vendored submission JSONs")
    parser.add_argument("--local", action="append", default=None,
                        help="Local ProteoBench-scorer result JSON (repeatable; "
                             "defaults to the Exp-1/Exp-2 tool results)")
    parser.add_argument("--json-out", default=None,
                        help="Write the machine-readable summary JSON here")
    parser.add_argument("--html-out", default=None,
                        help="Write a standalone HTML comparison here")
    args = parser.parse_args(argv)

    external = load_external_submissions(args.external_dir)
    local_paths = args.local if args.local else _DEFAULT_LOCAL
    local = load_local_results([p for p in local_paths if os.path.exists(p)])
    missing = [p for p in local_paths if not os.path.exists(p)]
    for path in missing:
        print(f"note: local result not found, skipped: {path}", file=sys.stderr)

    summary = build_summary(external, local)
    counts = summary["counts"]
    print(
        f"submissions: {counts['total']} total | {counts['compatible']} compatible | "
        f"{counts['incompatible']} incompatible | "
        f"{counts['unknown_insufficient_metadata']} unknown/insufficient"
    )
    print(f"local rows: {len(summary['local_results'])}")

    if args.json_out:
        os.makedirs(os.path.dirname(os.path.abspath(args.json_out)), exist_ok=True)
        with open(args.json_out, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(summary, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
        print(f"summary JSON -> {args.json_out}")
    if args.html_out:
        os.makedirs(os.path.dirname(os.path.abspath(args.html_out)), exist_ok=True)
        with open(args.html_out, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(render_html(summary))
        print(f"HTML -> {args.html_out}")
    if not args.json_out and not args.html_out:
        # No output requested: still deterministic, print only the counts line.
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
