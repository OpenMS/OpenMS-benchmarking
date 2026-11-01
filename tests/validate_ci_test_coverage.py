#!/usr/bin/env python3
"""CI test-coverage guard for the lightweight PR checks.

Compares two sets and fails if they disagree:

  * expected   - every *tracked* ``test_*.py`` file under ``benchmark/``
                 (including nested directories), as reported by
                 ``git ls-files``.
  * configured - the test files listed in the ``Run Python test suites``
                 step of ``.github/workflows/pr-checks.yml``.

This script never runs tests. It only enforces that the workflow's explicit
test list stays in agreement with the repository, so a newly added test file
turns CI red until a maintainer deliberately decides where it belongs: add it
to the workflow list (if it is lightweight enough for PR CI) or record it in
EXCLUDED_TESTS below with a reason (if it is not). Nothing is ever skipped
silently.

The workflow is parsed with PyYAML. Only ``jobs`` is read: the top-level
``on`` key is deliberately never accessed, because YAML 1.1 loads it as the
boolean ``True`` rather than the string ``"on"``.

Exit codes: 0 = in agreement, 1 = coverage mismatch, 2 = guard could not run.
"""

import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "pr-checks.yml"
WORKFLOW_STEP = "Run Python test suites"
SCOPE_DIR = "benchmark"

# Deliberate exclusions: tracked benchmark test files that are intentionally
# NOT run by the lightweight PR CI, each with its reason. A test belongs here
# only if it cannot run in this environment (heavy dependencies, datasets,
# GPUs, OpenMS binaries, ...). Every other test must be listed in the
# workflow's "Run Python test suites" step. Excluded files are reported by
# this guard on every run, so the exclusion stays visible.
EXCLUDED_TESTS = {
    # "benchmark/test_example.py": "requires OpenMS binaries and reference data",
}


def fatal(message):
    """Guard could not run (exit 2), distinct from a coverage mismatch (1)."""
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(2)


def discovered_tests():
    """Tracked test_*.py files under benchmark/, nested directories included."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--", f"{SCOPE_DIR}/"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError:
        fatal("git is not available on PATH")
    except subprocess.CalledProcessError as exc:
        fatal(f"git ls-files failed: {exc.stderr.strip()}")
    return sorted(
        path
        for path in result.stdout.split("\0")
        if path
        and path.endswith(".py")
        and Path(path).name.startswith("test_")
        and (REPO_ROOT / path).is_file()
    )


def configured_tests():
    """Test files listed in the workflow's 'Run Python test suites' step."""
    if not WORKFLOW.is_file():
        fatal(f"workflow not found: {WORKFLOW}")
    try:
        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        steps = doc["jobs"]["tests"]["steps"]
    except yaml.YAMLError as exc:
        fatal(f"cannot parse {WORKFLOW.name}: {exc}")
    except (KeyError, TypeError) as exc:
        fatal(f"unexpected structure in {WORKFLOW.name} (jobs.tests.steps): {exc!r}")
    step = next(
        (s for s in steps if isinstance(s, dict) and s.get("name") == WORKFLOW_STEP),
        None,
    )
    if step is None:
        fatal(f"no step named {WORKFLOW_STEP!r} in {WORKFLOW.name}")
    run = step.get("run")
    if not isinstance(run, str):
        fatal(f"step {WORKFLOW_STEP!r} has no run block")
    # The workflow keeps an explicit `for f in \ path \ ...; do` list. Only
    # the loop header is parsed, never the body, and a change to the loop
    # shape fails loudly here instead of silently passing the guard.
    match = re.search(r"for\s+f\s+in(.*?);?\s*do\b", run, re.DOTALL)
    if not match:
        fatal(f"no 'for f in ...; do' test list found in step {WORKFLOW_STEP!r}")
    entries = [
        token.rstrip(";") for token in match.group(1).replace("\\", " ").split()
    ]
    tests = [entry for entry in entries if entry.endswith(".py")]
    if not tests:
        fatal(f"the test list in step {WORKFLOW_STEP!r} contains no .py entries")
    return tests


def main():
    expected = discovered_tests()
    configured = configured_tests()

    duplicates = sorted({t for t in configured if configured.count(t) > 1})
    out_of_scope = sorted({t for t in configured if not t.startswith(f"{SCOPE_DIR}/")})
    stale = sorted(
        {
            t
            for t in configured
            if t.startswith(f"{SCOPE_DIR}/") and t not in expected
        }
    )
    missing = sorted(
        t for t in expected if t not in configured and t not in EXCLUDED_TESTS
    )
    stale_exclusions = sorted(t for t in EXCLUDED_TESTS if t not in expected)

    print("== CI test coverage guard ==")
    print(f"discovered: {len(expected)} tracked test_*.py files under {SCOPE_DIR}/")
    print(
        f"configured: {len(configured)} entries in the {WORKFLOW_STEP!r} step "
        f"of {WORKFLOW.name}"
    )
    print(f"excluded:   {len(EXCLUDED_TESTS)} deliberate exclusions")
    for path, reason in sorted(EXCLUDED_TESTS.items()):
        print(f"  [excluded] {path} - {reason}")
    print()

    def report(label, paths, hint):
        if paths:
            print(f"[{label}]")
            for path in paths:
                print(f"  {path}")
            print(f"  hint: {hint}")
            print()

    report(
        "MISSING",
        missing,
        "add it to the workflow's 'Run Python test suites' list, or to "
        "EXCLUDED_TESTS in tests/validate_ci_test_coverage.py with a reason",
    )
    report(
        "STALE",
        stale,
        "listed in the workflow but not a tracked test_*.py under "
        "benchmark/ - remove or correct the entry",
    )
    report(
        "DUPLICATE",
        duplicates,
        "listed more than once in the workflow - remove the duplicate",
    )
    report(
        "OUT-OF-SCOPE",
        out_of_scope,
        "the workflow list must only contain paths under benchmark/",
    )
    report(
        "STALE-EXCLUSION",
        stale_exclusions,
        "EXCLUDED_TESTS references a file that is not a discovered test - "
        "remove the outdated exclusion",
    )

    problems = len(missing) + len(stale) + len(duplicates) + len(out_of_scope)
    problems += len(stale_exclusions)
    if problems:
        print(f"RESULT: FAIL - {problems} problem(s)")
        return 1
    print(
        f"RESULT: OK - all {len(expected)} discovered test file(s) are "
        f"accounted for ({len(EXCLUDED_TESTS)} excluded)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
