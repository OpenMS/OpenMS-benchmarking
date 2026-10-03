#!/usr/bin/env python3
"""Tests for openswath_metrics.py (OpenSwath DIA benchmark).

Uses small synthetic TraML / featureXML files written on the fly, plus one
regression test against the real committed OpenSwath fixtures - no OpenMS
build needed.

The synthetic files mirror the real artifacts: a namespaced TraML whose
transitions carry a precursor isolation-window m/z and a peptideRef, and a
plain (unnamespaced) featureXML whose picked features carry the transition ids
as <UserParam name="native_id"> on their subordinate features.

What is deliberately NOT tested here: PSMs, FDR and decoy counts. DIA emits no
spectrum-level identifications, so there is nothing in these artifacts to count.

Run:  python3 benchmark/test_openswath_metrics.py
"""

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import openswath_metrics as om  # noqa: E402

FIXTURES = os.path.join(HERE, "fixtures")


TRAML_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<TraML version="1.0.0" xmlns="http://psi.hupo.org/ms/traml">
  <ProteinList>
{proteins}  </ProteinList>
  <CompoundList>
{peptides}  </CompoundList>
  <TransitionList>
{transitions}  </TransitionList>
</TraML>
"""

PROTEIN_TEMPLATE = (
    '    <Protein id="{pid}">\n'
    '      <cvParam cvRef="MS" accession="MS:1000885" name="protein accession" '
    'value="{acc}"/>\n'
    '    </Protein>\n'
)

PEPTIDE_TEMPLATE = (
    '    <Peptide id="{pid}" sequence="{seq}">\n'
    '      <cvParam cvRef="MS" accession="MS:1000041" name="charge state" '
    'value="{charge}"/>\n'
    '      <ProteinRef ref="{protein}"/>\n'
    '    </Peptide>\n'
)

TRANSITION_TEMPLATE = (
    '    <Transition id="{tid}" peptideRef="{pep}">\n'
    '      <Precursor>\n'
    '        <cvParam cvRef="MS" accession="MS:1000827" '
    'name="isolation window target m/z" value="{mz}"/>\n'
    '      </Precursor>\n'
    '      <Product/>\n'
    '    </Transition>\n'
)

FEATUREXML_TEMPLATE = """<?xml version="1.0" encoding="ISO-8859-1"?>
<featureMap version="1.9" id="fm_test">
  <dataProcessing completion_time="1999-12-31T23:59:59">
    <software name="OpenSwathWorkflow" version="test"/>
  </dataProcessing>
  <featureList count="{count}">
{features}  </featureList>
</featureMap>
"""

# One picked feature: the transition ids it was picked from are the
# <UserParam name="native_id"> values on its subordinate features, which is
# how OpenSwath writes them.
FEATURE_TEMPLATE = """    <feature id="{fid}">
      <position dim="0">1.0</position>
      <position dim="1">2.0</position>
      <intensity>1.0</intensity>
      <overallquality>0.5</overallquality>
      <subordinate>
{subordinates}      </subordinate>
    </feature>
"""

SUBORDINATE_TEMPLATE = (
    '        <feature id="{fid}_s{sid}">\n'
    '          <intensity>1.0</intensity>\n'
    '          <UserParam type="string" name="native_id" value="{native_id}"/>\n'
    '          <UserParam type="string" name="FeatureLevel" value="MS2"/>\n'
    '        </feature>\n'
)


def make_traml(path, proteins, peptides, transitions):
    """proteins: [(id, accession)]; peptides: [(id, seq, charge, protein)];
    transitions: [(id, peptide_id, precursor_mz)]."""
    body = TRAML_TEMPLATE.format(
        proteins="".join(PROTEIN_TEMPLATE.format(pid=p, acc=a) for p, a in proteins),
        peptides="".join(PEPTIDE_TEMPLATE.format(pid=p, seq=s, charge=c, protein=pr)
                         for p, s, c, pr in peptides),
        transitions="".join(TRANSITION_TEMPLATE.format(tid=t, pep=p, mz=z)
                            for t, p, z in transitions),
    )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)


def make_featurexml(path, features):
    """features: [native_id, ...] - each entry becomes one picked feature."""
    blocks = []
    for i, natives in enumerate(features):
        subs = "".join(
            SUBORDINATE_TEMPLATE.format(fid="f_%d" % i, sid=j, native_id=n)
            for j, n in enumerate(natives))
        blocks.append(FEATURE_TEMPLATE.format(fid="f_%d" % i, subordinates=subs))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(FEATUREXML_TEMPLATE.format(count=len(features),
                                            features="".join(blocks)))


def test_traml_parsing():
    """Transitions resolve to peptides and proteins; precursor identity is the
    (isolation m/z, charge) tuple, not the peptide."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "lib.TraML")
        make_traml(
            path,
            proteins=[("ProtA", "uniprot_a"), ("ProtB", "uniprot_b")],
            peptides=[("PepA", "AAAA", "2", "ProtA"),
                      ("PepB", "BBBB", "2", "ProtB"),
                      ("PepC", "CCCC", "3", "ProtB"),
                      ("PepOrphan", "DDDD", "2", "ProtA")],
            transitions=[("t1", "PepA", "400.5"),
                         ("t2", "PepA", "400.5"),
                         ("t3", "PepA", "999.9"),   # same peptide, other m/z
                         ("t4", "PepB", "500.5"),
                         ("t5", "PepC", "600.5")],
        )
        lib = om.parse_traml(path)

        # transition -> peptide, and the peptide -> protein mapping
        assert lib["transitions"]["t1"]["peptide"] == "PepA", lib["transitions"]
        assert lib["peptides"]["PepA"]["proteins"] == {"ProtA"}, lib["peptides"]
        assert lib["peptides"]["PepB"]["proteins"] == {"ProtB"}, lib["peptides"]
        assert lib["peptides"]["PepC"]["charge"] == "3", lib["peptides"]

        # precursor identity: PepA has TWO precursor m/z values -> two
        # precursors, so peptide count and precursor count legitimately differ.
        assert lib["library_peptides"] == 4, lib          # incl. PepOrphan
        assert lib["library_transitions"] == 5, lib
        assert lib["library_proteins"] == 2, lib
        # PepA(400.5), PepA(999.9), PepB(500.5), PepC(600.5) = 4; PepOrphan
        # has no transitions so it contributes no precursor.
        assert lib["library_precursor_mz"] == 4, lib

        assert lib["transitions"]["t1"]["precursor"] == ("400.5", "2"), lib
        assert lib["transitions"]["t3"]["precursor"] == ("999.9", "2"), lib
    print("PASS: TraML parsing (transition->peptide->protein, precursor identity)")


def test_feature_join_counts():
    """Distinct peptides/precursors/proteins reached through the join, plus
    transitions used and library coverage."""
    with tempfile.TemporaryDirectory() as tmp:
        traml = os.path.join(tmp, "lib.TraML")
        make_traml(
            traml,
            proteins=[("ProtA", "uniprot_a"), ("ProtB", "uniprot_b")],
            peptides=[("PepA", "AAAA", "2", "ProtA"),
                      ("PepB", "BBBB", "2", "ProtB"),
                      ("PepC", "CCCC", "2", "ProtA"),
                      ("PepOrphan", "DDDD", "2", "ProtB")],
            transitions=[("t1", "PepA", "400.5"),
                         ("t2", "PepA", "400.5"),
                         ("t3", "PepB", "500.5")],
        )
        feat = os.path.join(tmp, "out.featureXML")
        # two features, both reaching PepA, plus one reaching PepB
        make_featurexml(feat, [["t1", "t2"], ["t2"], ["t3"]])

        counts = om.count_identifications(om.parse_traml(traml), feat)

        assert counts["identified_peptides"] == 2, counts        # PepA, PepB
        assert counts["identified_precursor_mz"] == 2, counts    # one m/z each
        assert counts["identified_proteins"] == 2, counts       # ProtA, ProtB
        assert counts["transitions_used"] == 3, counts
        assert counts["library_peptides"] == 4, counts          # incl. orphan
        assert counts["library_precursor_mz"] == 2, counts
        assert counts["library_transitions"] == 3, counts
        # 2 of 4 library peptides
        assert counts["library_coverage"] == 0.5, counts
    print("PASS: feature join (distinct peptides/precursors/proteins, coverage)")


def test_unmatched_transition_fails_loudly():
    """A feature referencing a transition the library does not define must
    raise, never be counted as an identification."""
    with tempfile.TemporaryDirectory() as tmp:
        traml = os.path.join(tmp, "lib.TraML")
        make_traml(traml,
                   proteins=[("ProtA", "uniprot_a")],
                   peptides=[("PepA", "AAAA", "2", "ProtA")],
                   transitions=[("t1", "PepA", "400.5")])
        feat = os.path.join(tmp, "out.featureXML")
        make_featurexml(feat, [["t1"], ["t_unexpected"]])
        try:
            om.count_identifications(om.parse_traml(traml), feat)
        except om.OpenSwathMetricsError as exc:
            assert "t_unexpected" in str(exc), exc
        else:
            raise AssertionError("an unknown transition native_id was accepted")
    print("PASS: unmatched transition fails loudly (no fabricated identification)")


def test_empty_and_missing_inputs():
    """No features -> zero identifications, coverage 0.0; an unreadable or
    unparseable file raises OpenSwathMetricsError."""
    with tempfile.TemporaryDirectory() as tmp:
        traml = os.path.join(tmp, "lib.TraML")
        make_traml(traml,
                   proteins=[("ProtA", "uniprot_a")],
                   peptides=[("PepA", "AAAA", "2", "ProtA"),
                             ("PepB", "BBBB", "2", "ProtA")],
                   transitions=[("t1", "PepA", "400.5")])
        lib = om.parse_traml(traml)

        empty = os.path.join(tmp, "empty.featureXML")
        make_featurexml(empty, [])
        counts = om.count_identifications(lib, empty)
        assert counts["identified_peptides"] == 0, counts
        assert counts["library_coverage"] == 0.0, counts
        assert counts["library_peptides"] == 2, counts

        for bad in (os.path.join(tmp, "nope.featureXML"),
                    os.path.join(tmp, "broken.TraML")):
            with open(bad, "w", encoding="utf-8") as fh:
                fh.write("<not valid xml <<<")
            try:
                if bad.endswith(".TraML"):
                    om.parse_traml(bad)
                else:
                    om.count_identifications(lib, bad)
            except om.OpenSwathMetricsError:
                pass
            else:
                raise AssertionError("%s did not raise OpenSwathMetricsError" % bad)
    print("PASS: empty featureXML and unreadable/unparseable inputs")


def test_reference_fixture_join():
    """Regression guard on the real committed OpenSwath fixtures: the vendored
    library has 7 peptides but only 6 precursor m/z values, because one library
    peptide carries no transitions and therefore can never be identified."""
    traml = os.path.join(FIXTURES, "OpenSwathWorkflow_1_input.TraML")
    feat = os.path.join(FIXTURES, "OpenSwathWorkflow_1_output.featureXML")
    lib = om.parse_traml(traml)
    counts = om.count_identifications(lib, feat)

    assert lib["library_peptides"] == 7, counts
    assert lib["library_precursor_mz"] == 6, counts
    assert lib["library_transitions"] == 18, counts
    assert counts["identified_peptides"] == 6, counts
    assert counts["identified_precursor_mz"] == 6, counts
    assert counts["identified_proteins"] == 1, counts
    assert counts["transitions_used"] == 18, counts
    assert counts["library_coverage"] == 0.857143, counts
    # No PSM/FDR key may ever appear: DIA emits no spectrum-level
    # identifications, so such a count would be fabricated.
    assert not [k for k in counts if "psm" in k.lower()], counts
    assert not [k for k in counts if "fdr" in k.lower()], counts
    print("PASS: reference fixture join (7 peptides / 6 precursor m/z / 6 identified)")


def test_isobaric_peptides_share_one_precursor_mz():
    """F1 regression guard: precursor identity is the (m/z, charge) isolation
    window, so two isobaric peptides (e.g. Leu/Ile) at the same precursor m/z and
    charge count as ONE precursor but TWO peptides.

    This is intentionally NOT upstream OpenMS transition-group semantics, where
    MRMFeatureFinderScoring keys groups by Transition@peptideRef alone and the
    same pair would be two groups. The `_precursor_mz` metric name exists to
    keep that distinction visible in the report; if this test ever fails, the
    precursor definition changed and the rename plus the module docstring must
    change with it.
    """
    with tempfile.TemporaryDirectory() as tmp:
        traml = os.path.join(tmp, "lib.TraML")
        # PepLeu / PepIle are isobaric at 2+, so they share one precursor m/z.
        make_traml(
            traml,
            proteins=[("ProtA", "uniprot_a")],
            peptides=[("PepLeu", "PEPTIDELEU", "2", "ProtA"),
                      ("PepIle", "PEPTIDEILE", "2", "ProtA")],
            transitions=[("t1", "PepLeu", "500.5001"),
                         ("t2", "PepIle", "500.5001")],
        )
        lib = om.parse_traml(traml)

        assert lib["library_peptides"] == 2, lib
        assert lib["library_precursor_mz"] == 1, lib   # one isolation window
        assert lib["transitions"]["t1"]["precursor"] == ("500.5001", "2"), lib
        assert lib["transitions"]["t2"]["precursor"] == ("500.5001", "2"), lib

        feat = os.path.join(tmp, "out.featureXML")
        make_featurexml(feat, [["t1"], ["t2"]])
        counts = om.count_identifications(lib, feat)

        # both peptides identified, but through a single precursor window
        assert counts["identified_peptides"] == 2, counts
        assert counts["identified_precursor_mz"] == 1, counts
        assert counts["identified_proteins"] == 1, counts
        assert counts["transitions_used"] == 2, counts
        assert counts["library_coverage"] == 1.0, counts
    print("PASS: isobaric peptides share one precursor m/z (F1 semantics locked)")


def main():
    test_traml_parsing()
    test_feature_join_counts()
    test_unmatched_transition_fails_loudly()
    test_empty_and_missing_inputs()
    test_reference_fixture_join()
    test_isobaric_peptides_share_one_precursor_mz()
    print("\nAll openswath_metrics tests passed.")


if __name__ == "__main__":
    main()