#!/usr/bin/env python3
"""Test v2 schema changes for report_generate.py."""

import json
import os
import sys
import tempfile
import shutil

# Add parent directory to path so we can import report_generate
sys.path.insert(0, os.path.dirname(__file__))
from report_generate import normalize_smoke, normalize_proteobench, load_results, _promote_v1, SCHEMA_V2

def test_v1_promotion():
    """Test that v1 results are promoted to v2 correctly."""
    v1_data = {
        "schema": "openms-benchmarking/report/v1",
        "benchmark": "smoke",
        "milestone": 1,
        "openms_sha": "abc123def456",
        "cache": "warm",
        "run_id": "1234567890",
        "run_at": "2026-08-15T11:01:59Z",
        "stages": [
            {"name": "decoy_database", "wall_time_s": 1.0, "cpu_time_s": 0.8, "peak_rss_kb": 1000, "status": "pass"},
            {"name": "comet", "wall_time_s": 2.0, "cpu_time_s": 1.5, "peak_rss_kb": 2000, "status": "pass"},
        ],
        "verdict": "pass",
        "build": {"wall_time_s": 363, "artifact_bytes": 149265170},
    }
    
    promoted = _promote_v1(v1_data)
    
    # Check schema version
    assert promoted["schema"] == SCHEMA_V2, f"Expected v2 schema, got {promoted['schema']}"
    
    # Check v2 structure
    assert "identity" in promoted, "Missing identity field"
    assert "run" in promoted, "Missing run field"
    assert "performance" in promoted, "Missing performance field"
    assert "metrics" in promoted, "Missing metrics field"
    
    # Check identity fields
    assert promoted["identity"]["benchmark"] == "smoke"
    assert promoted["identity"]["software"]["name"] == "OpenMS"
    assert promoted["identity"]["software"]["version"] == "abc123def456"
    
    # Check run fields
    assert promoted["run"]["run_id"] == "1234567890"
    assert promoted["run"]["cache"] == "warm"
    
    # Check performance fields
    assert promoted["performance"]["wall_time_s"] == 3.0  # 1.0 + 2.0
    assert promoted["performance"]["peak_rss_kb"] == 2000  # max of stages
    
    # Check metrics
    assert promoted["metrics"]["verdict"] == "pass"
    
    # v1 compat fields should NOT exist at top level
    assert "_v1_compat" not in promoted, "_v1_compat should not exist in clean v2 output"
    assert "openms_sha" not in promoted, "openms_sha should not exist at top level"
    assert "cache" not in promoted, "cache should not exist at top level"
    assert "verdict" not in promoted, "verdict should not exist at top level"
    
    print("PASS: v1 promotion test")
    return True

def test_v2_passthrough():
    """Test that v2 results pass through unchanged."""
    v2_data = {
        "schema": SCHEMA_V2,
        "identity": {"benchmark": "smoke", "software": {"name": "OpenMS", "version": "xyz"}},
        "run": {"run_id": "999", "run_at": "", "cache": "cold"},
        "performance": {"wall_time_s": 5.0, "cpu_time_s": 4.0, "peak_rss_kb": 1000, "build": {}, "stages": []},
        "metrics": {"verdict": "pass"},
    }
    
    result = _promote_v1(v2_data)
    
    # Should be unchanged
    assert result["schema"] == SCHEMA_V2
    assert result["identity"]["software"]["version"] == "xyz"
    assert result["performance"]["wall_time_s"] == 5.0
    
    print("PASS: v2 passthrough test")
    return True

def test_load_results_v1_layout():
    """Test that load_results works with v1 directory layout."""
    with tempfile.TemporaryDirectory() as tmpdir:
        results_dir = os.path.join(tmpdir, "results")
        os.makedirs(os.path.join(results_dir, "openms"))
        os.makedirs(os.path.join(results_dir, "tools"))
        
        # Create a v1 OpenMS result
        v1_data = {
            "schema": "openms-benchmarking/report/v1",
            "benchmark": "smoke",
            "openms_sha": "test123",
            "run_id": "111",
            "run_at": "2026-01-01T00:00:00Z",
            "cache": "warm",
            "stages": [],
            "verdict": "pass",
            "build": {},
        }
        with open(os.path.join(results_dir, "openms", "smoke-111.json"), "w") as f:
            json.dump(v1_data, f)
        
        # Create a v1 tool result
        v1_tool = {
            "schema": "openms-benchmarking/report/v1",
            "source": "tool",
            "label": "Test Tool",
            "metrics": {"test_metric": 42},
        }
        with open(os.path.join(results_dir, "tools", "test-tool.json"), "w") as f:
            json.dump(v1_tool, f)
        
        openms_runs, tool_results = load_results(results_dir)
        
        assert len(openms_runs) == 1, f"Expected 1 openms run, got {len(openms_runs)}"
        assert len(tool_results) == 1, f"Expected 1 tool result, got {len(tool_results)}"
        
        # Check that v1 was promoted
        assert openms_runs[0]["schema"] == SCHEMA_V2
        assert tool_results[0]["schema"] == SCHEMA_V2
        
        print("PASS: load_results v1 layout test")
        return True

def test_load_results_v2_layout():
    """Test that load_results works with v2 directory layout."""
    with tempfile.TemporaryDirectory() as tmpdir:
        results_dir = os.path.join(tmpdir, "results")
        os.makedirs(os.path.join(results_dir, "smoke", "openms"))
        os.makedirs(os.path.join(results_dir, "proteobench", "reference"))
        
        # Create a v2 OpenMS result
        v2_data = {
            "schema": SCHEMA_V2,
            "identity": {"benchmark": "smoke", "software": {"name": "OpenMS", "version": "v2test"}},
            "run": {"run_id": "222", "run_at": "2026-02-01T00:00:00Z", "cache": "cold"},
            "performance": {"wall_time_s": 3.0, "cpu_time_s": 2.0, "peak_rss_kb": 500, "build": {}, "stages": []},
            "metrics": {"verdict": "pass"},
        }
        with open(os.path.join(results_dir, "smoke", "openms", "smoke-222.json"), "w") as f:
            json.dump(v2_data, f)
        
        # Create a v2 tool result
        v2_tool = {
            "schema": SCHEMA_V2,
            "identity": {"benchmark": "proteobench", "software": {"name": "Comet", "version": "1.0"}},
            "run": {"run_id": "333", "run_at": "2026-02-01T00:00:00Z"},
            "metrics": {"quantified_precursors": 100},
        }
        with open(os.path.join(results_dir, "proteobench", "reference", "proteobench-test.json"), "w") as f:
            json.dump(v2_tool, f)
        
        openms_runs, tool_results = load_results(results_dir)
        
        assert len(openms_runs) == 1, f"Expected 1 openms run, got {len(openms_runs)}"
        assert len(tool_results) == 1, f"Expected 1 tool result, got {len(tool_results)}"
        
        # Check that v2 was passed through
        assert openms_runs[0]["schema"] == SCHEMA_V2
        assert openms_runs[0]["identity"]["software"]["version"] == "v2test"
        assert tool_results[0]["schema"] == SCHEMA_V2
        
        print("PASS: load_results v2 layout test")
        return True

def test_load_results_mixed_layout():
    """Test that load_results handles mixed v1 and v2 layouts."""
    with tempfile.TemporaryDirectory() as tmpdir:
        results_dir = os.path.join(tmpdir, "results")
        
        # v1 layout
        os.makedirs(os.path.join(results_dir, "openms"))
        v1_data = {
            "schema": "openms-benchmarking/report/v1",
            "benchmark": "smoke",
            "openms_sha": "v1test",
            "run_id": "444",
            "run_at": "2026-01-01T00:00:00Z",
            "cache": "warm",
            "stages": [],
            "verdict": "pass",
            "build": {},
        }
        with open(os.path.join(results_dir, "openms", "smoke-444.json"), "w") as f:
            json.dump(v1_data, f)
        
        # v2 layout
        os.makedirs(os.path.join(results_dir, "smoke", "openms"))
        v2_data = {
            "schema": SCHEMA_V2,
            "identity": {"benchmark": "smoke", "software": {"name": "OpenMS", "version": "v2test"}},
            "run": {"run_id": "555", "run_at": "2026-02-01T00:00:00Z", "cache": "cold"},
            "performance": {"wall_time_s": 4.0, "cpu_time_s": 3.0, "peak_rss_kb": 600, "build": {}, "stages": []},
            "metrics": {"verdict": "pass"},
        }
        with open(os.path.join(results_dir, "smoke", "openms", "smoke-555.json"), "w") as f:
            json.dump(v2_data, f)
        
        openms_runs, tool_results = load_results(results_dir)
        
        # Should find both runs
        assert len(openms_runs) == 2, f"Expected 2 openms runs, got {len(openms_runs)}"
        
        # Both should be v2
        assert all(run["schema"] == SCHEMA_V2 for run in openms_runs)
        
        # Should be sorted by run_at
        assert openms_runs[0]["run"]["run_id"] == "444"  # older
        assert openms_runs[1]["run"]["run_id"] == "555"  # newer
        
        print("PASS: load_results mixed layout test")
        return True

if __name__ == "__main__":
    print("Running v2 schema tests...")
    print()
    
    tests = [
        test_v1_promotion,
        test_v2_passthrough,
        test_load_results_v1_layout,
        test_load_results_v2_layout,
        test_load_results_mixed_layout,
    ]
    
    passed = 0
    failed = 0
    
    for test in tests:
        try:
            if test():
                passed += 1
            else:
                failed += 1
        except Exception as e:
            print(f"FAIL: {test.__name__} failed: {e}")
            failed += 1
    
    print()
    print(f"Results: {passed} passed, {failed} failed")
    
    if failed > 0:
        sys.exit(1)
    else:
        print("SUCCESS: All tests passed!")
