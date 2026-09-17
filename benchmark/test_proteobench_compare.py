#!/usr/bin/env python3
"""Focused tests for benchmark/proteobench_compare.py.

All fixtures are small synthetic dicts/files defined in this module; no real
submission files are copied into the tests. Pure parsing/filtering functions
are tested directly, decoupled from HTML rendering.
"""

import copy
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import proteobench_compare as pbc  # noqa: E402


def make_submission(
    intermediate_hash="a" * 40,
    precursor="[-10 ppm, 10 ppm]",
    fragment="[-0.02 Da, 0.02 Da]",
    enzyme="Trypsin",
    miscleavages=2,
    fdr=0.01,
    nr1=5000,
    nr3=3000,
    nr6=1500,
):
    """A synthetic submission that passes every filter criterion by default."""
    data = {
        "id": "tool_20250101_000000",
        "software_name": "ToolX",
        "software_version": "1.2.3",
        "search_engine": "Engine",
        "search_engine_version": "2023.01",
        "intermediate_hash": intermediate_hash,
        "proteobench_version": "0.17.0",
        "precursor_mass_tolerance": precursor,
        "fragment_mass_tolerance": fragment,
        "enzyme": enzyme,
        "allowed_miscleavages": miscleavages,
        "ident_fdr_psm": fdr,
        "results": {
            "1": {"nr_feature": nr1, "median_abs_epsilon_global": 0.2, "CV_median": 0.22, "roc_auc": 0.9},
            "3": {"nr_feature": nr3, "median_abs_epsilon_global": 0.21, "CV_median": 0.23, "roc_auc": 0.91},
            "6": {"nr_feature": nr6, "median_abs_epsilon_global": 0.22, "CV_median": 0.24, "roc_auc": 0.92},
        },
    }
    return data


def write_submission(directory, data, filename=None):
    if filename is None:
        filename = data["intermediate_hash"] + ".json"
    path = os.path.join(directory, filename)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    return path


class ToleranceParsingTest(unittest.TestCase):
    def test_range_ppm(self):
        self.assertEqual(pbc.parse_tolerance("[-4.5 ppm, 4.5 ppm]"), (4.5, "ppm"))

    def test_range_da(self):
        self.assertEqual(pbc.parse_tolerance("[-0.02 Da, 0.02 Da]"), (0.02, "da"))

    def test_single_value_shapes(self):
        self.assertEqual(pbc.parse_tolerance("10.00ppm"), (10.0, "ppm"))
        self.assertEqual(pbc.parse_tolerance("5 ppm"), (5.0, "ppm"))
        self.assertEqual(pbc.parse_tolerance("0.02Da"), (0.02, "da"))

    def test_missing_and_nan(self):
        self.assertIsNone(pbc.parse_tolerance(None))
        self.assertIsNone(pbc.parse_tolerance(float("nan")))

    def test_malformed(self):
        self.assertIsNone(pbc.parse_tolerance("not a tolerance"))
        self.assertIsNone(pbc.parse_tolerance("[-10 ppm, 0.02 Da]"))  # mixed units
        self.assertIsNone(pbc.parse_tolerance("[-10, 10]"))  # no unit

    def test_usable_number_rejects_unusable(self):
        self.assertIsNone(pbc.usable_number(None))
        self.assertIsNone(pbc.usable_number("abc"))
        self.assertIsNone(pbc.usable_number(float("nan")))
        self.assertIsNone(pbc.usable_number(float("inf")))
        self.assertEqual(pbc.usable_number(2), 2.0)
        self.assertEqual(pbc.usable_number("0.01"), 0.01)


class ClassificationTest(unittest.TestCase):
    def test_default_passes(self):
        status, failed = pbc.classify_submission(make_submission())
        self.assertEqual(status, pbc.COMPATIBLE)
        self.assertEqual(failed, [])

    def test_precursor_boundary(self):
        self.assertEqual(
            pbc.classify_submission(make_submission(precursor="[-10 ppm, 10 ppm]"))[0],
            pbc.COMPATIBLE,
        )
        self.assertEqual(
            pbc.classify_submission(make_submission(precursor="[-10.1 ppm, 10.1 ppm]"))[0],
            pbc.INCOMPATIBLE,
        )

    def test_fragment_boundaries(self):
        self.assertEqual(
            pbc.classify_submission(make_submission(fragment="[-0.05 Da, 0.05 Da]"))[0],
            pbc.COMPATIBLE,
        )
        self.assertEqual(
            pbc.classify_submission(make_submission(fragment="[-50 ppm, 50 ppm]"))[0],
            pbc.COMPATIBLE,
        )
        self.assertEqual(
            pbc.classify_submission(make_submission(fragment="[-0.06 Da, 0.06 Da]"))[0],
            pbc.INCOMPATIBLE,
        )
        self.assertEqual(
            pbc.classify_submission(make_submission(fragment="[-51 ppm, 51 ppm]"))[0],
            pbc.INCOMPATIBLE,
        )

    def test_nan_fdr_is_unknown(self):
        status, failed = pbc.classify_submission(make_submission(fdr=float("nan")))
        self.assertEqual(status, pbc.UNKNOWN)
        self.assertEqual(failed, ["missing_or_unusable_metadata"])

    def test_malformed_tolerance_is_unknown(self):
        status, _ = pbc.classify_submission(make_submission(precursor="garbage"))
        self.assertEqual(status, pbc.UNKNOWN)

    def test_missing_enzyme_is_unknown(self):
        data = make_submission()
        del data["enzyme"]
        self.assertEqual(pbc.classify_submission(data)[0], pbc.UNKNOWN)

    def test_wrong_enzyme_is_incompatible(self):
        status, failed = pbc.classify_submission(make_submission(enzyme="LysC"))
        self.assertEqual(status, pbc.INCOMPATIBLE)
        self.assertIn("enzyme", failed)

    def test_mc3_is_incompatible(self):
        status, failed = pbc.classify_submission(make_submission(miscleavages=3))
        self.assertEqual(status, pbc.INCOMPATIBLE)
        self.assertIn("missed_cleavages", failed)

    def test_fdr_far_from_target_is_incompatible(self):
        status, failed = pbc.classify_submission(make_submission(fdr=0.006))
        self.assertEqual(status, pbc.INCOMPATIBLE)
        self.assertIn("psm_fdr", failed)

    def test_fdr_near_target_is_compatible(self):
        status, _ = pbc.classify_submission(make_submission(fdr=0.01 + 0.0009))
        self.assertEqual(status, pbc.COMPATIBLE)


class LoadAndValidateTest(unittest.TestCase):
    def test_hash_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = make_submission(intermediate_hash="b" * 40)
            write_submission(tmp, data, filename="c" * 40 + ".json")
            with self.assertRaises(ValueError):
                pbc.load_external_submissions(tmp)

    def test_malformed_json_is_deterministic_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "d" * 40 + ".json"), "w") as handle:
                handle.write("{not json")
            records = pbc.load_external_submissions(tmp)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["status"], pbc.UNKNOWN)
            self.assertEqual(records[0]["failed_criteria"], ["malformed_json"])
            records2 = pbc.load_external_submissions(tmp)
            self.assertEqual(records, records2)

    def test_records_sorted_by_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            for stem in ("e" * 40, "a" * 40, "c" * 40):
                write_submission(tmp, make_submission(intermediate_hash=stem))
            records = pbc.load_external_submissions(tmp)
            self.assertEqual(
                [r["filename"] for r in records],
                ["a" * 40 + ".json", "c" * 40 + ".json", "e" * 40 + ".json"],
            )


class FlattenMetricsTest(unittest.TestCase):
    def test_n6_nr_feature_maps(self):
        data = make_submission(nr6=1234)
        flat = pbc.flatten_n_metrics(data, 6)
        self.assertEqual(flat["nr_feature"], 1234)

    def test_missing_n_yields_empty(self):
        self.assertEqual(pbc.flatten_n_metrics(make_submission(), 4), {})
        self.assertEqual(pbc.flatten_n_metrics({"results": None}, 3), {})

    def test_nan_metric_omitted(self):
        data = make_submission()
        data["results"]["3"]["median_abs_epsilon_global"] = float("nan")
        flat = pbc.flatten_n_metrics(data, 3)
        self.assertNotIn("median_abs_epsilon_global", flat)
        self.assertIn("nr_feature", flat)


class LocalResultsTest(unittest.TestCase):
    def test_local_quantified_precursors_maps_to_n6(self):
        local = {
            "label": "Exp 1 (Comet->Percolator)",
            "dataset": "PXD028735 (HYE mixed-species)",
            "tool": {"name": "quantms", "version": "1.8.0",
                     "search_engine": "Comet", "search_engine_version": "2023.01 rev. 2"},
            "proteobench": {"version": "0.18.2", "module": "quant_lfq_DDA_ion_QExactive",
                            "input_format": "quantms",
                            "intermediate_hash": "2a" * 20},
            "metrics": {"quantified_precursors": 34635,
                        "median_abs_epsilon": 0.211, "mean_abs_epsilon": 0.307,
                        "cv_median": 0.227, "roc_auc": 0.894},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "local.json")
            with open(path, "w") as handle:
                json.dump(local, handle)
            rows = pbc.load_local_results([path])
        self.assertEqual(rows[0]["nr_feature_n6"], 34635)
        self.assertEqual(rows[0]["scorer_version"], "0.18.2")
        self.assertEqual(rows[0]["search_engine_version"], "2023.01 rev. 2")

    def test_real_exp_files_load(self):
        real = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "results", "tools", "proteobench-Exp-1-Comet--Percolator.json",
        )
        if not os.path.exists(real):
            self.skipTest("Exp-1 result not present")
        rows = pbc.load_local_results([real])
        self.assertEqual(rows[0]["nr_feature_n6"], 34635)


class SummaryAndDeterminismTest(unittest.TestCase):
    def _build(self, tmp):
        write_submission(tmp, make_submission(
            intermediate_hash="1" * 40,
            precursor="[-10 ppm, 10 ppm]", fragment="[-0.02 Da, 0.02 Da]",
        ))
        write_submission(tmp, make_submission(
            intermediate_hash="2" * 40, miscleavages=3,
        ))
        write_submission(tmp, make_submission(
            intermediate_hash="3" * 40, fdr=float("nan"),
        ))
        external = pbc.load_external_submissions(tmp)
        return pbc.build_summary(external, [])

    def test_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = self._build(tmp)
            self.assertEqual(summary["counts"]["total"], 3)
            self.assertEqual(summary["counts"]["compatible"], 1)
            self.assertEqual(summary["counts"]["incompatible"], 1)
            self.assertEqual(summary["counts"]["unknown_insufficient_metadata"], 1)

    def test_no_schema_identifier_in_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = self._build(tmp)
            text = json.dumps(summary)
            self.assertNotIn("v1", text.replace("v1.2.3", "").replace("2023.01", ""))
            for banned in ("openms-benchmarking/report", "schema"):
                self.assertNotIn(banned, text)

    def test_deterministic_summary_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = self._build(tmp)
            # Rebuild from a fresh copy of the same inputs.
            tmp2 = tempfile.mkdtemp()
            self.addCleanup(lambda: __import__("shutil").rmtree(tmp2, ignore_errors=True))
            for name in os.listdir(tmp):
                with open(os.path.join(tmp, name), "rb") as handle:
                    payload = handle.read()
                with open(os.path.join(tmp2, name), "wb") as handle:
                    handle.write(payload)
            second = pbc.load_external_submissions(tmp2)
            second_summary = pbc.build_summary(second, [])
            self.assertEqual(
                json.dumps(first, sort_keys=True), json.dumps(second_summary, sort_keys=True)
            )

    def test_malformed_submission_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "4" * 40 + ".json"), "w") as handle:
                handle.write("{\"results\": [broken")
            first = pbc.load_external_submissions(tmp)
            second = pbc.load_external_submissions(tmp)
            self.assertEqual(first, second)
            self.assertEqual(first[0]["status"], pbc.UNKNOWN)


class HtmlTest(unittest.TestCase):
    def _html(self, tmp):
        summary = self._build(tmp)
        return pbc.render_html(summary), summary

    def _build(self, tmp):
        write_submission(tmp, make_submission(intermediate_hash="1" * 40))
        write_submission(tmp, make_submission(intermediate_hash="2" * 40, miscleavages=3))
        external = pbc.load_external_submissions(tmp)
        local = [
            {
                "label": "Exp 1", "dataset": "PXD028735", "software": "quantms",
                "software_version": "1.8.0", "search_engine": "Comet",
                "search_engine_version": "2023.01 rev. 2", "scorer_version": "0.18.2",
                "module": "quant_lfq_DDA_ion_QExactive",
                "intermediate_hash": "2a" * 20, "nr_feature_n6": 34635,
                "median_abs_epsilon": 0.211, "mean_abs_epsilon": 0.307,
                "cv_median": 0.227, "roc_auc": 0.894,
            }
        ]
        return pbc.build_summary(external, local)

    def test_filter_summary_line_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            page, summary = self._html(tmp)
            counts = summary["counts"]
            for token in (
                f"{counts['total']} total",
                f"{counts['compatible']} compatible",
                f"{counts['incompatible']} incompatible",
            ):
                self.assertIn(token, page)

    def test_no_ranking_language(self):
        with tempfile.TemporaryDirectory() as tmp:
            page, _ = self._html(tmp)
            lowered = page.lower()
            for banned in ("winner", "best", "leaderboard", "rank", "medal", "top performer"):
                self.assertNotIn(banned, lowered)

    def test_local_rows_labeled(self):
        with tempfile.TemporaryDirectory() as tmp:
            page, _ = self._html(tmp)
            self.assertIn("local ProteoBench scorer", page)
            self.assertIn("34,635", page)  # 1-levels formatting of nr_feature_n6

    def test_provenance_and_caveats_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            page, _ = self._html(tmp)
            self.assertIn(pbc.SOURCE_COMMIT, page)
            self.assertIn("SoCe entrapment", page)
            self.assertIn("cannot be proven", page)
            self.assertIn("contextual", page)

    def test_excluded_submissions_listed_with_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            page, _ = self._html(tmp)
            self.assertIn("Excluded from the compatible view", page)
            self.assertIn("missed_cleavages", page)


class CliTest(unittest.TestCase):
    def test_end_to_end_deterministic_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            ext = os.path.join(tmp, "external")
            os.makedirs(ext)
            write_submission(ext, make_submission(intermediate_hash="1" * 40))
            write_submission(ext, make_submission(intermediate_hash="2" * 40, miscleavages=3))
            local = os.path.join(tmp, "local.json")
            with open(local, "w") as handle:
                json.dump({
                    "label": "Exp 1", "tool": {"name": "quantms", "version": "1.8.0",
                                               "search_engine": "Comet",
                                               "search_engine_version": "2023.01"},
                    "proteobench": {"version": "0.18.2",
                                    "module": "quant_lfq_DDA_ion_QExactive",
                                    "intermediate_hash": "2a" * 20},
                    "metrics": {"quantified_precursors": 34635,
                                "median_abs_epsilon": 0.211, "mean_abs_epsilon": 0.307,
                                "cv_median": 0.227, "roc_auc": 0.894},
                }, handle)
            script = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "proteobench_compare.py"
            )
            outputs = []
            for run in (1, 2):
                jout = os.path.join(tmp, f"summary{run}.json")
                hout = os.path.join(tmp, f"report{run}.html")
                result = subprocess.run(
                    [sys.executable, script, "--external-dir", ext, "--local", local,
                     "--json-out", jout, "--html-out", hout],
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                outputs.append(
                    (
                        hashlib.sha256(open(jout, "rb").read()).hexdigest(),
                        hashlib.sha256(open(hout, "rb").read()).hexdigest(),
                    )
                )
            self.assertEqual(outputs[0], outputs[1])


if __name__ == "__main__":
    unittest.main()
