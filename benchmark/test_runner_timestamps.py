#!/usr/bin/env python3
"""Regression checks for UTC timestamps emitted by benchmark runners."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent
RUNNERS = (
    ROOT / "run_openswath_benchmark.sh",
    ROOT / "run_prose_benchmark.sh",
)


def test_runners_use_timezone_aware_utc():
    for runner in RUNNERS:
        source = runner.read_text(encoding="utf-8")
        assert ".utcnow(" not in source, runner
        assert "timezone.utc" in source, runner
        assert '.strftime("%Y-%m-%dT%H:%M:%SZ")' in source, runner


def test_timestamp_format_is_unchanged():
    import datetime

    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", timestamp)


if __name__ == "__main__":
    test_runners_use_timezone_aware_utc()
    test_timestamp_format_is_unchanged()
    print("2 tests passed")
