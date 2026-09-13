#!/usr/bin/env python3
"""Tests for prose_metrics.py (ProSE + PeptDeep entrapment benchmark).

Uses small synthetic idXML files written on the fly - no real dataset, no
ProSE build needed. The synthetic files mirror the structure of real ProSE
output (PR #9975): SearchParameters with extra_features, one
PeptideIdentification per PSM whose single PeptideHit carries the q-value as
main score, a target_decoy UserParam, and space-separated protein_refs.

Run:  python3 benchmark/test_prose_metrics.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import prose_metrics as pm  # noqa: E402


IDXML_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<IdXML version="1.5">
  <SearchParameters id="SP_0" db="entrapment.fasta">
    <UserParam type="string" name="extra_features" value="{extra_features}"/>
  </SearchParameters>
  <IdentificationRun date="2026-09-06T00:00:00" search_engine="ProSE" search_engine_version="test" search_parameters_ref="SP_0">
    <ProteinIdentification score_type="" higher_score_better="true">
      <ProteinHit id="PH_0" accession="TARGET1" score="0.0" sequence=""/>
      <ProteinHit id="PH_1" accession="TARGET2" score="0.0" sequence=""/>
      <ProteinHit id="PH_2" accession="ENTRAPMENT_1" score="0.0" sequence=""/>
      <ProteinHit id="PH_3" accession="DECOY_TARGET1" score="0.0" sequence=""/>
      <ProteinHit id="PH_4" accession="DECOY_ENTRAPMENT_1" score="0.0" sequence=""/>
    </ProteinIdentification>
{peptide_ids}
  </IdentificationRun>
</IdXML>
"""

# One PeptideIdentification with one top hit. score is the q-value whenever
# score_type="q-value"; refs are ProteinHit ids.
PID_TEMPLATE = """    <PeptideIdentification score_type="{score_type}" higher_score_better="{hsb}" spectrum_reference="spectrum={i}">
      <PeptideHit score="{score}" sequence="{sequence}" charge="2" protein_refs="{refs}">
{userparams}      </PeptideHit>
    </PeptideIdentification>"""


def userparam(name, value, type_="string"):
    return '        <UserParam type="%s" name="%s" value="%s"/>\n' % (type_, name, value)


def make_idxml(path, pids, extra_features="score,delta_score,isotope_error"):
    """pids: list of dicts accepted by PID_TEMPLATE."""
    body = "".join(PID_TEMPLATE.format(i=i, **p) for i, p in enumerate(pids))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(IDXML_TEMPLATE.format(extra_features=extra_features, peptide_ids=body))


def pid(sequence, score, refs, target_decoy, score_type="q-value", extra=None):
    ups = userparam("target_decoy", target_decoy)
    for k, v in (extra or {}).items():
        ups += userparam(k, v, "float")
    return {
        "sequence": sequence, "score": score, "refs": refs,
        "target_decoy": target_decoy, "score_type": score_type,
        "hsb": "false" if score_type == "q-value" else "true",
        "userparams": ups,
    }


def test_qvalue_filtering():
    """Only PSMs with q <= 0.01 count; inclusive at exactly 0.01."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.idXML")
        make_idxml(path, [
            pid("PEPTIDEA", 0.001, "PH_0", "target"),   # kept
            pid("PEPTIDEB", 0.01, "PH_0", "target"),    # kept (inclusive tie)
            pid("PEPTIDEC", 0.010001, "PH_0", "target"),  # dropped
            pid("PEPTIDED", 0.5, "PH_0", "target"),     # dropped
        ])
        parsed = pm.parse_idxml(path, "ENTRAPMENT_")
        counts = pm.count_psms(parsed)
        assert counts["target_psms_at_1pct_fdr"] == 2, counts
        assert counts["target_peptides_at_1pct_fdr"] == 2, counts
        assert counts["psms_total"] == 4, counts
    print("PASS: q-value filtering (inclusive <= 0.01)")


def test_target_vs_entrapment_classification():
    """Accession-prefix policy: entrapment prefix wins; decoys never target."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.idXML")
        make_idxml(path, [
            pid("T_PEPTIDE", 0.001, "PH_0", "target"),          # target
            pid("E_PEPTIDE", 0.001, "PH_2", "target"),          # entrapment (prefix)
            pid("SHARED", 0.001, "PH_0 PH_2", "target"),        # shared -> entrapment (conservative)
            pid("T_MULTI", 0.001, "PH_0 PH_1", "target"),       # two targets -> target
            pid("DECOY_HIT", 0.001, "PH_3", "decoy"),           # decoy -> neither
            pid("E_DECOY", 0.001, "PH_4", "decoy"),             # entrapment decoy -> neither
        ])
        parsed = pm.parse_idxml(path, "ENTRAPMENT_")
        counts = pm.count_psms(parsed)
        assert counts["target_psms_at_1pct_fdr"] == 2, counts          # T_PEPTIDE, T_MULTI
        assert counts["entrapment_psms_at_1pct_fdr"] == 2, counts      # E_PEPTIDE, SHARED
        assert counts["target_peptides_at_1pct_fdr"] == 2, counts
        assert counts["entrapment_peptides_at_1pct_fdr"] == 2, counts
    print("PASS: target vs entrapment classification")


def test_unique_peptide_counting():
    """Peptides counted once per distinct sequence notation across PSMs."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.idXML")
        make_idxml(path, [
            pid("PEPTIDEA", 0.001, "PH_0", "target"),
            pid("PEPTIDEA", 0.002, "PH_1", "target"),              # same seq, other protein
            pid("PEPTIDEA", 0.003, "PH_0", "target"),              # same seq again
            pid("M(Oxidation)PEPTIDEA", 0.001, "PH_0", "target"),  # modified -> distinct
            pid("PEPTIDEB", 0.001, "PH_2", "target"),
            pid("PEPTIDEB", 0.002, "PH_2", "target"),
        ])
        parsed = pm.parse_idxml(path, "ENTRAPMENT_")
        counts = pm.count_psms(parsed)
        assert counts["target_psms_at_1pct_fdr"] == 4, counts
        assert counts["target_peptides_at_1pct_fdr"] == 2, counts  # PEPTIDEA, M(Oxidation)PEPTIDEA
        assert counts["entrapment_psms_at_1pct_fdr"] == 2, counts
        assert counts["entrapment_peptides_at_1pct_fdr"] == 1, counts
    print("PASS: unique peptide counting (distinct sequence notation)")


def test_fallback_qvalue_userparam():
    """Without score_type=q-value, the percolator_q_value UserParam is used."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.idXML")
        make_idxml(path, [
            pid("PEPTIDEA", 5.0, "PH_0", "target", score_type="svm",
                extra={"percolator_q_value": 0.001}),
            pid("PEPTIDEB", 1.0, "PH_0", "target", score_type="svm",
                extra={"percolator_q_value": 0.5}),
        ])
        parsed = pm.parse_idxml(path, "ENTRAPMENT_")
        counts = pm.count_psms(parsed)
        assert counts["target_psms_at_1pct_fdr"] == 1, counts
    print("PASS: q-value fallback via percolator_q_value UserParam")


def test_missing_peptdeep_features_fail():
    """Baseline extra_features must NOT satisfy the check; full set must."""
    with tempfile.TemporaryDirectory() as tmp:
        # 1) baseline-style extra_features -> all five missing
        path = os.path.join(tmp, "baseline.idXML")
        make_idxml(path, [pid("PEPTIDEA", 0.001, "PH_0", "target")])
        missing = pm.check_peptdeep_features(path)
        assert sorted(missing) == sorted(pm.PEPTDEEP_FEATURES), missing

        # 2) registered but never written to any hit -> still missing
        path2 = os.path.join(tmp, "registered_only.idXML")
        make_idxml(path2, [pid("PEPTIDEA", 0.001, "PH_0", "target")],
                   extra_features=",".join(pm.PEPTDEEP_FEATURES))
        missing2 = pm.check_peptdeep_features(path2)
        assert sorted(missing2) == sorted(pm.PEPTDEEP_FEATURES), missing2

        # 3) registered AND present on the hit -> pass (empty missing list)
        path3 = os.path.join(tmp, "peptdeep.idXML")
        features = ",".join(["score", "delta_score"] + list(pm.PEPTDEEP_FEATURES))
        make_idxml(path3, [pid("PEPTIDEA", 0.001, "PH_0", "target",
                               extra={f: 0.5 for f in pm.PEPTDEEP_FEATURES})],
                   extra_features=features)
        missing3 = pm.check_peptdeep_features(path3)
        assert missing3 == [], missing3
    print("PASS: missing PeptDeep features detected (registration and per-hit)")


def test_runner_verdict_fails_on_missing_features():
    """End-to-end runner test: the stub ProSE really produces valid
    baseline-style idXML outputs for BOTH arms (so the two required stages
    pass), but the peptdeep arm carries none of the five PeptDeep features.
    The feature guard must then yield prose.json verdict=fail and a
    non-zero runner exit - not an incidental crash on missing files."""
    with tempfile.TemporaryDirectory() as tmp:
        def fwd(p):
            # forward slashes: unambiguous for bash and for Python on Windows
            return p.replace("\\", "/")

        # Inputs required by the runner's existence checks (content is
        # irrelevant: the stub ProSE ignores them).
        input_mzml = fwd(os.path.join(tmp, "run.mzML"))
        input_fasta = fwd(os.path.join(tmp, "entrapment.fasta"))
        for path in (input_mzml, input_fasta):
            with open(path, "w") as fh:
                fh.write("stub\n")

        # What the stub writes for both arms: valid ProSE-style output with
        # baseline extra_features and no PeptDeep features anywhere.
        stub_idxml = fwd(os.path.join(tmp, "stub_output.idXML"))
        make_idxml(stub_idxml, [pid("PEPTIDEA", 0.001, "PH_0", "target")])

        # Stub ProSE: copies the prepared idXML to -out_idxml, writes a
        # minimal summary YAML to -summary_out, always exits 0. (LF endings:
        # a shebang script with CRLF breaks on strict-bash platforms.)
        stub = fwd(os.path.join(tmp, "ProSE"))
        with open(stub, "w", newline="\n") as fh:
            fh.write(
                "#!/usr/bin/env bash\n"
                "out=''\nsummary=''\nprev=''\n"
                "for arg in \"$@\"; do\n"
                "  if [[ \"${prev}\" == '-out_idxml' ]]; then out=\"${arg}\"; fi\n"
                "  if [[ \"${prev}\" == '-summary_out' ]]; then summary=\"${arg}\"; fi\n"
                "  prev=\"${arg}\"\n"
                "done\n"
                "if [[ -n \"${out}\" ]]; then cp \"${STUB_IDXML}\" \"${out}\"; fi\n"
                "if [[ -n \"${summary}\" ]]; then"
                " echo 'shared: {seconds_total: 1.0}' > \"${summary}\"; fi\n"
                "exit 0\n"
            )
        os.chmod(stub, 0o755)

        results = fwd(os.path.join(tmp, "results"))
        script = fwd(os.path.join(HERE, "run_prose_benchmark.sh"))

        # Hermetic `python3` for the runner's inline Python blocks: on Windows
        # Git Bash, python3 often resolves to the Microsoft Store alias stub,
        # so shim it to the interpreter running this test (on Linux this is a
        # no-op passthrough).
        shim_dir = fwd(os.path.join(tmp, "bin"))
        os.makedirs(shim_dir)
        shim = os.path.join(shim_dir, "python3")
        with open(shim, "w", newline="\n") as fh:
            fh.write("#!/usr/bin/env bash\nexec '%s' \"$@\"\n"
                     % sys.executable.replace("\\", "/"))
        os.chmod(shim, 0o755)
        path_env = shim_dir + os.pathsep + os.environ.get("PATH", "")

        # Resolve bash explicitly: on Windows, CreateProcess searching bare
        # "bash" hits System32's WSL launcher before Git Bash's PATH entries,
        # and WSL bash cannot open C:/... script paths. shutil.which() only
        # walks PATH directories, so it lands on the Git Bash / POSIX bash.
        bash_exe = shutil.which("bash") or "bash"
        proc = subprocess.run(
            [bash_exe, script],
            env=dict(os.environ, PATH=path_env, OPENMS_BIN=tmp, PROSE_INPUT=input_mzml,
                     PROSE_FASTA=input_fasta, PROSE_EXE=stub, PERCOLATOR_EXE=stub,
                     RESULTS_DIR=results, PROSE_ENTRAPMENT_PREFIX="ENTRAPMENT_",
                     STUB_IDXML=stub_idxml),
            capture_output=True, text=True)

        # The runner must have completed the whole pipeline (both stages
        # pass, prose.json written) and failed ONLY on the feature guard.
        prose_json = os.path.join(results, "prose.json")
        assert os.path.exists(prose_json), proc.stdout + proc.stderr
        with open(prose_json) as fh:
            data = json.load(fh)
        assert all(s["status"] == "pass" for s in data["stages"]), data["stages"]
        assert data["arms"]["baseline"].get("peptdeep_features_missing") is None
        assert data["arms"]["peptdeep"]["peptdeep_features_missing"] == \
            list(pm.PEPTDEEP_FEATURES), data["arms"]["peptdeep"]
        assert data["verdict"] == "fail", data["verdict"]
        assert proc.returncode != 0, \
            "runner must exit non-zero when the peptdeep arm lacks its features"
    print("PASS: runner verdict fails when PeptDeep features are missing")


def test_normalize_prose_peptdeep():
    """The v2 normalization subcommand maps prose.json into the v2 model."""
    sys.path.insert(0, os.path.join(HERE, "report"))
    import report_generate as rg

    with tempfile.TemporaryDirectory() as tmp:
        results = os.path.join(tmp, "results")
        os.makedirs(results)
        raw = {
            "schema": "openms-benchmarking/report/v2",
            "benchmark": "prose_peptdeep",
            "verdict": "pass",
            "openms_sha": "cafe123",
            "run_at": "2026-09-06T00:00:00Z",
            "arms": {
                "baseline": {
                    "target_psms_at_1pct_fdr": 100,
                    "target_peptides_at_1pct_fdr": 80,
                    "entrapment_psms_at_1pct_fdr": 2,
                    "entrapment_peptides_at_1pct_fdr": 2,
                    "wall_time_s": 10.0, "peak_rss_mb": 500.0,
                    "peptdeep_features_missing": [],
                },
                "peptdeep": {
                    "target_psms_at_1pct_fdr": 120,
                    "target_peptides_at_1pct_fdr": 95,
                    "entrapment_psms_at_1pct_fdr": 1,
                    "entrapment_peptides_at_1pct_fdr": 1,
                    "wall_time_s": 14.0, "peak_rss_mb": 700.0,
                    "peptdeep_features_missing": [],
                },
            },
            "stages": [
                {"name": "prose_baseline", "required": True, "exit_code": 0,
                 "wall_time_s": 10.0, "cpu_time_s": 8.0, "peak_rss_kb": 512000,
                 "status": "pass", "reason": ""},
                {"name": "prose_peptdeep", "required": True, "exit_code": 0,
                 "wall_time_s": 14.0, "cpu_time_s": 11.0, "peak_rss_kb": 716800,
                 "status": "pass", "reason": ""},
            ],
            "configuration": {"entrapment_prefix": "ENTRAPMENT_",
                              "peptdeep_instrument": "QE"},
        }
        with open(os.path.join(results, "prose.json"), "w") as fh:
            json.dump(raw, fh)
        with open(os.path.join(results, "meta.txt"), "w") as fh:
            fh.write("openms_sha=cafe123\nentrapment_prefix=ENTRAPMENT_\n"
                     "peptdeep_instrument=QE\nprose_input=run1.mzML\n")

        class Args:
            run_id = "42"
            cache = "none"
            run_at = ""
            dataset = ""
            results_dir = results
            # default out path: results/prose_peptdeep/openms/*.json, which is
            # exactly the v2 layout load_results() discovers
            out = None

        rg.normalize_prose_peptdeep(Args())
        with open(os.path.join(results, "prose_peptdeep", "openms",
                               "prose_peptdeep-42.json")) as fh:
            norm = json.load(fh)

        assert norm["schema"] == rg.SCHEMA_V2
        assert norm["identity"]["benchmark"] == "prose_peptdeep"
        assert norm["identity"]["software"] == {"name": "OpenMS", "version": "cafe123"}
        assert norm["performance"]["wall_time_s"] == 24.0   # sum of stage walls
        assert norm["performance"]["peak_rss_kb"] == 716800  # max stage peak
        m = norm["metrics"]
        assert m["verdict"] == "pass"
        # headline metrics, arm-qualified, both arms in one result
        assert m["target_psms_at_1pct_fdr_baseline"] == 100
        assert m["target_psms_at_1pct_fdr_peptdeep"] == 120
        assert m["target_peptides_at_1pct_fdr_baseline"] == 80
        assert m["target_peptides_at_1pct_fdr_peptdeep"] == 95
        assert m["wall_time_s_peptdeep"] == 14.0
        assert m["peak_rss_mb_peptdeep"] == 700.0

        # and the generic discovery accepts it as an OpenMS run
        runs, tools = rg.load_results(results)
        assert any(r["identity"]["benchmark"] == "prose_peptdeep" for r in runs)
    print("PASS: normalization into v2 (arm-qualified metrics, generic discovery)")


def main():
    test_qvalue_filtering()
    test_target_vs_entrapment_classification()
    test_unique_peptide_counting()
    test_fallback_qvalue_userparam()
    test_missing_peptdeep_features_fail()
    test_runner_verdict_fails_on_missing_features()
    test_normalize_prose_peptdeep()
    print("\nAll prose_metrics tests passed.")


if __name__ == "__main__":
    main()
