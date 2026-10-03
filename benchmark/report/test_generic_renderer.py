#!/usr/bin/env python3
"""
Comprehensive tests for the generic renderer.

Tests:
1. v1 backward compatibility (old files still render)
2. v2 Smoke rendering
3. v2 ProteoBench rendering
4. v2 OpenSwath rendering
5. Synthetic future benchmark rendering
6. Mixed v1/v2 results
7. Dynamic stage discovery (no hard-coded names)
8. Dynamic metric comparison
9. Missing/optional sections
10. OpenSwath normalization (raw v1 -> v2)
11. OpenSwath baseline selection + comparison end-to-end
12. CI artifact discovery at any depth (merged-artifact layout)
"""

import json
import math
import os
import sys
import tempfile
import shutil

# Ensure we can import the module
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from report_generate import (
    _promote_v1, load_results, render, _identity, _run, _performance,
    _metrics, _verdict, _cache, _stages, _build, _software_version,
    _benchmark_name, _dataset, _run_id, _discover_shared_metrics,
    _flat_compare, fmt_seconds, fmt_bytes, fmt_delta, pick_baseline,
    normalize_openswath, _sha_is_ancestor, _GH_COMPARE_CACHE, _GH_API_DISABLED,
    _runtime_source, _runtime_identity, render_cmd
)


def test_promote_v1_smoke():
    """v1 smoke file should be promoted to v2 correctly."""
    v1 = {
        "schema": "openms-benchmarking/report/v1",
        "source": "openms",
        "benchmark": "smoke",
        "milestone": 1,
        "run_id": "31881021123",
        "run_at": "2026-08-15T11:01:59Z",
        "openms_sha": "f1768367fa66f7901b4fa78a9ebece64b2ce9024",
        "cache": "warm",
        "dataset": "smoke fixture (CometAdapter_3)",
        "use_ms2rescore": False,
        "build": {"wall_time_s": 363.0, "artifact_bytes": 149265170},
        "stages": [
            {"name": "decoy_database", "wall_time_s": 5.05, "status": "pass"},
            {"name": "comet", "wall_time_s": 5.23, "status": "pass"},
        ],
        "verdict": "pass",
    }
    v2 = _promote_v1(v1)
    assert v2["schema"] == "openms-benchmarking/report/v2"
    assert _benchmark_name(v2) == "smoke"
    assert _software_version(v2) == "f1768367fa66f7901b4fa78a9ebece64b2ce9024"
    assert _cache(v2) == "warm"
    assert _verdict(v2) == "pass"
    assert len(_stages(v2)) == 2
    assert _build(v2)["wall_time_s"] == 363.0
    print("  PASS: v1 smoke promotion")


def test_promote_v1_proteobench():
    """v1 proteobench file should be promoted to v2 correctly."""
    v1 = {
        "schema": "openms-benchmarking/report/v1",
        "source": "tool",
        "benchmark": "proteobench",
        "run_id": "Exp 1",
        "label": "Exp 1",
        "run_at": "2026-08-11T12:00:43",
        "tool": {"name": "quantms", "version": "1.8.0"},
        "proteobench": {"version": "0.18.2", "module": "quant_lfq_DDA_ion_QExactive"},
        "dataset": "PXD028735",
        "metrics": {"quantified_precursors": 34635, "roc_auc": 0.89},
    }
    v2 = _promote_v1(v1)
    assert v2["schema"] == "openms-benchmarking/report/v2"
    assert _benchmark_name(v2) == "proteobench"
    assert _run_id(v2) == "Exp 1"
    assert _metrics(v2)["quantified_precursors"] == 34635
    assert v2.get("tool", {}).get("name") == "quantms"
    print("  PASS: v1 proteobench promotion")


def test_v2_passthrough():
    """v2 results should pass through without modification."""
    v2 = {
        "schema": "openms-benchmarking/report/v2",
        "identity": {"benchmark": "smoke", "software": {"version": "abc123"}},
        "run": {"run_id": "test", "cache": "warm"},
        "performance": {"stages": []},
        "metrics": {"verdict": "pass"},
    }
    result = _promote_v1(v2)
    assert result is v2  # same object, no copy
    assert _software_version(result) == "abc123"
    print("  PASS: v2 passthrough")


def test_render_smoke_v1():
    """Renderer should handle v1 smoke files via promotion."""
    tmpdir = tempfile.mkdtemp()
    try:
        # Write v1 smoke file
        os.makedirs(os.path.join(tmpdir, "openms"), exist_ok=True)
        v1 = {
            "schema": "openms-benchmarking/report/v1",
            "source": "openms",
            "benchmark": "smoke",
            "milestone": 1,
            "run_id": "31881021123",
            "run_at": "2026-08-15T11:01:59Z",
            "openms_sha": "f1768367fa66f7901b4fa78a9ebece64b2ce9024",
            "cache": "warm",
            "dataset": "smoke fixture",
            "use_ms2rescore": False,
            "build": {"wall_time_s": 363.0, "artifact_bytes": 149265170},
            "stages": [
                {"name": "decoy_database", "wall_time_s": 5.05, "status": "pass", "cpu_time_s": 0.01, "peak_rss_kb": 41328},
                {"name": "comet", "wall_time_s": 5.23, "status": "pass", "cpu_time_s": 0.22, "peak_rss_kb": 62072},
                {"name": "percolator", "wall_time_s": 5.06, "status": "pass", "cpu_time_s": 0.06, "peak_rss_kb": 50004},
            ],
            "verdict": "pass",
            "skipped_optional": [],
            "tool_versions": {},
        }
        path = os.path.join(tmpdir, "openms", "smoke-31881021123.json")
        with open(path, "w") as f:
            json.dump(v1, f)

        openms_runs, tool_results = load_results(tmpdir)
        assert len(openms_runs) == 1
        current = openms_runs[0]

        out_path = os.path.join(tmpdir, "report.html")
        import datetime as _dt
        render(current, None, openms_runs, tool_results, out_path, _dt.datetime(2026, 8, 27, tzinfo=_dt.timezone.utc))

        with open(out_path, encoding="utf-8") as f:
            html = f.read()

        assert "smoke" in html.lower()
        assert "decoy_database" in html
        assert "comet" in html
        assert "percolator" in html
        assert "No regression detected" in html
        assert "31881021123" in html
        print("  PASS: render smoke v1")
    finally:
        shutil.rmtree(tmpdir)


def test_render_openswath():
    """Renderer should handle OpenSwath results with correctness section."""
    tmpdir = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(tmpdir, "openswath", "openms"), exist_ok=True)
        v2 = {
            "schema": "openms-benchmarking/report/v2",
            "identity": {
                "benchmark": "openswath",
                "dataset": "OpenSwath DIA fixture",
                "software": {"name": "OpenMS", "version": "f1768367"},
            },
            "run": {"run_id": "999", "run_at": "2026-08-27T10:00:00Z", "cache": "warm"},
            "performance": {
                "wall_time_s": 12.5,
                "cpu_time_s": 45.2,
                "peak_rss_kb": 280000,
                "build": {"wall_time_s": 363, "artifact_bytes": 149265170},
                "stages": [
                    {"name": "openswath_workflow", "wall_time_s": 12.5, "status": "pass", "cpu_time_s": 45.2, "peak_rss_kb": 280000}
                ],
            },
            "metrics": {"verdict": "pass"},
            "correctness": {"features_match": True, "actual_features": 6},
        }
        path = os.path.join(tmpdir, "openswath", "openms", "openswath-999.json")
        with open(path, "w") as f:
            json.dump(v2, f)

        openms_runs, tool_results = load_results(tmpdir)
        current = openms_runs[0]

        out_path = os.path.join(tmpdir, "report.html")
        import datetime as _dt
        render(current, None, openms_runs, tool_results, out_path, _dt.datetime(2026, 8, 27, tzinfo=_dt.timezone.utc))

        with open(out_path, encoding="utf-8") as f:
            html = f.read()

        assert "openswath" in html.lower()
        assert "openswath_workflow" in html
        assert "Correctness" in html
        assert "features_match" in html
        assert "actual_features" in html
        print("  PASS: render OpenSwath")
    finally:
        shutil.rmtree(tmpdir)


def test_render_proteobench():
    """Renderer should handle ProteoBench tool results."""
    tmpdir = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(tmpdir, "proteobench", "reference"), exist_ok=True)
        v2 = {
            "schema": "openms-benchmarking/report/v2",
            "identity": {
                "benchmark": "proteobench",
                "dataset": "PXD028735",
                "software": {"name": "quantms", "version": "1.8.0"},
            },
            "run": {"run_id": "Exp 1", "run_at": "2026-08-11T12:00:43"},
            "metrics": {"quantified_precursors": 34635, "roc_auc": 0.89},
            "tool": {"name": "quantms", "version": "1.8.0"},
        }
        path = os.path.join(tmpdir, "proteobench", "reference", "proteobench-exp1.json")
        with open(path, "w") as f:
            json.dump(v2, f)

        # Need at least one openms run for load_results to work
        os.makedirs(os.path.join(tmpdir, "openms"), exist_ok=True)
        v2_openms = {
            "schema": "openms-benchmarking/report/v2",
            "identity": {"benchmark": "smoke", "dataset": "fixture", "software": {"version": "abc"}},
            "run": {"run_id": "1", "cache": "warm"},
            "performance": {"stages": []},
            "metrics": {"verdict": "pass"},
        }
        with open(os.path.join(tmpdir, "openms", "smoke-1.json"), "w") as f:
            json.dump(v2_openms, f)

        openms_runs, tool_results = load_results(tmpdir)
        assert len(tool_results) == 1
        current = openms_runs[0]

        out_path = os.path.join(tmpdir, "report.html")
        import datetime as _dt
        render(current, None, openms_runs, tool_results, out_path, _dt.datetime(2026, 8, 27, tzinfo=_dt.timezone.utc))

        with open(out_path, encoding="utf-8") as f:
            html = f.read()

        assert "Reference results" in html
        assert "quantified_precursors" in html
        assert "roc_auc" in html
        assert "PXD028735" in html
        print("  PASS: render ProteoBench")
    finally:
        shutil.rmtree(tmpdir)


def test_render_synthetic_future_benchmark():
    """Renderer should handle a completely new benchmark type without code changes."""
    tmpdir = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(tmpdir, "metabolomics", "openms"), exist_ok=True)
        v2 = {
            "schema": "openms-benchmarking/report/v2",
            "identity": {
                "benchmark": "metabolomics",
                "dataset": "HMDB test set",
                "software": {"name": "OpenMS", "version": "abc123"},
                "configuration": {"mode": "positive"},
            },
            "run": {"run_id": "42", "run_at": "2026-08-27T10:00:00Z", "cache": "cold"},
            "performance": {
                "wall_time_s": 45.0,
                "cpu_time_s": 120.0,
                "peak_rss_kb": 500000,
                "build": {"wall_time_s": 600, "artifact_bytes": 200000000},
                "stages": [
                    {"name": "feature_detection", "wall_time_s": 20.0, "status": "pass", "cpu_time_s": 60.0, "peak_rss_kb": 300000},
                    {"name": "annotation", "wall_time_s": 25.0, "status": "pass", "cpu_time_s": 60.0, "peak_rss_kb": 500000},
                ],
            },
            "metrics": {
                "verdict": "pass",
                "metabolites_identified": 1234,
                "false_discovery_rate": 0.05,
            },
        }
        path = os.path.join(tmpdir, "metabolomics", "openms", "metabolomics-42.json")
        with open(path, "w") as f:
            json.dump(v2, f)

        openms_runs, tool_results = load_results(tmpdir)
        current = openms_runs[0]

        out_path = os.path.join(tmpdir, "report.html")
        import datetime as _dt
        render(current, None, openms_runs, tool_results, out_path, _dt.datetime(2026, 8, 27, tzinfo=_dt.timezone.utc))

        with open(out_path, encoding="utf-8") as f:
            html = f.read()

        assert "metabolomics" in html.lower()
        assert "feature_detection" in html
        assert "annotation" in html
        assert "metabolites_identified" in html
        assert "false_discovery_rate" in html
        assert "HMDB test set" in html
        print("  PASS: render synthetic future benchmark")
    finally:
        shutil.rmtree(tmpdir)


def test_dynamic_stage_discovery():
    """Stages should be discovered dynamically, not hard-coded."""
    tmpdir = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(tmpdir, "custom", "openms"), exist_ok=True)
        v2 = {
            "schema": "openms-benchmarking/report/v2",
            "identity": {"benchmark": "custom", "dataset": "test", "software": {"version": "v1"}},
            "run": {"run_id": "1", "cache": "warm"},
            "performance": {
                "stages": [
                    {"name": "stage_alpha", "wall_time_s": 1.0, "status": "pass", "cpu_time_s": 0.5, "peak_rss_kb": 100},
                    {"name": "stage_beta", "wall_time_s": 2.0, "status": "pass", "cpu_time_s": 1.0, "peak_rss_kb": 200},
                    {"name": "stage_gamma", "wall_time_s": 3.0, "status": "fail", "cpu_time_s": 1.5, "peak_rss_kb": 300},
                ],
            },
            "metrics": {"verdict": "fail"},
        }
        path = os.path.join(tmpdir, "custom", "openms", "custom-1.json")
        with open(path, "w") as f:
            json.dump(v2, f)

        openms_runs, tool_results = load_results(tmpdir)
        current = openms_runs[0]

        out_path = os.path.join(tmpdir, "report.html")
        import datetime as _dt
        render(current, None, openms_runs, tool_results, out_path, _dt.datetime(2026, 8, 27, tzinfo=_dt.timezone.utc))

        with open(out_path, encoding="utf-8") as f:
            html = f.read()

        # All three custom stage names should appear
        assert "stage_alpha" in html
        assert "stage_beta" in html
        assert "stage_gamma" in html
        # No hard-coded smoke stages
        assert "decoy_database" not in html
        assert "comet" not in html
        assert "percolator" not in html
        print("  PASS: dynamic stage discovery")
    finally:
        shutil.rmtree(tmpdir)


def test_dynamic_metric_comparison():
    """Comparison should discover shared metrics dynamically."""
    tmpdir = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(tmpdir, "smoke", "openms"), exist_ok=True)
        base = {
            "schema": "openms-benchmarking/report/v2",
            "identity": {"benchmark": "smoke", "dataset": "test", "software": {"version": "old"}},
            "run": {"run_id": "1", "run_at": "2026-08-01", "cache": "warm"},
            "performance": {"stages": []},
            "metrics": {"verdict": "pass", "my_custom_metric": 100},
        }
        current = {
            "schema": "openms-benchmarking/report/v2",
            "identity": {"benchmark": "smoke", "dataset": "test", "software": {"version": "new"}},
            "run": {"run_id": "2", "run_at": "2026-08-02", "cache": "warm"},
            "performance": {"stages": []},
            "metrics": {"verdict": "pass", "my_custom_metric": 120},
        }
        with open(os.path.join(tmpdir, "smoke", "openms", "smoke-1.json"), "w") as f:
            json.dump(base, f)
        with open(os.path.join(tmpdir, "smoke", "openms", "smoke-2.json"), "w") as f:
            json.dump(current, f)

        openms_runs, tool_results = load_results(tmpdir)
        assert len(openms_runs) == 2
        cur = openms_runs[-1]
        bl = openms_runs[0]

        shared = _discover_shared_metrics(cur, bl)
        assert "verdict" in shared
        assert "my_custom_metric" in shared

        out_path = os.path.join(tmpdir, "report.html")
        import datetime as _dt
        render(cur, bl, openms_runs, tool_results, out_path, _dt.datetime(2026, 8, 27, tzinfo=_dt.timezone.utc))

        with open(out_path, encoding="utf-8") as f:
            html = f.read()

        assert "my_custom_metric" in html
        assert "Comparison" in html
        print("  PASS: dynamic metric comparison")
    finally:
        shutil.rmtree(tmpdir)


def test_no_duplicate_v2_fields():
    """v2 output should not contain duplicate v1 fields at top level."""
    tmpdir = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(tmpdir, "openms"), exist_ok=True)
        # Simulate what normalize_smoke now outputs (v2 only)
        v2 = {
            "schema": "openms-benchmarking/report/v2",
            "identity": {
                "benchmark": "smoke",
                "dataset": "test",
                "software": {"name": "OpenMS", "version": "abc123"},
                "configuration": {"use_ms2rescore": False},
            },
            "run": {"run_id": "1", "cache": "warm"},
            "performance": {
                "wall_time_s": 10.0,
                "stages": [{"name": "test_stage", "wall_time_s": 10.0, "status": "pass"}],
            },
            "metrics": {"verdict": "pass"},
        }
        # Verify no duplicate top-level fields
        assert "openms_sha" not in v2  # should be in identity.software.version
        assert "cache" not in v2  # should be in run.cache
        assert "verdict" not in v2 or isinstance(v2.get("verdict"), type(None))  # should be in metrics
        # The 'verdict' key IS in metrics dict, not at top level
        assert "verdict" in v2["metrics"]
        assert "verdict" not in {k: v for k, v in v2.items() if k != "metrics"}

        with open(os.path.join(tmpdir, "openms", "smoke-1.json"), "w") as f:
            json.dump(v2, f)

        openms_runs, _ = load_results(tmpdir)
        assert len(openms_runs) == 1
        print("  PASS: no duplicate v2 fields")
    finally:
        shutil.rmtree(tmpdir)


def test_flat_compare_includes_stage_and_build():
    """_flat_compare should flatten stage and build data for comparison."""
    from report_generate import _flat_compare
    result = {
        "schema": "openms-benchmarking/report/v2",
        "identity": {"benchmark": "smoke", "software": {"version": "abc"}},
        "run": {"run_id": "1", "run_at": "2026-08-01"},
        "performance": {
            "build": {"wall_time_s": 363.0, "artifact_bytes": 149265170},
            "stages": [
                {"name": "comet", "status": "pass", "wall_time_s": 5.0, "cpu_time_s": 0.2, "peak_rss_kb": 60000},
                {"name": "percolator", "status": "pass", "wall_time_s": 4.0, "cpu_time_s": 0.1, "peak_rss_kb": 50000},
            ],
        },
        "metrics": {"verdict": "pass"},
        "correctness": {"feature_count": 6},
    }
    flat = _flat_compare(result)
    assert flat["verdict"] == "pass"
    assert flat["build.wall_time_s"] == 363.0
    assert flat["build.artifact_bytes"] == 149265170
    assert flat["stage.comet.status"] == "pass"
    assert flat["stage.comet.wall_time_s"] == 5.0
    assert flat["stage.comet.cpu_time_s"] == 0.2
    assert flat["stage.comet.peak_rss_kb"] == 60000
    assert flat["stage.percolator.status"] == "pass"
    assert flat["correctness.feature_count"] == 6
    print("  PASS: _flat_compare includes stage and build")


def test_flat_compare_empty_sections():
    """_flat_compare should not produce spurious keys for empty sections."""
    from report_generate import _flat_compare
    result = {
        "schema": "openms-benchmarking/report/v2",
        "identity": {"benchmark": "proteobench", "software": {"version": "v1"}},
        "run": {"run_id": "1"},
        "performance": {"build": {}, "stages": []},
        "metrics": {"verdict": "unknown", "roc_auc": 0.89},
    }
    flat = _flat_compare(result)
    assert "verdict" in flat
    assert "roc_auc" in flat
    # No stage or build keys for empty sections
    assert not any(k.startswith("stage.") for k in flat)
    assert not any(k.startswith("build.") for k in flat)
    print("  PASS: _flat_compare empty sections")


def test_smoke_comparison_shows_stage_data():
    """Comparison table should include stage and build metrics for Smoke runs."""
    tmpdir = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(tmpdir, "smoke", "openms"), exist_ok=True)
        base = {
            "schema": "openms-benchmarking/report/v2",
            "identity": {"benchmark": "smoke", "dataset": "test", "software": {"version": "old"}},
            "run": {"run_id": "1", "run_at": "2026-08-01", "cache": "warm"},
            "performance": {
                "build": {"wall_time_s": 500.0, "artifact_bytes": 100000},
                "stages": [
                    {"name": "decoy_database", "wall_time_s": 5.0, "cpu_time_s": 0.01, "peak_rss_kb": 40000, "status": "pass"},
                    {"name": "comet", "wall_time_s": 5.0, "cpu_time_s": 0.2, "peak_rss_kb": 60000, "status": "pass"},
                ],
            },
            "metrics": {"verdict": "pass"},
        }
        current = {
            "schema": "openms-benchmarking/report/v2",
            "identity": {"benchmark": "smoke", "dataset": "test", "software": {"version": "new"}},
            "run": {"run_id": "2", "run_at": "2026-08-02", "cache": "warm"},
            "performance": {
                "build": {"wall_time_s": 360.0, "artifact_bytes": 100000},
                "stages": [
                    {"name": "decoy_database", "wall_time_s": 3.0, "cpu_time_s": 0.01, "peak_rss_kb": 40000, "status": "pass"},
                    {"name": "comet", "wall_time_s": 4.0, "cpu_time_s": 0.2, "peak_rss_kb": 60000, "status": "pass"},
                ],
            },
            "metrics": {"verdict": "pass"},
        }
        with open(os.path.join(tmpdir, "smoke", "openms", "smoke-1.json"), "w") as f:
            json.dump(base, f)
        with open(os.path.join(tmpdir, "smoke", "openms", "smoke-2.json"), "w") as f:
            json.dump(current, f)

        openms_runs, tool_results = load_results(tmpdir)
        cur = openms_runs[-1]
        bl = openms_runs[0]

        shared = _discover_shared_metrics(cur, bl)
        # Must include stage and build keys, not just verdict
        assert "stage.decoy_database.wall_time_s" in shared
        assert "stage.comet.wall_time_s" in shared
        assert "build.wall_time_s" in shared
        assert "verdict" in shared

        # Render and check HTML contains stage data
        import datetime as _dt
        out_path = os.path.join(tmpdir, "report.html")
        render(cur, bl, openms_runs, tool_results, out_path, _dt.datetime(2026, 8, 27, tzinfo=_dt.timezone.utc))
        with open(out_path, encoding="utf-8") as f:
            html_content = f.read()
        assert "stage.decoy_database.wall_time_s" in html_content
        assert "build.wall_time_s" in html_content
        print("  PASS: smoke comparison shows stage data")
    finally:
        shutil.rmtree(tmpdir)


def test_stage_regression_detection():
    """Stage pass->fail regression should be detected."""
    tmpdir = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(tmpdir, "smoke", "openms"), exist_ok=True)
        base = {
            "schema": "openms-benchmarking/report/v2",
            "identity": {"benchmark": "smoke", "dataset": "test", "software": {"version": "old"}},
            "run": {"run_id": "1", "run_at": "2026-08-01", "cache": "warm"},
            "performance": {
                "build": {},
                "stages": [
                    {"name": "comet", "wall_time_s": 5.0, "status": "pass"},
                ],
            },
            "metrics": {"verdict": "pass"},
        }
        current = {
            "schema": "openms-benchmarking/report/v2",
            "identity": {"benchmark": "smoke", "dataset": "test", "software": {"version": "new"}},
            "run": {"run_id": "2", "run_at": "2026-08-02", "cache": "warm"},
            "performance": {
                "build": {},
                "stages": [
                    {"name": "comet", "wall_time_s": 5.0, "status": "fail"},
                ],
            },
            "metrics": {"verdict": "pass"},
        }
        with open(os.path.join(tmpdir, "smoke", "openms", "smoke-1.json"), "w") as f:
            json.dump(base, f)
        with open(os.path.join(tmpdir, "smoke", "openms", "smoke-2.json"), "w") as f:
            json.dump(current, f)

        openms_runs, tool_results = load_results(tmpdir)
        cur = openms_runs[-1]
        bl = openms_runs[0]

        # Directly test regression detection logic
        shared = _discover_shared_metrics(cur, bl)
        cur_c = _flat_compare(cur)
        base_c = _flat_compare(bl)
        regressed = []
        for key in shared:
            if key.startswith("stage.") and key.endswith(".status"):
                if cur_c.get(key) != "pass" and base_c.get(key) == "pass":
                    regressed.append(key)
        assert "stage.comet.status" in regressed, f"Expected regression detected, got {regressed}"

        # Also verify the rendered HTML contains "Regression detected"
        import datetime as _dt
        out_path = os.path.join(tmpdir, "report.html")
        render(cur, bl, openms_runs, tool_results, out_path, _dt.datetime(2026, 8, 27, tzinfo=_dt.timezone.utc))
        with open(out_path, encoding="utf-8") as f:
            html_content = f.read()
        assert "Regression detected" in html_content
        print("  PASS: stage regression detection")
    finally:
        shutil.rmtree(tmpdir)


def test_proteobench_no_spurious_stage_keys():
    """ProteoBench comparison should not invent stage/build keys."""
    tmpdir = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(tmpdir, "proteobench", "reference"), exist_ok=True)
        base = {
            "schema": "openms-benchmarking/report/v2",
            "identity": {"benchmark": "proteobench", "dataset": "PXD028735", "software": {"version": "quantms-1.0"}},
            "run": {"run_id": "Exp 1", "run_at": "2026-08-01"},
            "performance": {"build": {}, "stages": []},
            "metrics": {"verdict": "unknown", "roc_auc": 0.89, "quantified_precursors": 34635},
            "tool": {"name": "quantms", "version": "1.0"},
        }
        current = {
            "schema": "openms-benchmarking/report/v2",
            "identity": {"benchmark": "proteobench", "dataset": "PXD028735", "software": {"version": "quantms-1.1"}},
            "run": {"run_id": "Exp 2", "run_at": "2026-08-02"},
            "performance": {"build": {}, "stages": []},
            "metrics": {"verdict": "unknown", "roc_auc": 0.91, "quantified_precursors": 41379},
            "tool": {"name": "quantms", "version": "1.1"},
        }
        with open(os.path.join(tmpdir, "proteobench", "reference", "pb-1.json"), "w") as f:
            json.dump(base, f)
        with open(os.path.join(tmpdir, "proteobench", "reference", "pb-2.json"), "w") as f:
            json.dump(current, f)

        _, tool_results = load_results(tmpdir)
        cur = tool_results[-1]
        bl = tool_results[0]

        shared = _discover_shared_metrics(cur, bl)
        # Should only have real metrics, no stage/build keys
        stage_keys = [k for k in shared if k.startswith('stage.')]
        build_keys = [k for k in shared if k.startswith('build.')]
        assert not stage_keys, f'Unexpected stage keys: {stage_keys}'
        assert not build_keys, f'Unexpected build keys: {build_keys}'
        assert "roc_auc" in shared
        assert "quantified_precursors" in shared
        print("  PASS: proteobench no spurious stage keys")
    finally:
        shutil.rmtree(tmpdir)


def test_formatter_helpers():
    """Test formatting helper functions."""
    assert fmt_seconds(5.05) == "5.0s"
    assert fmt_seconds(65.0) == "1m 05s"
    assert fmt_seconds(3665.0) == "1h 01m 05s"
    assert fmt_bytes(149265170) == "142.4 MiB"
    assert fmt_bytes(41328 * 1024) == "40.4 MiB"
    print("  PASS: formatter helpers")


def _write_raw_openswath(path, run_id, run_at, wall=2.76, verdict="pass"):
    """Write a raw CI-style openswath.json (mirrors run_openswath_benchmark.sh output)."""
    raw = {
        "schema": "openms-benchmarking/report/v1",
        "source": "openms",
        "benchmark": "openswath_dia",
        "milestone": 2,
        "run_id": run_id,
        "run_at": run_at,
        "openms_sha": "f1768367fa66f7901b4fa78a9ebece64b2ce9024",
        "cache": "none",
        "dataset": "OpenSwathWorkflow_1 (DIA, 7 peptides, 5 SWATH windows)",
        "stages": [
            {"name": "openswath_workflow", "required": True, "exit_code": 0,
             "wall_time_s": wall, "cpu_time_s": 0.21, "peak_rss_kb": 65656,
             "status": "pass", "reason": ""}
        ],
        "verdict": verdict,
        "correctness": {
            "expected_features": 6, "actual_features": 6, "features_match": True,
            "overall_quality_sum": 14.088245, "total_intensity": 149891.59,
            "qc_charge_distribution": [[1, 19], [2, 19], [3, 19]],
        },
        "tool_versions": {"openswath_version": "n/a"},
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(raw, fh)
    return raw


def _normalize_args(raw_path, run_id, results_dir, **overrides):
    import argparse
    kw = dict(cache="", build_time=3412.0, artifact_bytes=149265164,
              run_at="", dataset="", out=None,
              runtime_source="source", package_filename="", package_sha256="",
              package_archive_dir="", package_date="")
    kw.update(overrides)
    return argparse.Namespace(openswath_json=raw_path, run_id=run_id,
                              results_dir=results_dir, **kw)


def _mk_run(bench, run_id, sha, run_at, cache="none"):
    """Minimal stored v2 OpenMS run for baseline-selection tests."""
    return {
        "schema": "openms-benchmarking/report/v2",
        "_file": f"{bench}/openms/{bench}-{run_id}.json",
        "identity": {
            "benchmark": bench,
            "benchmark_version": 1,
            "dataset": "synthetic",
            "software": {"name": "OpenMS", "version": sha},
            "configuration": {"use_ms2rescore": False},
        },
        "run": {"run_id": run_id, "run_at": run_at, "cache": cache},
        "performance": {
            "wall_time_s": 1.0, "cpu_time_s": 1.0, "peak_rss_kb": 1,
            "build": {"wall_time_s": 60.0, "artifact_bytes": 1000},
            "stages": [],
        },
        "metrics": {"verdict": "pass"},
    }


def test_sha_is_ancestor_offline_safe(monkeypatch=None):
    """_sha_is_ancestor treats unknown/failed lookups as None (never raises)
    and short-form prefixes of the same commit are not ancestors."""
    old_cache, old_disabled = _GH_COMPARE_CACHE.copy(), _GH_API_DISABLED
    _GH_COMPARE_CACHE.clear()
    try:
        # short-form prefix of itself -> same commit -> not an ancestor
        full = "e4b9609c95cac0c64b7d991d0c047d5fac2c7a27"
        assert _sha_is_ancestor(full, full) is False
        assert _sha_is_ancestor(full[:10], full) is False
        assert _sha_is_ancestor(full, full[:10]) is False
        # empty inputs are unknown
        assert _sha_is_ancestor("", full) is None
        # unreachable API -> None, not an exception (bogus host + no network
        # path is not a GitHub error class we can rely on, so assert only on
        # the non-raising contract for empty/disabled cases)
    finally:
        _GH_COMPARE_CACHE.clear()
        _GH_COMPARE_CACHE.update(old_cache)
        import report_generate
        report_generate._GH_API_DISABLED = old_disabled


def test_sha_is_ancestor_uses_compare_api(monkeypatch=None):
    """The compare-API path returns True for ancestors and False for
    non-ancestors, using an injected urllib handler (no real network)."""
    import report_generate
    import urllib.request as _ur

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def make_urlopen(compare_status):
        def urlopen(req, timeout=None):
            url = req.full_url
            assert "api.github.com/repos/OpenMS/OpenMS/compare/" in url
            # report_generate reads via json.load(resp): feed a tiny file obj
            import io

            resp_read = io.BytesIO(
                ('{"status": "%s"}' % compare_status).encode("ascii")
            )
            resp_read.__enter__ = lambda s: s
            resp_read.__exit__ = lambda s, *a: False
            return resp_read
        return urlopen

    old_cache, old_disabled = _GH_COMPARE_CACHE.copy(), _GH_API_DISABLED
    _GH_COMPARE_CACHE.clear()
    real_urlopen = _ur.urlopen
    try:
        base = "5f0f5de7b5019a9114a8231670534b1621250556"
        head = "e4b9609c95cac0c64b7d991d0c047d5fac2c7a27"
        _ur.urlopen = make_urlopen("ahead")  # base is ancestor of head
        assert _sha_is_ancestor(base, head) is True
        assert (base, head) in _GH_COMPARE_CACHE  # cached

        other = "ffffffffffffffffffffffffffffffffffffffff"
        _ur.urlopen = make_urlopen("diverged")  # not an ancestor
        assert _sha_is_ancestor(other, head) is False
    finally:
        _ur.urlopen = real_urlopen
        _GH_COMPARE_CACHE.clear()
        _GH_COMPARE_CACHE.update(old_cache)
        report_generate._GH_API_DISABLED = old_disabled


def test_sha_is_ancestor_rate_limit_disables(monkeypatch=None):
    """A 403/429 response disables further API use for the process instead of
    hammering a rate-limited endpoint; already-cached pairs stay cached."""
    import report_generate
    import urllib.error
    import urllib.request as _ur

    calls = []

    def urlopen_403(req, timeout=None):
        calls.append(req.full_url)
        raise urllib.error.HTTPError(req.full_url, 403, "rate limited", {}, None)

    old_cache, old_disabled = _GH_COMPARE_CACHE.copy(), _GH_API_DISABLED
    _GH_COMPARE_CACHE.clear()
    real_urlopen = _ur.urlopen
    try:
        _ur.urlopen = urlopen_403
        assert _sha_is_ancestor("aaaa", "bbbb") is None
        assert _sha_is_ancestor("cccc", "dddd") is None
        assert len(calls) == 1  # second lookup served from the disabled flag
        assert report_generate._GH_API_DISABLED is True
    finally:
        _ur.urlopen = real_urlopen
        _GH_COMPARE_CACHE.clear()
        _GH_COMPARE_CACHE.update(old_cache)
        report_generate._GH_API_DISABLED = old_disabled


def test_pick_baseline_prefers_ancestor_sha():
    """With SHA ancestry available, the baseline is the newest ancestor run,
    even when a newer non-ancestor run exists by timestamp."""
    sha_a = "1111111111111111111111111111111111111111"
    sha_b = "2222222222222222222222222222222222222222"
    sha_c = "3333333333333333333333333333333333333333"
    old_cache = _GH_COMPARE_CACHE.copy()
    _GH_COMPARE_CACHE.clear()
    _GH_COMPARE_CACHE[(sha_a, sha_c)] = True   # run A is an ancestor
    _GH_COMPARE_CACHE[(sha_b, sha_c)] = False  # run B is NOT (diverged)
    try:
        runs = [
            _mk_run("smoke", "A", sha_a, "2026-08-01T00:00:00Z"),
            _mk_run("smoke", "B", sha_b, "2026-08-02T00:00:00Z"),
        ]
        current = _mk_run("smoke", "C", sha_c, "2026-08-03T00:00:00Z")
        baseline = pick_baseline(current, runs)
        assert _run_id(baseline) == "A", _run_id(baseline)
    finally:
        _GH_COMPARE_CACHE.clear()
        _GH_COMPARE_CACHE.update(old_cache)


def test_pick_baseline_falls_back_to_timestamp_when_no_ancestor():
    """No ancestor (or ancestry unresolvable) -> timestamp rule, unchanged."""
    sha_a = "1111111111111111111111111111111111111111"
    sha_b = "2222222222222222222222222222222222222222"
    sha_c = "3333333333333333333333333333333333333333"
    old_cache = _GH_COMPARE_CACHE.copy()
    _GH_COMPARE_CACHE.clear()
    _GH_COMPARE_CACHE[(sha_a, sha_c)] = False  # A is not an ancestor
    _GH_COMPARE_CACHE[(sha_b, sha_c)] = False  # B is not an ancestor
    try:
        runs = [
            _mk_run("smoke", "A", sha_a, "2026-08-01T00:00:00Z"),
            _mk_run("smoke", "B", sha_b, "2026-08-02T00:00:00Z"),
        ]
        current = _mk_run("smoke", "C", sha_c, "2026-08-03T00:00:00Z")
        baseline = pick_baseline(current, runs)
        assert _run_id(baseline) == "B"  # latest by run_at
        # same result when ancestry explicitly disabled
        baseline = pick_baseline(current, runs, use_sha_ancestry=False)
        assert _run_id(baseline) == "B"
        # and when the current run's SHA is unusable
        current2 = _mk_run("smoke", "C", "unknown", "2026-08-03T00:00:00Z")
        baseline = pick_baseline(current2, runs)
        assert _run_id(baseline) == "B"
    finally:
        _GH_COMPARE_CACHE.clear()
        _GH_COMPARE_CACHE.update(old_cache)


def test_pick_baseline_unknown_ancestry_falls_back():
    """Unresolvable ancestry (API unavailable -> None) falls back to the
    timestamp rule rather than crashing or selecting nothing."""
    sha_a = "1111111111111111111111111111111111111111"
    sha_b = "2222222222222222222222222222222222222222"
    sha_c = "3333333333333333333333333333333333333333"
    old_cache, old_disabled = _GH_COMPARE_CACHE.copy(), _GH_API_DISABLED
    _GH_COMPARE_CACHE.clear()
    _GH_COMPARE_CACHE[(sha_a, sha_c)] = None  # ancestry unresolvable
    _GH_COMPARE_CACHE[(sha_b, sha_c)] = None
    try:
        runs = [
            _mk_run("smoke", "A", sha_a, "2026-08-01T00:00:00Z"),
            _mk_run("smoke", "B", sha_b, "2026-08-02T00:00:00Z"),
        ]
        current = _mk_run("smoke", "C", sha_c, "2026-08-03T00:00:00Z")
        baseline = pick_baseline(current, runs)
        assert _run_id(baseline) == "B"
    finally:
        _GH_COMPARE_CACHE.clear()
        _GH_COMPARE_CACHE.update(old_cache)
        import report_generate
        report_generate._GH_API_DISABLED = old_disabled


def test_normalize_openswath_to_v2():
    """normalize openswath maps a raw v1 result into valid v2, preserving identity,
    stage metrics, correctness and verdict."""
    tmpdir = tempfile.mkdtemp()
    try:
        raw_path = os.path.join(tmpdir, "openswath.json")
        _write_raw_openswath(raw_path, "32995654115", "2026-08-26T18:40:29Z")
        normalize_openswath(_normalize_args(raw_path, "32995654115", tmpdir))

        out_path = os.path.join(tmpdir, "openswath_dia", "openms",
                                "openswath_dia-32995654115.json")
        assert os.path.exists(out_path), "default output path not created"
        with open(out_path, encoding="utf-8") as fh:
            out = json.load(fh)

        assert out["schema"] == "openms-benchmarking/report/v2"
        assert out["identity"]["benchmark"] == "openswath_dia"
        assert out["identity"]["software"]["version"] == \
            "f1768367fa66f7901b4fa78a9ebece64b2ce9024"
        assert out["identity"]["dataset"].startswith("OpenSwathWorkflow_1")
        assert out["run"]["run_id"] == "32995654115"
        assert out["run"]["run_at"] == "2026-08-26T18:40:29Z"  # taken from raw
        assert out["run"]["cache"] == "none"                    # taken from raw
        st = out["performance"]["stages"][0]
        assert st["name"] == "openswath_workflow" and st["wall_time_s"] == 2.76
        assert st["peak_rss_kb"] == 65656 and st["status"] == "pass"
        assert out["performance"]["wall_time_s"] == 2.76
        assert out["performance"]["build"] == {"wall_time_s": 3412.0,
                                                "artifact_bytes": 149265164}
        assert out["metrics"]["verdict"] == "pass"
        assert out["correctness"]["actual_features"] == 6
        assert out["correctness"]["features_match"] is True

        # the stored file must round-trip through the generic loader
        runs, _ = load_results(tmpdir)
        assert len(runs) == 1 and _benchmark_name(runs[0]) == "openswath_dia"
        print("  PASS: normalize openswath -> v2")
    finally:
        shutil.rmtree(tmpdir)


def test_normalize_openswath_rejects_nonfinite():
    """NaN/Infinity stage metrics must fail loudly, not silently enter the schema."""
    tmpdir = tempfile.mkdtemp()
    try:
        raw_path = os.path.join(tmpdir, "openswath.json")
        raw = _write_raw_openswath(raw_path, "X", "2026-08-26T18:40:29Z")
        raw["stages"][0]["wall_time_s"] = float("nan")
        with open(raw_path, "w", encoding="utf-8") as fh:
            json.dump(raw, fh)  # emits a bare NaN token
        try:
            normalize_openswath(_normalize_args(raw_path, "X", tmpdir))
        except SystemExit:
            print("  PASS: normalize openswath rejects non-finite metrics")
        else:
            raise AssertionError("NaN wall_time_s was accepted")
    finally:
        shutil.rmtree(tmpdir)


def test_normalize_openswath_baseline_comparison():
    """Two normalized OpenSwath runs get baseline selection and a comparison
    table from the existing generic machinery - no OpenSwath-specific code."""
    tmpdir = tempfile.mkdtemp()
    try:
        for rid, at, wall in (("100", "2026-08-26T18:40:29Z", 2.76),
                              ("200", "2026-08-27T18:40:29Z", 3.10)):
            raw_path = os.path.join(tmpdir, f"raw-{rid}.json")
            _write_raw_openswath(raw_path, rid, at, wall=wall)
            normalize_openswath(_normalize_args(raw_path, rid, tmpdir))

        runs, tools = load_results(tmpdir)
        assert len(runs) == 2 and not tools
        current = runs[-1]                     # newest by run_at
        baseline = pick_baseline(current, runs)
        assert _run_id(baseline) == "100" and _run_id(current) == "200"

        shared = _discover_shared_metrics(current, baseline)
        assert "stage.openswath_workflow.wall_time_s" in shared
        assert "correctness.actual_features" in shared
        assert "build.wall_time_s" in shared

        out_path = os.path.join(tmpdir, "report.html")
        import datetime as _dt
        render(current, baseline, runs, tools, out_path,
               _dt.datetime(2026, 8, 27, tzinfo=_dt.timezone.utc))
        with open(out_path, encoding="utf-8") as fh:
            html = fh.read()
        assert "openswath_dia" in html
        assert "openswath_workflow" in html
        assert "Correctness" in html and "features_match" in html
        assert "current vs previous run" in html
        assert "32995654115" not in html  # synthetic ids only
        print("  PASS: normalize openswath baseline comparison end-to-end")
    finally:
        shutil.rmtree(tmpdir)


def test_normalize_openswath_missing_correctness_defaults():
    """Adapted from PR #1's test_normalize_openswath_missing_files: a raw result
    without correctness/tool_versions blocks normalizes with empty defaults
    instead of crashing (promised by the current raw-JSON implementation)."""
    tmpdir = tempfile.mkdtemp()
    try:
        raw_path = os.path.join(tmpdir, "openswath.json")
        raw = _write_raw_openswath(raw_path, "MIN", "2026-08-26T18:40:29Z")
        raw.pop("correctness")
        raw.pop("tool_versions")
        with open(raw_path, "w", encoding="utf-8") as fh:
            json.dump(raw, fh)
        normalize_openswath(_normalize_args(raw_path, "MIN", tmpdir))

        out_path = os.path.join(tmpdir, "openswath_dia", "openms",
                                "openswath_dia-MIN.json")
        with open(out_path, encoding="utf-8") as fh:
            out = json.load(fh)
        assert out["schema"] == "openms-benchmarking/report/v2"
        assert out["correctness"] == {}
        assert out["tool_versions"] == {}
        assert out["metrics"]["verdict"] == "pass"
        print("  PASS: normalize openswath missing correctness/tool_versions -> empty defaults")
    finally:
        shutil.rmtree(tmpdir)


def test_normalize_openswath_missing_or_empty_stages():
    """Adapted from PR #1's test_normalize_openswath_missing_stages/_empty_stages:
    missing or empty stage lists must not crash and must aggregate to zero,
    with verdict taken from the raw payload (default 'unknown')."""
    tmpdir = tempfile.mkdtemp()
    try:
        raw_path = os.path.join(tmpdir, "openswath.json")
        raw = _write_raw_openswath(raw_path, "NOSTAGE", "2026-08-26T18:40:29Z")
        raw.pop("stages")
        raw.pop("verdict")
        with open(raw_path, "w", encoding="utf-8") as fh:
            json.dump(raw, fh)
        normalize_openswath(_normalize_args(raw_path, "NOSTAGE", tmpdir))
        with open(os.path.join(tmpdir, "openswath_dia", "openms",
                               "openswath_dia-NOSTAGE.json"), encoding="utf-8") as fh:
            out = json.load(fh)
        assert out["performance"]["stages"] == []
        assert out["performance"]["wall_time_s"] == 0
        assert out["performance"]["cpu_time_s"] == 0
        assert out["performance"]["peak_rss_kb"] == 0
        assert out["metrics"]["verdict"] == "unknown"

        raw2_path = os.path.join(tmpdir, "openswath-empty.json")
        raw2 = _write_raw_openswath(raw2_path, "EMPTYSTAGE", "2026-08-26T18:40:29Z")
        raw2["stages"] = []
        with open(raw2_path, "w", encoding="utf-8") as fh:
            json.dump(raw2, fh)
        normalize_openswath(_normalize_args(raw2_path, "EMPTYSTAGE", tmpdir))
        with open(os.path.join(tmpdir, "openswath_dia", "openms",
                               "openswath_dia-EMPTYSTAGE.json"), encoding="utf-8") as fh:
            out2 = json.load(fh)
        assert out2["performance"]["stages"] == []
        assert out2["performance"]["wall_time_s"] == 0
        print("  PASS: normalize openswath missing/empty stages -> zero aggregates")
    finally:
        shutil.rmtree(tmpdir)


def test_normalize_openswath_failure_propagation():
    """Adapted from PR #1's test_normalize_openswath_required_stage_fail: a failing
    required stage and a fail verdict must survive normalization unchanged."""
    tmpdir = tempfile.mkdtemp()
    try:
        raw_path = os.path.join(tmpdir, "openswath.json")
        raw = _write_raw_openswath(raw_path, "FAILRUN", "2026-08-26T18:40:29Z",
                                   wall=0.5, verdict="fail")
        raw["stages"][0]["status"] = "fail"
        raw["stages"][0]["exit_code"] = 1
        raw["stages"][0]["reason"] = "crashed"
        with open(raw_path, "w", encoding="utf-8") as fh:
            json.dump(raw, fh)
        normalize_openswath(_normalize_args(raw_path, "FAILRUN", tmpdir))
        with open(os.path.join(tmpdir, "openswath_dia", "openms",
                               "openswath_dia-FAILRUN.json"), encoding="utf-8") as fh:
            out = json.load(fh)
        assert out["metrics"]["verdict"] == "fail"
        st = out["performance"]["stages"][0]
        assert st["status"] == "fail" and st["exit_code"] == 1
        assert st["reason"] == "crashed"
        assert out["performance"]["wall_time_s"] == 0.5
        print("  PASS: normalize openswath failure propagation")
    finally:
        shutil.rmtree(tmpdir)


def test_runtime_source_default():
    """Pre-Stage-1 results (no identity.runtime) read as source mode."""
    run = {"schema": "openms-benchmarking/report/v2",
           "identity": {"benchmark": "smoke", "software": {"version": "abc123"}}}
    assert _runtime_source(run) == "source"
    # _runtime_identity with default args emits the minimal source object
    import argparse
    ns = argparse.Namespace(runtime_source="source")
    assert _runtime_identity(ns) == {"runtime_source": "source"}
    print("  PASS: runtime_source defaults to source")


def test_runtime_identity_package_requires_metadata():
    """Package mode without full package metadata must fail loudly (normalize
    can never write a package run whose provenance is incomplete)."""
    import argparse
    ns = argparse.Namespace(runtime_source="package", package_filename="",
                            package_sha256="abc", package_archive_dir="",
                            package_date="")
    try:
        _runtime_identity(ns)
    except SystemExit:
        print("  PASS: package identity refuses incomplete metadata")
    else:
        raise AssertionError("incomplete package metadata did not fail")


def test_normalize_openswath_package_provenance():
    """normalize openswath with --runtime-source package records the full
    package identity and keeps it distinct from a source run's identity."""
    tmpdir = tempfile.mkdtemp()
    try:
        raw_path = os.path.join(tmpdir, "openswath.json")
        _write_raw_openswath(raw_path, "424242", "2026-09-27T00:00:00Z")
        normalize_openswath(_normalize_args(
            raw_path, "424242", tmpdir,
            runtime_source="package",
            package_filename="OpenMS-3.6.0-pre-nightly-2026-09-24-Debian-Linux-x86_64.deb",
            package_sha256="deadbeef" * 8,
            package_archive_dir="2026.09.25",
            package_date="2026-09-24",
            build_time=0.0,
        ))
        out_path = os.path.join(tmpdir, "openswath_dia", "openms",
                                "openswath_dia-424242.json")
        with open(out_path, encoding="utf-8") as fh:
            out = json.load(fh)
        rt = out["identity"]["runtime"]
        assert rt["runtime_source"] == "package"
        assert rt["package"]["filename"].endswith(".deb")
        assert rt["package"]["sha256"] == "deadbeef" * 8
        assert rt["package"]["archive_dir"] == "2026.09.25"
        assert rt["package"]["date"] == "2026-09-24"
        # ...and the same file still round-trips through the loader
        runs, _ = load_results(tmpdir)
        assert len(runs) == 1 and _runtime_source(runs[0]) == "package"
        print("  PASS: normalize openswath records package provenance")
    finally:
        shutil.rmtree(tmpdir)


def test_package_run_excluded_from_sha_ancestry():
    """A package run must never be selected as an SHA-ancestor baseline, and a
    package current run must fall back to the timestamp rule."""
    base = _mk_run("smoke", "111", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                   "2026-09-01T00:00:00Z")
    base["identity"]["runtime"] = {"runtime_source": "source"}
    pkg_newer = _mk_run("smoke", "222", "8b25c6e",
                        "2026-09-02T00:00:00Z")
    pkg_newer["identity"]["runtime"] = {
        "runtime_source": "package",
        "package": {"filename": "x.deb", "sha256": "d" * 64,
                    "archive_dir": "2026.09.03", "date": "2026-09-02"},
    }
    current = _mk_run("smoke", "333", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                      "2026-09-03T00:00:00Z")
    current["identity"]["runtime"] = {"runtime_source": "source"}
    import report_generate
    saved_fn = report_generate._sha_is_ancestor
    try:
        # Deterministic ancestry: every source run is an "ancestor". The
        # package run must be filtered from the candidate pool BEFORE this
        # predicate is consulted, so despite qualifying by SHA it can't win.
        report_generate._sha_is_ancestor = lambda b, h: True
        b1 = pick_baseline(current, [base, pkg_newer, current])
        assert b1 is base, "package run selected as SHA-ancestor baseline"
        # Package current: ancestry comparison is skipped entirely, so even a
        # source run that would qualify by SHA is not used; timestamp decides.
        report_generate._sha_is_ancestor = lambda b, h: False
        b2 = pick_baseline(pkg_newer, [base, current, pkg_newer])
        assert b2 is base, "package current did not fall back to timestamp rule"
    finally:
        report_generate._sha_is_ancestor = saved_fn
    print("  PASS: package runs excluded from SHA-ancestor selection")


def test_current_run_never_its_own_baseline():
    """--current must never compare a run against itself: the current file is
    also discovered from the results tree as a *separate* object, and
    pick_baseline excludes only by object identity. Baseline selection must
    therefore exclude the current run by file path; with no other candidates
    the report renders without a comparison card instead of self-comparing."""
    tmpdir = tempfile.mkdtemp()
    try:
        run = {"schema": "openms-benchmarking/report/v2",
               "identity": {"benchmark": "smoke", "dataset": "t",
                            "software": {"version": "a" * 40}},
               "run": {"run_id": "77", "run_at": "2026-09-01T00:00:00Z",
                       "cache": "none"},
               "performance": {"stages": []},
               "metrics": {"verdict": "pass"}}
        cur_path = os.path.join(tmpdir, "smoke", "openms", "smoke-77.json")
        os.makedirs(os.path.dirname(cur_path), exist_ok=True)
        with open(cur_path, "w", encoding="utf-8") as fh:
            json.dump(run, fh)

        import argparse
        args = argparse.Namespace(results_dir=tmpdir, current=cur_path,
                                  baseline=None, no_sha_baseline=False,
                                  out=os.path.join(tmpdir, "report.html"))
        render_cmd(args)  # must not exit even with zero baseline candidates
        with open(args.out, encoding="utf-8") as fh:
            html = fh.read()
        assert "current vs previous run" not in html, \
            "current run was compared against itself"
        print("  PASS: --current is never its own baseline")
    finally:
        shutil.rmtree(tmpdir)


def test_promote_v1_carries_file_reference():
    """load_results records _file before v1 promotion; the promoted run must
    keep it. Without this, every promoted v1 run renders with an empty source
    label and --current deduplication (which keys on _file) cannot see it."""
    v1 = {"schema": "openms-benchmarking/report/v1", "source": "openms",
          "benchmark": "smoke", "run_id": "5",
          "run_at": "2026-08-01T00:00:00Z",
          "openms_sha": "f1768367fa66f7901b4fa78a9ebece64b2ce9024"}
    v1["_file"] = "smoke/openms/smoke-5.json"  # as load_results sets it
    promoted = _promote_v1(v1)
    assert promoted.get("_file") == "smoke/openms/smoke-5.json"
    # ...and a v1 run discovered from disk carries it end to end
    tmpdir = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(tmpdir, "openms"))
        with open(os.path.join(tmpdir, "openms", "smoke-5.json"), "w") as fh:
            json.dump(v1, fh)
        runs, _ = load_results(tmpdir)
        assert runs and runs[0].get("_file") == "openms/smoke-5.json"
        print("  PASS: v1 promotion carries the _file reference")
    finally:
        shutil.rmtree(tmpdir)


def test_load_results_finds_nested_ci_artifact_layout():
    """CI merges several uploaded artifacts into one results/ directory, and each
    artifact carries the job's own top-level directory (results/,
    results-openswath/, results-prose/, ...). The v2 runs therefore land at
    results/<artifact-dir>/<benchmark>/openms/*.json - one level deeper than the
    local layout. Discovery used to glob a fixed depth, so every one of those
    runs (the OpenSwath ones especially) was invisible to the CI report even
    though the Trends machinery can plot them once loaded.

    Runs must be found at any depth, the flat v1 and v2 layouts must keep
    working, and nothing may be loaded twice.
    """

    def write_json(path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def v2_run(bench, run_id, run_at, sha="v2test"):
        return {
            "schema": "openms-benchmarking/report/v2",
            "identity": {"benchmark": bench, "benchmark_version": 1,
                         "dataset": "synthetic",
                         "software": {"name": "OpenMS", "version": sha},
                         "configuration": {"use_ms2rescore": False}},
            "run": {"run_id": run_id, "run_at": run_at, "cache": "none"},
            "performance": {"wall_time_s": 1.0, "cpu_time_s": 1.0,
                            "peak_rss_kb": 1,
                            "build": {"wall_time_s": 60.0, "artifact_bytes": 1000},
                            "stages": []},
            "metrics": {"verdict": "pass"},
        }

    tmpdir = tempfile.mkdtemp()
    try:
        merged = os.path.join(tmpdir, "results")

        # nested OpenSwath run, exactly where the merged CI artifact puts it
        raw_path = os.path.join(merged, "results-openswath", "openswath.json")
        os.makedirs(os.path.dirname(raw_path), exist_ok=True)
        _write_raw_openswath(raw_path, "900", "2026-09-20T12:00:00Z")
        normalize_openswath(_normalize_args(
            raw_path, "900", os.path.join(merged, "results-openswath")))

        # nested tool result from the ProSE artifact
        write_json(os.path.join(merged, "results-prose", "proteobench",
                                "reference", "comet.json"),
                   v2_run("proteobench", "333", "2026-03-01T00:00:00Z"))

        # flat v1 layout, unchanged
        write_json(os.path.join(merged, "openms", "smoke-111.json"), {
            "schema": "openms-benchmarking/report/v1", "benchmark": "smoke",
            "openms_sha": "v1test", "run_id": "111",
            "run_at": "2026-01-01T00:00:00Z", "cache": "warm",
            "stages": [], "verdict": "pass", "build": {},
        })
        # flat v2 layout, unchanged
        write_json(os.path.join(merged, "smoke", "openms", "smoke-222.json"),
                   v2_run("smoke", "222", "2026-02-01T00:00:00Z"))

        runs, tools = load_results(merged)

        # exactly the four files written above: depth-agnostic discovery must not
        # also re-scan the nested trees through a second, shallower glob
        assert len(runs) == 3, [r["_file"] for r in runs]
        assert len(tools) == 1, [t["_file"] for t in tools]

        by_id = {r["run"]["run_id"]: r for r in runs}
        assert set(by_id) == {"111", "222", "900"}, sorted(by_id)
        assert by_id["900"]["_file"] == \
            "results-openswath/openswath_dia/openms/openswath_dia-900.json", \
            by_id["900"]["_file"]
        assert by_id["222"]["_file"] == "smoke/openms/smoke-222.json", \
            by_id["222"]["_file"]
        # the flat v1 run is still promoted on the way in
        assert by_id["111"]["schema"] == "openms-benchmarking/report/v2", by_id["111"]
        # oldest first, with the nested run joining the same ordering
        assert [r["run"]["run_id"] for r in runs] == ["111", "222", "900"]
        assert tools[0]["_file"] == \
            "results-prose/proteobench/reference/comet.json", tools[0]["_file"]
        print("  PASS: load_results finds runs in the nested CI artifact layout")
    finally:
        shutil.rmtree(tmpdir)


def main():
    print("Running generic renderer tests...\n")
    tests = [
        test_promote_v1_smoke,
        test_promote_v1_proteobench,
        test_v2_passthrough,
        test_formatter_helpers,
        test_render_smoke_v1,
        test_render_openswath,
        test_render_proteobench,
        test_render_synthetic_future_benchmark,
        test_dynamic_stage_discovery,
        test_dynamic_metric_comparison,
        test_no_duplicate_v2_fields,
        test_flat_compare_includes_stage_and_build,
        test_flat_compare_empty_sections,
        test_smoke_comparison_shows_stage_data,
        test_stage_regression_detection,
        test_proteobench_no_spurious_stage_keys,
        test_normalize_openswath_to_v2,
        test_normalize_openswath_rejects_nonfinite,
        test_sha_is_ancestor_offline_safe,
        test_sha_is_ancestor_uses_compare_api,
        test_sha_is_ancestor_rate_limit_disables,
        test_pick_baseline_prefers_ancestor_sha,
        test_pick_baseline_falls_back_to_timestamp_when_no_ancestor,
        test_pick_baseline_unknown_ancestry_falls_back,
        test_normalize_openswath_baseline_comparison,
        test_normalize_openswath_missing_correctness_defaults,
        test_normalize_openswath_missing_or_empty_stages,
        test_normalize_openswath_failure_propagation,
        test_runtime_source_default,
        test_runtime_identity_package_requires_metadata,
        test_normalize_openswath_package_provenance,
        test_package_run_excluded_from_sha_ancestry,
        test_current_run_never_its_own_baseline,
        test_promote_v1_carries_file_reference,
        test_load_results_finds_nested_ci_artifact_layout,
    ]
    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"  FAIL: {test.__name__}: {e}")
            failed += 1
    print(f"\n{passed}/{passed + failed} tests passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
