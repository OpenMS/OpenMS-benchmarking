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

    render [--results-dir DIR] [--current PATH] [--out report.html]
        Discover all results, compare the current run against the stored
        baseline, and render report.html.
"""

import argparse
import datetime as _dt
import glob
import html
import json
import math
import os
import re
import sys

SCHEMA_V1 = "openms-benchmarking/report/v1"
SCHEMA_V2 = "openms-benchmarking/report/v2"
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


def pick_baseline(current, openms_runs):
    """The stored baseline = the previous OpenMS run of the same benchmark."""
    if not openms_runs:
        return None
    cur_at = _run_at(current)
    cur_bench = _benchmark_name(current)
    same_bench = [r for r in openms_runs
                  if _benchmark_name(r) == cur_bench and r is not current]
    if not same_bench:
        return None
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


def render(current, baseline, openms_runs, tool_results, out_path, generated):
    esc = html.escape
    cache = _cache(current)
    cache_cls = {"cold": "warn", "warm": "ok", "none": "neutral"}.get(cache, "neutral")
    verdict = _verdict(current)
    verdict_cls = status_class(verdict)
    bench = _benchmark_name(current)
    sha = _software_version(current)

    # ----- header ----------------------------------------------------------
    head = f"""
    <div class="card"><div class="inner">
      <h1>OpenMS Benchmark Report</h1>
      <div class="dim">{esc(bench)} benchmark —
      generated {esc(generated.strftime('%Y-%m-%d %H:%M %Z'))}</div>
      <dl class="meta">
        <div><dt>Software SHA</dt><dd>{esc(short_sha(sha))} <span class="mono" title="{esc(sha)}">({esc(sha[:6])})</span></dd></div>
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
    if not args.build_time or not args.artifact_bytes or not args.run_id:
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


def normalize_openswath(args):
    """Convert a raw CI openswath.json into a normalized v2 OpenMS run result."""
    with open(args.openswath_json, encoding="utf-8") as fh:
        raw = json.load(fh)
    if not args.build_time or not args.artifact_bytes or not args.run_id:
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
        "metrics": {
            "verdict": raw.get("verdict", "unknown"),
        },
        "correctness": raw.get("correctness", {}),
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
    baseline = pick_baseline(current, openms_runs)
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
    po.set_defaults(fn=normalize_openswath)

    pp = pn.add_parser("proteobench", help="normalize a local ProteoBench scoring JSON")
    pp.add_argument("pb_json")
    pp.add_argument("--label", required=True)
    pp.add_argument("--dataset", default="",
                    help="dataset id/label, e.g. PXD028735 (HYE mixed-species)")
    pp.add_argument("--results-dir", default="benchmark/results")
    pp.add_argument("--out")
    pp.set_defaults(fn=normalize_proteobench)

    r = sub.add_parser("render", help="discover results and render report.html")
    r.add_argument("--results-dir", default="benchmark/results")
    r.add_argument("--current", help="explicit current run result file")
    r.add_argument("--out", default="benchmark/reports/report.html")
    r.set_defaults(fn=render_cmd)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
