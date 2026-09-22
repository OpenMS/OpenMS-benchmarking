#!/usr/bin/env python3
"""Generate the static GitHub Pages site for OpenMS-benchmarking.

Two views over the same ProteoBench external-context data layer
(benchmark/proteobench_compare.py, reused unmodified as a library):

  index.html    landing page linking both views
  pinned.html   REFERENCE view (default): the vendored, byte-verbatim
                snapshot of Proteobench/Results_quant_ion_DDA at the
                pinned commit, plus the local Exp-1/Exp-2 ProteoBench-
                scorer results, clearly identified as local.
  latest.html   OPTIONAL view: the upstream repository at its current
                HEAD (supplied via --latest-from; this script never
                touches the network). Clearly labeled as latest and
                never silently replacing the pinned reference view.

The pinned view is fully deterministic and reproducible from a clean
checkout with no network access. The latest view is inherently tied to
the upstream HEAD it was generated from; it records that HEAD SHA (no
wall-clock timestamps anywhere).
"""

import argparse
import importlib.util
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PC_PATH = os.path.join(_HERE, "proteobench_compare.py")
_spec = importlib.util.spec_from_file_location("proteobench_compare", _PC_PATH)
pc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pc)


def _label_local_rows(summary):
    """Make the Exp-1/Exp-2 identity visible in the rendered tables.

    The shared renderer shows each local row's software name; prepend the
    row's own label (e.g. 'Exp 1 (Comet->Percolator)') so the page shows
    which local experiment each row is. Pages-presentation only: the
    comparison tool's own output is unchanged.
    """
    for row in summary["local_results"]:
        if row.get("label") and row.get("software"):
            row["software"] = f"{row['label']} - {row['software']}"
    return summary


def build_summary_from(external_dir):
    """Build the deterministic comparison summary for one external dir."""
    external = pc.load_external_submissions(external_dir)
    if not external:
        raise SystemExit(f"no submission JSONs found in {external_dir}")
    local_paths = list(pc._DEFAULT_LOCAL)
    missing = [p for p in local_paths if not os.path.exists(p)]
    for path in missing:
        print(f"note: local result not found, skipped: {path}", file=sys.stderr)
    local = pc.load_local_results([p for p in local_paths if os.path.exists(p)])
    return _label_local_rows(pc.build_summary(external, local))

_VIEW_STYLE = (
    "<style>"
    ".viewnav{margin:0 0 1rem;padding:0.4rem 0;border-bottom:2px solid #ccc;}"
    ".viewnav a{margin-right:1.2rem;}"
    ".banner{border:2px solid;padding:0.6rem 0.8rem;margin:0.8rem 0;border-radius:4px;}"
    ".banner.pinned{border-color:#1a56db;background:#eff6ff;}"
    ".banner.latest{border-color:#b45309;background:#fffbeb;}"
    "</style>"
)


def _nav(active):
    pinned_label = "[Reference view (pinned)]" if active == "pinned" else "Reference view (pinned) &mdash; default"
    latest_label = "[Latest view]" if active == "latest" else "Latest view (optional)"
    return (
        '<nav class="viewnav"><strong>Views:</strong> '
        '<a href="index.html">Overview</a> | '
        f'<a href="pinned.html">{pinned_label}</a> | '
        f'<a href="latest.html">{latest_label}</a>'
        "</nav>"
    )


def _inject(html, banner, title_suffix):
    """Insert banner + view style after <body> and suffix the <title>."""
    body_anchor = "<body>\n"
    if html.count(body_anchor) != 1:
        raise SystemExit("unexpected render_html structure: <body> anchor not unique")
    html = html.replace(body_anchor, body_anchor + _VIEW_STYLE + banner, 1)
    title_anchor = "<title>ProteoBench External-Context Comparison</title>"
    if html.count(title_anchor) != 1:
        raise SystemExit("unexpected render_html structure: <title> anchor not unique")
    html = html.replace(
        title_anchor,
        f"<title>ProteoBench External-Context Comparison{title_suffix}</title>",
        1,
    )
    return html


def render_pinned(summary):
    banner = (
        _nav("pinned")
        + '<div class="banner pinned">'
        + '<strong>Reference view (pinned) &mdash; the default.</strong> '
        + f'External submissions: <code>{pc.UPSTREAM_REPO}</code> @ '
        + f'<code>{pc.SOURCE_COMMIT}</code> (vendored byte-verbatim snapshot; '
        + 'per-file SHA256 hashes in <code>benchmark/results/proteobench/external/PROVENANCE.md</code>). '
        + f'Module <code>{pc.MODULE_ID}</code>. '
        + 'Rows marked &ldquo;local ProteoBench scorer&rdquo; are our own Exp-1/Exp-2 results, '
        + 'kept separate from the external submissions. '
        + 'This page is reproducible from the repository checkout alone; it is not &ldquo;latest&rdquo;.'
        + "</div>"
    )
    return _inject(pc.render_html(summary), banner, " &mdash; Reference (pinned) view")


def render_latest(summary, head_sha):
    banner = (
        _nav("latest")
        + '<div class="banner latest">'
        + '<strong>Latest view &mdash; NOT the reference.</strong> '
        + f'Generated from <code>{pc.UPSTREAM_REPO}</code> at upstream HEAD '
        + f'<code>{head_sha}</code>. '
    )
    if head_sha == pc.SOURCE_COMMIT:
        banner += "Upstream HEAD currently equals the pinned commit. "
    banner += (
        'The <a href="pinned.html">pinned reference view</a> remains the citable default; '
        'this page reflects whatever upstream contained when the site was last generated.'
        "</div>"
    )
    html = pc.render_html(summary)
    # The shared renderer's footer asserts the PINNED provenance; in the
    # latest view that would be false, so replace that one paragraph with
    # the latest equivalent (asserted, never silent).
    m = re.search(r"<p><strong>Provenance\.</strong>.*?</p>", pc._PROVENANCE_FOOTER, re.S)
    if not m:
        raise SystemExit("unexpected _PROVENANCE_FOOTER structure: no provenance paragraph")
    old_p = m.group(0)
    new_p = (
        f"<p><strong>Provenance.</strong> Submissions: {pc.UPSTREAM_REPO} @ "
        f"upstream HEAD <code>{head_sha}</code> (latest view, generated from a "
        "fresh upstream clone). The pinned reference snapshot and its per-file "
        'SHA256 hashes live in the <a href="pinned.html">reference view</a> / '
        "<code>benchmark/results/proteobench/external/PROVENANCE.md</code>. "
        f"Module: <code>{pc.MODULE_ID}</code>.</p>"
    )
    if html.count(old_p) != 1:
        raise SystemExit("unexpected render_html output: pinned provenance paragraph not found exactly once")
    html = html.replace(old_p, new_p, 1)
    return _inject(html, banner, " &mdash; Latest view (not the reference)")

_INDEX_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OpenMS Benchmarking &mdash; ProteoBench External-Context Comparison</title>
<style>
body{{font-family:sans-serif;margin:2rem;max-width:60rem;line-height:1.5;}}
.viewcard{{border:1px solid #ccc;border-radius:6px;padding:1rem 1.2rem;margin:1rem 0;}}
.viewcard h2{{margin:0 0 0.4rem 0;font-size:1.15rem;}}
.pinned{{border-left:6px solid #1a56db;}}
.latest{{border-left:6px solid #b45309;}}
code{{background:#f3f4f6;padding:0.1rem 0.3rem;border-radius:3px;}}
.muted{{color:#555;}}
</style>
</head>
<body>
<h1>OpenMS Benchmarking &mdash; ProteoBench External-Context Comparison</h1>
<p>Descriptive external context for our OpenMS/quantms results against the
ProteoBench <code>{module_id}</code> submissions (PXD028735, QE HF-X HYE
ion-LFQ DDA). Descriptive only &mdash; no ranking is implied.</p>

<div class="viewcard pinned">
<h2><a href="pinned.html">Reference view (pinned) &mdash; default</a></h2>
<p>The vendored, byte-verbatim snapshot of
<code>{upstream_repo}</code> @ <code>{source_commit}</code>
({n_json} submission JSONs; per-file SHA256 hashes in
<code>benchmark/results/proteobench/external/PROVENANCE.md</code>),
alongside our local <strong>Exp-1</strong> (Comet&rarr;Percolator) and
<strong>Exp-2</strong> (Comet&rarr;MS2Rescore&rarr;Percolator)
ProteoBench-scorer results, explicitly identified as local.
Reproducible from the repository checkout alone.</p>
</div>

<div class="viewcard latest">
<h2><a href="latest.html">Latest view (optional)</a></h2>
<p>The same comparison generated against the upstream repository at its
current HEAD at site-generation time. <em>Not the reference</em>: the
pinned view above remains the citable default, and this page reflects
whatever upstream contained most recently.</p>
</div>

<p class="muted"><strong>Provenance.</strong> Upstream:
<code>{upstream_repo}</code>; pinned commit:
<code>{source_commit}</code>; module: <code>{module_id}</code>.
Local results are produced by our own ProteoBench scorer runs (see
<code>benchmark/results/tools/proteobench-Exp-*.json</code>) and are not
historical ProteoBench submissions.</p>
<p class="muted">Regenerate with
<code>python3 benchmark/pages_generate.py --site-dir site [--latest-from &lt;upstream-clone&gt;]</code>.</p>
</body>
</html>
"""


def _landing_page(n_json):
    return _INDEX_TEMPLATE.format(
        module_id=pc.MODULE_ID,
        upstream_repo=pc.UPSTREAM_REPO,
        source_commit=pc.SOURCE_COMMIT,
        n_json=n_json,
    )


def _upstream_head(upstream_dir):
    """Read the HEAD SHA of a local upstream clone (no network)."""
    head = os.path.join(upstream_dir, ".git", "HEAD")
    if not os.path.exists(head):
        raise SystemExit(f"--latest-from {upstream_dir} is not a git clone (no .git/HEAD)")
    with open(head, encoding="utf-8") as fh:
        content = fh.read().strip()
    if content.startswith("ref:"):
        ref = content.split(":", 1)[1].strip()
        ref_path = os.path.join(upstream_dir, ".git", ref)
        with open(ref_path, encoding="utf-8") as fh:
            return fh.read().strip()
    return content


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Generate the static GitHub Pages site "
                    "(pinned reference view + optional latest view)."
    )
    parser.add_argument(
        "--site-dir", required=True,
        help="Output directory for the generated site",
    )
    parser.add_argument(
        "--latest-from", default=None, metavar="DIR",
        help="Upstream shallow-clone directory for the optional latest view "
             "(must contain the submission JSONs at its root). "
             "Omit to generate only the pinned reference view.",
    )
    args = parser.parse_args(argv)

    os.makedirs(args.site_dir, exist_ok=True)
    # Artifact-based Pages deployment: no Jekyll processing.
    with open(os.path.join(args.site_dir, ".nojekyll"), "w", encoding="utf-8") as fh:
        fh.write("")

    with open(os.path.join(args.site_dir, "index.html"), "w",
              encoding="utf-8", newline="\n") as fh:
        fh.write(_landing_page(46))
    print(f"wrote {args.site_dir}/index.html")

    pinned_summary = build_summary_from(pc._DEFAULT_EXTERNAL)
    with open(os.path.join(args.site_dir, "pinned.html"), "w",
              encoding="utf-8", newline="\n") as fh:
        fh.write(render_pinned(pinned_summary))
    counts = pinned_summary["counts"]
    print(f"wrote {args.site_dir}/pinned.html "
          f"({counts['total']} external submissions, "
          f"{len(pinned_summary['local_results'])} local rows)")

    if args.latest_from:
        head_sha = _upstream_head(args.latest_from)
        latest_summary = build_summary_from(args.latest_from)
        with open(os.path.join(args.site_dir, "latest.html"), "w",
                  encoding="utf-8", newline="\n") as fh:
            fh.write(render_latest(latest_summary, head_sha))
        print(f"wrote {args.site_dir}/latest.html (upstream HEAD {head_sha[:12]})")
    else:
        print("note: --latest-from not provided; latest view not generated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
