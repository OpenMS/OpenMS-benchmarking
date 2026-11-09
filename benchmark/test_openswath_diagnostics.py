#!/usr/bin/env python3
"""Tests for openswath_diagnostics.py (OpenSwath DIA failure diagnostics).

Covers the cases the runner has to survive once openswath.json says "not pass":
a real library_error, an empty one, a missing key, malformed JSON, a JSON value
that is not an object, and a missing file. Also checks that the rendered output
stays bounded, because it is written to a CI log.

Run:  python3 benchmark/test_openswath_diagnostics.py
"""

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import openswath_diagnostics as od  # noqa: E402


def _write(tmp, report, name="openswath.json"):
    path = os.path.join(tmp, name)
    with open(path, "w") as handle:
        if isinstance(report, str):
            handle.write(report)
        else:
            json.dump(report, handle)
    return path


def _report(**correctness):
    return {
        "verdict": "fail",
        "stages": [
            {"name": "openswath_workflow", "required": True, "exit_code": 0,
             "status": "pass", "reason": ""},
            {"name": "join", "required": True, "exit_code": 1,
             "status": "fail", "reason": "required stage failed"},
        ],
        "correctness": correctness,
    }


def _joined(lines):
    return "\n".join(lines)


def test_valid_library_error_is_reported():
    with tempfile.TemporaryDirectory() as tmp:
        message = (
            "feature 1 references transition native_id 'group_Precursor_i0', "
            "which the library does not define"
        )
        path = _write(tmp, _report(library_error=message))
        out = _joined(od.describe_failure(path))

        assert "verdict: fail" in out, out
        assert ("library_error: " + message) in out, out
        # the failing stage is listed, the passing one is not
        assert "stage join:" in out, out
        assert "stage openswath_workflow" not in out, out
        # artifacts are reported even though none exist here
        assert "artifact output.featureXML: missing" in out, out
    print("PASS: a valid library_error is printed verbatim")


def test_empty_library_error_is_named():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(tmp, _report(library_error="   "))
        out = _joined(od.describe_failure(path))

        assert "library_error: (empty string)" in out, out
    print("PASS: an empty library_error is named instead of printed as blank")


def test_missing_library_error_key_is_distinguished():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(tmp, _report(actual_features=6))
        out = _joined(od.describe_failure(path))

        assert "library_error: (key absent" in out, out
        # the non-passing stage is still reported, so a pure stage failure is
        # not mistaken for a silent join failure
        assert "stage join:" in out, out
    print("PASS: an absent library_error key does not look like an empty one")


def test_malformed_json_does_not_raise():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(tmp, '{"verdict": "fail", "correctness": {')
        lines = od.describe_failure(path)

        assert len(lines) == 1, lines
        assert "not valid JSON" in lines[0], lines
        # no fragment of the file's content leaks into the log
        assert "correctness" not in lines[0], lines
    print("PASS: malformed JSON is reported without echoing its content")


def test_non_object_json_is_reported():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(tmp, json.dumps(["fail"]))
        lines = od.describe_failure(path)

        assert "not a JSON object" in lines[0], lines
        assert "list" in lines[0], lines
    print("PASS: a JSON value that is not an object is reported")


def test_missing_file_is_reported():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "absent.json")
        lines = od.describe_failure(path)

        assert len(lines) == 1, lines
        assert "is missing" in lines[0], lines
    print("PASS: a missing openswath.json is reported")


def test_long_error_is_clipped():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(tmp, _report(library_error="x" * (od.MAX_ERROR_CHARS * 3)))
        out = _joined(od.describe_failure(path))

        assert "more characters]" in out, out
        for line in od.describe_failure(path):
            assert len(line) <= od.MAX_ERROR_CHARS + 200, len(line)
    print("PASS: a long library_error is clipped to a bounded log line")


def test_non_string_library_error_is_reported():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(tmp, _report(library_error={"detail": "nested"}))
        out = _joined(od.describe_failure(path))

        assert "library_error: (not a string: dict)" in out, out
    print("PASS: a non-string library_error is reported by type, not dumped")


def test_main_never_changes_exit_status():
    with tempfile.TemporaryDirectory() as tmp:
        missing = os.path.join(tmp, "absent.json")
        assert od.main([missing]) == 0
        # wrong usage is the only non-zero case, and it is not the failure path
        assert od.main([]) == 2
    print("PASS: main() returns 0 on a missing report (diagnostics never fail the run)")


def main():
    test_valid_library_error_is_reported()
    test_empty_library_error_is_named()
    test_missing_library_error_key_is_distinguished()
    test_malformed_json_does_not_raise()
    test_non_object_json_is_reported()
    test_missing_file_is_reported()
    test_long_error_is_clipped()
    test_non_string_library_error_is_reported()
    test_main_never_changes_exit_status()
    print("\nAll openswath_diagnostics tests passed.")


if __name__ == "__main__":
    main()
