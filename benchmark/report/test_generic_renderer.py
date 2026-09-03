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
"""

import json
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
    _flat_compare, fmt_seconds, fmt_bytes, fmt_delta
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


def test_normalize_openswath_basic():
    """normalize_openswath should produce valid v2 output from raw results."""
    from report_generate import normalize_openswath, SCHEMA_V2
    import argparse
    tmpdir = tempfile.mkdtemp()
    try:
        raw_dir = os.path.join(tmpdir, "raw")
        os.makedirs(raw_dir)

        # Write meta.txt
        with open(os.path.join(raw_dir, "meta.txt"), "w") as f:
            f.write("openms_sha=abc123def456\n")
            f.write("openswath_version=OpenMS 3.1.0\n")

        # Write stages.tsv
        with open(os.path.join(raw_dir, "stages.tsv"), "w") as f:
            f.write("openswath_workflow\ttrue\t0\t12.50\t45.20\t280000\tpass\t\n")

        # Write a minimal featureXML
        featurexml = os.path.join(raw_dir, "output.featureXML")
        with open(featurexml, "w") as f:
            f.write('<?xml version="1.0"?>\n')
            f.write('<featureList>\n')
            for i in range(6):
                f.write(f'  <feature><overallquality>{0.8 + i * 0.02}</overallquality><intensity>{1000 + i * 100}</intensity></feature>\n')
            f.write('</featureList>\n')

        # Write QC JSON
        with open(os.path.join(raw_dir, "output.json"), "w") as f:
            json.dump({"ChargeDistributionMS1": [1, 2, 3]}, f)

        # Build args
        args = argparse.Namespace(
            run_id="test-run-1",
            cache="cold",
            run_at="2026-09-01T10:00:00Z",
            dataset="",
            results_dir=raw_dir,
            out=os.path.join(tmpdir, "openswath", "openms", "openswath-test-run-1.json"),
        )
        normalize_openswath(args)

        # Read and verify output
        out_path = args.out
        assert os.path.exists(out_path), f"Output not written: {out_path}"
        with open(out_path) as f:
            result = json.load(f)

        assert result["schema"] == SCHEMA_V2
        assert result["identity"]["benchmark"] == "openswath_dia"
        assert result["identity"]["software"]["version"] == "abc123def456"
        assert result["run"]["run_id"] == "test-run-1"
        assert result["run"]["cache"] == "cold"
        assert result["performance"]["wall_time_s"] == 12.5
        assert result["performance"]["stages"][0]["name"] == "openswath_workflow"
        assert result["performance"]["stages"][0]["status"] == "pass"
        assert result["metrics"]["verdict"] == "pass"
        assert result["correctness"]["actual_features"] == 6
        assert result["correctness"]["features_match"] is True
        assert result["correctness"]["expected_features"] == 6
        assert len(result["correctness"]["qc_charge_distribution"]) == 3
        assert "tool_versions" not in result
        print("  PASS: normalize_openswath basic")
    finally:
        shutil.rmtree(tmpdir)


def test_normalize_openswath_loads_via_load_results():
    """Normalized OpenSwath output should load directly without v1 promotion."""
    from report_generate import normalize_openswath, load_results, SCHEMA_V2
    import argparse
    tmpdir = tempfile.mkdtemp()
    try:
        raw_dir = os.path.join(tmpdir, "raw")
        os.makedirs(raw_dir)

        with open(os.path.join(raw_dir, "meta.txt"), "w") as f:
            f.write("openms_sha=test123\n")
        with open(os.path.join(raw_dir, "stages.tsv"), "w") as f:
            f.write("openswath_workflow\ttrue\t0\t5.00\t10.00\t100000\tpass\t\n")

        out_dir = os.path.join(tmpdir, "openswath", "openms")
        os.makedirs(out_dir)
        out_path = os.path.join(out_dir, "openswath-ci-42.json")

        args = argparse.Namespace(
            run_id="ci-42",
            cache="none",
            run_at="",
            dataset="",
            results_dir=raw_dir,
            out=out_path,
        )
        normalize_openswath(args)

        # Load via load_results (the renderer's entry point)
        # Need to use the parent of the openswath dir as results_dir
        results_base = os.path.join(tmpdir, "results")
        # Move the normalized output into the expected v2 layout
        v2_dir = os.path.join(results_base, "openswath", "openms")
        os.makedirs(v2_dir)
        shutil.copy(out_path, v2_dir)

        openms_runs, tool_results = load_results(results_base)
        assert len(openms_runs) == 1
        assert openms_runs[0]["schema"] == SCHEMA_V2  # no promotion needed
        assert openms_runs[0]["identity"]["benchmark"] == "openswath_dia"
        assert openms_runs[0]["run"]["run_id"] == "ci-42"
        print("  PASS: normalize_openswath loads via load_results")
    finally:
        shutil.rmtree(tmpdir)


def test_normalize_openswath_missing_files():
    """normalize_openswath should handle missing featureXML and QC JSON gracefully."""
    from report_generate import normalize_openswath, SCHEMA_V2
    import argparse
    tmpdir = tempfile.mkdtemp()
    try:
        raw_dir = os.path.join(tmpdir, "raw")
        os.makedirs(raw_dir)

        with open(os.path.join(raw_dir, "meta.txt"), "w") as f:
            f.write("openms_sha=sha123\n")
        with open(os.path.join(raw_dir, "stages.tsv"), "w") as f:
            f.write("openswath_workflow\ttrue\t0\t3.00\t5.00\t50000\tpass\t\n")
        # No featureXML, no output.json

        out_path = os.path.join(tmpdir, "result.json")
        args = argparse.Namespace(
            run_id="minimal",
            cache="none",
            run_at="",
            dataset="",
            results_dir=raw_dir,
            out=out_path,
        )
        normalize_openswath(args)

        with open(out_path) as f:
            result = json.load(f)

        assert result["schema"] == SCHEMA_V2
        assert result["correctness"]["actual_features"] == 0
        assert result["correctness"]["features_match"] is False
        assert result["correctness"]["overall_quality_sum"] == 0
        assert result["correctness"]["total_intensity"] == 0
        assert result["correctness"]["qc_charge_distribution"] == []
        print("  PASS: normalize_openswath missing files")
    finally:
        shutil.rmtree(tmpdir)


def test_normalize_openswath_required_stage_fail():
    """normalize_openswath should set verdict=fail when a required stage fails."""
    from report_generate import normalize_openswath
    import argparse
    tmpdir = tempfile.mkdtemp()
    try:
        raw_dir = os.path.join(tmpdir, "raw")
        os.makedirs(raw_dir)

        with open(os.path.join(raw_dir, "meta.txt"), "w") as f:
            f.write("openms_sha=sha456\n")
        with open(os.path.join(raw_dir, "stages.tsv"), "w") as f:
            f.write("openswath_workflow\ttrue\t1\t1.00\t0.50\t1000\tfail\tcrashed\n")

        out_path = os.path.join(tmpdir, "result.json")
        args = argparse.Namespace(
            run_id="fail-case",
            cache="none",
            run_at="",
            dataset="",
            results_dir=raw_dir,
            out=out_path,
        )
        normalize_openswath(args)

        with open(out_path) as f:
            result = json.load(f)
        assert result["metrics"]["verdict"] == "fail"
        assert result["performance"]["stages"][0]["status"] == "fail"
        print("  PASS: normalize_openswath required stage fail")
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
        test_normalize_openswath_basic,
        test_normalize_openswath_loads_via_load_results,
        test_normalize_openswath_missing_files,
        test_normalize_openswath_required_stage_fail,
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
