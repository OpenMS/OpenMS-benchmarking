#!/usr/bin/env python3
"""Tests for openswath_metrics.py (OpenSwath DIA benchmark).

Uses small synthetic TraML / featureXML files written on the fly, plus one
regression test against the real committed OpenSwath fixtures - no OpenMS
build needed.

The synthetic files mirror the real artifacts: a namespaced TraML whose
transitions carry a precursor isolation-window m/z and a peptideRef, and a
plain (unnamespaced) featureXML whose picked features carry the transition ids
as <UserParam name="native_id"> on their subordinate features.

The two sides deliberately speak different id namespaces, exactly as OpenMS
does: the TraML keeps its own opaque source ids ("t1", "5", "TIC_x"), while
the featureXML carries the CANONICAL id - the transition's zero-based position
in TraML document order - because that is what
OpenSwathLibraryIDNormalizer::normalizeSourceIDs() writes before
OpenSwathWorkflow runs. The canonical() helper performs that translation the
way the normalizer does: by position, never by arithmetic on the source id.

What is deliberately NOT tested here: PSMs, FDR and decoy counts. DIA emits no
spectrum-level identifications, so there is nothing in these artifacts to count.

Run:  python3 benchmark/test_openswath_metrics.py
"""

import os
import sys
import tempfile
import xml.etree.ElementTree as ET

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
    '          <UserParam type="string" name="FeatureLevel" value="{level}"/>\n'
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


def make_featurexml(path, features, levels=None):
    """features: [canonical native_id, ...] - each entry becomes one picked
    feature. Values are CANONICAL ids, not TraML source ids; see canonical().

    levels is an optional [[FeatureLevel, ...], ...] parallel to features;
    every subordinate defaults to "MS2". Pass "MS1" for a precursor-extraction
    subordinate, which is what OpenSwath writes next to the MS2 ones.
    """
    blocks = []
    for i, natives in enumerate(features):
        lv = ["MS2"] * len(natives) if levels is None else levels[i]
        subs = "".join(
            SUBORDINATE_TEMPLATE.format(fid="f_%d" % i, sid=j,
                                         native_id=n, level=lv[j])
            for j, n in enumerate(natives))
        blocks.append(FEATURE_TEMPLATE.format(fid="f_%d" % i, subordinates=subs))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(FEATUREXML_TEMPLATE.format(count=len(features),
                                            features="".join(blocks)))


def canonical(transitions, *source_ids):
    """Canonical id for each source id, by TraML document position.

    This is the same rule OpenSwathLibraryIDNormalizer::normalizeSourceIDs()
    applies: the i-th <Transition> in document order gets canonical id str(i).
    Tests translate source ids through this instead of pasting them into a
    featureXML, because OpenSwathWorkflow only ever writes canonical ids.
    """
    index = {tid: str(i) for i, (tid, _pep, _mz) in enumerate(transitions)}
    return [index[sid] for sid in source_ids]


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

        # canonical id -> source id, by TraML document position: t1 is the
        # 0th <Transition> in the file, so a featureXML native_id of "0" means
        # source transition "t1".
        assert lib["transition_canonical_to_source"] == {
            "0": "t1", "1": "t2", "2": "t3", "3": "t4", "4": "t5"}, lib

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
        transitions = [("t1", "PepA", "400.5"),
                       ("t2", "PepA", "400.5"),
                       ("t3", "PepB", "500.5")]
        make_traml(
            traml,
            proteins=[("ProtA", "uniprot_a"), ("ProtB", "uniprot_b")],
            peptides=[("PepA", "AAAA", "2", "ProtA"),
                      ("PepB", "BBBB", "2", "ProtB"),
                      ("PepC", "CCCC", "2", "ProtA"),
                      ("PepOrphan", "DDDD", "2", "ProtB")],
            transitions=transitions,
        )
        feat = os.path.join(tmp, "out.featureXML")
        # two features, both reaching PepA, plus one reaching PepB. The
        # featureXML speaks canonical ids ("0","1","2") while the TraML
        # keeps source ids ("t1","t2","t3") - the OpenMS contract.
        make_featurexml(feat, [canonical(transitions, "t1", "t2"),
                               canonical(transitions, "t2"),
                               canonical(transitions, "t3")])

        counts = om.count_identifications(om.parse_traml(traml), feat)

        assert counts["identified_peptides"] == 2, counts        # PepA, PepB
        assert counts["identified_precursor_mz"] == 2, counts    # one m/z each
        assert counts["identified_proteins"] == 2, counts       # ProtA, ProtB
        assert counts["transitions_used"] == 3, counts
        assert counts["library_peptides"] == 4, counts          # incl. orphans
        # Only PepA and PepB carry transitions; PepC and PepOrphan are both
        # declared-but-transition-less, so 4 declared splits 2 + 2.
        assert counts["library_peptides_identifiable"] == 2, counts  # PepA, PepB
        assert counts["library_peptides_orphan"] == 2, counts   # PepC, PepOrphan
        assert counts["library_precursor_mz"] == 2, counts
        assert counts["library_transitions"] == 3, counts
        # both IDENTIFIABLE peptides were found, so coverage is 1.0 over the
        # identifiable base even though the library declares twice as many
        # peptides as can actually be searched.
        assert counts["library_coverage"] == 1.0, counts
    print("PASS: feature join (distinct peptides/precursors/proteins, coverage)")


def test_unmatched_transition_fails_loudly():
    """A feature referencing a canonical transition the library does not define
    must raise, never be counted as an identification.

    Also locks the absence of a source-id fallback: a native_id that happens to
    be a TraML *source* id must be rejected too, because source and canonical
    ids are different namespaces that can collide.
    """
    with tempfile.TemporaryDirectory() as tmp:
        traml = os.path.join(tmp, "lib.TraML")
        make_traml(traml,
                   proteins=[("ProtA", "uniprot_a")],
                   peptides=[("PepA", "AAAA", "2", "ProtA")],
                   transitions=[("t1", "PepA", "400.5")])
        lib = om.parse_traml(traml)
        # one transition -> the only canonical id is "0"
        assert lib["transition_canonical_to_source"] == {"0": "t1"}, lib

        # an unknown canonical id is rejected, and the id is named in the error
        feat = os.path.join(tmp, "out.featureXML")
        make_featurexml(feat, [["0"], ["99"]])
        try:
            om.count_identifications(lib, feat)
        except om.OpenSwathMetricsError as exc:
            assert "99" in str(exc), exc
        else:
            raise AssertionError("an unknown canonical transition native_id was accepted")

        # "t1" is a source id, not a canonical id: it must NOT be accepted via
        # a fallback lookup, or a stale featureXML would silently mis-join.
        stale = os.path.join(tmp, "stale.featureXML")
        make_featurexml(stale, [["t1"]])
        try:
            om.count_identifications(lib, stale)
        except om.OpenSwathMetricsError as exc:
            assert "t1" in str(exc), exc
        else:
            raise AssertionError("a TraML source id was accepted as a canonical id")
    print("PASS: unmatched canonical transition fails loudly (no fabricated identification)")


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
        # PepB is declared but transition-less, so only 1 peptide is
        # identifiable even though the library declares 2.
        assert counts["library_peptides_identifiable"] == 1, counts
        assert counts["library_peptides_orphan"] == 1, counts

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
    library declares 7 peptides but only 6 carry transitions, because
    PEPTIDEA_Extra has no transitions and therefore can never be identified.
    The run is perfect on every other axis (18/18 transitions, 6/6 precursor
    m/z), so coverage must be 6/6 = 1.0 over the identifiable subset, while
    the orphan stays visible as its own count instead of penalising it."""
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
    # declared / identifiable / orphan must all stay observable
    assert counts["library_peptides"] == 7, counts
    assert counts["library_peptides_identifiable"] == 6, counts
    assert counts["library_peptides_orphan"] == 1, counts
    assert counts["library_peptides_orphan"] == (
        counts["library_peptides"] - counts["library_peptides_identifiable"]), counts
    assert counts["library_coverage"] == 1.0, counts
    # No PSM/FDR key may ever appear: DIA emits no spectrum-level
    # identifications, so such a count would be fabricated.
    assert not [k for k in counts if "psm" in k.lower()], counts
    assert not [k for k in counts if "fdr" in k.lower()], counts
    print("PASS: reference fixture join (7 declared / 6 identifiable / 6 identified "
          "-> coverage 1.0)")


def test_orphan_peptide_does_not_penalise_coverage():
    """F5 regression guard: a declared-but-transition-less peptide must not
    lower library_coverage.

    The fixture library carries one such orphan (PEPTIDEA_Extra). Two runs
    that find every IDENTIFIABLE peptide must both report coverage 1.0,
    whether or not the library also declares orphans. Before this guard the
    metric divided by all declared peptides, so the same perfect run reported
    6/7 = 0.857143 and looked like a 14% identification failure caused by the
    tool. If this test ever fails, either the denominator changed again or the
    orphan counts changed -- both require the module docstring to change too.
    """
    traml = os.path.join(FIXTURES, "OpenSwathWorkflow_1_input.TraML")
    feat = os.path.join(FIXTURES, "OpenSwathWorkflow_1_output.featureXML")
    lib = om.parse_traml(traml)
    counts = om.count_identifications(lib, feat)

    # every identifiable peptide was found
    assert counts["identified_peptides"] == counts["library_peptides_identifiable"], \
        counts
    # so coverage is exactly 1.0 despite the orphan being declared
    assert counts["library_peptides_orphan"] > 0, counts   # the orphan is present
    assert counts["library_coverage"] == 1.0, counts

    # Same result with an orphan-free library: 3 declared, 3 identifiable,
    # all 3 identified -> coverage 1.0. Identifiable coverage is therefore
    # independent of how many orphans the library happens to declare.
    with tempfile.TemporaryDirectory() as tmp:
        lib2 = os.path.join(tmp, "noorphan.TraML")
        transitions2 = [("t1", "PepA", "400.5"),
                        ("t2", "PepB", "500.5"),
                        ("t3", "PepC", "600.5")]
        make_traml(
            lib2,
            proteins=[("ProtA", "uniprot_a")],
            peptides=[("PepA", "AAAA", "2", "ProtA"),
                      ("PepB", "BBBB", "2", "ProtA"),
                      ("PepC", "CCCC", "2", "ProtA")],
            transitions=transitions2,
        )
        feat2 = os.path.join(tmp, "all.featureXML")
        make_featurexml(feat2, [canonical(transitions2, "t1"),
                                canonical(transitions2, "t2"),
                                canonical(transitions2, "t3")])
        c2 = om.count_identifications(om.parse_traml(lib2), feat2)
        assert c2["library_peptides"] == 3, c2
        assert c2["library_peptides_identifiable"] == 3, c2
        assert c2["library_peptides_orphan"] == 0, c2
        assert c2["library_coverage"] == 1.0, c2

    # A genuine miss must still be penalised: drop one peptide's feature and
    # coverage falls below 1.0. This is what stops the orphan exclusion from
    # degenerating into "coverage is always 1.0".
    with tempfile.TemporaryDirectory() as tmp:
        lib3 = os.path.join(tmp, "miss.TraML")
        transitions3 = [("t1", "PepA", "400.5"),
                        ("t2", "PepB", "500.5"),
                        ("t3", "PepC", "600.5")]
        make_traml(
            lib3,
            proteins=[("ProtA", "uniprot_a")],
            peptides=[("PepA", "AAAA", "2", "ProtA"),
                      ("PepB", "BBBB", "2", "ProtA"),
                      ("PepC", "CCCC", "2", "ProtA")],
            transitions=transitions3,
        )
        feat3 = os.path.join(tmp, "partial.featureXML")
        # PepC not identified
        make_featurexml(feat3, [canonical(transitions3, "t1"),
                                canonical(transitions3, "t2")])
        c3 = om.count_identifications(om.parse_traml(lib3), feat3)
        assert c3["identified_peptides"] == 2, c3
        assert c3["library_peptides_identifiable"] == 3, c3
        assert c3["library_coverage"] == 0.666667, c3

    print("PASS: orphan peptide does not penalise coverage; real misses still do")


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
        transitions = [("t1", "PepLeu", "500.5001"),
                       ("t2", "PepIle", "500.5001")]
        make_traml(
            traml,
            proteins=[("ProtA", "uniprot_a")],
            peptides=[("PepLeu", "PEPTIDELEU", "2", "ProtA"),
                      ("PepIle", "PEPTIDEILE", "2", "ProtA")],
            transitions=transitions,
        )
        lib = om.parse_traml(traml)

        assert lib["library_peptides"] == 2, lib
        assert lib["library_precursor_mz"] == 1, lib   # one isolation window
        assert lib["transitions"]["t1"]["precursor"] == ("500.5001", "2"), lib
        assert lib["transitions"]["t2"]["precursor"] == ("500.5001", "2"), lib

        feat = os.path.join(tmp, "out.featureXML")
        make_featurexml(feat, [canonical(transitions, "t1"),
                               canonical(transitions, "t2")])
        counts = om.count_identifications(lib, feat)

        # both peptides identified, but through a single precursor window
        assert counts["identified_peptides"] == 2, counts
        assert counts["identified_precursor_mz"] == 1, counts
        assert counts["identified_proteins"] == 1, counts
        assert counts["transitions_used"] == 2, counts
        assert counts["library_coverage"] == 1.0, counts
    print("PASS: isobaric peptides share one precursor m/z (F1 semantics locked)")


def test_canonical_mapping_follows_traml_document_order():
    """The canonical id is the transition's zero-based position in the TraML
    document - nothing else. Source ids are opaque strings, so the mapping must
    hold for non-numeric and non-contiguous ids and must never be derived from
    the source id (here source "5" is canonical "0")."""
    with tempfile.TemporaryDirectory() as tmp:
        traml = os.path.join(tmp, "lib.TraML")
        transitions = [("5", "PepA", "400.5"),
                       ("TIC_x", "PepA", "400.5"),
                       ("9", "PepB", "500.5"),
                       ("TIC_y", "PepB", "500.5"),
                       ("0", "PepC", "600.5")]   # source id that LOOKS canonical
        make_traml(
            traml,
            proteins=[("ProtA", "uniprot_a")],
            peptides=[("PepA", "AAAA", "2", "ProtA"),
                      ("PepB", "BBBB", "2", "ProtA"),
                      ("PepC", "CCCC", "2", "ProtA")],
            transitions=transitions,
        )
        lib = om.parse_traml(traml)
        assert lib["transition_canonical_to_source"] == {
            "0": "5", "1": "TIC_x", "2": "9", "3": "TIC_y", "4": "0"}, lib

        feat = os.path.join(tmp, "out.featureXML")
        make_featurexml(feat, [["0", "1", "2", "3", "4"]])
        counts = om.count_identifications(lib, feat)
        assert counts["transitions_used"] == 5, counts
        assert counts["identified_peptides"] == 3, counts
        assert counts["identified_precursor_mz"] == 3, counts
        assert counts["library_coverage"] == 1.0, counts
    print("PASS: canonical mapping follows TraML document order (non-numeric ids)")


def test_source_ids_1_to_18_join_against_canonical_ids():
    """Regression for the normalized-ID contract: a library whose TraML source
    ids are "1".."18" (as the vendored OpenSwath fixture is) must join against
    a featureXML written with canonical ids "0".."17", exactly as
    OpenSwathWorkflow emits them. Source ids are never arithmetic-converted,
    and a source-id featureXML must still be rejected rather than mis-joined."""
    with tempfile.TemporaryDirectory() as tmp:
        traml = os.path.join(tmp, "lib.TraML")
        proteins = [("ProtA", "uniprot_a")]
        peptides = [("Pep%d" % i, "SEQ%d" % i, "2", "ProtA") for i in range(6)]
        transitions = []
        for pep in range(6):
            for k in range(3):
                transitions.append((str(1 + pep * 3 + k),
                                    "Pep%d" % pep,
                                    "400.%d" % (pep + 1)))
        make_traml(traml, proteins, peptides, transitions)

        lib = om.parse_traml(traml)
        assert lib["library_transitions"] == 18, lib
        assert lib["transition_canonical_to_source"] == {
            str(i): str(i + 1) for i in range(18)}, lib

        feat = os.path.join(tmp, "out.featureXML")
        make_featurexml(feat, [[str(i) for i in range(18)]])
        counts = om.count_identifications(lib, feat)
        assert counts["library_transitions"] == 18, counts
        assert counts["transitions_used"] == 18, counts
        assert counts["identified_peptides"] == 6, counts
        assert counts["identified_precursor_mz"] == 6, counts
        assert counts["library_coverage"] == 1.0, counts

        # The stale contract must still fail loudly rather than mis-join:
        # source id "18" is not a canonical id (canonical ids stop at "17").
        stale = os.path.join(tmp, "stale.featureXML")
        make_featurexml(stale, [[str(i) for i in range(1, 19)]])
        try:
            om.count_identifications(lib, stale)
        except om.OpenSwathMetricsError as exc:
            assert "18" in str(exc), exc
        else:
            raise AssertionError("a source-id featureXML was accepted as canonical")
    print("PASS: TraML source ids 1..18 join against canonical ids 0..17")


def test_reference_featurexml_uses_canonical_ids():
    """The vendored reference featureXML must speak the canonical contract:
    its 18 MS2 native_ids are exactly "0".."17" while the TraML beside it keeps
    source ids "1".."18". If this fails, the fixture is stale again (OpenMS
    renumbered or reordered the library) and both the fixture and
    PROVENANCE.md must be refreshed from the pinned upstream commit."""
    traml = os.path.join(FIXTURES, "OpenSwathWorkflow_1_input.TraML")
    feat = os.path.join(FIXTURES, "OpenSwathWorkflow_1_output.featureXML")
    lib = om.parse_traml(traml)
    assert lib["transition_canonical_to_source"] == {
        str(i): str(i + 1) for i in range(18)}, lib

    root = ET.parse(feat).getroot()
    native_ids = sorted({u.get("value") for u in root.iter("UserParam")
                         if u.get("name") == "native_id"})
    # both sides sorted lexicographically, so no assumption about id shape
    assert native_ids == sorted(str(i) for i in range(18)), native_ids
    assert len(native_ids) == 18, native_ids
    print("PASS: reference featureXML carries canonical ids 0..17")


def test_ms1_subordinates_are_excluded_by_feature_level():
    """OpenSwath writes MS1 precursor-extraction subordinates next to the MS2
    ones; their native_id is a PRECURSOR id ("<canonical>_Precursor_i<k>")
    that no TraML <Transition> defines. They must not enter the transition
    join, and the exclusion must key off FeatureLevel rather than the shape of
    the id: an MS1 subordinate carrying a perfectly valid canonical id must
    still be ignored, or it would silently inflate the metrics."""
    with tempfile.TemporaryDirectory() as tmp:
        traml = os.path.join(tmp, "lib.TraML")
        transitions = [("t1", "PepA", "400.5"),
                       ("t2", "PepB", "500.5"),
                       ("t3", "PepC", "600.5")]
        make_traml(
            traml,
            proteins=[("ProtA", "uniprot_a")],
            peptides=[("PepA", "AAAA", "2", "ProtA"),
                      ("PepB", "BBBB", "2", "ProtA"),
                      ("PepC", "CCCC", "2", "ProtA")],
            transitions=transitions,
        )
        lib = om.parse_traml(traml)

        # MS2 subordinates only reach canonical "0" and "1". Canonical "2"
        # (source t3, PepC) is carried by an MS1 subordinate instead, so it
        # must stay unidentified - and the two "*_Precursor_i*" ids must not
        # make the join raise.
        ms2 = canonical(transitions, "t1", "t2")
        mixed = os.path.join(tmp, "mixed.featureXML")
        make_featurexml(mixed,
                        [ms2 + ["9_Precursor_i0", "9_Precursor_i1", "2"]],
                        levels=[["MS2", "MS2", "MS1", "MS1", "MS1"]])
        with_ms1 = om.count_identifications(lib, mixed)

        plain = os.path.join(tmp, "plain.featureXML")
        make_featurexml(plain, [ms2])
        without_ms1 = om.count_identifications(lib, plain)

        assert with_ms1 == without_ms1, (with_ms1, without_ms1)
        # if an MS1 row had leaked in, canonical "2" would identify PepC too
        assert with_ms1["transitions_used"] == 2, with_ms1
        assert with_ms1["identified_peptides"] == 2, with_ms1
        assert with_ms1["library_coverage"] == 0.666667, with_ms1
    print("PASS: MS1 precursor subordinates are excluded from the transition join")


def test_unknown_ms2_transition_still_fails_loudly():
    """FeatureLevel decides WHICH subordinates join; it must not weaken
    validation. An MS2 subordinate whose canonical id the library does not
    define still raises, even when unresolvable-but-valid MS1 subordinates sit
    next to it."""
    with tempfile.TemporaryDirectory() as tmp:
        traml = os.path.join(tmp, "lib.TraML")
        make_traml(traml,
                   proteins=[("ProtA", "uniprot_a")],
                   peptides=[("PepA", "AAAA", "2", "ProtA")],
                   transitions=[("t1", "PepA", "400.5")])
        feat = os.path.join(tmp, "out.featureXML")
        make_featurexml(feat, [["0", "99", "7_Precursor_i0"]],
                        levels=[["MS2", "MS2", "MS1"]])
        try:
            om.count_identifications(om.parse_traml(traml), feat)
        except om.OpenSwathMetricsError as exc:
            assert "99" in str(exc), exc
        else:
            raise AssertionError("an unknown canonical MS2 native_id was accepted")
    print("PASS: unknown canonical MS2 id still fails loudly with MS1 present")


def main():
    test_traml_parsing()
    test_feature_join_counts()
    test_unmatched_transition_fails_loudly()
    test_empty_and_missing_inputs()
    test_reference_fixture_join()
    test_reference_featurexml_uses_canonical_ids()
    test_canonical_mapping_follows_traml_document_order()
    test_source_ids_1_to_18_join_against_canonical_ids()
    test_ms1_subordinates_are_excluded_by_feature_level()
    test_unknown_ms2_transition_still_fails_loudly()
    test_orphan_peptide_does_not_penalise_coverage()
    test_isobaric_peptides_share_one_precursor_mz()
    print("\nAll openswath_metrics tests passed.")


if __name__ == "__main__":
    main()