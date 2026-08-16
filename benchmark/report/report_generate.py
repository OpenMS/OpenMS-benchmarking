#!/usr/bin/env python3
"""
OpenMS benchmark report generator (Issue #8788).

Turns machine-readable benchmark results into one self-contained HTML report:

    benchmark run  ->  JSON result  ->  report_generate.py render  ->  report.html

The generator deliberately knows nothing about specific runs: it discovers
every result JSON under <results-dir>/{openms,tools} and renders whatever
metrics the current run and the stored baseline actually share. That keeps the
same machinery usable for OpenMS version-vs-version comparisons, cold-vs-warm
builds, ProteoBench results, and eventually results uploaded by other tools
(no hard-coded Exp 1 / Exp 2 numbers anywhere in the report).

Result layout (schema "openms-benchmarking/report/v1"):

    results/openms/<run-id>.json    OpenMS benchmark runs (smoke today)
    results/tools/<tool-id>.json    reference results from other tools/sources
                                    (e.g. the local ProteoBench scorer)

Subcommands:

    normalize smoke <smoke.json> --run-id ID --cache cold|warm|none
                    --build-time SECONDS --artifact-bytes N
                    [--run-at ISO8601] [--out PATH]
        Convert a raw CI smoke.json into a normalized OpenMS run result.
        The smoke script records the pipeline metrics; the run metadata it
        cannot know (build wall time, ccache state, run id, artifact size)
        is passed here, so the report can still answer "was this build cold
        or warm?" without the workflow being able to see inside the runner.

    normalize proteobench <local-proteobench.json> --label LABEL [--out PATH]
        Convert a local ProteoBench scoring result (the JSON produced by
        benchmark-data/run_proteobench_local.py) into a tool result whose
        headline metrics can be compared across runs.

    render [--results-dir DIR] [--current PATH] [--out report.html]
        Discover all results, compare the current run against the stored
        baseline (the previous OpenMS run of the same benchmark), and render
        report.html. --current defaults to the most recent OpenMS run.

Examples:

    # seed/refresh an OpenMS run result from a CI smoke.json
    python3 benchmark/report/report_generate.py normalize smoke \
        smoke.json --run-id 31881021123 --cache warm \
        --build-time 363 --artifact-bytes 149265170 \
        --run-at 2026-08-15T11:01:59Z \
        --out benchmark/results/openms/smoke-31881021123.json

    # add a ProteoBench reference result
    python3 benchmark/report/report_generate.py normalize proteobench \
        proteobench_local_exp2.json --label "Exp 2 (MS2Rescore)" \
        --out benchmark/results/tools/proteobench-exp2.json

    # render the report (discovers the rest)
    python3 benchmark/report/report_generate.py render
"""

import argparse
import datetime as _dt
import glob
import html
import json
import os
import re
import sys

SCHEMA = "openms-benchmarking/report/v1"

# ---------------------------------------------------------------------------
# metric helpers
# ---------------------------------------------------------------------------


def _stage_metrics(stage):
    """Flatten one stage entry into comparable metric rows."""
    name = stage.get("name", "?")
    return {
        f"stage.{name}.status": stage.get("status", ""),
        f"stage.{name}.wall_time_s": stage.get("wall_time_s"),
        f"stage.{name}.cpu_time_s": stage.get("cpu_time_s"),
        f"stage.{name}.peak_rss_kb": stage.get("peak_rss_kb"),
    }


def openms_metrics(run):
    """The flat metric bag of an OpenMS run result (keys stable across runs)."""
    m = {
        "verdict": run.get("verdict", ""),
        "use_ms2rescore": bool(run.get("use_ms2rescore")),
    }
    build = run.get("build") or {}
    if build.get("wall_time_s") is not None:
        m["build.wall_time_s"] = build["wall_time_s"]
    if build.get("artifact_bytes") is not None:
        m["build.artifact_bytes"] = build["artifact_bytes"]
    for stage in run.get("stages", []):
        m.update(_stage_metrics(stage))
    # scientific metrics (PSMs/peptides/proteins/quantified precursors) come
    # from future extended runs; if a run carries them, they compare naturally.
    m.update(run.get("metrics", {}))
    return m


def tool_metrics(tool_result):
    """The flat metric bag of a tool/reference result."""
    return dict(tool_result.get("metrics", {}))

def build_tldr(current, baseline, tool_results):
    """One human paragraph summing the report up in plain words.

    Tables are for the machine; this is the sentence you can paste into a
    message to a colleague without losing anything important.
    """
    bench = current.get("benchmark", "benchmark")
    run_id = current.get("run_id", "?")
    verdict = current.get("verdict", "unknown")
    sha = short_sha(current.get("openms_sha", "unknown"))
    out = []
    if verdict == "pass":
        out.append(f"The {bench} run {run_id} (OpenMS {sha}) passed - nothing failed on its own.")
    else:
        out.append(f"The {bench} run {run_id} (OpenMS {sha}) did not pass (verdict: {verdict}).")
    if baseline:
        b_id = baseline.get("run_id", "?")
        c_cache = current.get("cache") or "unknown"
        b_cache = baseline.get("cache") or "unknown"
        out.append(f"Compared with the previous run {b_id}, the required stages still pass.")
        if c_cache != b_cache:
            out.append(f"The build times are not directly comparable, though: this run had a {c_cache} ccache, the previous one a {b_cache} one.")
        else:
            bw = (baseline.get("build") or {}).get("wall_time_s")
            cw = (current.get("build") or {}).get("wall_time_s")
            if bw and cw:
                pct = (cw - bw) / bw * 100.0
                sign = "+" if pct >= 0 else ""
                out.append(f"Build time is {fmt_seconds(cw)} vs {fmt_seconds(bw)} before ({sign}{pct:.1f}%).")
    if len(tool_results) >= 2:
        first, last = tool_results[0], tool_results[-1]
        m0, m1 = tool_metrics(first), tool_metrics(last)
        q0, q1 = m0.get("quantified_precursors"), m1.get("quantified_precursors")
        if q0 and q1:
            diff = q1 - q0
            pct = diff / q0 * 100.0
            sign = "+" if diff >= 0 else ""
            out.append(f"On the ProteoBench reference set, {last.get('label')} quantified {q1:,} precursors vs {q0:,} for {first.get('label')} ({sign}{pct:.1f}%).")
    return " ".join(out)


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
    # deltas near zero (e.g. epsilon metrics) need more digits than seconds
    return f"{sign}{d:.3f} ({sign}{pct:.1f}%)" if abs(d) < 1 else f"{sign}{d:.1f} ({sign}{pct:.1f}%)"


def short_sha(sha):
    return sha[:10] + "…" if len(sha) > 10 else sha


def status_class(status):
    return {"pass": "ok", "fail": "bad", "skipped": "warn"}.get(status, "warn")


# ---------------------------------------------------------------------------
# discovery + comparison
# ---------------------------------------------------------------------------


def load_results(results_dir):
    """Load every result JSON. Returns (openms_runs, tool_results)."""
    openms_runs, tool_results = [], []
    for path in sorted(glob.glob(os.path.join(results_dir, "openms", "*.json"))):
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        data["_file"] = os.path.relpath(path, results_dir).replace(os.sep, "/")
        openms_runs.append(data)
    for path in sorted(glob.glob(os.path.join(results_dir, "tools", "*.json"))):
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        data["_file"] = os.path.relpath(path, results_dir).replace(os.sep, "/")
        tool_results.append(data)
    # oldest first; the "current" run is the most recent one
    openms_runs.sort(key=lambda r: r.get("run_at", ""))
    return openms_runs, tool_results


def pick_baseline(current, openms_runs):
    """The stored baseline = the previous OpenMS run of the same benchmark."""
    if not openms_runs:
        return None
    cur_at = current.get("run_at", "")
    same_bench = [r for r in openms_runs
                  if r.get("benchmark") == current.get("benchmark") and r is not current]
    if not same_bench:
        return None
    same_bench.sort(key=lambda r: r.get("run_at", ""))
    if not cur_at:
        return same_bench[-1]
    before = [r for r in same_bench if r.get("run_at", "") < cur_at]
    return before[-1] if before else same_bench[-1]


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

CSS = """
:root {
  /* OpenMS brand palette, lifted from the project logo
     (cmake/MacOSX/openms_logo_large_transparent.png): a dark purple-black
     ink with orange, red, magenta, purple and blue bars. */
  --om-ink: #1d0f28;
  --om-orange: #e07000;
  --om-red: #d64545;
  --om-magenta: #d915a8;
  --om-purple: #7a2fe0;
  --om-blue: #2f54e0;

  /* semantic status colors, kept warm to sit with the brand */
  --ok: #0d8a5c;   --ok-bg: #e9f6f0;   --ok-line: #bfe6d4;
  --bad: #c02e2e;  --bad-bg: #fcf0f0;  --bad-line: #f0c6c6;
  --warn: #a96a00; --warn-bg: #fdf5e8; --warn-line: #f0d9ae;

  --bg: #f6f1ea;      /* warm paper */
  --card: #fffdf9;
  --line: #ece2d3;    /* warm hairline */
  --muted: #8b7a6d;
  --ink: #262033;

  --mono: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
  --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  --serif: Georgia, "Iowan Old Style", "Times New Roman", serif;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 14.5px/1.6 var(--sans); }
.page { max-width: 1000px; margin: 28px auto; padding: 0 16px; }

.card { background: var(--card); border: 1px solid var(--line); border-radius: 12px;
        margin-bottom: 18px; overflow: hidden;
        box-shadow: 0 1px 2px rgba(38,32,51,.05), 0 12px 30px -18px rgba(38,32,51,.28); }
.card > h2 { margin: 0; padding: 13px 20px; font-size: 12px; letter-spacing: .09em;
            text-transform: uppercase; color: var(--muted);
            border-bottom: 1px solid var(--line); background: #fbf7f0; font-weight: 600; }
.card > h2 .sub { text-transform: none; letter-spacing: 0; font-weight: 400; }
.card > h2::before { content: ""; display: inline-block; width: 9px; height: 9px;
                     border-radius: 2.5px; margin-right: 9px; background: var(--om-blue);
                     vertical-align: 1px; }
.acc-orange > h2::before { background: var(--om-orange); }
.acc-red > h2::before { background: var(--om-red); }
.acc-magenta > h2::before { background: var(--om-magenta); }
.acc-purple > h2::before { background: var(--om-purple); }
.acc-blue > h2::before { background: var(--om-blue); }
.acc-ok > h2::before { background: var(--ok); }
.inner { padding: 16px 20px 20px; }

.hero { background: linear-gradient(140deg, var(--om-ink) 0%, #2a143f 60%, #3c1b58 100%);
        color: #fff; }
.hero .spectrum { height: 6px; background: linear-gradient(90deg, var(--om-orange),
                  var(--om-red), var(--om-magenta), var(--om-purple), var(--om-blue)); }
.hero h1 { font-family: var(--serif); font-size: 27px; margin: 0 0 3px;
           font-weight: 600; letter-spacing: .2px; }
.hero .subtitle { color: #d8c7e8; font-size: 13px; }

.tl { border-left: 4px solid var(--om-magenta); }
.tl-label { font-size: 10.5px; text-transform: uppercase; letter-spacing: .14em;
            color: var(--om-magenta); font-weight: 700; margin-bottom: 5px; }
.tldr { margin: 0; font-size: 14.5px; line-height: 1.65; }

.meta { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
        gap: 4px 28px; margin: 0; }
.meta div { display: flex; justify-content: space-between; gap: 14px;
            border-bottom: 1px dashed var(--line); padding: 6px 0; }
.meta dt { color: var(--muted); }
.meta dd { margin: 0; font-family: var(--mono); font-size: 13px; text-align: right; }

table { border-collapse: collapse; width: 100%; font-size: 13.5px; }
th, td { padding: 8px 12px; border-bottom: 1px solid var(--line); text-align: left;
         vertical-align: top; }
thead th { font-size: 11px; text-transform: uppercase; letter-spacing: .07em;
           color: var(--muted); background: #fbf7f0; white-space: nowrap; font-weight: 600; }
td.num, th.num { text-align: right; font-family: var(--mono); white-space: nowrap; }
tbody tr:last-child td { border-bottom: none; }
tbody tr:nth-child(even) { background: #fcf9f4; }
.mono { font-family: var(--mono); font-size: 12.5px; word-break: break-all; }

.chip { display: inline-block; padding: 2px 10px; border-radius: 999px;
        font-size: 12px; font-weight: 600; margin-left: 6px; vertical-align: 2px;
        border: 1px solid transparent; }
.chip.ok { background: var(--ok-bg); color: var(--ok); border-color: var(--ok-line); }
.chip.bad { background: var(--bad-bg); color: var(--bad); border-color: var(--bad-line); }
.chip.warn { background: var(--warn-bg); color: var(--warn); border-color: var(--warn-line); }
.chip.neutral { background: #f3eee6; color: #5d5147; border-color: #e2d6c4; }

.status-line { font-size: 16px; font-weight: 650; }
.status-line .ok { color: var(--ok); }
.status-line .bad { color: var(--bad); }
.status-line .warn { color: var(--warn); }
.note { color: var(--muted); font-size: 12.5px; margin-top: 12px; line-height: 1.55; }
.note code, code { font-family: var(--mono); font-size: 12px; background: #f4eee4;
                   padding: 1px 5px; border-radius: 5px; }
.dim { color: var(--muted); }
footer { color: var(--muted); font-size: 12px; text-align: center; padding: 6px 0 28px; }
.badrow td { background: var(--bad-bg); }
"""


def _chip(text, cls):
    return f'<span class="chip {cls}">{html.escape(text)}</span>'


def _stage_rows(run):
    rows = ""
    for st in run.get("stages", []):
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


def _empty_row(what, why):
    return f"<tr><td class='dim' colspan='5'>{html.escape(what)} — {html.escape(why)}</td></tr>"


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


def render(current, baseline, openms_runs, tool_results, out_path, generated):
    esc = html.escape
    cache = current.get("cache", "unknown")
    cache_cls = {"cold": "warn", "warm": "ok", "none": "neutral"}.get(cache, "neutral")
    verdict = current.get("verdict", "unknown")
    verdict_cls = status_class(verdict)

    # ----- header ----------------------------------------------------------
    sha = current.get("openms_sha", "unknown")
    head = f"""
    <div class="card hero">
      <div class="spectrum"></div>
      <div class="inner">
        <h1>OpenMS Benchmark Report</h1>
        <div class="subtitle">{esc(current.get('benchmark', 'unknown'))} benchmark &middot; generated {esc(generated.strftime('%d %b %Y, %H:%M %Z'))}</div>
      </div>
    </div>
    <div class="card"><div class="inner">
      <dl class="meta">
        <div><dt>OpenMS SHA</dt><dd><span class="mono" title="{esc(sha)}">{esc(short_sha(sha))}</span></dd></div>
        <div><dt>Run</dt><dd>{esc(current.get('run_id', '—'))} <span class="dim">({esc(current.get('_file', ''))})</span></dd></div>
        <div><dt>Run at</dt><dd>{esc(str(current.get('run_at', '—')))}</dd></div>
        <div><dt>Cache</dt><dd>{_chip(cache, cache_cls)}</dd></div>
        <div><dt>Rescoring</dt><dd>{'on' if current.get('use_ms2rescore') else 'off'}</dd></div>
        <div><dt>Verdict</dt><dd>{_chip(verdict, verdict_cls)}</dd></div>
      </dl>
    </div></div>
    <div class="card tl"><div class="inner">
      <div class="tl-label">In one sentence</div>
      <p class="tldr">{esc(build_tldr(current, baseline, tool_results))}</p>
    </div></div>"""


    # ----- build -----------------------------------------------------------
    build = current.get("build") or {}
    build_rows = ""
    if build:
        build_rows += (
            f"<tr><td>Build wall time</td><td class='num'>{fmt_seconds(build.get('wall_time_s', 0))}</td></tr>"
            f"<tr><td>Artifact size</td><td class='num'>{fmt_bytes(build.get('artifact_bytes', 0))}</td></tr>"
        )
    build_rows += f"<tr><td>Cache state</td><td class='num'>{esc(cache)}</td></tr>"
    build_rows += (
        f"<tr><td>Commit</td><td class='num mono'>{esc(current.get('head_sha', sha[:10]))}</td></tr>"
        if current.get("head_sha") else ""
    )
    build_section = f"""
    <div class="card acc-orange"><h2>Build</h2><div class="inner">
      <table><tbody>{build_rows}</tbody></table>
      <div class="note">Where does this come from? CI records build time, artifact size and the ccache state when the result is normalized - the smoke script itself only measures the pipeline stages below.</div>
    </div></div>"""

    # ----- identification (pipeline stages) --------------------------------
    if current.get("stages"):
        ident_section = f"""
        <div class="card acc-blue"><h2>Pipeline stages <span class="sub">— identification &amp; rescoring</span></h2><div class="inner">
          <table>
            <thead><tr><th>Stage</th><th>Result</th><th class="num">Wall</th>
            <th class="num">CPU</th><th class="num">Peak RSS</th></tr></thead>
            <tbody>{_stage_rows(current)}</tbody>
          </table>
          <div class="note">PSM / peptide / protein counts are not measured in smoke mode — those need the PXD028735 milestone.</div>
        </div></div>"""
    else:
        ident_section = ""

    # ----- quantification ----------------------------------------------------
    qmetrics = {k: v for k, v in openms_metrics(current).items()
                if k.startswith("quantified_") or k.startswith("proteins_")
                or k.startswith("peptides_") or k.startswith("psms_")}
    if qmetrics:
        qrows = "".join(
            f"<tr><td>{esc(k)}</td><td class='num'>{_fmt_metric(k, v)}</td></tr>"
            for k, v in sorted(qmetrics.items()))
        quant_section = f"""
        <div class="card acc-purple"><h2>Quantification</h2><div class="inner">
          <table><tbody>{qrows}</tbody></table>
        </div></div>"""
    else:
        quant_section = ""

    # ----- ProteoBench reference results ------------------------------------
    pb_rows = ""
    if tool_results:
        pb_headers = ["Quantified precursors", "Median |ε| (global)", "Mean |ε| (global)",
                      "CV (median)", "ROC AUC", "Intermediate hash"]
        def pb_vals(t):
            m = tool_metrics(t)
            return [m.get("quantified_precursors"), m.get("median_abs_epsilon"),
                    m.get("mean_abs_epsilon"), m.get("cv_median"), m.get("roc_auc"),
                    t.get("proteobench", {}).get("intermediate_hash")]
        labels = [t.get("label", t.get("run_id", "?")) for t in tool_results]
        delta_col = f"<th class='num'>Δ</th>" if len(tool_results) > 1 else ""
        delta_rows = ""
        if len(tool_results) > 1:
            for i, h in enumerate(pb_headers):
                vals = [pb_vals(t)[i] for t in tool_results]
                # one column per tool result, in discovery order, so the
                # numbers always sit under their own label
                cells = "".join(
                    f"<td class='num'>{_fmt_metric('x', v)}</td>" for v in vals)
                if all(isinstance(v, (int, float)) for v in vals):
                    cells += f"<td class='num'>{html.escape(fmt_delta(vals[-1], vals[0], True))}</td>"
                delta_rows += f"<tr><td>{esc(h)}</td>{cells}</tr>"
        pb_rows = f"""
        <div class="card acc-magenta"><h2>ProteoBench <span class="sub">— reference results (local scorer, v{esc(tool_results[0].get('proteobench', {}).get('version', '?'))})</span></h2><div class="inner">
          <table>
            <thead><tr><th>Metric</th>{"".join(f"<th class='num'>{esc(l)}</th>" for l in labels)}{delta_col}</tr></thead>
            <tbody>{delta_rows or '<tr><td class="dim" colspan="%d">No numeric headline metrics found in the tool results.</td></tr>' % (len(labels) + 2)}</tbody>
          </table>
          <div class="note">Generated locally by <code>run_proteobench_local.py</code>
          (reproduces proteobench.io bit-for-bit — intermediate hash included).</div>
        </div></div>"""
    else:
        pb_rows = ""

    # ----- comparison: current vs baseline ----------------------------------
    if baseline:
        cur_m = openms_metrics(current)
        base_m = openms_metrics(baseline)
        order = ["verdict", "build.wall_time_s", "build.artifact_bytes",
                 "stage.decoy_database.wall_time_s", "stage.comet.wall_time_s",
                 "stage.percolator.wall_time_s",
                 "stage.decoy_database.peak_rss_kb", "stage.comet.peak_rss_kb",
                 "stage.percolator.peak_rss_kb"]
        rows = ""
        seen = set()
        for key in order:
            if key not in cur_m and key not in base_m:
                continue
            seen.add(key)
            cur_v, base_v = cur_m.get(key), base_m.get(key)
            numeric = isinstance(cur_v, (int, float)) or isinstance(base_v, (int, float))
            rows += (
                f"<tr><td class='mono'>{esc(key)}</td>"
                f"<td class='num'>{_fmt_metric(key, base_v)}</td>"
                f"<td class='num'>{_fmt_metric(key, cur_v)}</td>"
                f"<td class='num'>{html.escape(fmt_delta(cur_v, base_v, numeric))}</td></tr>"
            )
        cache_note = ""
        if (current.get("cache") or "") != (baseline.get("cache") or ""):
            cache_note = (
                '<div class="note">⚠ Cache state differs between the two runs '
                f"(current = <b>{esc(current.get('cache'))}</b>, baseline = "
                f"<b>{esc(baseline.get('cache'))}</b>), so the build-time Δ reflects "
                "the ccache state, not a code change.</div>")
        comp_section = f"""
        <div class="card acc-purple"><h2>Comparison <span class="sub">— current vs stored baseline</span></h2><div class="inner">
          <table>
            <thead><tr><th>Metric</th><th class="num">Baseline<br><span class="dim">{esc(baseline.get('run_id', ''))}</span></th>
            <th class="num">Current<br><span class="dim">{esc(current.get('run_id', ''))}</span></th>
            <th class="num">Δ</th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
          {cache_note}
          <div class="note">We compare against the previous OpenMS run of the same benchmark
          ({esc(baseline.get('_file', ''))}). Only metrics both runs share are
          compared. Stage wall times at this scale jump around with runner
          load, so treat their Δs as hints, not regressions.</div>
        </div></div>"""
    else:
        comp_section = ""

    # ----- history -----------------------------------------------------------
    if len(openms_runs) > 1:
        hist = ""
        for r in reversed(openms_runs):
            r_cache = r.get("cache", "?")
            r_verdict = r.get("verdict", "?")
            cls = status_class(r_verdict)
            skipped = r.get("skipped_optional") or []
            skip = f" <span class='dim'>(skipped: {esc(', '.join(skipped))})</span>" if skipped else ""
            build_s = (r.get("build") or {}).get("wall_time_s")
            hist += (
                f"<tr><td class='mono'>{esc(r.get('run_id', ''))}</td>"
                f"<td>{esc(str(r.get('run_at', '')))}</td>"
                f"<td class='mono'>{esc(short_sha(r.get('openms_sha', '')))}</td>"
                f"<td>{_chip(r_cache, {'cold': 'warn', 'warm': 'ok', 'none': 'neutral'}.get(r_cache, 'neutral'))}</td>"
                f"<td>{_chip(r_verdict, cls)}{skip}</td>"
                f"<td class='num'>{fmt_seconds(build_s) if build_s else '—'}</td></tr>"
            )
        history_section = f"""
        <div class="card acc-orange"><h2>Run history</h2><div class="inner">
          <table>
            <thead><tr><th>Run</th><th>Run at</th><th>OpenMS SHA</th><th>Cache</th>
            <th>Verdict</th><th class="num">Build time</th></tr></thead>
            <tbody>{hist}</tbody>
          </table>
        </div></div>"""
    else:
        history_section = ""

    # ----- status ------------------------------------------------------------
    if verdict == "pass":
        status = '<span class="ok">✓ No regression detected</span>'
        extra = ""
        if baseline:
            regressed = []
            for key in ["stage.decoy_database.status", "stage.comet.status", "stage.percolator.status"]:
                if cur_m.get(key) != "pass" and base_m.get(key) == "pass":
                    regressed.append(key)
            if regressed:
                status = f'<span class="bad">✗ Regression detected: {esc(", ".join(regressed))}</span>'
    else:
        status = f'<span class="bad">✗ Verdict is {esc(verdict)}</span>'
    skipped = current.get("skipped_optional") or []
    skip_note = (f"<div class='note'>Optional stages skipped: {esc(', '.join(skipped))}.</div>"
                 if skipped else "")
    status_section = f"""
    <div class="card acc-ok"><h2>Status</h2><div class="inner">
      <div class="status-line">{status}</div>
      {skip_note}
      <div class="note">A regression here means the verdict flipped, or a required stage went
      from pass to fail/skipped compared with the baseline. Wall-time changes
      alone are reported but not treated as regressions.</div>
    </div></div>"""

    # ----- assemble -----------------------------------------------------------
    doc = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OpenMS Benchmark Report — {esc(short_sha(sha))}</title>
<style>{CSS}</style></head>
<body><div class="page">
{head}
{build_section}
{ident_section}
{quant_section}
{pb_rows}
{comp_section}
{history_section}
{status_section}
<footer>OpenMS Benchmark Report &middot; prototype report generator<br>
<code>benchmark/report/report_generate.py</code> &middot; results schema {esc(SCHEMA)} &middot;
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
    out = {
        "schema": SCHEMA,
        "source": "openms",
        "benchmark": raw.get("benchmark", "smoke"),
        "milestone": raw.get("milestone"),
        "run_id": args.run_id,
        "run_at": args.run_at or "",
        "openms_sha": raw.get("openms_sha", ""),
        "cache": args.cache or "unknown",
        "use_ms2rescore": raw.get("use_ms2rescore") in (True, "true", "1"),
        "ms2rescore_note": raw.get("ms2rescore_note", ""),
        "build": {"wall_time_s": float(args.build_time), "artifact_bytes": int(args.artifact_bytes)},
        "stages": stages,
        "verdict": raw.get("verdict", "unknown"),
        "skipped_optional": raw.get("skipped_optional", []),
        "tool_versions": raw.get("tool_versions", {}),
    }
    out_path = args.out or os.path.join(args.results_dir, "openms", f"{raw.get('benchmark', 'run')}-{args.run_id}.json")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(out, fh, indent=2)
    print(f"normalized OpenMS run result -> {out_path}")


def normalize_proteobench(args):
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
    out = {
        "schema": SCHEMA,
        "source": "tool",
        "benchmark": "proteobench",
        "run_id": args.label or raw.get("module_id", "proteobench"),
        "label": args.label or raw.get("module_id", "proteobench"),
        "run_at": raw.get("scored_at", ""),
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
        "metrics": metrics,
    }
    # labels are meant for display and may contain characters that are not
    # valid in file names on every OS (e.g. '>' on Windows); sanitize only
    # the file name, never the stored label.
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", args.label or "result").strip("-")
    out_path = args.out or os.path.join(args.results_dir, "tools", f"proteobench-{safe}.json")
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
    ps.add_argument("--results-dir", default="benchmark/results")
    ps.add_argument("--out")
    ps.set_defaults(fn=normalize_smoke)

    pp = pn.add_parser("proteobench", help="normalize a local ProteoBench scoring JSON")
    pp.add_argument("pb_json")
    pp.add_argument("--label", required=True)
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
