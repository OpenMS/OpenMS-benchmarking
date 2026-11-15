#!/usr/bin/env python3
"""Tests for benchmark/recover_nightly_results.py (no network, no real gh).

Run: python benchmark/test_recover_nightly_results.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import recover_nightly_results as rnr  # noqa: E402

BENCH_ID = "42333314804"
MIN_RUN_AT = rnr.parse_run_at(rnr.DEFAULT_MIN_RUN_AT)


_DEFAULT_PACKAGE = {
    "filename": "OpenMS-3.7.0-pre-nightly-2026-11-06-Debian-Linux-x86_64.deb",
    "sha256": "0" * 64,
    "archive_dir": "2026.11.07",
    "date": "2026-11-06",
}
_OMIT = object()  # identity.runtime.package absent altogether
_DEFAULT = object()  # use the standard package object


def payload(
    bench_id: str,
    benchmark: str = "smoke",
    run_at: str = "2026-11-07T03:17:21Z",
    package=_DEFAULT,
) -> dict:
    pkg = _DEFAULT_PACKAGE if package is _DEFAULT else package
    runtime: dict = {"runtime_source": "package"}
    if pkg is not _OMIT:
        runtime["package"] = pkg
    return {
        "schema": rnr.SCHEMA,
        "identity": {
            "benchmark": benchmark,
            "benchmark_version": 1,
            "dataset": "fixture",
            "software": {"name": "OpenMS", "version": "5e974cdef84a778717bc156c842c9d46baecb604"},
            "configuration": {},
            "runtime": runtime,
        },
        "run": {"run_id": bench_id, "run_at": run_at, "cache": "none"},
        "performance": {
            "wall_time_s": 1.0,
            "cpu_time_s": 0.1,
            "peak_rss_kb": 1024,
            "build": {"wall_time_s": 0.0, "artifact_bytes": 1},
            "stages": [],
        },
        "metrics": {"verdict": "pass"},
    }


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


class ZipSafetyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def _zip(self, name: str, member: str, data: str = "{}") -> Path:
        path = self.tmp / name
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr(member, data)
        return path

    def test_rejects_parent_traversal(self) -> None:
        with self.assertRaises(rnr.RecoveryError):
            rnr.safe_extract(self._zip("a.zip", "../evil.json"), self.tmp / "a")

    def test_rejects_absolute_path(self) -> None:
        with self.assertRaises(rnr.RecoveryError):
            rnr.safe_extract(self._zip("b.zip", "/etc/evil.json"), self.tmp / "b")

    def test_rejects_symlink_member(self) -> None:
        path = self.tmp / "c.zip"
        with zipfile.ZipFile(path, "w") as zf:
            info = zipfile.ZipInfo("link")
            info.external_attr = (0o120777 << 16)  # symlink mode
            zf.writestr(info, "target")
        with self.assertRaises(rnr.RecoveryError):
            rnr.safe_extract(path, self.tmp / "c")

    def test_rejects_backslash_rooted_member(self) -> None:
        with self.assertRaises(rnr.RecoveryError):
            rnr.safe_extract(self._zip("e.zip", "\\outside.json"), self.tmp / "e")

    def test_rejects_unc_member(self) -> None:
        with self.assertRaises(rnr.RecoveryError):
            rnr.safe_extract(self._zip("f.zip", "//server/share/evil.json"), self.tmp / "f")

    def test_rejects_drive_letter_member(self) -> None:
        with self.assertRaises(rnr.RecoveryError):
            rnr.safe_extract(self._zip("g.zip", "C:\\temp\\evil.json"), self.tmp / "g")

    def test_rejects_backslash_traversal(self) -> None:
        with self.assertRaises(rnr.RecoveryError):
            rnr.safe_extract(self._zip("h.zip", "..\\evil.json"), self.tmp / "h")

    def test_extracts_nested_member(self) -> None:
        rnr.safe_extract(
            self._zip("d.zip", "smoke/openms/smoke-1.json", '{"ok": true}'), self.tmp / "d"
        )
        self.assertTrue((self.tmp / "d" / "smoke" / "openms" / "smoke-1.json").is_file())


class FindResultTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def test_finds_single_result_and_ignores_tools(self) -> None:
        (self.tmp / "smoke" / "openms").mkdir(parents=True)
        (self.tmp / "tools").mkdir()
        (self.tmp / "smoke" / "openms" / "smoke-1.json").write_text("{}")
        (self.tmp / "tools" / "smoke-1.json").write_text("{}")
        found = rnr.find_result_json(self.tmp, "smoke-")
        self.assertEqual(found.name, "smoke-1.json")
        self.assertIn("openms", found.parts)

    def test_missing_and_ambiguous_fail(self) -> None:
        with self.assertRaises(rnr.RecoveryError):
            rnr.find_result_json(self.tmp, "smoke-")
        (self.tmp / "a").mkdir()
        (self.tmp / "b").mkdir()
        (self.tmp / "a" / "smoke-1.json").write_text("{}")
        (self.tmp / "b" / "smoke-1.json").write_text("{}")
        with self.assertRaises(rnr.RecoveryError):
            rnr.find_result_json(self.tmp, "smoke-")


class ValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def _write(self, data, name=None) -> Path:
        path = self.tmp / (name or f"smoke-{BENCH_ID}.json")
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_accepts_valid_package_result(self) -> None:
        stamp = rnr.validate_result(self._write(payload(BENCH_ID)), BENCH_ID, "smoke", MIN_RUN_AT)
        self.assertEqual(stamp.year, 2026)

    def test_rejects_each_contract_violation(self) -> None:
        cases = {
            "schema": lambda d: d.__setitem__("schema", "v1"),
            "run_id": lambda d: d["run"].__setitem__("run_id", "999"),
            "runtime": lambda d: d["identity"]["runtime"].__setitem__("runtime_source", "source"),
            "benchmark": lambda d: d["identity"].__setitem__("benchmark", "openswath_dia"),
            "naive_time": lambda d: d["run"].__setitem__("run_at", "2026-11-07T03:17:21"),
            "old_time": lambda d: d["run"].__setitem__("run_at", "2026-11-01T00:00:00Z"),
            "not_an_object": None,
        }
        for label, mutate in cases.items():
            with self.subTest(label=label):
                if mutate is None:
                    path = self._write([payload(BENCH_ID)])
                else:
                    data = payload(BENCH_ID)
                    mutate(data)
                    path = self._write(data)
                with self.assertRaises(rnr.RecoveryError):
                    rnr.validate_result(path, BENCH_ID, "smoke", MIN_RUN_AT)

    def test_rejects_filename_that_does_not_match_run_id(self) -> None:
        data = payload(BENCH_ID)
        path = self._write(data, name="smoke-11111111111.json")
        with self.assertRaises(rnr.RecoveryError):
            rnr.validate_result(path, BENCH_ID, "smoke", MIN_RUN_AT)


class DownloadArtifactTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    @staticmethod
    def _zip_bytes(member: str, data: str = '{"ok": true}') -> bytes:
        import io

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(member, data)
        return buffer.getvalue()

    def test_downloads_and_safely_extracts_artifact(self) -> None:
        archive_bytes = self._zip_bytes("smoke/openms/smoke-1.json")

        def fake_run(command, stdout, stderr, check):
            stdout.write(archive_bytes)
            return subprocess.CompletedProcess(command, 0, b"", b"")

        with mock.patch.object(rnr.subprocess, "run", side_effect=fake_run) as run:
            result = rnr.download_artifact(
                "OpenMS/OpenMS-benchmarking",
                BENCH_ID,
                {"id": 123, "name": "untrusted-name"},
                self.tmp,
            )

        self.assertEqual(
            run.call_args.args[0],
            [
                "gh",
                "api",
                "repos/OpenMS/OpenMS-benchmarking/actions/artifacts/123/zip",
            ],
        )
        self.assertEqual(
            (result / "smoke/openms/smoke-1.json").read_text(encoding="utf-8"),
            '{"ok": true}',
        )
        self.assertFalse((self.tmp / "artifact-123.zip").exists())

    def test_rejects_invalid_artifact_ids_before_download(self) -> None:
        for artifact_id in (None, True, 0, -1, "../123", "invalid"):
            with self.subTest(artifact_id=artifact_id):
                with mock.patch.object(rnr.subprocess, "run") as run:
                    with self.assertRaises(rnr.RecoveryError):
                        rnr.download_artifact(
                            "OpenMS/OpenMS-benchmarking",
                            BENCH_ID,
                            {"id": artifact_id},
                            self.tmp,
                        )
                    run.assert_not_called()

    def test_download_rejects_unsafe_zip_before_extraction(self) -> None:
        archive_bytes = self._zip_bytes("../outside.json")

        def fake_run(command, stdout, stderr, check):
            stdout.write(archive_bytes)
            return subprocess.CompletedProcess(command, 0, b"", b"")

        with mock.patch.object(rnr.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(rnr.RecoveryError):
                rnr.download_artifact(
                    "OpenMS/OpenMS-benchmarking",
                    BENCH_ID,
                    {"id": 124},
                    self.tmp,
                )

        self.assertFalse((self.tmp / "outside.json").exists())
        self.assertFalse((self.tmp / "artifact-124.zip").exists())

    def test_download_failure_reports_error_and_cleans_zip(self) -> None:
        def fake_run(command, stdout, stderr, check):
            return subprocess.CompletedProcess(
                command, 1, b"", b"authentication failed"
            )

        with mock.patch.object(rnr.subprocess, "run", side_effect=fake_run):
            with self.assertRaisesRegex(
                rnr.RecoveryError, "authentication failed"
            ):
                rnr.download_artifact(
                    "OpenMS/OpenMS-benchmarking",
                    BENCH_ID,
                    {"id": 125},
                    self.tmp,
                )

        self.assertFalse((self.tmp / "artifact-125.zip").exists())

class ArtifactSelectionTest(unittest.TestCase):
    def test_requires_exactly_one_of_each(self) -> None:
        ok = [
            {"name": "benchmark-results-abc", "id": 1, "expired": False},
            {"name": "benchmark-openswath-abc", "id": 2, "expired": False},
            {"name": "benchmark-report-abc", "id": 3, "expired": False},
        ]
        selected = rnr.select_artifacts(ok)
        self.assertEqual(selected["smoke"]["id"], 1)
        self.assertEqual(selected["openswath_dia"]["id"], 2)

    def test_expired_and_duplicate_fail(self) -> None:
        expired = [
            {"name": "benchmark-results-abc", "id": 1, "expired": True},
            {"name": "benchmark-openswath-abc", "id": 2, "expired": False},
        ]
        with self.assertRaises(rnr.RecoveryError):
            rnr.select_artifacts(expired)
        duplicate = [
            {"name": "benchmark-results-abc", "id": 1, "expired": False},
            {"name": "benchmark-results-def", "id": 2, "expired": False},
            {"name": "benchmark-openswath-abc", "id": 3, "expired": False},
        ]
        with self.assertRaises(rnr.RecoveryError):
            rnr.select_artifacts(duplicate)


class RecoveryFlowTest(unittest.TestCase):
    """End-to-end dry run and apply against a temporary git repository."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "Test")
        (self.repo / "README.md").write_text("seed\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-qm", "init")
        git(self.repo, "branch", "benchmark-results")

        # A local bare repository stands in for origin, so the fetch-based
        # freshness checks run for real without any network access.
        self.origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "--bare", "-q", str(self.origin)], check=True)
        git(self.repo, "remote", "add", "origin", str(self.origin))
        git(self.repo, "push", "-q", "origin", "main", "benchmark-results")
        git(self.repo, "fetch", "-q", "origin")
        self.staging = self.tmp / "staging"
        # Per-benchmark package override, used by the package-pair tests below.
        self.package_overrides: dict[str, object] = {}

        self._patches = [
            mock.patch.object(rnr, "list_artifacts", side_effect=self._artifacts),
            mock.patch.object(rnr, "download_artifact", side_effect=self._download),
        ]
        for patcher in self._patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    # -- fakes -------------------------------------------------------------
    @staticmethod
    def _artifacts(repo, run_id):
        return [
            {"name": "benchmark-results-abc", "id": 1, "expired": False},
            {"name": "benchmark-openswath-abc", "id": 2, "expired": False},
            {"name": "benchmark-report-abc", "id": 3, "expired": False},
        ]

    def _download(self, repo, run_id, artifact, workdir):
        dest = Path(workdir) / artifact["name"]
        dest.mkdir(parents=True, exist_ok=True)
        if artifact["name"].startswith("benchmark-results"):
            rel = Path("smoke") / "openms" / f"smoke-{run_id}.json"
            benchmark = "smoke"
        else:
            rel = Path("openswath_dia") / "openms" / f"openswath_dia-{run_id}.json"
            benchmark = "openswath_dia"
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        (dest / rel).write_text(
            json.dumps(payload(run_id, benchmark, package=self.package_overrides.get(benchmark, _DEFAULT))),
            encoding="utf-8",
        )
        # Decoy that must be ignored by discovery.
        (dest / "tools").mkdir(exist_ok=True)
        (dest / "tools" / f"smoke-{run_id}.json").write_text("{}", encoding="utf-8")
        return dest

    def _args(self, *extra: str) -> list[str]:
        return [
            "--repo-dir",
            str(self.repo),
            "--staging-dir",
            str(self.staging),
            "--runs",
            BENCH_ID,
            *extra,
        ]

    # -- tests -------------------------------------------------------------
    def test_dry_run_stages_but_does_not_touch_the_repo(self) -> None:
        before = git(self.repo, "rev-list", "--count", "HEAD").strip()
        self.assertEqual(rnr.main(self._args()), 0)
        self.assertEqual(git(self.repo, "rev-list", "--count", "HEAD").strip(), before)
        self.assertEqual(git(self.repo, "status", "--porcelain").strip(), "")
        staged = self.staging / "validated"
        self.assertTrue((staged / "openms" / f"smoke-{BENCH_ID}.json").is_file())
        self.assertTrue(
            (staged / "openswath_dia" / "openms" / f"openswath_dia-{BENCH_ID}.json").is_file()
        )

    # -- package-pair contract (parity with the nightly persistence guard) ---
    def _assert_pair_rejected(self) -> None:
        """A bad pair fails closed before staging, committing, or recovery."""
        before = git(self.repo, "rev-list", "--count", "HEAD").strip()
        self.assertEqual(rnr.main(self._args()), 1)
        self.assertEqual(git(self.repo, "rev-list", "--count", "HEAD").strip(), before)
        self.assertEqual(git(self.repo, "status", "--porcelain").strip(), "")
        self.assertFalse((self.repo / "openms" / f"smoke-{BENCH_ID}.json").exists())
        self.assertFalse(
            (self.repo / "openswath_dia" / "openms" / f"openswath_dia-{BENCH_ID}.json").exists()
        )
        staged = self.staging / "validated"
        self.assertFalse((staged / "openms" / f"smoke-{BENCH_ID}.json").exists())
        self.assertFalse(
            (staged / "openswath_dia" / "openms" / f"openswath_dia-{BENCH_ID}.json").exists()
        )

    def test_rejects_mismatched_package_pair(self) -> None:
        self.package_overrides["openswath_dia"] = dict(_DEFAULT_PACKAGE, sha256="1" * 64)
        self._assert_pair_rejected()

    def test_rejects_missing_package_object(self) -> None:
        self.package_overrides["smoke"] = _OMIT
        self._assert_pair_rejected()

    def test_rejects_null_package_object(self) -> None:
        self.package_overrides["openswath_dia"] = None
        self._assert_pair_rejected()

    def test_rejects_empty_package_field(self) -> None:
        self.package_overrides["smoke"] = dict(_DEFAULT_PACKAGE, archive_dir="")
        self._assert_pair_rejected()

    def test_rejects_missing_package_field(self) -> None:
        partial = dict(_DEFAULT_PACKAGE)
        del partial["sha256"]
        self.package_overrides["openswath_dia"] = partial
        self._assert_pair_rejected()

    def test_rejects_non_string_package_field(self) -> None:
        self.package_overrides["openswath_dia"] = dict(_DEFAULT_PACKAGE, date=True)
        self._assert_pair_rejected()

    def test_accepts_unknown_extra_package_keys(self) -> None:
        extra = dict(_DEFAULT_PACKAGE, bytes=12345)
        self.package_overrides["smoke"] = extra
        self.package_overrides["openswath_dia"] = extra
        self.assertEqual(rnr.main(self._args()), 0)
        self.assertTrue(
            (self.staging / "validated" / "openms" / f"smoke-{BENCH_ID}.json").is_file()
        )

    def test_apply_commits_one_commit_with_both_files(self) -> None:
        git(self.repo, "switch", "-q", "benchmark-results")
        self.assertEqual(rnr.main(self._args("--apply")), 0)
        self.assertTrue((self.repo / "openms" / f"smoke-{BENCH_ID}.json").is_file())
        self.assertTrue(
            (self.repo / "openswath_dia" / "openms" / f"openswath_dia-{BENCH_ID}.json").is_file()
        )
        self.assertEqual(git(self.repo, "status", "--porcelain").strip(), "")
        self.assertEqual(git(self.repo, "rev-list", "--count", "HEAD").strip(), "2")
        subject = git(self.repo, "log", "-1", "--format=%s").strip()
        self.assertIn(f"benchmark run {BENCH_ID}", subject)

    def test_apply_requires_the_data_branch_checked_out(self) -> None:
        self.assertEqual(rnr.main(self._args("--apply")), 2)

    def test_apply_requires_a_clean_tree(self) -> None:
        git(self.repo, "switch", "-q", "benchmark-results")
        (self.repo / "dirty.txt").write_text("x", encoding="utf-8")
        self.assertEqual(rnr.main(self._args("--apply")), 2)

    def test_missing_local_branch_is_a_usage_error(self) -> None:
        git(self.repo, "branch", "-D", "benchmark-results")
        git(self.repo, "switch", "-q", "-c", "other")
        self.assertEqual(rnr.main(self._args("--apply")), 2)

    def test_collision_on_branch_refuses_to_overwrite(self) -> None:
        git(self.repo, "switch", "-q", "benchmark-results")
        target = self.repo / "openms" / f"smoke-{BENCH_ID}.json"
        target.parent.mkdir(parents=True)
        target.write_text('{"keep": "me"}', encoding="utf-8")
        # Committed collision: the clean-tree requirement passes and the
        # collision guard is what refuses the import.
        git(self.repo, "add", f"openms/smoke-{BENCH_ID}.json")
        git(self.repo, "commit", "-qm", "existing result")
        self.assertEqual(rnr.main(self._args("--apply")), 1)
        self.assertEqual(target.read_text(encoding="utf-8"), '{"keep": "me"}')

    def test_collision_on_main_ref_refuses(self) -> None:
        # main still holds the frozen pre-split results; importing the same id
        # must be refused even though the branch checkout is clean.
        git(self.repo, "switch", "-q", "main")
        target = self.repo / "openms" / "smoke-999.json"
        target.parent.mkdir(parents=True)
        target.write_text("{}", encoding="utf-8")
        git(self.repo, "add", "openms/smoke-999.json")
        git(self.repo, "commit", "-qm", "frozen")
        rel = "openms/smoke-999.json"
        self.assertTrue(rnr.path_on_ref(self.repo, "refs/heads/main", rel))
        with self.assertRaises(rnr.RecoveryError):
            rnr.ensure_no_collision(self.repo, [rel])

    def test_collision_on_fetched_origin_main_refuses_when_local_main_is_stale(self) -> None:
        rel = "openms/smoke-777.json"
        git(self.repo, "switch", "-q", "main")
        target = self.repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}", encoding="utf-8")
        git(self.repo, "add", rel)
        git(self.repo, "commit", "-qm", "upstream only")
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "reset", "-q", "--hard", "HEAD~1")  # local main now stale
        git(self.repo, "fetch", "-q", "origin")
        self.assertFalse(rnr.path_on_ref(self.repo, "refs/heads/main", rel))
        self.assertTrue(rnr.path_on_ref(self.repo, "refs/remotes/origin/main", rel))
        with self.assertRaises(rnr.RecoveryError):
            rnr.ensure_no_main_collision(self.repo, [rel])

    def test_partial_import_resumes_without_duplicating(self) -> None:
        other = "42442257487"
        git(self.repo, "switch", "-q", "benchmark-results")
        args = [
            "--repo-dir",
            str(self.repo),
            "--staging-dir",
            str(self.staging),
            "--runs",
            f"{BENCH_ID},{other}",
            "--apply",
        ]

        real_git = rnr.git

        def failing_git(repo_dir, *cmd, check=True):
            if cmd and cmd[0] == "commit" and other in " ".join(cmd):
                raise rnr.RecoveryError("simulated commit failure")
            return real_git(repo_dir, *cmd, check=check)

        with mock.patch.object(rnr, "git", side_effect=failing_git):
            self.assertEqual(rnr.main(args), 1)

        # The failed run was rolled back, so the checkout is clean and only the
        # first run is committed.
        self.assertEqual(git(self.repo, "status", "--porcelain").strip(), "")
        self.assertEqual(git(self.repo, "rev-list", "--count", "HEAD").strip(), "2")
        self.assertTrue((self.repo / "openms" / f"smoke-{BENCH_ID}.json").is_file())
        self.assertFalse((self.repo / "openms" / f"smoke-{other}.json").exists())

        # Re-running the same selection skips the completed run and imports the
        # remaining one exactly once.
        self.assertEqual(rnr.main(args), 0)
        self.assertEqual(git(self.repo, "rev-list", "--count", "HEAD").strip(), "3")
        self.assertTrue((self.repo / "openms" / f"smoke-{other}.json").is_file())
        subjects = git(self.repo, "log", "--format=%s").splitlines()
        self.assertEqual(sum(BENCH_ID in subject for subject in subjects), 1)
        self.assertEqual(sum(other in subject for subject in subjects), 1)

    def test_unreachable_remote_fails_closed_before_writing(self) -> None:
        git(self.repo, "switch", "-q", "benchmark-results")
        commits = git(self.repo, "rev-list", "--count", "HEAD").strip()
        git(self.repo, "remote", "set-url", "origin", str(self.tmp / "missing.git"))
        self.assertEqual(rnr.main(self._args("--apply")), 1)
        self.assertEqual(git(self.repo, "rev-list", "--count", "HEAD").strip(), commits)
        self.assertEqual(git(self.repo, "status", "--porcelain").strip(), "")

    def test_stale_local_data_branch_is_refused(self) -> None:
        git(self.repo, "switch", "-q", "benchmark-results")
        git(self.repo, "switch", "-q", "main")
        (self.repo / "upstream.txt").write_text("x", encoding="utf-8")
        git(self.repo, "add", "upstream.txt")
        git(self.repo, "commit", "-qm", "remote moved ahead")
        git(self.repo, "push", "-q", "origin", "main:benchmark-results")
        git(self.repo, "switch", "-q", "benchmark-results")
        commits = git(self.repo, "rev-list", "--count", "HEAD").strip()
        self.assertEqual(rnr.main(self._args("--apply")), 1)
        self.assertEqual(git(self.repo, "rev-list", "--count", "HEAD").strip(), commits)
        self.assertEqual(git(self.repo, "status", "--porcelain").strip(), "")


class CliValidationTest(unittest.TestCase):
    def test_duplicate_run_ids_fail_before_artifact_discovery(self) -> None:
        stderr = io.StringIO()

        with mock.patch.object(rnr, "list_artifacts") as list_artifacts:
            with contextlib.redirect_stderr(stderr):
                result = rnr.main(["--runs", f"{BENCH_ID},{BENCH_ID}"])

        self.assertEqual(result, 2)
        self.assertIn("duplicate benchmark run id(s)", stderr.getvalue())
        self.assertIn(BENCH_ID, stderr.getvalue())
        list_artifacts.assert_not_called()

if __name__ == "__main__":
    unittest.main(verbosity=2)
