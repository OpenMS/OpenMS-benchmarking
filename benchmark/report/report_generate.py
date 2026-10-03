#!/usr/bin/env python3
"""
OpenMS benchmark report generator (Issue #8788).

Turns machine-readable benchmark results into one self-contained HTML report:

    benchmark run  ->  JSON result  ->  report_generate.py render  ->  report.html

The generator deliberately knows nothing about specific runs: it discovers
every result JSON under <results-dir>/{openms,tools}/ or <results-dir>/<benchmark>/
and renders whatever metrics the current run and the stored baseline actually
share.  That keeps the same machinery usable for OpenMS version-vs-version
comparisons, cold-vs-warm builds, ProteoBench results, OpenSwath results,
and eventually results uploaded by other tools.

Result layout (schema "openms-benchmarking/report/v2"):

    results/openms/<run-id>.json    OpenMS benchmark runs (smoke today)
    results/tools/<tool-id>.json    reference results from other tools/sources
                                    (e.g. the local ProteoBench scorer)
    results/<benchmark>/openms/*.json     v2 layout: OpenMS runs
    results/<benchmark>/reference/*.json  v2 layout: tool results

The renderer reads exclusively from v2 schema fields:

    identity   – benchmark, dataset, software, configuration
    run        – run_id, run_at, cache
    performance – build, stages[], wall/cpu/rss
    metrics    – flat bag of name/value pairs (compared dynamically)
    correctness – optional validation data
    tool       – optional external software metadata

Subcommands:

    normalize smoke <smoke.json> --run-id ID --cache cold|warm|none
                    --build-time SECONDS --artifact-bytes N
                    [--run-at ISO8601] [--out PATH]
        Convert a raw CI smoke.json into a normalized OpenMS run result.

    normalize openswath <openswath.json> --run-id ID
                    --build-time SECONDS --artifact-bytes N
                    [--cache cold|warm|none] [--run-at ISO8601] [--out PATH]
        Convert a raw CI openswath.json (OpenSwath DIA benchmark) into a
        normalized OpenMS run result.  Correctness metrics from the raw result
        are carried into the v2 correctness section unchanged.

    normalize proteobench <local-proteobench.json> --label LABEL [--out PATH]
        Convert a local ProteoBench scoring result into a tool result.

    render [--results-dir DIR] [--current PATH] [--baseline RUN-ID|SHA|PATH]
           [--no-sha-baseline] [--out report.html]
        Discover all results, compare the current run against the stored
        baseline, and render report.html.

        Baseline selection: an explicit --baseline wins; otherwise the default
        is the latest stored run of the same benchmark whose OpenMS version is
        an ancestor of the current run's version (resolved via the GitHub
        compare API, cached per render). If no ancestor run is known, or
        ancestry cannot be resolved (offline, rate-limited), it falls back to
        the previous timestamp rule; --no-sha-baseline disables the ancestry
        step entirely.
"""

import argparse
import csv
import datetime as _dt
import glob
import html
import json
import math
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

SCHEMA_V1 = "openms-benchmarking/report/v1"
SCHEMA_V2 = "openms-benchmarking/report/v2"


# ---------------------------------------------------------------------------
# Runtime provenance (Stage 1: package-vs-source).
#
# identity.runtime is ADDITIVE in the v2 schema: source-mode results carry
# {"runtime_source": "source"} and are otherwise byte-identical to the
# pre-Stage-1 output. Package-mode results additionally carry a "package"
# object (filename, sha256, archive_dir, date) so a packaged run can never be
# mistaken for an exact-SHA source build in the report.
# ---------------------------------------------------------------------------
def _add_runtime_source_args(parser):
    """Attach the shared runtime-provenance options to a normalize subparser."""
    parser.add_argument("--runtime-source", choices=["source", "package"],
                        default="source",
                        help="where the benchmarked OpenMS binaries came from")
    parser.add_argument("--package-filename", default="",
                        help="package mode: .deb filename as published")
    parser.add_argument("--package-sha256", default="",
                        help="package mode: SHA256 of the .deb")
    parser.add_argument("--package-archive-dir", default="",
                        help="package mode: nightly archive upload-day directory")
    parser.add_argument("--package-date", default="",
                        help="package mode: build night encoded in the package")


def _runtime_identity(args):
    """Build the identity.runtime object from normalize args (None when absent)."""
    src = getattr(args, "runtime_source", "source") or "source"
    runtime = {"runtime_source": src}
    if src == "package":
        pkg = {
            "filename": getattr(args, "package_filename", ""),
            "sha256": getattr(args, "package_sha256", ""),
            "archive_dir": getattr(args, "package_archive_dir", ""),
            "date": getattr(args, "package_date", ""),
        }
        missing = [k for k, v in pkg.items() if not v]
        if missing:
            sys.exit("normalize package-mode run missing required package metadata: "
                     + ", ".join("--package-" + m.replace("_", "-") for m in missing))
        runtime["package"] = pkg
    return runtime

SCHEMA = SCHEMA_V2  # default output schema

# ---------------------------------------------------------------------------
# metric helpers
# ---------------------------------------------------------------------------


def fmt_seconds(s):
    """123.0 -> '2m 03s'; 4581 -> '1h 16m 21s'."""
    s = float(s)
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = int(round(s % 60))
    if h:
        return f"{h}h {m:02d}m {sec:02d}s"
    if m:
        return f"{m}m {sec:02d}s"
    if s < 10:
        return f"{s:.1f}s"
    return f"{s:.0f}s"


def fmt_bytes(b):
    b = float(b)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if b < 1024 or unit == "GiB":
            return f"{b:.0f} {unit}" if unit == "B" else f"{b:.1f} {unit}"
        b /= 1024


def fmt_delta(cur, prev, numeric):
    """Render the Δ cell of one comparison row."""
    if cur is None or prev is None:
        return "—"
    if not numeric:
        return "same" if cur == prev else f"{prev} → {cur}"
    if prev == 0:
        return "—"
    d = cur - prev
    pct = d / prev * 100.0
    sign = "+" if d >= 0 else ""
    return f"{sign}{d:.3f} ({sign}{pct:.1f}%)" if abs(d) < 1 else f"{sign}{d:.1f} ({sign}{pct:.1f}%)"


def fmt_delta_abs(cur, prev):
    """Absolute delta for stage wall times: '+3.9s', not '+299.2%'."""
    if cur is None or prev is None or prev == 0:
        return "—"
    d = cur - prev
    sign = "+" if d >= 0 else ""
    return f"{sign}{d:.1f}s"


def short_sha(sha):
    return sha[:10] + "…" if len(sha) > 10 else sha


def status_class(status):
    return {"pass": "ok", "fail": "bad", "skipped": "warn"}.get(status, "warn")


def _chip(text, cls):
    return f'<span class="chip {cls}">{html.escape(text)}</span>'


def _fmt_metric(name, value):
    """Human formatting for a known metric name, else raw."""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        if name.endswith("artifact_bytes"):
            return fmt_bytes(value)
        if name.endswith("wall_time_s"):
            return fmt_seconds(value)
        if name.endswith("cpu_time_s"):
            return f"{value:.2f} s"
        if name.endswith("peak_rss_kb"):
            return fmt_bytes(float(value) * 1024)
        if name.endswith("_s"):
            return fmt_seconds(value)
        if abs(value) < 10:
            return f"{value:.3f}"
        return f"{value:,.0f}"
    return str(value)


# ---------------------------------------------------------------------------
# v2 data access helpers
# ---------------------------------------------------------------------------

def _identity(result):
    """Return the identity dict from a v2 result."""
    return result.get("identity", {})


def _run(result):
    """Return the run dict from a v2 result."""
    return result.get("run", {})


def _performance(result):
    """Return the performance dict from a v2 result."""
    return result.get("performance", {})


def _metrics(result):
    """Return the metrics dict from a v2 result."""
    return result.get("metrics", {})


def _software_version(result):
    """Return the software version/SHA from a v2 result."""
    return _identity(result).get("software", {}).get("version", "unknown")


def _runtime_source(result):
    """Return 'source' or 'package' (default 'source' for pre-Stage-1 results)."""
    return _identity(result).get("runtime", {}).get("runtime_source", "source")


def _benchmark_name(result):
    """Return the benchmark name from a v2 result."""
    return _identity(result).get("benchmark", "unknown")


def _dataset(result):
    """Return the dataset from a v2 result."""
    return _identity(result).get("dataset", "unknown")


def _stages(result):
    """Return the stages list from a v2 result."""
    return _performance(result).get("stages", [])


def _build(result):
    """Return the build dict from a v2 result."""
    return _performance(result).get("build", {})


def _verdict(result):
    """Return the verdict from a v2 result."""
    return _metrics(result).get("verdict", "unknown")


def _cache(result):
    """Return the cache state from a v2 result."""
    return _run(result).get("cache", "unknown")


def _run_id(result):
    """Return the run_id from a v2 result."""
    return _run(result).get("run_id", "")


def _run_at(result):
    """Return the run_at from a v2 result."""
    return _run(result).get("run_at", "")


def _configuration(result):
    """Return the configuration dict from a v2 result."""
    return _identity(result).get("configuration", {})


# ---------------------------------------------------------------------------
# v1 → v2 promotion
# ---------------------------------------------------------------------------


def _promote_v1(data):
    """Promote a v1 result to v2 schema by wrapping fields in the v2 structure."""
    if data.get("schema") == SCHEMA_V2:
        return data
    # v1 -> v2 promotion
    promoted = {
        "schema": SCHEMA_V2,
        "identity": {
            "benchmark": data.get("benchmark", "smoke"),
            "benchmark_version": data.get("milestone", 1),
            "dataset": data.get("dataset", "smoke fixture"),
            "software": {
                "name": data.get("source", "OpenMS") if data.get("source") == "tool" else "OpenMS",
                "version": data.get("openms_sha", data.get("tool", {}).get("version", "")),
            },
            "configuration": {
                "use_ms2rescore": data.get("use_ms2rescore", False),
            },
        },
        "run": {
            "run_id": data.get("run_id", data.get("label", "")),
            "run_at": data.get("run_at", ""),
            "cache": data.get("cache", "unknown"),
        },
        "performance": {
            "wall_time_s": sum(s.get("wall_time_s", 0) for s in data.get("stages", [])),
            "cpu_time_s": sum(s.get("cpu_time_s", 0) for s in data.get("stages", [])),
            "peak_rss_kb": max((s.get("peak_rss_kb", 0) for s in data.get("stages", [])), default=0),
            "build": data.get("build", {}),
            "stages": data.get("stages", []),
        },
        "metrics": {
            "verdict": data.get("verdict", "unknown"),
        },
    }
    # Carry over tool-specific fields for tool results
    if data.get("source") == "tool":
        promoted["tool"] = data.get("tool", {})
        promoted["metrics"].update(data.get("metrics", {}))
        # Keep proteobench metadata
        if data.get("proteobench"):
            promoted["proteobench"] = data["proteobench"]
    # Carry over correctness for OpenSwath-like results
    if data.get("correctness"):
        promoted["correctness"] = data["correctness"]
    # Carry over tool_versions for pipeline results
    if data.get("tool_versions"):
        promoted["tool_versions"] = data["tool_versions"]
    # Carry over openms metrics that aren't verdict
    if data.get("metrics"):
        promoted["metrics"].update(data["metrics"])
    # Carry over the discovery path (load_results sets _file before calling
    # this): without it every promoted v1 run loses its file reference and
    # renders with an empty source label; --current deduplication also keys
    # on this field.
    if data.get("_file"):
        promoted["_file"] = data["_file"]
    return promoted


# ---------------------------------------------------------------------------
# discovery + comparison
# ---------------------------------------------------------------------------


def load_results(results_dir):
    """Load every result JSON. Returns (openms_runs, tool_results).

    Supports both v1 (flat openms/tools directories) and v2 (benchmark-specific
    subdirectories) layouts, with automatic v1->v2 promotion.
    """
    openms_runs, tool_results = [], []

    # v1 layout: results/openms/*.json and results/tools/*.json
    for path in sorted(glob.glob(os.path.join(results_dir, "openms", "*.json"))):
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        data["_file"] = os.path.relpath(path, results_dir).replace(os.sep, "/")
        openms_runs.append(_promote_v1(data))
    for path in sorted(glob.glob(os.path.join(results_dir, "tools", "*.json"))):
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        data["_file"] = os.path.relpath(path, results_dir).replace(os.sep, "/")
        tool_results.append(_promote_v1(data))

    # v2 layout: results/<benchmark>/openms/*.json and results/<benchmark>/reference/*.json
    for benchmark_dir in sorted(glob.glob(os.path.join(results_dir, "*"))):
        if not os.path.isdir(benchmark_dir):
            continue
        benchmark_name = os.path.basename(benchmark_dir)
        if benchmark_name in ("openms", "tools"):
            continue  # already handled above
        # OpenMS runs
        for path in sorted(glob.glob(os.path.join(benchmark_dir, "openms", "*.json"))):
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            data["_file"] = os.path.relpath(path, results_dir).replace(os.sep, "/")
            openms_runs.append(_promote_v1(data))
        # Reference/tool results
        for path in sorted(glob.glob(os.path.join(benchmark_dir, "reference", "*.json"))):
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            data["_file"] = os.path.relpath(path, results_dir).replace(os.sep, "/")
            tool_results.append(_promote_v1(data))

    # oldest first; the "current" run is the most recent one
    openms_runs.sort(key=lambda r: _run_at(r))
    return openms_runs, tool_results


# GitHub compare-API cache for SHA-ancestor baseline selection (per render).
_GH_COMPARE_CACHE = {}
_GH_API_DISABLED = False


def _sha_is_ancestor(base_sha, head_sha):
    """True if base_sha is an ancestor of head_sha, False if not, None if unknown.

    Uses the GitHub compare API on OpenMS/OpenMS: compare(base...head) reports
    status "ahead" exactly when base is an ancestor of head. Rate limiting
    (403/429) and network/infrastructure failures disable further lookups for
    this process; per-pair unknowns (e.g. 404 for a version string that is not
    a commit) are cached without disabling. Callers must treat None as "fall
    back to the timestamp rule".
    """
    global _GH_API_DISABLED
    if not base_sha or not head_sha:
        return None
    if base_sha == head_sha or head_sha.startswith(base_sha) or base_sha.startswith(head_sha):
        return False  # same commit (full or short form) is not an ancestor
    key = (base_sha, head_sha)
    if key in _GH_COMPARE_CACHE:
        return _GH_COMPARE_CACHE[key]
    if _GH_API_DISABLED:
        return None
    url = ("https://api.github.com/repos/OpenMS/OpenMS/compare/"
           + urllib.parse.quote(base_sha, safe="") + "..."
           + urllib.parse.quote(head_sha, safe=""))
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "openms-benchmarking-render",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429):
            _GH_API_DISABLED = True
        _GH_COMPARE_CACHE[key] = None
        return None
    except (urllib.error.URLError, OSError, ValueError):
        _GH_API_DISABLED = True
        _GH_COMPARE_CACHE[key] = None
        return None
    status = data.get("status")
    result = {"ahead": True, "behind": False, "diverged": False}.get(status)
    _GH_COMPARE_CACHE[key] = result
    return result


def pick_baseline(current, openms_runs, use_sha_ancestry=True):
    """The stored baseline = the previous OpenMS run of the same benchmark.

    Default: prefer the latest stored run of the same benchmark whose OpenMS
    version is an *ancestor* of the current run's version (resolved via the
    GitHub compare API, cached per render), so the comparison stays meaningful
    even when newer unrelated runs exist. When no ancestor run is known, or
    ancestry cannot be resolved (offline, rate-limited, unknown SHAs), fall
    back to the timestamp rule: the latest run of the same benchmark with
    run_at < current's (or simply the latest other run when run_at is
    missing). use_sha_ancestry=False (--no-sha-baseline) skips the ancestry
    step entirely.
    """
    if not openms_runs:
        return None
    cur_bench = _benchmark_name(current)
    same_bench = [r for r in openms_runs
                  if _benchmark_name(r) == cur_bench and r is not current]
    if not same_bench:
        return None

    if use_sha_ancestry:
        cur_sha = _software_version(current)
        # Stage 1: SHA-ancestry comparison is defined between source builds.
        # A package run's version string may be a nightly short SHA of a
        # moving branch; mixing it into a source-SHA lineage would compare
        # different *kinds* of versions. It stays in the timestamp pool only.
        if _runtime_source(current) == "package":
            use_sha_ancestry = False
        if use_sha_ancestry and cur_sha and cur_sha != "unknown":
            ancestors = [r for r in same_bench
                         if _runtime_source(r) == "source"
                         and _sha_is_ancestor(_software_version(r), cur_sha)]
            if ancestors:
                ancestors.sort(key=lambda r: _run_at(r))
                return ancestors[-1]

    cur_at = _run_at(current)
    same_bench.sort(key=lambda r: _run_at(r))
    if not cur_at:
        return same_bench[-1]
    before = [r for r in same_bench if _run_at(r) < cur_at]
    return before[-1] if before else same_bench[-1]


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

CSS = """
:root {
  --ink: #1f2430; --muted: #6b7280; --line: #e5e7eb; --bg: #ffffff;
  --accent: #1a56db; --ok: #047857; --ok-bg: #ecfdf5; --bad: #b91c1c;
  --bad-bg: #fef2f2; --warn: #b45309; --warn-bg: #fffbeb;
  --mono: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
  --sans: system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
}
* { box-sizing: border-box; }
body { margin: 0; background: #f3f4f6; color: var(--ink);
       font: 14px/1.55 var(--sans); }
.page { max-width: 980px; margin: 24px auto; padding: 0 16px; }
.card { background: var(--bg); border: 1px solid var(--line); border-radius: 10px;
        margin-bottom: 18px; overflow: hidden; }
.card > h2 { margin: 0; padding: 12px 18px; font-size: 13px; letter-spacing: .08em;
             text-transform: uppercase; color: var(--muted);
             border-bottom: 1px solid var(--line); background: #fafafa; }
.card > h2 .sub { text-transform: none; letter-spacing: 0; font-weight: 400; }
.inner { padding: 14px 18px 18px; }

h1 { font-size: 22px; margin: 0 0 4px; }
.meta { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
        gap: 8px 24px; margin-top: 12px; }
.meta div { display: flex; justify-content: space-between; gap: 16px;
            border-bottom: 1px dashed var(--line); padding: 3px 0; }
.meta dt { color: var(--muted); }
.meta dd { margin: 0; font-family: var(--mono); font-size: 13px; text-align: right; }

table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { padding: 7px 10px; border-bottom: 1px solid var(--line); text-align: left;
         vertical-align: top; }
thead th { font-size: 11px; text-transform: uppercase; letter-spacing: .06em;
           color: var(--muted); background: #fafafa; white-space: nowrap; }
td.num, th.num { text-align: right; font-family: var(--mono); white-space: nowrap; }
tbody tr:last-child td { border-bottom: none; }
tbody tr:nth-child(even) { background: #fcfcfd; }
.mono { font-family: var(--mono); font-size: 12.5px; word-break: break-all; }

.chip { display: inline-block; padding: 1px 9px; border-radius: 999px;
        font-size: 12px; font-weight: 600; margin-left: 6px; vertical-align: 2px; }
.chip.ok { background: var(--ok-bg); color: var(--ok); border: 1px solid #a7f3d0; }
.chip.bad { background: var(--bad-bg); color: var(--bad); border: 1px solid #fecaca; }
.chip.warn { background: var(--warn-bg); color: var(--warn); border: 1px solid #fde68a; }
.chip.neutral { background: #f3f4f6; color: #374151; border: 1px solid #d1d5db; }

.status-line { font-size: 15px; font-weight: 600; }
.status-line .ok { color: var(--ok); }
.status-line .bad { color: var(--bad); }
.status-line .warn { color: var(--warn); }
.note { color: var(--muted); font-size: 12.5px; margin-top: 10px; }
.note code, code { font-family: var(--mono); font-size: 12px; background: #f3f4f6;
                   padding: 0 4px; border-radius: 4px; }
.dim { color: var(--muted); }
footer { color: var(--muted); font-size: 12px; text-align: center; padding: 8px 0 24px; }
.badrow td { background: #fef2f2; }

/* trend bars (CSS-only, no JS/SVG) */
.tbar-cell { min-width: 130px; }
.tbar-wrap { display: block; width: 100%; height: 8px; margin: 3px 0 4px;
             background: #eef2f7; border: 1px solid var(--line);
             border-radius: 4px; overflow: hidden; }
.tbar { display: block; height: 100%; background: var(--accent); }
"""


def _stage_rows(stages):
    """Render stage rows dynamically from the stages list."""
    rows = ""
    for st in stages:
        cls = status_class(st.get("status", ""))
        req = "required" if st.get("required") else "optional"
        reason = st.get("reason") or ""
        reason = f'<span class="dim"> — {html.escape(reason)}</span>' if reason else ""
        rows += (
            f"<tr>"
            f"<td class='mono'>{html.escape(st.get('name', ''))}</td>"
            f"<td>{_chip(st.get('status', ''), cls)} <span class='dim'>({req})</span></td>"
            f"<td class='num'>{fmt_seconds(st.get('wall_time_s', 0))}</td>"
            f"<td class='num'>{st.get('cpu_time_s', 0):.2f} s</td>"
            f"<td class='num'>{fmt_bytes(st.get('peak_rss_kb', 0) * 1024)}</td>"
            f"</tr>{reason}"
        )
    return rows


def _flat_compare(result):
    """Flatten a result into comparable key-value pairs.

    Reads from metrics[], performance.build, performance.stages[], and
    correctness — without duplicating data in the stored schema.  The
caller gets a single dict suitable for comparison.
    """
    out = {}
    # Scientific / benchmark-specific metrics
    for k, v in _metrics(result).items():
        out[k] = v
    # Build performance
    b = _build(result)
    if b.get("wall_time_s") is not None:
        out["build.wall_time_s"] = b["wall_time_s"]
    if b.get("artifact_bytes") is not None:
        out["build.artifact_bytes"] = b["artifact_bytes"]
    # Stage metrics (dynamically discovered)
    for stage in _stages(result):
        name = stage.get("name", "?")
        if stage.get("status") is not None:
            out[f"stage.{name}.status"] = stage["status"]
        if stage.get("wall_time_s") is not None:
            out[f"stage.{name}.wall_time_s"] = stage["wall_time_s"]
        if stage.get("cpu_time_s") is not None:
            out[f"stage.{name}.cpu_time_s"] = stage["cpu_time_s"]
        if stage.get("peak_rss_kb") is not None:
            out[f"stage.{name}.peak_rss_kb"] = stage["peak_rss_kb"]
    # Correctness metrics (e.g. OpenSwath feature counts)
    for k, v in (result.get("correctness") or {}).items():
        out[f"correctness.{k}"] = v
    return out


def _discover_shared_metrics(current, baseline):
    """Dynamically discover metrics present in both current and baseline.

    Uses _flat_compare() so build, stage, and correctness data are
    included alongside the flat metrics dict.
    """
    cur_c = _flat_compare(current)
    base_c = _flat_compare(baseline)
    shared = sorted(set(cur_c.keys()) | set(base_c.keys()))
    return shared


# ---------------------------------------------------------------------------
# historical trends
# ---------------------------------------------------------------------------

_TREND_FIXED_COLUMNS = (
    "performance.wall_time_s",
    "performance.cpu_time_s",
    "performance.peak_rss_kb",
)


def _is_numeric_value(value):
    """True for real numbers; bool is deliberately NOT numeric."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _trend_value(run, column):
    """Value of one trend column for a run, or None when not recorded.

    Fixed performance scalars are read from performance[]; every other
    column is a key of the metrics dict.
    """
    if column in _metrics(run):
        return _metrics(run)[column]
    if column.startswith("performance."):
        return (_performance(run) or {}).get(column[len("performance."):])
    return None


def build_history_series(openms_runs):
    """Group historical runs into per-benchmark trend series.

    Runs are grouped by (identity.benchmark, identity.dataset,
    identity.runtime.runtime_source) and each series is sorted oldest ->
    newest by run_at.  Metric columns are discovered dynamically: the three
    fixed performance scalars plus every metrics key except 'verdict'.  A
    column is classified series-wide — numeric only when every non-None
    value is a real number (bool never counts) — and columns without a
    single recorded value are dropped.  Missing values stay None so the
    renderer can mark them.
    """
    groups = {}
    for run in openms_runs:
        key = (_benchmark_name(run), _dataset(run), _runtime_source(run))
        groups.setdefault(key, []).append(run)

    series_list = []
    for key in sorted(groups):
        runs = groups[key]
        runs.sort(key=lambda r: _run_at(r))
        names = set(_TREND_FIXED_COLUMNS)
        for run in runs:
            names.update(k for k in (_metrics(run) or {}) if k != "verdict")
        values = {}
        kinds = {}
        for name in sorted(names):
            vals = [_trend_value(run, name) for run in runs]
            present = [v for v in vals if v is not None]
            if not present:
                continue  # nothing recorded for this column in this series
            values[name] = vals
            kinds[name] = ("numeric" if all(_is_numeric_value(v) for v in present)
                           else "categorical")
        series_list.append({
            "key": key,
            "label": " · ".join(str(part) for part in key),
            "runs": runs,
            "columns": [{"name": name, "kind": kinds[name]}
                        for name in sorted(kinds)],
            "values": values,
        })
    return series_list


def _trend_series_section(series_list):
    """Render one Trends card per history series ("" when there are none).

    Numeric columns get a CSS-only bar whose width is normalized to the
    column's min-max range within the series (constant columns render a
    full bar; widths are clamped to [0, 100]); missing values render as an
    em dash with no bar.  Categorical values render as literal text.
    """
    if not series_list:
        return ""
    esc = html.escape
    sections = ""
    for series in series_list:
        label = esc(series["label"])
        header = "<th>Run at</th><th>Run</th><th>SHA</th>"
        for col in series["columns"]:
            header += f'<th class="num">{esc(col["name"])}</th>'
        ranges = {}
        for col in series["columns"]:
            if col["kind"] == "numeric":
                present = [v for v in series["values"][col["name"]]
                           if v is not None]
                ranges[col["name"]] = (min(present), max(present))
        rows = ""
        for i, run in enumerate(series["runs"]):
            cells = (f"<td>{esc(_run_at(run))}</td>"
                     f"<td class='mono'>{esc(_run_id(run))}</td>"
                     f"<td class='mono'>{esc(short_sha(_software_version(run)))}</td>")
            for col in series["columns"]:
                name = col["name"]
                value = series["values"][name][i]
                if value is None:
                    cells += '<td class="num tbar-cell">—</td>'
                elif col["kind"] == "numeric":
                    lo, hi = ranges[name]
                    if hi == lo:
                        width = 100.0  # constant column: deterministic full bar
                    else:
                        width = (value - lo) / (hi - lo) * 100.0
                    width = max(0.0, min(100.0, width))
                    cells += (f'<td class="num tbar-cell">'
                              f'<div class="tbar-wrap"><div class="tbar" '
                              f'style="width:{width:.1f}%"></div></div>'
                              f'{esc(_fmt_metric(name, value))}</td>')
                else:
                    cells += f'<td class="num tbar-cell">{esc(_fmt_metric(name, value))}</td>'
            rows += f"<tr>{cells}</tr>"
        sections += f"""
        <div class="card"><h2>Trends <span class="sub">— {label}</span></h2><div class="inner">
          <table>
            <thead><tr>{header}</tr></thead>
            <tbody>{rows}</tbody>
          </table>
          <div class="note">Oldest → newest run per column. Each numeric column
          is scaled independently to its min-max range within this series (a
          constant column shows a full bar); a dash (—) marks a value the run
          did not record. Categorical values and the per-run verdict live in
          <b>Run history</b>.</div>
        </div></div>"""
    return sections


def render(current, baseline, openms_runs, tool_results, out_path, generated):
    esc = html.escape
    cache = _cache(current)
    cache_cls = {"cold": "warn", "warm": "ok", "none": "neutral"}.get(cache, "neutral")
    verdict = _verdict(current)
    verdict_cls = status_class(verdict)
    bench = _benchmark_name(current)
    sha = _software_version(current)
    rt = _runtime_source(current)

    # ----- header ----------------------------------------------------------
    # Runtime provenance must be unambiguous: a package-mode run is labeled
    # with its package identity and can never read as an exact-SHA build.
    if rt == "package":
        pkg = _identity(current).get("runtime", {}).get("package", {})
        sha_row = (f"<div><dt>OpenMS revision</dt><dd>{esc(short_sha(sha))} "
                   f"<span class=\"mono\" title=\"{esc(sha)}\">({esc(sha[:6])})</span> "
                   f"— embedded in package</dd></div>")
        runtime_rows = (
            f"<div><dt>Runtime</dt><dd>{_chip('PACKAGE · nightly .deb', 'warn')}</dd></div>"
            f"<div><dt>Package</dt><dd class='mono'>{esc(pkg.get('filename', ''))}</dd></div>"
            f"<div><dt>Package SHA256</dt><dd class='mono'>{esc(pkg.get('sha256', ''))}</dd></div>"
            f"<div><dt>Package archive</dt><dd>{esc(pkg.get('archive_dir', ''))} "
            f"<span class='dim'>(build night {esc(pkg.get('date', ''))})</span></dd></div>"
        )
    else:
        sha_row = (f"<div><dt>Software SHA</dt><dd>{esc(short_sha(sha))} "
                   f"<span class=\"mono\" title=\"{esc(sha)}\">({esc(sha[:6])})</span></dd></div>")
        runtime_rows = f"<div><dt>Runtime</dt><dd>{_chip('SOURCE · exact SHA build', 'ok')}</dd></div>"

    head = f"""
    <div class="card"><div class="inner">
      <h1>OpenMS Benchmark Report</h1>
      <div class="dim">{esc(bench)} benchmark —
      generated {esc(generated.strftime('%Y-%m-%d %H:%M %Z'))}</div>
      <dl class="meta">
        {runtime_rows}
        {sha_row}
        <div><dt>Run</dt><dd>{esc(_run_id(current))} <span class="dim">({esc(current.get('_file', ''))})</span></dd></div>
        <div><dt>Run at</dt><dd>{esc(_run_at(current))}</dd></div>
        <div><dt>Cache</dt><dd>{_chip(cache, cache_cls)}</dd></div>
        <div><dt>Dataset</dt><dd>{esc(_dataset(current))}</dd></div>
        <div><dt>Verdict</dt><dd>{_chip(verdict, verdict_cls)}</dd></div>
      </dl>
    </div></div>"""

    # ----- build (optional) ------------------------------------------------
    build = _build(current)
    build_section = ""
    if build:
        build_rows = (
            f"<tr><td>Build wall time</td><td class='num'>{fmt_seconds(build.get('wall_time_s', 0))}</td></tr>"
            f"<tr><td>Artifact size</td><td class='num'>{fmt_bytes(build.get('artifact_bytes', 0))}</td></tr>"
            f"<tr><td>Cache state</td><td class='num'>{esc(cache)}</td></tr>"
        )
        build_section = f"""
        <div class="card"><h2>Build</h2><div class="inner">
          <table><tbody>{build_rows}</tbody></table>
          <div class="note">Build metrics come from the run metadata recorded at
          <code>normalize</code> time (CI knows them; the benchmark script itself does not).</div>
        </div></div>"""

    # ----- stages (optional, dynamically discovered) ------------------------
    stages = _stages(current)
    stages_section = ""
    if stages:
        stage_names = ", ".join(s.get("name", "?") for s in stages)
        stages_section = f"""
        <div class="card"><h2>Pipeline stages <span class="sub">— {esc(stage_names)}</span></h2><div class="inner">
          <table>
            <thead><tr><th>Stage</th><th>Result</th><th class="num">Wall</th>
            <th class="num">CPU</th><th class="num">Peak RSS</th></tr></thead>
            <tbody>{_stage_rows(stages)}</tbody>
          </table>
        </div></div>"""

    # ----- correctness (optional) -------------------------------------------
    correctness = current.get("correctness", {})
    correctness_section = ""
    if correctness:
        crows = "".join(
            f"<tr><td class='mono'>{esc(k)}</td><td class='num'>{_fmt_metric(k, v)}</td></tr>"
            for k, v in sorted(correctness.items())
        )
        correctness_section = f"""
        <div class="card"><h2>Correctness</h2><div class="inner">
          <table><tbody>{crows}</tbody></table>
        </div></div>"""

    # ----- metrics (dynamic, flat key-value table) --------------------------
    metrics = _metrics(current)
    # Exclude verdict from the metrics table (already shown in header)
    display_metrics = {k: v for k, v in sorted(metrics.items()) if k != "verdict"}
    metrics_section = ""
    if display_metrics:
        mrows = "".join(
            f"<tr><td class='mono'>{esc(k)}</td><td class='num'>{_fmt_metric(k, v)}</td></tr>"
            for k, v in display_metrics.items()
        )
        metrics_section = f"""
        <div class="card"><h2>Metrics</h2><div class="inner">
          <table><tbody>{mrows}</tbody></table>
        </div></div>"""

    # ----- tool / reference results (dynamic) ------------------------------
    tool_section = ""
    if tool_results:
        # Collect all unique metric keys across tool results
        all_metric_keys = []
        for t in tool_results:
            for k in sorted(_metrics(t).keys()):
                if k not in all_metric_keys:
                    all_metric_keys.append(k)
        labels = [_run_id(t) or "?" for t in tool_results]
        delta_col = "<th class='num'>Δ</th>" if len(tool_results) > 1 else ""
        trows = ""
        for mk in all_metric_keys:
            vals = [_metrics(t).get(mk) for t in tool_results]
            cells = "".join(
                f"<td class='num'>{_fmt_metric(mk, v)}</td>" for v in vals
            )
            if all(isinstance(v, (int, float)) for v in vals if v is not None):
                cells += f"<td class='num'>{html.escape(fmt_delta(vals[-1], vals[0], True))}</td>"
            trows += f"<tr><td>{esc(mk)}</td>{cells}</tr>"
        # Build tool metadata display
        tool_meta_rows = ""
        if tool_results:
            t = tool_results[0]
            tool_info = t.get("tool", {})
            if tool_info:
                for tk, tv in sorted(tool_info.items()):
                    tool_meta_rows += f"<tr><td>{esc(tk)}</td><dd>{esc(str(tv))}</dd></tr>"
            pb_info = t.get("proteobench", {})
            if pb_info:
                for pk, pv in sorted(pb_info.items()):
                    tool_meta_rows += f"<tr><td>{esc(pk)}</td><dd>{esc(str(pv))}</dd></tr>"
        meta_html = f"""
          <dl class="meta">
            <div><dt>Tool</dt><dd>{esc(tool_results[0].get('tool', {}).get('name', '—'))}</dd></div>
            <div><dt>Version</dt><dd>{esc(tool_results[0].get('tool', {}).get('version', '—'))}</dd></div>
          </dl>""" if tool_results[0].get("tool") else ""
        tool_section = f"""
        <div class="card"><h2>Reference results <span class="sub">— {esc(_dataset(tool_results[0]))}</span></h2><div class="inner">
          {meta_html}
          <table>
            <thead><tr><th>Metric</th>{"".join(f"<th class='num'>{esc(l)}</th>" for l in labels)}{delta_col}</tr></thead>
            <tbody>{trows or '<tr><td class="dim" colspan="%d">No metrics found.</td></tr>' % (len(labels) + 2)}</tbody>
          </table>
        </div></div>"""

    # ----- comparison: current vs baseline (dynamic) -----------------------
    comp_section = ""
    if baseline:
        shared = _discover_shared_metrics(current, baseline)
        cur_c = _flat_compare(current)
        base_c = _flat_compare(baseline)
        rows = ""
        for key in shared:
            cur_v, base_v = cur_c.get(key), base_c.get(key)
            numeric = isinstance(cur_v, (int, float)) or isinstance(base_v, (int, float))
            delta_cell = html.escape(fmt_delta(cur_v, base_v, numeric))
            rows += (
                f"<tr><td class='mono'>{esc(key)}</td>"
                f"<td class='num'>{_fmt_metric(key, base_v)}</td>"
                f"<td class='num'>{_fmt_metric(key, cur_v)}</td>"
                f"<td class='num'>{delta_cell}</td></tr>"
            )
        cache_note = ""
        if _cache(current) != _cache(baseline):
            cache_note = (
                '<div class="note">⚠ Cache state differs between the two runs '
                f"(current = <b>{esc(_cache(current))}</b>, baseline = "
                f"<b>{esc(_cache(baseline))}</b>), so the build-time Δ reflects "
                "the ccache state, not a code change.</div>")
        comp_section = f"""
        <div class="card"><h2>Comparison <span class="sub">— current vs previous run</span></h2><div class="inner">
          <table>
            <thead><tr><th>Metric</th><th class="num">Baseline<br><span class="dim">{esc(_run_id(baseline))}</span></th>
            <th class="num">Current<br><span class="dim">{esc(_run_id(current))}</span></th>
            <th class="num">Δ</th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
{'          ' + cache_note if cache_note else ''}
          <div class="note">Baseline = previous OpenMS run of the same benchmark
          ({esc(baseline.get('_file', ''))}). Only metrics present in both runs are
          compared.</div>
        </div></div>"""

    # ----- historical trends (one card per benchmark/dataset/runtime series) -
    trends_section = _trend_series_section(build_history_series(openms_runs))

    # ----- history ---------------------------------------------------------
    history_section = ""
    if len(openms_runs) > 1:
        hist = ""
        for r in reversed(openms_runs):
            r_cache = _cache(r)
            r_verdict = _verdict(r)
            cls = status_class(r_verdict)
            build_s = _build(r).get("wall_time_s")
            hist += (
                f"<tr><td class='mono'>{esc(_run_id(r))}</td>"
                f"<td>{esc(_benchmark_name(r))}</td>"
                f"<td>{esc(_run_at(r))}</td>"
                f"<td class='mono'>{esc(short_sha(_software_version(r)))}</td>"
                f"<td>{_chip(r_cache, {'cold': 'warn', 'warm': 'ok', 'none': 'neutral'}.get(r_cache, 'neutral'))}</td>"
                f"<td>{_chip(r_verdict, cls)}</td>"
                f"<td class='num'>{fmt_seconds(build_s) if build_s else '—'}</td></tr>"
            )
        history_section = f"""
        <div class="card"><h2>Run history</h2><div class="inner">
          <table>
            <thead><tr><th>Run</th><th>Benchmark</th><th>Run at</th><th>SHA</th><th>Cache</th>
            <th>Verdict</th><th class="num">Build time</th></tr></thead>
            <tbody>{hist}</tbody>
          </table>
        </div></div>"""

    # ----- status ----------------------------------------------------------
    if verdict == "pass":
        status = '<span class="ok">✓ No regression detected</span>'
        if baseline:
            cur_c = _flat_compare(current)
            base_c = _flat_compare(baseline)
            regressed = []
            for key in shared:
                if key.startswith("stage.") and key.endswith(".status"):
                    if cur_c.get(key) != "pass" and base_c.get(key) == "pass":
                        regressed.append(key)
            if regressed:
                status = f'<span class="bad">✗ Regression detected: {esc(", ".join(regressed))}</span>'
    else:
        status = f'<span class="bad">✗ Verdict is {esc(verdict)}</span>'
    status_section = f"""
    <div class="card"><h2>Status</h2><div class="inner">
      <div class="status-line">{status}</div>
      <div class="note">Regression = required stage flipping pass → fail/skipped, or
      a non-pass verdict, relative to the stored baseline. Wall-time changes alone
      are reported but not treated as regressions.</div>
    </div></div>"""

    # ----- assemble --------------------------------------------------------
    doc = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OpenMS Benchmark Report — {esc(short_sha(sha))}</title>
<style>{CSS}</style></head>
<body><div class="page">
{head}
{build_section}
{stages_section}
{correctness_section}
{metrics_section}
{tool_section}
{comp_section}
{trends_section}
{history_section}
{status_section}
<footer>Generated by <code>benchmark/report/report_generate.py</code>
({esc(generated.strftime('%Y-%m-%d %H:%M %Z'))}) · results schema {esc(SCHEMA)} ·
regenerate with <code>python3 benchmark/report/report_generate.py render</code></footer>
</div></body></html>"""
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(doc)
    print(f"report written to {out_path}")


# ---------------------------------------------------------------------------
# normalization subcommands
# ---------------------------------------------------------------------------


def normalize_smoke(args):
    """Convert a raw CI smoke.json into a normalized v2 OpenMS run result."""
    with open(args.smoke_json, encoding="utf-8") as fh:
        raw = json.load(fh)
    if args.build_time is None or not args.artifact_bytes or not args.run_id:
        sys.exit("normalize smoke needs --run-id, --build-time and --artifact-bytes")
    stages = []
    for st in raw.get("stages", []):
        stages.append({
            "name": st.get("name"),
            "required": bool(st.get("required")),
            "exit_code": st.get("exit_code"),
            "wall_time_s": st.get("wall_time_s"),
            "cpu_time_s": st.get("cpu_time_s"),
            "peak_rss_kb": st.get("peak_rss_kb"),
            "status": st.get("status"),
            "reason": st.get("reason", ""),
        })
    # v2 schema only — no duplicate v1 fields
    out = {
        "schema": SCHEMA_V2,
        "identity": {
            "benchmark": raw.get("benchmark", "smoke"),
            "benchmark_version": raw.get("milestone", 1),
            "dataset": args.dataset or "smoke fixture (CometAdapter_3)",
            "software": {
                "name": "OpenMS",
                "version": raw.get("openms_sha", ""),
            },
            "configuration": {
                "use_ms2rescore": raw.get("use_ms2rescore") in (True, "true", "1"),
            },
            "runtime": _runtime_identity(args),
        },
        "run": {
            "run_id": args.run_id,
            "run_at": args.run_at or "",
            "cache": args.cache or "unknown",
        },
        "performance": {
            "wall_time_s": sum(s.get("wall_time_s", 0) for s in stages),
            "cpu_time_s": sum(s.get("cpu_time_s", 0) for s in stages),
            "peak_rss_kb": max((s.get("peak_rss_kb", 0) for s in stages), default=0),
            "build": {
                "wall_time_s": float(args.build_time),
                "artifact_bytes": int(args.artifact_bytes),
            },
            "stages": stages,
        },
        "metrics": {
            "verdict": raw.get("verdict", "unknown"),
        },
        "tool_versions": raw.get("tool_versions", {}),
    }
    # default output path
    out_path = args.out or os.path.join(
        args.results_dir, raw.get("benchmark", "smoke"), "openms",
        f"{raw.get('benchmark', 'run')}-{args.run_id}.json"
    )
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(out, fh, indent=2)
    print(f"normalized OpenMS run result -> {out_path}")


# correctness[] key -> metrics[] key, for the OpenSwath DIA benchmark.
#
# The generic renderer treats metrics{} as a flat bag of name/value pairs and
# discovers its columns dynamically, so surfacing the numeric measurements
# here is all it takes for them to become Trends columns. Two keys are renamed
# because their OpenMS-canonical names are unclear once flattened into a
# metric bag; every other key keeps its own name.
#
# The `_precursor_mz` keys are NOT renamed for clarity, they carry a semantic
# decision: library_precursor_mz / identified_precursor_mz measure distinct
# isolation-window (m/z, charge) identities, which is deliberately NOT OpenMS's
# own transition-group identity. MRMFeatureFinderScoring keys transition
# groups by Transition@peptideRef alone, so an isobaric pair (Leu/Ile) counts
# as ONE precursor_mz here but TWO transition groups there. The `_mz` suffix is
# what keeps that difference from being silently flattened into an alias of the
# peptide count; see benchmark/openswath_metrics.py and the isobaric regression
# test in benchmark/test_openswath_metrics.py.
#
# Deliberately NOT mapped:
#   expected_features     a hardcoded expectation, not a measurement
#   features_match        a boolean verdict, and booleans are not numeric
#   qc_charge_distribution a list, not a scalar
#   library_error         a string; the failure is already carried by verdict
#
# PSM / FDR counts are absent by design: DIA emits no spectrum-level
# identifications. See benchmark/openswath_metrics.py for the definitions.
_OPENSWATH_CORRECTNESS_METRICS = {
    "actual_features": "identified_features",
    "overall_quality_sum": "feature_quality_sum",
    "total_intensity": "total_intensity",
    "library_peptides": "library_peptides",
    "library_precursor_mz": "library_precursor_mz",
    "library_transitions": "library_transitions",
    "identified_peptides": "identified_peptides",
    "identified_precursor_mz": "identified_precursor_mz",
    "identified_proteins": "identified_proteins",
    "transitions_used": "transitions_used",
    "library_coverage": "library_coverage",
}


def normalize_openswath(args):
    """Convert a raw CI openswath.json into a normalized v2 OpenMS run result."""
    with open(args.openswath_json, encoding="utf-8") as fh:
        raw = json.load(fh)
    if args.build_time is None or not args.artifact_bytes or not args.run_id:
        sys.exit("normalize openswath needs --run-id, --build-time and --artifact-bytes")
    stages = []
    for st in raw.get("stages", []):
        for key in ("wall_time_s", "cpu_time_s"):
            val = st.get(key)
            if val is not None and not math.isfinite(float(val)):
                sys.exit(f"stage {st.get('name', '?')!r}: non-finite {key}; refusing to normalize")
        peak = st.get("peak_rss_kb")
        if peak is not None and not math.isfinite(float(peak)):
            sys.exit(f"stage {st.get('name', '?')!r}: non-finite peak_rss_kb; refusing to normalize")
        stages.append({
            "name": st.get("name"),
            "required": bool(st.get("required")),
            "exit_code": st.get("exit_code"),
            "wall_time_s": st.get("wall_time_s"),
            "cpu_time_s": st.get("cpu_time_s"),
            "peak_rss_kb": st.get("peak_rss_kb"),
            "status": st.get("status"),
            "reason": st.get("reason", ""),
        })
    benchmark = raw.get("benchmark", "openswath")
    # The raw correctness block is preserved verbatim below; its numeric
    # measurements are additionally lifted into metrics so the generic Trends
    # and Comparison machinery plot them over time. Keys absent from an older
    # or partial raw result are simply not invented. `or {}` also covers a raw
    # result whose correctness is JSON null (previously stored verbatim, so
    # this must not become a crash).
    correctness = raw.get("correctness") or {}
    metrics = {"verdict": raw.get("verdict", "unknown")}
    for raw_key, metric_key in _OPENSWATH_CORRECTNESS_METRICS.items():
        value = correctness.get(raw_key)
        if value is not None:
            metrics[metric_key] = value
    out = {
        "schema": SCHEMA_V2,
        "identity": {
            "benchmark": benchmark,
            "benchmark_version": raw.get("milestone", 1),
            "dataset": args.dataset or raw.get("dataset", "OpenSwath DIA fixture"),
            "software": {
                "name": "OpenMS",
                "version": raw.get("openms_sha", ""),
            },
            "configuration": {},
            "runtime": _runtime_identity(args),
        },
        "run": {
            "run_id": args.run_id,
            "run_at": args.run_at or raw.get("run_at", ""),
            "cache": args.cache or raw.get("cache", "unknown"),
        },
        "performance": {
            "wall_time_s": sum(s.get("wall_time_s", 0) for s in stages),
            "cpu_time_s": sum(s.get("cpu_time_s", 0) for s in stages),
            "peak_rss_kb": max((s.get("peak_rss_kb", 0) for s in stages), default=0),
            "build": {
                "wall_time_s": float(args.build_time),
                "artifact_bytes": int(args.artifact_bytes),
            },
            "stages": stages,
        },
        "metrics": metrics,
        "correctness": correctness,
        "tool_versions": raw.get("tool_versions", {}),
    }
    out_path = args.out or os.path.join(
        args.results_dir, benchmark, "openms",
        f"{benchmark}-{args.run_id}.json"
    )
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(out, fh, indent=2)
    print(f"normalized OpenMS run result -> {out_path}")


def normalize_prose_peptdeep(args):
    """Convert raw ProSE+PeptDeep benchmark output into a normalized v2 run result.

    Reads prose.json, stages.tsv and meta.txt from the results directory
    produced by run_prose_benchmark.sh and writes a v2 canonical result JSON.
    The two arms (baseline, peptdeep) are configurations of one benchmark and
    stay in one result: the headline metrics become arm-qualified metric keys
    (target_psms_at_1pct_fdr_baseline / _peptdeep), which the generic renderer
    compares like any other metric. The peptdeep arm's wall time and peak RSS
    are the whole-run performance figures (both arms run the same search, so
    they are directly comparable across runs of the same SHA).
    """
    results_dir = args.results_dir

    # Read meta
    meta = {}
    meta_path = os.path.join(results_dir, "meta.txt")
    if os.path.exists(meta_path):
        with open(meta_path) as fh:
            for line in fh:
                if "=" in line:
                    k, _, v = line.partition("=")
                    meta[k.strip()] = v.strip()

    # Read stages (the raw prose.json already carries them; stages.tsv is the
    # fallback when only a CI artifact subset is available)
    stages = []
    prose_json_path = os.path.join(results_dir, "prose.json")
    raw = {}
    if os.path.exists(prose_json_path):
        with open(prose_json_path, encoding="utf-8") as fh:
            raw = json.load(fh)
        stages = raw.get("stages", [])
    if not stages:
        stages_path = os.path.join(results_dir, "stages.tsv")
        if os.path.exists(stages_path):
            with open(stages_path) as fh:
                for row in csv.reader(fh, delimiter="\t"):
                    if not row:
                        continue
                    name, required, rc, wall, cpu, peak, status, reason = (row + [""] * 8)[:8]

                    def _num(v, cast, default):
                        try:
                            return cast(v)
                        except (ValueError, TypeError):
                            return default

                    stages.append({
                        "name": name,
                        "required": required == "true",
                        "exit_code": _num(rc, int, 0),
                        "wall_time_s": _num(wall, float, 0.0),
                        "cpu_time_s": _num(cpu, float, 0.0),
                        "peak_rss_kb": _num(peak, int, 0),
                        "status": status,
                        "reason": reason,
                    })

    arms = raw.get("arms", {})
    metrics = {
        "verdict": raw.get("verdict", "fail" if not stages else "unknown"),
    }
    for arm in ("baseline", "peptdeep"):
        counts = arms.get(arm)
        if not counts:
            continue
        for key in ("target_psms_at_1pct_fdr", "target_peptides_at_1pct_fdr",
                    "entrapment_psms_at_1pct_fdr", "entrapment_peptides_at_1pct_fdr"):
            metrics[f"{key}_{arm}"] = counts.get(key)
        metrics[f"wall_time_s_{arm}"] = counts.get("wall_time_s")
        metrics[f"peak_rss_mb_{arm}"] = counts.get("peak_rss_mb")
    metrics = {k: v for k, v in metrics.items() if v is not None}

    # Empty/missing stages are never a pass — a benchmark with no stages did
    # not run successfully (same rule as normalize_openswath).
    if not stages:
        verdict = "fail"
    else:
        required_ok = all(s.get("status") == "pass" for s in stages if s.get("required"))
        verdict = "pass" if required_ok else "fail"

    out = {
        "schema": SCHEMA_V2,
        "identity": {
            "benchmark": "prose_peptdeep",
            "dataset": args.dataset or meta.get("prose_input", ""),
            "software": {
                "name": "OpenMS",
                "version": meta.get("openms_sha", raw.get("openms_sha", "")),
            },
            "configuration": {
                "entrapment_prefix": meta.get("entrapment_prefix", ""),
                "peptdeep_instrument": meta.get("peptdeep_instrument", ""),
                "prose_extra_search_args": meta.get("prose_extra_search_args", ""),
            },
        },
        "run": {
            "run_id": args.run_id,
            "run_at": args.run_at or raw.get("run_at", ""),
            "cache": args.cache or "unknown",
        },
        "performance": {
            "wall_time_s": sum(s.get("wall_time_s", 0) for s in stages),
            "cpu_time_s": sum(s.get("cpu_time_s", 0) for s in stages),
            "peak_rss_kb": max((s.get("peak_rss_kb", 0) for s in stages), default=0),
            "build": {},
            "stages": stages,
        },
        "metrics": metrics,
    }

    # default output path — inside --results-dir so CI artifact upload captures it
    out_path = args.out or os.path.join(
        args.results_dir, "prose_peptdeep", "openms",
        f"prose_peptdeep-{args.run_id}.json"
    )
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(out, fh, indent=2)
    print(f"normalized ProSE+PeptDeep run result -> {out_path}")


def normalize_proteobench(args):
    """Convert a local ProteoBench scoring JSON into a normalized v2 tool result."""
    with open(args.pb_json, encoding="utf-8") as fh:
        raw = json.load(fh)
    hm = raw.get("headline_metrics") or {}
    metrics = {
        "quantified_precursors": hm.get("nr_feature"),
        "median_abs_epsilon": hm.get("median_abs_epsilon_global"),
        "mean_abs_epsilon": hm.get("mean_abs_epsilon_global"),
        "cv_median": hm.get("CV_median"),
        "roc_auc": hm.get("roc_auc"),
    }
    metrics = {k: v for k, v in metrics.items() if v is not None}
    ui = raw.get("user_input") or {}
    dataset = args.dataset or (raw.get("input_file") or "").split(".")[0] or "unknown"
    # v2 schema only — no duplicate v1 fields
    out = {
        "schema": SCHEMA_V2,
        "identity": {
            "benchmark": "proteobench",
            "benchmark_version": 1,
            "dataset": dataset,
            "software": {
                "name": ui.get("software_name", "?"),
                "version": ui.get("software_version", "?"),
            },
            "configuration": {
                "search_engine": ui.get("search_engine", "?"),
                "search_engine_version": ui.get("search_engine_version", "?"),
            },
        },
        "run": {
            "run_id": args.label or raw.get("module_id", "proteobench"),
            "run_at": raw.get("scored_at", ""),
        },
        "metrics": metrics,
        "tool": {
            "name": ui.get("software_name", "?"),
            "version": ui.get("software_version", "?"),
            "search_engine": ui.get("search_engine", "?"),
            "search_engine_version": ui.get("search_engine_version", "?"),
        },
        "proteobench": {
            "version": raw.get("proteobench_version", "?"),
            "module": raw.get("module_id", "?"),
            "input_format": raw.get("input_format", "?"),
            "intermediate_hash": raw.get("intermediate_hash", ""),
        },
    }
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", args.label or "result").strip("-")
    out_path = args.out or os.path.join(
        args.results_dir, "proteobench", "reference", f"proteobench-{safe}.json"
    )
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(out, fh, indent=2)
    print(f"normalized tool result -> {out_path}")


def _resolve_baseline_ref(ref, openms_runs, results_dir):
    """Resolve a --baseline reference: a stored run id, an OpenMS SHA (short
    or full), or a path to a result JSON file."""
    for r in openms_runs:
        if _run_id(r) == ref:
            return r
    candidates = [r for r in openms_runs
                  if _software_version(r) and _software_version(r).startswith(ref)]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        sys.exit(f"--baseline {ref!r}: SHA matches {len(candidates)} stored runs; "
                 "disambiguate with the full SHA or a run id")
    if os.path.exists(ref):
        with open(ref, encoding="utf-8") as fh:
            data = json.load(fh)
        data["_file"] = os.path.relpath(ref, results_dir).replace(os.sep, "/")
        return data
    sys.exit(f"--baseline {ref!r}: no stored run id, SHA or file matches")


def render_cmd(args):
    results_dir = args.results_dir
    openms_runs, tool_results = load_results(results_dir)
    if not openms_runs:
        sys.exit(f"no OpenMS run results found under {results_dir}/openms")
    if args.current:
        with open(args.current, encoding="utf-8") as fh:
            current = json.load(fh)
        current["_file"] = os.path.relpath(args.current, results_dir).replace(os.sep, "/")
    else:
        current = openms_runs[-1]
    if args.baseline:
        baseline = _resolve_baseline_ref(args.baseline, openms_runs, results_dir)
    else:
        # pick_baseline excludes the current run by object identity; a
        # --current file also lives in the discovery pool as a *separate*
        # object, so exclude it by path instead. Keyed on the exact file,
        # not the run id: PXD028735-style runs legitimately share a run-id
        # prefix. An empty pool means there is nothing to compare against.
        baseline_pool = [r for r in openms_runs
                         if r.get("_file") != current["_file"]]
        baseline = (pick_baseline(current, baseline_pool,
                                  use_sha_ancestry=not args.no_sha_baseline)
                    if baseline_pool else None)
    render(current, baseline, openms_runs, tool_results, args.out,
           _dt.datetime.now(_dt.timezone.utc))


def main():
    # Windows consoles default to cp1252, which cannot encode some of the
    # characters used in labels/output; force UTF-8 so the script behaves
    # identically on every platform (CI runs on Linux anyway).
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("normalize", help="convert a raw result into a normalized run result")
    pn = p.add_subparsers(dest="sub", required=True)

    ps = pn.add_parser("smoke", help="normalize a CI smoke.json")
    ps.add_argument("smoke_json")
    ps.add_argument("--run-id", required=True)
    ps.add_argument("--cache", choices=["cold", "warm", "none", "unknown"], default="unknown")
    ps.add_argument("--build-time", type=float, required=True)
    ps.add_argument("--artifact-bytes", type=int, required=True)
    ps.add_argument("--run-at", default="")
    ps.add_argument("--dataset", default="",
                    help="what was benchmarked (default: smoke fixture)")
    ps.add_argument("--results-dir", default="benchmark/results")
    ps.add_argument("--out")
    _add_runtime_source_args(ps)
    ps.set_defaults(fn=normalize_smoke)

    po = pn.add_parser("openswath", help="normalize a CI openswath.json (OpenSwath DIA benchmark)")
    po.add_argument("openswath_json")
    po.add_argument("--run-id", required=True)
    po.add_argument("--cache", choices=["cold", "warm", "none", "unknown"], default="",
                    help="override the cache state (default: value recorded in the raw result)")
    po.add_argument("--build-time", type=float, required=True)
    po.add_argument("--artifact-bytes", type=int, required=True)
    po.add_argument("--run-at", default="",
                    help="override the run timestamp (default: value recorded in the raw result)")
    po.add_argument("--dataset", default="",
                    help="override the dataset label (default: value recorded in the raw result)")
    po.add_argument("--results-dir", default="benchmark/results")
    po.add_argument("--out")
    _add_runtime_source_args(po)
    po.set_defaults(fn=normalize_openswath)

    pp = pn.add_parser("proteobench", help="normalize a local ProteoBench scoring JSON")
    pp.add_argument("pb_json")
    pp.add_argument("--label", required=True)
    pp.add_argument("--dataset", default="",
                    help="dataset id/label, e.g. PXD028735 (HYE mixed-species)")
    pp.add_argument("--results-dir", default="benchmark/results")
    pp.add_argument("--out")
    pp.set_defaults(fn=normalize_proteobench)

    pr = pn.add_parser("prose-peptdeep", help="normalize raw ProSE+PeptDeep benchmark output into v2")
    pr.add_argument("--run-id", required=True)
    pr.add_argument("--cache", choices=["cold", "warm", "none", "unknown"], default="unknown")
    pr.add_argument("--run-at", default="")
    pr.add_argument("--dataset", default="",
                    help="what was benchmarked (default: the runner's input mzML)")
    pr.add_argument("--results-dir", required=True,
                    help="raw results directory from run_prose_benchmark.sh")
    pr.add_argument("--out")
    pr.set_defaults(fn=normalize_prose_peptdeep)

    r = sub.add_parser("render", help="discover results and render report.html")
    r.add_argument("--results-dir", default="benchmark/results")
    r.add_argument("--current", help="explicit current run result file")
    r.add_argument("--baseline",
                   help="explicit baseline: a stored run id, an OpenMS SHA "
                        "(short or full), or a path to a result JSON")
    r.add_argument("--no-sha-baseline", action="store_true",
                   help="disable SHA-ancestor baseline selection "
                        "(timestamp rule only)")
    r.add_argument("--out", default="benchmark/reports/report.html")
    r.set_defaults(fn=render_cmd)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
