#!/usr/bin/env python3
"""Recover nightly package-mode benchmark results onto ``benchmark-results``.

Context
-------
The nightly workflow (``.github/workflows/nightly-benchmark.yml``) persists
generated result JSONs to the unprotected ``benchmark-results`` branch. Before
that change it pushed to protected ``main``, so nightly runs #37-#41 failed
*after* their benchmark run succeeded. The benchmark artifacts are still
retained (90 days by default), so the validated JSONs can be re-imported.

What this tool does
-------------------
1. resolves each nested benchmark run's unexpired artifacts through ``gh``;
2. downloads and extracts the smoke and OpenSwath result JSONs, refusing ZIP
   members that are absolute, contain ``..``, or are symlinks;
3. validates every JSON: object shape, schema ``openms-benchmarking/report/v2``,
   ``run.run_id`` equal to the benchmark run ID, filename suffix equal to that
   ID, ``identity.runtime.runtime_source == "package"``, ``identity.benchmark``
   matching the expected benchmark, and a timezone-aware ``run.run_at`` newer
   than the last persisted run;
3b. requires the same package identity on both arms of a run - the same
   four non-empty-string fields (``filename``, ``sha256``, ``archive_dir``,
   ``date``) enforced by the nightly persistence guard, so a mixed-package
   pair can never be imported;
4. refreshes ``origin/main`` and ``origin/<data branch>`` first and refuses to
   import unless both resolve (never checks a ref that may be stale);
5. refuses a destination whose historical result already exists on ``main``
   under ``benchmark/results/``, and on the data branch accepts only a complete,
   byte-identical pair - so a partially completed import can be re-run: finished
   runs are reported as already present and skipped;
6. in dry-run mode (the default) only stages the files and prints a summary.

With ``--apply`` it writes the two files per run into the current checkout of
``benchmark-results`` and commits them, one commit per benchmark run. It never
pushes, never creates branches, never dispatches workflows, never changes
repository settings or branch protection. A failure partway through restores
the checkout (no stray copies, no staged paths) before exiting, so the same
command can simply be run again to resume with the remaining runs.

Recovery procedure
------------------
``benchmark-results`` holds only result JSONs, so it has no copy of this
script. Run the script from a ``main`` checkout and point ``--repo-dir`` at a
separate worktree of the data branch:

    # 1. From the main checkout that contains this script, create the data
    #    worktree (skip this if it already exists):
    git fetch origin
    git worktree add ../openms-benchmarking-data -b benchmark-results origin/benchmark-results
    #    (or, when a local benchmark-results branch already exists:)
    #    git worktree add ../openms-benchmarking-data benchmark-results

    # 2. Dry run: validates and stages, writes and commits nothing:
    python benchmark/recover_nightly_results.py --repo-dir ../openms-benchmarking-data

    # 3. Review the summary, then import locally - one commit per run, no push:
    python benchmark/recover_nightly_results.py --repo-dir ../openms-benchmarking-data --apply

    # 4. Review the commits in the data worktree and push the branch yourself:
    git -C ../openms-benchmarking-data log --oneline -5
    git -C ../openms-benchmarking-data push origin benchmark-results

Exit codes: 0 = ok, 1 = validation/import failure, 2 = usage/environment error.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

SCHEMA = "openms-benchmarking/report/v2"
DEFAULT_REPO = "OpenMS/OpenMS-benchmarking"
DEFAULT_BRANCH = "benchmark-results"
DEFAULT_MIN_RUN_AT = "2026-11-06T03:21:53Z"
DEFAULT_STAGING = Path(tempfile.gettempdir()) / "openms-benchmark-recovery"

# Nested benchmark run -> nightly run that failed to persist it.
NIGHTLY_BY_BENCH = {
    "42333314804": 37,
    "42442257487": 38,
    "42555927417": 39,
    "42748636332": 40,
    "42951989482": 41,
}

BENCHMARKS = (
    ("smoke", "benchmark-results", "smoke-", "openms"),
    ("openswath_dia", "benchmark-openswath", "openswath_dia-", "openswath_dia/openms"),
)

# Where the same run lives on ``main``: before the storage split, generated
# results were committed under the results tree, so the data branch's
# ``openms/smoke-<id>.json`` is ``benchmark/results/openms/smoke-<id>.json``
# there.
MAIN_RESULTS_PREFIX = "benchmark/results/"


def main_result_path(rel: str) -> str:
    """The ``main`` path holding the historical result for data path ``rel``."""
    return f"{MAIN_RESULTS_PREFIX}{rel}"


class RecoveryError(RuntimeError):
    """A recoverable, user-facing failure (validation, gh, git)."""


@dataclasses.dataclass
class Validated:
    """One benchmark run's validated result pair."""

    bench_id: str
    nightly: int | None
    run_at: datetime
    files: dict[str, Path]  # branch-relative path -> staged file


def _run(cmd, cwd=None, check=True):
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        cmd, cwd=cwd, capture_output=True, text=True, check=False
    )
    if check and proc.returncode != 0:
        raise RecoveryError(
            f"{' '.join(cmd)} failed ({proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc


# ---------------------------------------------------------------------------
# downloads
# ---------------------------------------------------------------------------


def list_artifacts(repo: str, run_id: str) -> list[dict]:
    """All artifacts of a workflow run, following pagination."""
    artifacts: list[dict] = []
    page = 1
    while True:
        url = f"repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100&page={page}"
        payload = json.loads(_run(["gh", "api", url]).stdout)
        page_items = payload.get("artifacts") or []
        artifacts.extend(page_items)
        total = int(payload.get("total_count") or 0)
        if not page_items or len(artifacts) >= total:
            return artifacts
        page += 1


def select_artifacts(artifacts: list[dict]) -> dict[str, dict]:
    """Exactly one unexpired results artifact and one openswath artifact."""
    selected: dict[str, dict] = {}
    for benchmark, prefix, _file_prefix, _dir in BENCHMARKS:
        matches = [
            a
            for a in artifacts
            if str(a.get("name", "")).startswith(prefix + "-") and not a.get("expired")
        ]
        if len(matches) != 1:
            names = [a.get("name") for a in matches]
            raise RecoveryError(
                f"expected exactly one unexpired '{prefix}-*' artifact, found {len(matches)}: {names}"
            )
        selected[benchmark] = matches[0]
    return selected


def _unsafe_member(name: str) -> bool:
    """True for a ZIP member that would escape the extraction directory.

    ZIP members are POSIX paths whatever the host OS, so separators are
    normalized first and interpreted with ``PurePosixPath``. Backslash-rooted
    (``\\outside.json``), UNC (``//host/share``), drive-letter (``C:\\x``),
    absolute and ``..`` members are all rejected before anything is written.
    """
    normalized = name.replace("\\", "/")
    if not normalized or normalized.startswith("/"):
        return True  # absolute, UNC root, or empty name
    if PurePosixPath(normalized).is_absolute():
        return True
    if re.match(r"^[A-Za-z]:", normalized):
        return True  # drive-letter path
    return ".." in PurePosixPath(normalized).parts


def safe_extract(zip_path: Path, dest: Path) -> None:
    """Extract an artifact ZIP, rejecting unsafe member paths and symlinks."""
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if _unsafe_member(info.filename):
                raise RecoveryError(
                    f"refusing unsafe ZIP member {info.filename!r} in {zip_path.name}"
                )
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise RecoveryError(
                    f"refusing symlink ZIP member {info.filename!r} in {zip_path.name}"
                )
        dest.mkdir(parents=True, exist_ok=True)
        zf.extractall(dest)



def download_artifact(repo: str, run_id: str, artifact: dict, workdir: Path) -> Path:
    """Download an artifact ZIP and safely extract it."""
    artifact_id = artifact.get("id")
    if (
        isinstance(artifact_id, bool)
        or not str(artifact_id).isdigit()
        or int(artifact_id) <= 0
    ):
        raise RecoveryError(f"invalid artifact ID: {artifact_id!r}")

    workdir.mkdir(parents=True, exist_ok=True)

    zip_path = workdir / f"artifact-{artifact_id}.zip"
    dest = workdir / f"artifact-{artifact_id}"

    command = [
        "gh",
        "api",
        f"repos/{repo}/actions/artifacts/{artifact_id}/zip",
    ]

    with zip_path.open("wb") as archive:
        proc = subprocess.run(
            command,
            stdout=archive,
            stderr=subprocess.PIPE,
            check=False,
        )

    if proc.returncode != 0:
        zip_path.unlink(missing_ok=True)
        error = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RecoveryError(
            f"artifact download failed ({proc.returncode}): {error}"
        )

    try:
        safe_extract(zip_path, dest)
    finally:
        zip_path.unlink(missing_ok=True)

    return dest


def find_result_json(root: Path, file_prefix: str) -> Path:
    """The single result JSON named ``<file_prefix><run_id>.json``."""
    matches = [
        p
        for p in sorted(root.rglob("*.json"))
        if p.name.startswith(file_prefix) and "tools" not in p.parts and "reference" not in p.parts
    ]
    if len(matches) != 1:
        raise RecoveryError(
            f"expected exactly one '{file_prefix}*.json' under {root}, found {len(matches)}: "
            f"{[str(m.relative_to(root)) for m in matches]}"
        )
    return matches[0]


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def parse_run_at(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise RecoveryError(f"run.run_at is not timezone-aware: {value!r}")
    return parsed


def validate_result(
    path: Path, bench_id: str, benchmark: str, min_run_at: datetime
) -> datetime:
    """Validate one result JSON against the persistence contract."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RecoveryError(f"{path.name}: not valid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise RecoveryError(f"{path.name}: top-level JSON value is not an object")
    if data.get("schema") != SCHEMA:
        raise RecoveryError(f"{path.name}: schema is {data.get('schema')!r}, expected {SCHEMA!r}")

    run = data.get("run") or {}
    if str(run.get("run_id")) != bench_id:
        raise RecoveryError(f"{path.name}: run.run_id is {run.get('run_id')!r}, expected {bench_id!r}")
    if not path.name.endswith(f"-{bench_id}.json"):
        raise RecoveryError(f"{path.name}: filename does not end with '-{bench_id}.json'")

    identity = data.get("identity") or {}
    runtime_source = (identity.get("runtime") or {}).get("runtime_source")
    if runtime_source != "package":
        raise RecoveryError(
            f"{path.name}: identity.runtime.runtime_source is {runtime_source!r}, expected 'package'"
        )
    if identity.get("benchmark") != benchmark:
        raise RecoveryError(
            f"{path.name}: identity.benchmark is {identity.get('benchmark')!r}, expected {benchmark!r}"
        )

    run_at_raw = run.get("run_at")
    if not isinstance(run_at_raw, str):
        raise RecoveryError(f"{path.name}: run.run_at is missing")
    run_at = parse_run_at(run_at_raw)
    if run_at <= min_run_at:
        raise RecoveryError(
            f"{path.name}: run.run_at {run_at_raw} is not newer than the last persisted run ({min_run_at.isoformat()})"
        )
    return run_at


REQUIRED_PACKAGE_FIELDS = ("filename", "sha256", "archive_dir", "date")


def package_identity(path: Path) -> object:
    """The (unvalidated) ``identity.runtime.package`` value of one result JSON."""
    data = json.loads(path.read_text(encoding="utf-8"))
    runtime = (data.get("identity") or {}).get("runtime") or {}
    return runtime.get("package")


def validate_package_pair(smoke_path: Path, openswath_path: Path, bench_id: str) -> None:
    """Both arms of one benchmark run must describe the same OpenMS package.

    Mirrors the nightly persistence guard exactly: each required field must be
    a non-empty string on both arms and all four must match. Unknown extra
    package keys are ignored, so additive schema changes stay compatible.
    ``identity.software.version`` is deliberately not required, and
    ``identity.package`` is never read (it is null; the populated path is
    ``identity.runtime.package``).
    """
    packages: dict[str, dict] = {}
    for label, path in (("smoke", smoke_path), ("openswath", openswath_path)):
        pkg = package_identity(path)
        if not isinstance(pkg, dict):
            raise RecoveryError(
                f"{path.name}: identity.runtime.package is missing or not an object ({pkg!r})"
            )
        absent = [
            k
            for k in REQUIRED_PACKAGE_FIELDS
            if not isinstance(pkg.get(k), str) or not pkg.get(k)
        ]
        if absent:
            raise RecoveryError(
                f"{path.name}: identity.runtime.package is missing required metadata: "
                f"{', '.join(absent)} (empty or not a string)"
            )
        packages[label] = pkg

    for field in REQUIRED_PACKAGE_FIELDS:
        if packages["smoke"][field] != packages["openswath"][field]:
            raise RecoveryError(
                f"benchmark run {bench_id}: package identity mismatch between smoke and "
                f"openswath: {field} smoke={packages['smoke'][field]!r} "
                f"openswath={packages['openswath'][field]!r}"
            )


# ---------------------------------------------------------------------------
# git helpers (read-only except in --apply)
# ---------------------------------------------------------------------------


def git(repo_dir: Path, *args, check=True):
    return _run(["git", *args], cwd=repo_dir, check=check)


def ref_exists(repo_dir: Path, ref: str) -> bool:
    return git(repo_dir, "rev-parse", "--verify", "--quiet", ref, check=False).returncode == 0


def path_on_ref(repo_dir: Path, ref: str, rel: str) -> bool:
    """True if ``rel`` exists on ``ref``; never calls cat-file for a missing ref."""
    if not ref_exists(repo_dir, ref):
        return False
    return git(repo_dir, "cat-file", "-e", f"{ref}:{rel}", check=False).returncode == 0


def main_refs(repo_dir: Path) -> list[str]:
    """Refs that must not already hold a destination, freshest first.

    ``origin/main`` is preferred because a local ``main`` can be stale; the
    local ref is still checked as a belt-and-braces guard.
    """
    candidates = ("refs/remotes/origin/main", "refs/heads/main")
    found = [ref for ref in candidates if ref_exists(repo_dir, ref)]
    return found or ["refs/heads/main"]


def ensure_no_main_collision(repo_dir: Path, rel_paths: list[str]) -> None:
    """Refuse a destination whose historical result already exists on ``main``.

    ``rel_paths`` are data-branch destinations (``openms/smoke-<id>.json``).
    Their historical counterparts on ``main`` sit under
    ``benchmark/results/``, so that is the path each destination is checked
    against: a file at the data layout on ``main`` is not this guard's concern,
    and a historical result must never be imported a second time.
    """
    refs = main_refs(repo_dir)
    for rel in rel_paths:
        historical = main_result_path(rel)
        for ref in refs:
            if path_on_ref(repo_dir, ref, historical):
                raise RecoveryError(
                    f"refusing to overwrite existing {historical} on {ref} "
                    f"(destination {rel} on the data branch)"
                )


def ensure_no_collision(repo_dir: Path, rel_paths: list[str]) -> None:
    """Strict check: absent on ``main`` and absent in the working tree."""
    ensure_no_main_collision(repo_dir, rel_paths)
    for rel in rel_paths:
        if (repo_dir / rel).exists():
            raise RecoveryError(f"refusing to overwrite existing working-tree file {rel}")


def fetch_remote_ref(repo_dir: Path, branch: str, remote: str = "origin") -> str:
    """Fetch ``main`` and ``branch`` from ``remote``; return the data-branch ref.

    Fails closed: an unreachable remote or a missing/renamed data branch must
    never be read as "no collision" or "up to date".
    """
    spec = [
        f"+refs/heads/main:refs/remotes/{remote}/main",
        f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}",
    ]
    proc = git(repo_dir, "fetch", "--quiet", remote, *spec, check=False)
    if proc.returncode != 0:
        raise RecoveryError(
            f"could not fetch '{remote}' (main and {branch}); refusing to import against "
            f"possibly stale refs: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    ref = f"refs/remotes/{remote}/{branch}"
    if not ref_exists(repo_dir, ref):
        raise RecoveryError(
            f"could not establish {ref}; '{branch}' may not exist on '{remote}' - "
            f"refusing to import (create the branch first)"
        )
    return ref


def ensure_on_remote_base(repo_dir: Path, remote_ref: str) -> None:
    """Refuse to import onto a local branch that is not based on ``remote_ref``."""
    proc = git(repo_dir, "merge-base", "--is-ancestor", remote_ref, "HEAD", check=False)
    if proc.returncode != 0:
        raise RecoveryError(
            f"local branch is not based on {remote_ref}; fetch and fast-forward "
            f"(or reset onto it) before --apply instead of importing onto a stale branch"
        )


def destination_state(repo_dir: Path, item: Validated) -> str:
    """``"import"`` if the pair is absent, ``"already-imported"`` if identical.

    Any other shape (only one file, or different bytes) fails closed, so a
    resumed import can never rewrite or half-complete an existing run.
    """
    present = {rel for rel in item.files if (repo_dir / rel).exists()}
    if not present:
        return "import"
    missing = sorted(set(item.files) - present)
    if missing:
        raise RecoveryError(
            f"benchmark run {item.bench_id} is only partially present on the data branch "
            f"(missing {missing}); refusing to complete a foreign partial pair"
        )
    for rel, src in sorted(item.files.items()):
        if (repo_dir / rel).read_bytes() != Path(src).read_bytes():
            raise RecoveryError(
                f"benchmark run {item.bench_id} already exists with different contents "
                f"({rel}); refusing to overwrite"
            )
    return "already-imported"


def staged_paths(repo_dir: Path) -> list[str]:
    out = git(repo_dir, "diff", "--cached", "--name-only").stdout
    return [line for line in out.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# discovery + import
# ---------------------------------------------------------------------------


def collect(
    repo: str,
    bench_ids: list[str],
    staging: Path,
    min_run_at: datetime,
) -> list[Validated]:
    """Download and validate everything before any repository change."""
    staging.mkdir(parents=True, exist_ok=True)
    validated: list[Validated] = []
    for bench_id in bench_ids:
        nightly = NIGHTLY_BY_BENCH.get(bench_id)
        workdir = Path(tempfile.mkdtemp(prefix=f"{bench_id}-", dir=staging))
        workdir.mkdir(parents=True, exist_ok=True)
        artifacts = select_artifacts(list_artifacts(repo, bench_id))
        files: dict[str, Path] = {}
        run_ats: list[datetime] = []
        result_paths: dict[str, Path] = {}
        dest_dirs: dict[str, str] = {}
        for benchmark, _prefix, file_prefix, dest_dir in BENCHMARKS:
            extracted = download_artifact(repo, bench_id, artifacts[benchmark], workdir)
            result = find_result_json(extracted, file_prefix)
            run_ats.append(validate_result(result, bench_id, benchmark, min_run_at))
            result_paths[benchmark] = result
            dest_dirs[benchmark] = dest_dir
        # Both arms must describe one package before anything is staged: a mixed
        # or incomplete pair must never leave a half-staged file behind.
        validate_package_pair(result_paths["smoke"], result_paths["openswath_dia"], bench_id)
        for benchmark, result in result_paths.items():
            rel = f"{dest_dirs[benchmark]}/{result.name}"
            target = staging / "validated" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(result, target)
            files[rel] = target
        # Both arms come from the same benchmark run; keep the newest stamp.
        run_at = max(run_ats)
        print(
            f"  OK  benchmark run {bench_id}"
            f" (nightly #{nightly if nightly is not None else '?'})"
            f"  run_at={run_at.isoformat()}"
        )
        print(f"      smoke     -> {files[f'openms/smoke-{bench_id}.json']}")
        print(
            f"      openswath -> {files[f'openswath_dia/openms/openswath_dia-{bench_id}.json']}"
        )
        validated.append(
            Validated(
                bench_id=bench_id,
                nightly=nightly,
                run_at=run_at,
                files=files,
            )
        )
    validated.sort(key=lambda v: (v.run_at, int(v.bench_id)))
    return validated


def import_run(repo_dir: Path, branch: str, item: Validated) -> None:
    """Copy one run's pair into the checkout and commit exactly those files.

    On any failure the copies and the staged paths are removed again, leaving
    the checkout exactly as it was found so a re-run can resume cleanly.
    """
    rel_paths = sorted(item.files)
    try:
        for rel, src in item.files.items():
            dest = repo_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)

        git(repo_dir, "add", "--", *rel_paths)
        staged = sorted(staged_paths(repo_dir))
        if staged != rel_paths:
            raise RecoveryError(
                f"staged paths are not exactly the two result files for run {item.bench_id}: {staged}"
            )
        subject = f"Recover package-mode benchmark results (benchmark run {item.bench_id})"
        body = (
            f"Nightly run #{item.nightly} failed to persist this pair while results "
            f"still targeted protected main. Imported from the retained benchmark "
            f"artifact; contents unmodified."
        )
        git(repo_dir, "commit", "-m", subject, "-m", body)
    except BaseException:
        git(repo_dir, "reset", "-q", "--", *rel_paths, check=False)
        for rel in rel_paths:
            (repo_dir / rel).unlink(missing_ok=True)
        raise
    print(f"      committed 2 file(s) for benchmark run {item.bench_id} on {branch}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Recover nightly benchmark results onto the benchmark-results branch "
        "(dry-run by default; never pushes).",
        epilog="See the module docstring for the full recovery procedure.",
    )
    parser.add_argument("--repo", default=DEFAULT_REPO, help="owner/name on GitHub")
    parser.add_argument("--repo-dir", default=".", help="local checkout to import into")
    parser.add_argument("--branch", default=DEFAULT_BRANCH, help="data branch name")
    parser.add_argument(
        "--runs",
        default=",".join(NIGHTLY_BY_BENCH),
        help="comma-separated nested benchmark run IDs (default: all known)",
    )
    parser.add_argument(
        "--nightly",
        default=None,
        help="comma-separated nightly run numbers to select instead of --runs",
    )
    parser.add_argument("--apply", action="store_true", help="write files and commit locally")
    parser.add_argument(
        "--staging-dir",
        default=str(DEFAULT_STAGING),
        help=f"where validated downloads are staged (default: {DEFAULT_STAGING})",
    )
    parser.add_argument(
        "--min-run-at",
        default=DEFAULT_MIN_RUN_AT,
        help=f"require run.run_at newer than this (default: {DEFAULT_MIN_RUN_AT})",
    )
    args = parser.parse_args(argv)

    repo_dir = Path(args.repo_dir).resolve()
    staging = Path(args.staging_dir).resolve()
    min_run_at = parse_run_at(args.min_run_at)

    if args.nightly:
        wanted = {int(token) for token in args.nightly.split(",") if token.strip()}
        bench_ids = [b for b, n in NIGHTLY_BY_BENCH.items() if n in wanted]
        missing = sorted(wanted - {NIGHTLY_BY_BENCH[b] for b in bench_ids})
        if missing:
            print(f"ERROR: no known benchmark run for nightly {missing}", file=sys.stderr)
            return 2
    else:
        bench_ids = [token.strip() for token in args.runs.split(",") if token.strip()]
    duplicates = sorted({
        bench_id for bench_id in bench_ids
        if bench_ids.count(bench_id) > 1
    })
    if duplicates:
        print(f"ERROR: duplicate benchmark run id(s): {duplicates}", file=sys.stderr)
        return 2

    unknown = [b for b in bench_ids if b not in NIGHTLY_BY_BENCH]
    if unknown:
        print(f"ERROR: unknown benchmark run id(s): {unknown}", file=sys.stderr)
        return 2

    current_branch = git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    on_data_branch = current_branch == args.branch

    if args.apply:
        if not ref_exists(repo_dir, f"refs/heads/{args.branch}"):
            print(
                f"ERROR: local branch '{args.branch}' does not exist; a maintainer must create it "
                f"and check it out before --apply (this tool never creates branches).",
                file=sys.stderr,
            )
            return 2
        if not on_data_branch:
            print(f"ERROR: --apply requires branch '{args.branch}' to be checked out (now: {current_branch})",
                  file=sys.stderr)
            return 2
        if git(repo_dir, "status", "--porcelain").stdout.strip():
            print("ERROR: working tree is not clean; commit or stash changes before --apply",
                  file=sys.stderr)
            return 2
        # Refresh before touching anything: a stale clone must never decide
        # whether a destination already exists, nor which tip to build on.
        # Residual window, by design: `main` can still move while the artifacts
        # below are downloaded, so this bounds the race rather than closing it.
        # That is acceptable because `main` is protected (no concurrent result
        # commits can appear there) and every write goes to the data branch,
        # whose tip the nightly workflow re-fetches immediately before pushing.
        try:
            remote_ref = fetch_remote_ref(repo_dir, args.branch)
            ensure_on_remote_base(repo_dir, remote_ref)
        except RecoveryError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    print(f"== recovering {len(bench_ids)} benchmark run(s) from {args.repo} ==")
    print(f"   staging: {staging}")
    print(f"   branch:  {args.branch}{'' if args.apply else ' (dry run; nothing is written or committed)'}")

    # Classify every run before writing anything: main collisions are always
    # fatal, and on the data branch only an identical existing pair is skipped.
    states: dict[str, str] = {}
    try:
        validated = collect(args.repo, bench_ids, staging, min_run_at)
        for item in validated:
            ensure_no_main_collision(repo_dir, sorted(item.files))
            if on_data_branch:
                states[item.bench_id] = destination_state(repo_dir, item)
            else:
                ensure_no_collision(repo_dir, sorted(item.files))
                states[item.bench_id] = "import"
    except RecoveryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print()
    print(f"validated {len(validated)} run(s); all JSONs passed schema/run-id/package/time checks")
    for item in validated:
        nightly = f"#{item.nightly}" if item.nightly is not None else "?"
        print(
            f"  nightly {nightly:>4}  benchmark {item.bench_id}  "
            f"run_at {item.run_at.isoformat()}  [{states[item.bench_id]}]"
        )

    pending = [item for item in validated if states[item.bench_id] == "import"]
    if not args.apply:
        print()
        print(
            f"{len(pending)} run(s) would be imported, "
            f"{len(validated) - len(pending)} already present on '{args.branch}'"
        )
        print("dry run: no repository changes. Re-run with --apply on the "
              f"'{args.branch}' branch to import.")
        return 0

    imported: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    for item in validated:
        if states[item.bench_id] == "already-imported":
            print(f"      already present, skipping benchmark run {item.bench_id}")
            skipped.append(item.bench_id)
            continue
        try:
            import_run(repo_dir, args.branch, item)
        except RecoveryError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            failed.append(item.bench_id)
            break
        imported.append(item.bench_id)

    print()
    print(f"imported:        {imported if imported else 'none'}")
    print(f"already present: {skipped if skipped else 'none'}")
    print(f"failed:          {failed if failed else 'none'}")
    if failed:
        print(
            "stopped at the first failure and restored the working tree; re-run the "
            "same command to resume with the remaining runs.",
            file=sys.stderr,
        )
        return 1
    print(f"{len(imported)} commit(s) on {args.branch}. Review with "
          f"'git log' and push the branch yourself; this tool never pushes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
