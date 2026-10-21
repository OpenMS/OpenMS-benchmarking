#!/usr/bin/env python3
"""Explain a failed OpenSwath benchmark report from openswath.json.

The runner used to print only::

    library join failed - see library_error in openswath.json

which names the field but not its value, so a CI failure did not say *why* the
TraML <-> featureXML join failed. This module renders that value, or states
precisely why there is none (missing file, unreadable file, malformed JSON,
absent or empty ``correctness.library_error``), and lists the non-passing
stages and the artifacts the join consumed.

Everything here is best-effort: the caller is already on a failure path, so
``describe_failure`` never raises for bad input and ``main`` never changes the
benchmark's exit status. Output is bounded - it never dumps the report itself.
"""

import json
import os
import sys

#: A library_error is a short message from openswath_metrics, but clip anyway:
#: a future error could embed a whole transition list, and the log is not a
#: place for that.
MAX_ERROR_CHARS = 2000

#: Stages are already summarised in stages.tsv; the log only needs the ones
#: that did not pass.
MAX_STAGE_LINES = 8

#: Files the join consumes or produces, reported as present/absent with size.
ARTIFACTS = ("output.featureXML", "output.chrom.mzML", "output.json")


def _clip(value):
    if len(value) <= MAX_ERROR_CHARS:
        return value
    return "%s ... [%d more characters]" % (
        value[:MAX_ERROR_CHARS],
        len(value) - MAX_ERROR_CHARS,
    )


def _stage_lines(report):
    """One line per stage that did not pass, bounded, required stages first."""
    stages = report.get("stages")
    if not isinstance(stages, list):
        return []

    failing = [stage for stage in stages if isinstance(stage, dict)
               and stage.get("status") != "pass"]
    failing.sort(key=lambda stage: stage.get("required") is not True)

    lines = []
    for stage in failing[:MAX_STAGE_LINES]:
        lines.append(
            "stage %s: status=%s exit_code=%s reason=%s"
            % (
                stage.get("name", "(unnamed)"),
                stage.get("status", "(missing)"),
                stage.get("exit_code", "(missing)"),
                stage.get("reason") or "(none)",
            )
        )
    if len(failing) > MAX_STAGE_LINES:
        lines.append("... %d more non-passing stages" % (len(failing) - MAX_STAGE_LINES))
    return lines


def _artifact_lines(directory):
    """Size of each result file, so a missing featureXML is visible at a glance."""
    lines = []
    for name in ARTIFACTS:
        path = os.path.join(directory, name)
        try:
            size = os.path.getsize(path)
        except OSError:
            lines.append("artifact %s: missing" % name)
        else:
            lines.append("artifact %s: %d bytes" % (name, size))
    return lines


def describe_failure(json_path):
    """Return bounded, human-readable lines describing why the run did not pass."""
    try:
        with open(json_path, "rb") as handle:
            raw = handle.read()
    except FileNotFoundError:
        return ["openswath.json is missing: %s" % json_path]
    except OSError as exc:
        return ["openswath.json could not be read: %s" % exc]

    try:
        report = json.loads(raw)
    except ValueError as exc:
        # json.JSONDecodeError and UnicodeDecodeError are both ValueErrors.
        return ["openswath.json is not valid JSON (%d bytes): %s" % (len(raw), exc)]

    if not isinstance(report, dict):
        return ["openswath.json is not a JSON object (got %s)" % type(report).__name__]

    lines = []
    verdict = report.get("verdict")
    lines.append("verdict: %s" % (verdict if isinstance(verdict, str) else "(missing)"))

    correctness = report.get("correctness")
    if not isinstance(correctness, dict):
        lines.append("library_error: (correctness section missing or not an object)")
    elif "library_error" not in correctness:
        lines.append("library_error: (key absent - the join did not report an error)")
    else:
        error = correctness["library_error"]
        if not isinstance(error, str):
            lines.append("library_error: (not a string: %s)" % type(error).__name__)
        elif not error.strip():
            lines.append("library_error: (empty string)")
        else:
            clipped = _clip(error)
            if "\n" in clipped:
                lines.append("library_error:")
                lines.extend("  %s" % part for part in clipped.splitlines())
            else:
                lines.append("library_error: %s" % clipped)

    lines.extend(_stage_lines(report))
    lines.extend(_artifact_lines(os.path.dirname(os.path.abspath(json_path))))
    return lines


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print("usage: openswath_diagnostics.py <openswath.json>", file=sys.stderr)
        return 2

    for line in describe_failure(argv[0]):
        print(line)
    # Diagnostics never change the benchmark's exit status.
    return 0


if __name__ == "__main__":
    sys.exit(main())
