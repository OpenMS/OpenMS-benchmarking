#!/usr/bin/env python3
"""Tests for openswath_metrics.py (OpenSwath DIA benchmark).

Uses small synthetic TraML / featureXML files written on the fly, plus
regression tests against the real committed OpenSwath fixtures and two real
upstream libraries that contain duplicate <Transition id> labels - no OpenMS
build needed.

The synthetic files mirror the real artifacts: a namespaced TraML whose
transitions carry a precursor isolation-window m/z and a peptideRef, and a
plain (unnamespaced) featureXML whose picked features carry the transition ids
as <UserParam name="native_id"> on their subordinate features, optionally with
the canonical precursor anchor (PeptideRef / MS1 precursor prefix), a
PrecursorMZ and PeptideIdentification hits.

The two sides deliberately speak different id namespaces, exactly as OpenMS
does: the TraML keeps its own opaque source ids ("t1", "5", "TIC_x") as
METADATA, while the featureXML carries the CANONICAL id - the transition's
zero-based position in TraML document order - because that is what
OpenSwathLibraryIDNormalizer::normalizeSourceIDs() writes before
OpenSwathWorkflow runs. The canonical() helper performs that translation the
way the normalizer does: by position, never by arithmetic on the source id.
The parser keys `library["transitions"]` by canonical id for the same reason,
so duplicate source labels stay distinct records instead of collapsing.

Anchor policy: features are anchored by PeptideRef (or, when that is absent,
by the MS1 precursor prefix of a native_id). Features with no anchor raise by
default; the tests that exercise the purely positional join opt out explicitly
with require_anchor=False and assert the positional/mixed evidence stamp.

test_runner_verdict_rejects_unvalidated_assignment() executes the Python block
embedded in run_openswath_benchmark.sh against stubbed run directories, so the
runner's verdict/JSON/read-back path is exercised without an OpenMS binary.

What is deliberately NOT tested here: PSMs, FDR and decoy counts. DIA emits no
spectrum-level identifications, so there is nothing in these artifacts to count.

Run:  python3 benchmark/test_openswath_metrics.py
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import openswath_metrics as om  # noqa: E402

FIXTURES = os.path.join(HERE, "fixtures")
RUNNER = os.path.join(HERE, "run_openswath_benchmark.sh")


TRAML_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<TraML version="1.0.0" xmlns="http://psi.hupo.org/ms/traml">
  <ProteinList>
{proteins}  </ProteinList>
  <CompoundList>
{peptides}{compounds}  </CompoundList>
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
    '{modification}'
    '      <cvParam cvRef="MS" accession="MS:1000041" name="charge state" '
    'value="{charge}"/>\n'
    '      <ProteinRef ref="{protein}"/>\n'
    '    </Peptide>\n'
)

MODIFICATION_TEMPLATE = (
    '      <Modification location="1" monoisotopicMassDelta="79.966331"/>\n'
)

COMPOUND_TEMPLATE = (
    '    <Compound id="{cid}">\n'
    '      <cvParam cvRef="MS" accession="MS:1000041" name="charge state" '
    'value="{charge}"/>\n'
    '    </Compound>\n'
)

TRANSITION_TEMPLATE = (
    '    <Transition id="{tid}" {ref_attr}="{ref}">\n'
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
# how OpenSwath writes them. PeptideRef/PrecursorMZ are feature-level
# userParams; PeptideIdentification blocks are optional.
FEATURE_TEMPLATE = """    <feature id="{fid}">
      <position dim="0">1.0</position>
      <position dim="1">2.0</position>
      <intensity>1.0</intensity>
      <overallquality>0.5</overallquality>
      <subordinate>
{subordinates}      </subordinate>
{meta}{identifications}    </feature>
"""

SUBORDINATE_TEMPLATE = (
    '        <feature id="{fid}_s{sid}">\n'
    '          <intensity>1.0</intensity>\n'
    '          <UserParam type="string" name="native_id" value="{native_id}"/>\n'
    '          <UserParam type="string" name="FeatureLevel" value="{level}"/>\n'
    '        </feature>\n'
)

IDENTIFICATION_TEMPLATE = (
    '      <PeptideIdentification score_type="test" higher_score_better="true" '
    'significance_threshold="0.0">\n'
    '        <PeptideHit score="1.0" sequence="{sequence}" charge="2"/>\n'
    '      </PeptideIdentification>\n'
)


def make_traml(path, proteins, peptides, transitions, compounds=(), modified=()):
    """proteins: [(id, accession)]; peptides: [(id, seq, charge, protein)];
    transitions: [(source_id, ref_id, precursor_mz[, "compound"])];
    compounds: [(id, charge)]; modified: peptide ids to carry a Modification."""
    transition_xml = []
    for entry in transitions:
        tid, ref, mz = entry[:3]
        ref_attr = "peptideRef" if len(entry) == 3 else "compoundRef"
        transition_xml.append(TRANSITION_TEMPLATE.format(
            tid=tid, ref_attr=ref_attr, ref=ref, mz=mz))
    body = TRAML_TEMPLATE.format(
        proteins="".join(PROTEIN_TEMPLATE.format(pid=p, acc=a) for p, a in proteins),
        peptides="".join(
            PEPTIDE_TEMPLATE.format(
                pid=p, seq=s, charge=c, protein=pr,
                modification=(MODIFICATION_TEMPLATE if p in modified else ""))
            for p, s, c, pr in peptides),
        compounds="".join(COMPOUND_TEMPLATE.format(cid=cid, charge=charge)
                          for cid, charge in compounds),
        transitions="".join(transition_xml),
    )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)


def make_featurexml(path, features, levels=None, peptide_refs=None,
                    precursor_mz=None, hit_sequences=None):
    """features: [[canonical native_id, ...], ...] - each entry becomes one
    picked feature. Values are CANONICAL ids, not TraML source ids; see
    canonical().

    levels is an optional [[FeatureLevel, ...], ...] parallel to features;
    every subordinate defaults to "MS2". Pass "MS1" for a precursor-extraction
    subordinate, which is what OpenSwath writes next to the MS2 ones.
    peptide_refs / precursor_mz are optional per-feature feature-level values
    (None omits them); hit_sequences is an optional [[sequence, ...], ...] that
    writes one PeptideIdentification with one PeptideHit per sequence.
    """
    blocks = []
    for i, natives in enumerate(features):
        lv = ["MS2"] * len(natives) if levels is None else levels[i]
        subs = "".join(
            SUBORDINATE_TEMPLATE.format(fid="f_%d" % i, sid=j,
                                         native_id=n, level=lv[j])
            for j, n in enumerate(natives))
        meta = ""
        if peptide_refs is not None and peptide_refs[i] is not None:
            meta += ('      <UserParam type="string" name="PeptideRef" '
                     'value="%s"/>\n' % peptide_refs[i])
        if precursor_mz is not None and precursor_mz[i] is not None:
            meta += ('      <UserParam type="float" name="PrecursorMZ" '
                     'value="%s"/>\n' % precursor_mz[i])
        identifications = ""
        if hit_sequences is not None:
            identifications = "".join(
                IDENTIFICATION_TEMPLATE.format(sequence=s)
                for s in hit_sequences[i])
        blocks.append(FEATURE_TEMPLATE.format(
            fid="f_%d" % i, subordinates=subs, meta=meta,
            identifications=identifications))
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
    index = {entry[0]: str(i) for i, entry in enumerate(transitions)}
    return [index[sid] for sid in source_ids]


def test_traml_parsing():
    """Transitions resolve to peptides and proteins; precursor identity is the
    (isolation m/z, charge) tuple, not the peptide; canonical precursor ids are
    the sorted union of declared peptide ids."""
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

        # transition records are keyed by CANONICAL id; the source label is
        # retained only as metadata for diagnostics.
        assert lib["transitions"]["0"]["peptide"] == "PepA", lib["transitions"]
        assert lib["transitions"]["0"]["source_id"] == "t1", lib["transitions"]
        assert lib["transitions"]["0"]["compound"] is None, lib["transitions"]
        assert lib["peptides"]["PepA"]["proteins"] == {"ProtA"}, lib["peptides"]
        assert lib["peptides"]["PepB"]["proteins"] == {"ProtB"}, lib["peptides"]
        assert lib["peptides"]["PepC"]["charge"] == "3", lib["peptides"]
        assert lib["peptides"]["PepA"]["sequence"] == "AAAA", lib["peptides"]
        assert lib["peptides"]["PepA"]["modified"] is False, lib["peptides"]

        # canonical id -> source id, by TraML document position: t1 is the
        # 0th <Transition> in the file, so a featureXML native_id of "0" means
        # source transition "t1".
        assert lib["transition_canonical_to_source"] == {
            "0": "t1", "1": "t2", "2": "t3", "3": "t4", "4": "t5"}, lib

        # canonical PRECURSOR ids: sorted peptide ids, orphans included even
        # though they have no transition (PepOrphan still reserves id 3).
        assert lib["precursor_ids"] == {
            "PepA": "0", "PepB": "1", "PepC": "2", "PepOrphan": "3"}, lib
        assert lib["precursor_canonical_to_id"]["3"] == "PepOrphan", lib

        # precursor identity: PepA has TWO precursor m/z values -> two
        # precursors, so peptide count and precursor count legitimately differ.
        assert lib["library_peptides"] == 4, lib          # incl. PepOrphan
        assert lib["library_transitions"] == 5, lib
        assert lib["library_transitions_total"] == 5, lib
        assert lib["library_compound_transitions"] == 0, lib
        assert lib["library_proteins"] == 2, lib
        # PepA(400.5), PepA(999.9), PepB(500.5), PepC(600.5) = 4; PepOrphan
        # has no transitions so it contributes no precursor.
        assert lib["library_precursor_mz"] == 4, lib

        assert lib["transitions"]["0"]["precursor"] == ("400.5", "2"), lib
        assert lib["transitions"]["2"]["precursor"] == ("999.9", "2"), lib
    print("PASS: TraML parsing (canonical keying, peptide->protein, precursor identity)")


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
        # keeps source ids ("t1","t2","t3") - the OpenMS contract. No
        # PeptideRef here, so the join is explicitly positional.
        make_featurexml(feat, [canonical(transitions, "t1", "t2"),
                               canonical(transitions, "t2"),
                               canonical(transitions, "t3")])

        counts = om.count_identifications(om.parse_traml(traml), feat,
                                          require_anchor=False)

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
        # no anchors anywhere: the explicit opt-out must stamp the result so
        # nobody can mistake it for a validated join.
        assert counts["assignment_evidence"] == "positional", counts
        assert counts["features_without_anchor"] == 3, counts
        assert counts["ambiguous_hit_features"] == 0, counts
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
            om.count_identifications(lib, feat, require_anchor=False)
        except om.OpenSwathMetricsError as exc:
            assert "99" in str(exc), exc
        else:
            raise AssertionError("an unknown canonical transition native_id was accepted")

        # "t1" is a source id, not a canonical id: it must NOT be accepted via
        # a fallback lookup, or a stale featureXML would silently mis-join.
        stale = os.path.join(tmp, "stale.featureXML")
        make_featurexml(stale, [["t1"]])
        try:
            om.count_identifications(lib, stale, require_anchor=False)
        except om.OpenSwathMetricsError as exc:
            assert "t1" in str(exc), exc
        else:
            raise AssertionError("a TraML source id was accepted as a canonical id")
    print("PASS: unmatched canonical transition fails loudly (no fabricated identification)")


def test_empty_and_missing_inputs():
    """No features -> zero identifications, coverage 0.0, evidence "none"; an
    unreadable or unparseable file raises OpenSwathMetricsError."""
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
        # nothing to anchor is not a missing anchor: an empty file keeps the
        # strict default and reports "none".
        assert counts["assignment_evidence"] == "none", counts
        assert counts["features_without_anchor"] == 0, counts

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
    the orphan stays visible as its own count instead of penalising it.

    The fixture also carries the canonical precursor anchor (PeptideRef) and
    PrecursorMZ on every feature, so the strict default must accept it.
    """
    traml = os.path.join(FIXTURES, "OpenSwathWorkflow_1_input.TraML")
    feat = os.path.join(FIXTURES, "OpenSwathWorkflow_1_output.featureXML")
    lib = om.parse_traml(traml)
    counts = om.count_identifications(lib, feat)

    assert lib["library_peptides"] == 7, counts
    assert lib["library_precursor_mz"] == 6, counts
    assert lib["library_transitions"] == 18, counts
    assert lib["library_transitions_total"] == 18, counts
    assert lib["library_compound_transitions"] == 0, counts
    # canonical precursor ids must include the transition-less orphan, which
    # reserves id 1 and pushes PEPTIDEB to 2 (the historical off-by-one).
    assert lib["precursor_ids"] == {
        "PEPTIDEA": "0", "PEPTIDEA_Extra": "1", "PEPTIDEB": "2",
        "PEPTIDEC": "3", "PEPTIDED": "4", "PEPTIDEE": "5",
        "PEPTIDEF": "6"}, lib["precursor_ids"]
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
    # every feature is anchored and the fixture carries no PeptideHit, so the
    # sequence check is skipped without weakening the anchor validation.
    assert counts["assignment_evidence"] == "anchor", counts
    assert counts["features_without_anchor"] == 0, counts
    assert counts["ambiguous_hit_features"] == 0, counts
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
        c2 = om.count_identifications(om.parse_traml(lib2), feat2,
                                      require_anchor=False)
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
        c3 = om.count_identifications(om.parse_traml(lib3), feat3,
                                      require_anchor=False)
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
        assert lib["transitions"]["0"]["precursor"] == ("500.5001", "2"), lib
        assert lib["transitions"]["1"]["precursor"] == ("500.5001", "2"), lib

        feat = os.path.join(tmp, "out.featureXML")
        make_featurexml(feat, [canonical(transitions, "t1"),
                               canonical(transitions, "t2")])
        counts = om.count_identifications(lib, feat, require_anchor=False)

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
        counts = om.count_identifications(lib, feat, require_anchor=False)
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
        counts = om.count_identifications(lib, feat, require_anchor=False)
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
            om.count_identifications(lib, stale, require_anchor=False)
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

        # Feature 0 is anchored to PepA (canonical precursor 0) and carries
        # two MS1 precursor subordinates whose ids are NOT transitions, plus
        # one MS1 subordinate carrying a perfectly valid canonical id ("2",
        # PepC's transition): all three must be ignored. Feature 1 is anchored
        # to PepB, so canonical "2" (source t3, PepC) stays unidentified.
        mixed = os.path.join(tmp, "mixed.featureXML")
        make_featurexml(mixed,
                        [["0", "0_Precursor_i0", "0_Precursor_i1", "2"],
                         ["1"]],
                        levels=[["MS2", "MS1", "MS1", "MS1"], ["MS2"]],
                        peptide_refs=["0", "1"])
        with_ms1 = om.count_identifications(lib, mixed)

        plain = os.path.join(tmp, "plain.featureXML")
        make_featurexml(plain, [["0"], ["1"]], peptide_refs=["0", "1"])
        without_ms1 = om.count_identifications(lib, plain)

        assert with_ms1 == without_ms1, (with_ms1, without_ms1)
        # if an MS1 row had leaked in, canonical "2" would identify PepC too
        assert with_ms1["transitions_used"] == 2, with_ms1
        assert with_ms1["identified_peptides"] == 2, with_ms1
        assert with_ms1["library_coverage"] == 0.666667, with_ms1
        assert with_ms1["assignment_evidence"] == "anchor", with_ms1
    print("PASS: MS1 precursor subordinates are excluded from the transition join")


def test_unknown_ms2_transition_still_fails_loudly():
    """FeatureLevel decides WHICH subordinates join; it must not weaken
    validation. An MS2 subordinate whose canonical id the library does not
    define still raises, even when valid MS1 precursor subordinates sit next
    to it."""
    with tempfile.TemporaryDirectory() as tmp:
        traml = os.path.join(tmp, "lib.TraML")
        make_traml(traml,
                   proteins=[("ProtA", "uniprot_a")],
                   peptides=[("PepA", "AAAA", "2", "ProtA")],
                   transitions=[("t1", "PepA", "400.5")])
        feat = os.path.join(tmp, "out.featureXML")
        make_featurexml(feat, [["0", "99", "0_Precursor_i0"]],
                        levels=[["MS2", "MS2", "MS1"]])
        try:
            om.count_identifications(om.parse_traml(traml), feat,
                                     require_anchor=False)
        except om.OpenSwathMetricsError as exc:
            assert "99" in str(exc), exc
        else:
            raise AssertionError("an unknown canonical MS2 native_id was accepted")
    print("PASS: unknown canonical MS2 id still fails loudly with MS1 present")


def test_unusable_transition_keeps_its_position():
    """A transition this parser cannot use must still consume its canonical
    index, or every transition after it would be renumbered and each feature
    would silently shift onto its neighbour. The unusable record is kept (so it
    stays reportable) and referencing it fails with the source id named."""
    with tempfile.TemporaryDirectory() as tmp:
        traml = os.path.join(tmp, "lib.TraML")
        make_traml(
            traml,
            proteins=[("ProtA", "uniprot_a")],
            peptides=[("PepA", "AAAA", "2", "ProtA"),
                      ("PepB", "BBBB", "2", "ProtA")],
            # t2 references a peptide the library does not declare
            transitions=[("t1", "PepA", "400.5"),
                         ("t2", "GhostPep", "450.5"),
                         ("t3", "PepB", "500.5")],
        )
        lib = om.parse_traml(traml)
        assert lib["library_transitions"] == 2, lib
        assert lib["library_transitions_total"] == 3, lib
        assert lib["transition_canonical_to_source"] == {
            "0": "t1", "1": "t2", "2": "t3"}, lib
        # position 1 is preserved but unusable
        assert lib["transitions"]["1"]["peptide"] is None, lib["transitions"]
        # canonical 2 is still PepB: the unusable transition did not renumber it
        assert lib["transitions"]["2"]["peptide"] == "PepB", lib["transitions"]

        ok = os.path.join(tmp, "ok.featureXML")
        make_featurexml(ok, [["2"]], peptide_refs=["1"])   # PepB is precursor 1
        counts = om.count_identifications(lib, ok)
        assert counts["identified_peptides"] == 1, counts

        unusable = os.path.join(tmp, "unusable.featureXML")
        make_featurexml(unusable, [["1"]], peptide_refs=["0"])
        try:
            om.count_identifications(lib, unusable)
        except om.OpenSwathMetricsError as exc:
            assert "t2" in str(exc), exc
        else:
            raise AssertionError("an unusable transition was counted")
    print("PASS: an unusable transition keeps its canonical position")


# --- duplicate source transition ids (real upstream libraries) ---------------


def test_duplicate_source_ids_across_peptides():
    """Real upstream shape: OpenSwath_identification_input.TraML reuses the
    source label "tr2" for transitions of TWO different peptides.

    Keying by source id (the old model) collapsed them, so canonical position 1
    resolved to the last record and was attributed to tr_gr2 while really
    belonging to tr_gr1. The parser must keep one record per element, keyed by
    canonical position, with the shared label kept only as metadata.
    """
    traml = os.path.join(FIXTURES, "OpenSwath_identification_input.TraML")
    lib = om.parse_traml(traml)

    assert lib["library_peptides"] == 2, lib
    assert lib["library_transitions"] == 6, lib
    assert lib["library_transitions_total"] == 6, lib
    assert lib["transition_canonical_to_source"] == {
        "0": "tr1", "1": "tr2", "2": "tr3",
        "3": "tr4", "4": "tr5", "5": "tr2"}, lib

    # the two transitions sharing the label survive as distinct, correctly
    # attributed records
    assert lib["transitions"]["1"]["source_id"] == "tr2", lib["transitions"]
    assert lib["transitions"]["1"]["peptide"] == "tr_gr1", lib["transitions"]
    assert lib["transitions"]["5"]["source_id"] == "tr2", lib["transitions"]
    assert lib["transitions"]["5"]["peptide"] == "tr_gr2", lib["transitions"]
    duplicates = [canonical for canonical, transition
                  in lib["transitions"].items() if transition["source_id"] == "tr2"]
    assert sorted(duplicates) == ["1", "5"], duplicates

    # both duplicate positions are usable in a real join: tr_gr1 owns 0/1,
    # tr_gr2 owns 2/3/4/5 (canonical precursor ids follow the sorted id order)
    assert lib["precursor_ids"] == {"tr_gr1": "0", "tr_gr2": "1"}, lib
    with tempfile.TemporaryDirectory() as tmp:
        feat = os.path.join(tmp, "out.featureXML")
        make_featurexml(feat, [["0", "1"], ["2", "3", "4", "5"]],
                        peptide_refs=["0", "1"])
        counts = om.count_identifications(lib, feat)
        assert counts["identified_peptides"] == 2, counts
        assert counts["transitions_used"] == 6, counts
        assert counts["assignment_evidence"] == "anchor", counts
    print("PASS: duplicate source ids across peptides stay distinct (tr_gr1/tr_gr2)")


def test_duplicate_source_ids_within_peptide():
    """Real upstream shape: OpenSwathDecoyGenerator_input.TraML reuses the
    source label "454" for two transitions of the SAME peptide (78).

    Both must remain distinct records and both must be usable, so the library
    counts 24 transitions even though it has only 23 distinct labels.
    """
    traml = os.path.join(FIXTURES, "OpenSwathDecoyGenerator_input.TraML")
    lib = om.parse_traml(traml)

    assert lib["library_peptides"] == 12, lib
    assert lib["library_transitions"] == 24, lib
    assert lib["library_transitions_total"] == 24, lib
    assert lib["transitions"]["22"]["source_id"] == "454", lib["transitions"]
    assert lib["transitions"]["22"]["peptide"] == "78", lib["transitions"]
    assert lib["transitions"]["23"]["source_id"] == "454", lib["transitions"]
    assert lib["transitions"]["23"]["peptide"] == "78", lib["transitions"]
    assert lib["precursor_ids"]["78"] == "0", lib["precursor_ids"]
    assert len(set(lib["transition_canonical_to_source"].values())) == 23, lib

    with tempfile.TemporaryDirectory() as tmp:
        feat = os.path.join(tmp, "out.featureXML")
        make_featurexml(feat, [[str(i) for i in range(24)]])
        counts = om.count_identifications(lib, feat, require_anchor=False)
        assert counts["transitions_used"] == 24, counts
        assert counts["identified_peptides"] == 12, counts
        assert counts["library_coverage"] == 1.0, counts
    print("PASS: duplicate source ids within one peptide stay distinct (24 records)")


# --- canonical precursor ids, compounds and anchors --------------------------


def test_compound_backed_feature_is_reported_not_counted():
    """Compound ids share the canonical precursor namespace (upstream sorts the
    union of <Peptide> and <Compound>), but a compound is not a peptide: its
    transitions keep their canonical positions and are reported separately,
    and a compound-backed feature is rejected with a clear message instead of
    entering the peptide metrics."""
    with tempfile.TemporaryDirectory() as tmp:
        traml = os.path.join(tmp, "lib.TraML")
        make_traml(
            traml,
            proteins=[("ProtA", "uniprot_a")],
            peptides=[("PepA", "AAAA", "2", "ProtA"),
                      ("PepB", "BBBB", "2", "ProtA"),
                      ("PepOrphan", "CCCC", "2", "ProtA")],
            compounds=[("LIG", "1")],
            transitions=[("t1", "PepA", "400.5"),
                         ("t2", "LIG", "300.5", "compound"),
                         ("t3", "PepB", "500.5")],
        )
        lib = om.parse_traml(traml)

        # sorted union: LIG (compound) < PepA < PepB < PepOrphan
        assert lib["precursor_ids"] == {
            "LIG": "0", "PepA": "1", "PepB": "2", "PepOrphan": "3"}, lib
        assert lib["library_peptides"] == 3, lib       # compounds excluded
        assert lib["library_transitions"] == 2, lib    # peptide-usable only
        assert lib["library_transitions_total"] == 3, lib
        assert lib["library_compound_transitions"] == 1, lib
        # the compound transition keeps its position and its reference
        assert lib["transitions"]["1"]["compound"] == "LIG", lib["transitions"]
        assert lib["transitions"]["1"]["peptide"] is None, lib["transitions"]

        feat = os.path.join(tmp, "ok.featureXML")
        make_featurexml(feat, [["0"]], peptide_refs=["1"])   # PepA, t1
        counts = om.count_identifications(lib, feat)
        assert counts["identified_peptides"] == 1, counts
        assert counts["transitions_used"] == 1, counts
        assert counts["library_compound_transitions"] == 1, counts
        # the compound must not inflate the peptide population or denominator
        assert counts["library_peptides"] == 3, counts
        assert counts["library_peptides_identifiable"] == 2, counts
        assert counts["library_coverage"] == 0.5, counts

        # a feature anchored to the compound must be refused, not counted
        compound_feature = os.path.join(tmp, "compound.featureXML")
        make_featurexml(compound_feature, [["1"]], peptide_refs=["0"])
        try:
            om.count_identifications(lib, compound_feature)
        except om.OpenSwathMetricsError as exc:
            assert "compound" in str(exc), exc
        else:
            raise AssertionError("a compound-backed feature was accepted")

        # so must a peptide-anchored feature built from a compound transition
        wrong_transition = os.path.join(tmp, "wrong.featureXML")
        make_featurexml(wrong_transition, [["1"]], peptide_refs=["1"])
        try:
            om.count_identifications(lib, wrong_transition)
        except om.OpenSwathMetricsError as exc:
            assert "compound" in str(exc), exc
        else:
            raise AssertionError("a compound transition was counted for a peptide")

    # peptide and compound ids share one namespace: a collision is invalid
    with tempfile.TemporaryDirectory() as tmp:
        collision = os.path.join(tmp, "collision.TraML")
        make_traml(collision,
                   proteins=[("ProtA", "uniprot_a")],
                   peptides=[("Shared", "AAAA", "2", "ProtA")],
                   compounds=[("Shared", "1")],
                   transitions=[("t1", "Shared", "400.5")])
        try:
            om.parse_traml(collision)
        except om.OpenSwathMetricsError as exc:
            assert "Shared" in str(exc), exc
        else:
            raise AssertionError("a peptide/compound id collision was accepted")
    print("PASS: compound-backed transitions keep positions but stay out of peptide metrics")


def test_permuted_transitions_fail_membership():
    """Order independence: when the library is permuted relative to the run,
    the anchored peptide must be unchanged while the subordinate ids no longer
    belong to it. The guard must fail with both peptides named, never shift the
    assignment onto a neighbour."""
    with tempfile.TemporaryDirectory() as tmp:
        traml = os.path.join(tmp, "lib.TraML")
        make_traml(
            traml,
            proteins=[("ProtA", "uniprot_a")],
            peptides=[("PepA", "AAAA", "2", "ProtA"),
                      ("PepB", "BBBB", "2", "ProtA")],
            transitions=[("t1", "PepA", "400.5"),
                         ("t2", "PepA", "400.5"),
                         ("t3", "PepB", "500.5"),
                         ("t4", "PepB", "500.5")],
        )
        lib = om.parse_traml(traml)
        assert lib["precursor_ids"] == {"PepA": "0", "PepB": "1"}, lib

        # correct pairing still works
        ok = os.path.join(tmp, "ok.featureXML")
        make_featurexml(ok, [["0", "1"]], peptide_refs=["0"])
        assert om.count_identifications(lib, ok)["identified_peptides"] == 1

        # permuted library: the anchor says PepB, the ids are PepA's
        permuted = os.path.join(tmp, "permuted.featureXML")
        make_featurexml(permuted, [["0", "1"]], peptide_refs=["1"])
        try:
            om.count_identifications(lib, permuted)
        except om.OpenSwathMetricsError as exc:
            message = str(exc)
            assert "PepB" in message and "PepA" in message, message
        else:
            raise AssertionError("a permuted library silently shifted the assignment")
    print("PASS: cross-peptide permutation fails membership validation")


def test_stale_one_based_pairing_fails():
    """The historical failure mode: a featureXML written with 1-based source
    ids joined against canonical ids shifts every assignment by one. With the
    anchor present the shift is detectable (canonical 3 belongs to Pep1, not to
    the anchored Pep0) and must raise instead of counting the neighbour."""
    with tempfile.TemporaryDirectory() as tmp:
        traml = os.path.join(tmp, "lib.TraML")
        proteins = [("ProtA", "uniprot_a")]
        peptides = [("Pep%d" % i, "SEQ%d" % i, "2", "ProtA") for i in range(3)]
        transitions = []
        for pep in range(3):
            for k in range(3):
                transitions.append((str(1 + pep * 3 + k), "Pep%d" % pep,
                                    "400.%d" % (pep + 1)))
        make_traml(traml, proteins, peptides, transitions)
        lib = om.parse_traml(traml)

        stale = os.path.join(tmp, "stale.featureXML")
        # anchored to Pep0 but carrying Pep0's "1","2" plus Pep1's "3"
        make_featurexml(stale, [["1", "2", "3"]], peptide_refs=["0"])
        try:
            om.count_identifications(lib, stale)
        except om.OpenSwathMetricsError as exc:
            message = str(exc)
            assert "Pep0" in message and "Pep1" in message, message
        else:
            raise AssertionError("a 1-based (stale) pairing was accepted")
    print("PASS: stale 1-based pairing fails where the anchor makes it detectable")


def test_ms1_precursor_prefix_anchors_when_peptide_ref_absent():
    """When PeptideRef is absent, the MS1 precursor prefix is the same assay
    identity in another encoding, so it anchors the feature. When both are
    present they must agree."""
    with tempfile.TemporaryDirectory() as tmp:
        traml = os.path.join(tmp, "lib.TraML")
        make_traml(
            traml,
            proteins=[("ProtA", "uniprot_a")],
            peptides=[("PepA", "AAAA", "2", "ProtA"),
                      ("PepB", "BBBB", "2", "ProtA")],
            transitions=[("t1", "PepA", "400.5"), ("t2", "PepB", "500.5")],
        )
        lib = om.parse_traml(traml)

        # PepA is canonical precursor 0; the MS1 subordinate names it
        anchored = os.path.join(tmp, "ms1.featureXML")
        make_featurexml(anchored, [["0", "0_Precursor_i0"]],
                        levels=[["MS2", "MS1"]])
        counts = om.count_identifications(lib, anchored)
        assert counts["assignment_evidence"] == "anchor", counts
        assert counts["identified_peptides"] == 1, counts
        assert counts["features_without_anchor"] == 0, counts

        # PeptideRef 0 vs MS1 precursor 1: the two encodings disagree
        disagree = os.path.join(tmp, "disagree.featureXML")
        make_featurexml(disagree, [["0", "1_Precursor_i0"]],
                        levels=[["MS2", "MS1"]], peptide_refs=["0"])
        try:
            om.count_identifications(lib, disagree)
        except om.OpenSwathMetricsError as exc:
            assert "disagree" in str(exc), exc
        else:
            raise AssertionError("disagreeing anchors were accepted")
    print("PASS: MS1 precursor prefix anchors, and anchor disagreement fails")


def test_missing_and_mixed_anchors_fail_closed():
    """No anchor -> strict error; an explicit opt-out is allowed but stamped
    positional. A file mixing anchored and unanchored features is also refused
    by default and reported as "mixed" only when explicitly opted out."""
    with tempfile.TemporaryDirectory() as tmp:
        traml = os.path.join(tmp, "lib.TraML")
        make_traml(
            traml,
            proteins=[("ProtA", "uniprot_a")],
            peptides=[("PepA", "AAAA", "2", "ProtA"),
                      ("PepB", "BBBB", "2", "ProtA")],
            transitions=[("t1", "PepA", "400.5"), ("t2", "PepB", "500.5")],
        )
        lib = om.parse_traml(traml)

        unanchored = os.path.join(tmp, "unanchored.featureXML")
        make_featurexml(unanchored, [["0"]])
        try:
            om.count_identifications(lib, unanchored)
        except om.OpenSwathMetricsError as exc:
            assert "anchor" in str(exc), exc
        else:
            raise AssertionError("an unanchored feature was accepted by default")

        positional = om.count_identifications(lib, unanchored,
                                             require_anchor=False)
        assert positional["assignment_evidence"] == "positional", positional
        assert positional["features_without_anchor"] == 1, positional
        assert positional["identified_peptides"] == 1, positional

        mixed_file = os.path.join(tmp, "mixed.featureXML")
        make_featurexml(mixed_file, [["0"], ["1"]],
                        peptide_refs=["0", None])
        try:
            om.count_identifications(lib, mixed_file)
        except om.OpenSwathMetricsError as exc:
            assert "f_1" in str(exc), exc
        else:
            raise AssertionError("a mixed anchored/unanchored file was accepted")

        mixed = om.count_identifications(lib, mixed_file, require_anchor=False)
        assert mixed["assignment_evidence"] == "mixed", mixed
        assert mixed["features_without_anchor"] == 1, mixed
        assert mixed["identified_peptides"] == 2, mixed

        # an anchor that does not resolve is drift, not a missing anchor: it
        # fails even when positional counting is explicitly allowed.
        unresolvable = os.path.join(tmp, "unresolvable.featureXML")
        make_featurexml(unresolvable, [["0"]], peptide_refs=["9"])
        for allow_positional in (False, True):
            try:
                om.count_identifications(lib, unresolvable,
                                         require_anchor=not allow_positional)
            except om.OpenSwathMetricsError as exc:
                assert "9" in str(exc), exc
            else:
                raise AssertionError("an unresolvable anchor was accepted")

        # An anchor establishes the peptide, not that any transition was
        # attributed to it. A feature with no MS2 native_id contributes
        # nothing and must be refused - even when positional counting is
        # explicitly allowed, because the feature IS anchored.
        no_ms2 = os.path.join(tmp, "no_ms2.featureXML")
        make_featurexml(no_ms2, [[]], peptide_refs=["0"])
        for allow_positional in (False, True):
            try:
                om.count_identifications(lib, no_ms2,
                                         require_anchor=not allow_positional)
            except om.OpenSwathMetricsError as exc:
                assert "MS2" in str(exc), exc
            else:
                raise AssertionError(
                    "an anchored feature with no MS2 transition was accepted")
    print("PASS: missing/mixed/empty anchored features fail closed; opt-out stamps positional/mixed")


# --- sequence and precursor m/z consistency ---------------------------------


def test_sequence_check_uses_anchor_and_reports_ambiguity():
    """The feature's PeptideHit sequence is copied from the assay peptide
    upstream, so it must contain the anchored sequence. Several candidates that
    include the anchor are accepted and counted as ambiguous; candidates that
    exclude it are a library/featureXML mismatch."""
    with tempfile.TemporaryDirectory() as tmp:
        traml = os.path.join(tmp, "lib.TraML")
        make_traml(
            traml,
            proteins=[("ProtA", "uniprot_a")],
            peptides=[("PepA", "AAAA", "2", "ProtA"),
                      ("PepB", "BBBB", "2", "ProtA")],
            transitions=[("t1", "PepA", "400.5"), ("t2", "PepB", "500.5")],
        )
        lib = om.parse_traml(traml)

        matched = os.path.join(tmp, "matched.featureXML")
        make_featurexml(matched, [["0"]], peptide_refs=["0"],
                        hit_sequences=[["AAAA"]])
        counts = om.count_identifications(lib, matched)
        assert counts["identified_peptides"] == 1, counts
        assert counts["ambiguous_hit_features"] == 0, counts

        ambiguous = os.path.join(tmp, "ambiguous.featureXML")
        make_featurexml(ambiguous, [["0"]], peptide_refs=["0"],
                        hit_sequences=[["AAAA", "BBBB"]])
        counts = om.count_identifications(lib, ambiguous)
        assert counts["identified_peptides"] == 1, counts   # anchor decides
        assert counts["ambiguous_hit_features"] == 1, counts

        missing = os.path.join(tmp, "missing.featureXML")
        make_featurexml(missing, [["0"]], peptide_refs=["0"],
                        hit_sequences=[["BBBB"]])
        try:
            om.count_identifications(lib, missing)
        except om.OpenSwathMetricsError as exc:
            assert "AAAA" in str(exc), exc
        else:
            raise AssertionError("a hit set without the anchored sequence was accepted")

        # no identification at all: skip the sequence check, not the join
        no_hits = os.path.join(tmp, "nohits.featureXML")
        make_featurexml(no_hits, [["0"]], peptide_refs=["0"])
        assert om.count_identifications(lib, no_hits)["identified_peptides"] == 1

        # An annotated hit must never switch off validation of the plain hits
        # beside it: here the only plain hit ("BBBB") contradicts the anchor.
        bypass = os.path.join(tmp, "bypass.featureXML")
        make_featurexml(bypass, [["0"]], peptide_refs=["0"],
                        hit_sequences=[["BBBB", "AAAAT(Phospho)"]])
        try:
            om.count_identifications(lib, bypass)
        except om.OpenSwathMetricsError as exc:
            assert "AAAA" in str(exc) and "BBBB" in str(exc), exc
        else:
            raise AssertionError("an annotated hit disabled plain-hit validation")

        # annotated extra candidates are still candidates: the anchor is
        # present, so the run is accepted and reported ambiguous.
        mixed = os.path.join(tmp, "mixedhits.featureXML")
        make_featurexml(mixed, [["0"]], peptide_refs=["0"],
                        hit_sequences=[["AAAA", "AAAAT(Phospho)"]])
        counts = om.count_identifications(lib, mixed)
        assert counts["ambiguous_hit_features"] == 1, counts

        # the same sequence repeated is one candidate, not ambiguity
        duplicated = os.path.join(tmp, "duphits.featureXML")
        make_featurexml(duplicated, [["0"]], peptide_refs=["0"],
                        hit_sequences=[["AAAA", "AAAA"]])
        counts = om.count_identifications(lib, duplicated)
        assert counts["ambiguous_hit_features"] == 0, counts

        # several hits sharing one non-anchor sequence are still one candidate
        same_wrong = os.path.join(tmp, "samewrong.featureXML")
        make_featurexml(same_wrong, [["0"]], peptide_refs=["0"],
                        hit_sequences=[["BBBB", "BBBB"]])
        try:
            om.count_identifications(lib, same_wrong)
        except om.OpenSwathMetricsError:
            pass
        else:
            raise AssertionError("a repeated non-anchor sequence was accepted")
    print("PASS: sequence check is per hit (annotated hits never disable plain ones)")


def test_modified_peptide_sequence_check_is_skipped():
    """A TraML peptide with <Modification> has a residue-only @sequence, so it
    must not be compared with an annotated featureXML sequence. Skipping the
    sequence check must not skip the transition-membership validation."""
    with tempfile.TemporaryDirectory() as tmp:
        traml = os.path.join(tmp, "lib.TraML")
        make_traml(
            traml,
            proteins=[("ProtA", "uniprot_a")],
            peptides=[("PepMod", "AAAAT", "2", "ProtA"),
                      ("PepOther", "CCCC", "2", "ProtA")],
            transitions=[("t1", "PepMod", "400.5"),
                         ("t2", "PepOther", "500.5")],
            modified=["PepMod"],
        )
        lib = om.parse_traml(traml)
        assert lib["peptides"]["PepMod"]["modified"] is True, lib["peptides"]
        assert lib["precursor_ids"]["PepMod"] == "0", lib

        annotated = os.path.join(tmp, "annotated.featureXML")
        make_featurexml(annotated, [["0"]], peptide_refs=["0"],
                        hit_sequences=[["AAAAT(Phospho)"]])
        counts = om.count_identifications(lib, annotated)
        assert counts["identified_peptides"] == 1, counts
        assert counts["ambiguous_hit_features"] == 0, counts

        # Ambiguity is about the candidate list, not about comparability: a
        # modified peptide with two distinct hits is ambiguous even though the
        # sequences cannot be compared.
        ambiguous = os.path.join(tmp, "ambiguous.featureXML")
        make_featurexml(ambiguous, [["0"]], peptide_refs=["0"],
                        hit_sequences=[["AAAAT(Phospho)", "BBBB"]])
        counts = om.count_identifications(lib, ambiguous)
        assert counts["identified_peptides"] == 1, counts
        assert counts["ambiguous_hit_features"] == 1, counts

        # membership is still validated even though the sequence check is off
        wrong = os.path.join(tmp, "wrong.featureXML")
        make_featurexml(wrong, [["1"]], peptide_refs=["0"])
        try:
            om.count_identifications(lib, wrong)
        except om.OpenSwathMetricsError as exc:
            assert "PepOther" in str(exc), exc
        else:
            raise AssertionError("membership validation was skipped with the sequence check")
    print("PASS: modified-peptide sequence comparison skipped, membership still enforced")


def test_precursor_mz_consistency_check():
    """PrecursorMZ is a redundant projection of the anchored peptide, so it is
    compared with a documented tolerance: formatting noise is tolerated, a
    feature glued to an unrelated peptide is not, and a missing value or a
    library without m/z skips the check."""
    with tempfile.TemporaryDirectory() as tmp:
        traml = os.path.join(tmp, "lib.TraML")
        make_traml(
            traml,
            proteins=[("ProtA", "uniprot_a")],
            peptides=[("PepA", "AAAA", "2", "ProtA"),
                      ("PepB", "BBBB", "2", "ProtA")],
            transitions=[("t1", "PepA", "510.05"), ("t2", "PepB", "500.01")],
        )
        lib = om.parse_traml(traml)

        # decimal-formatting noise must pass (the real fixture deviates by 1e-14)
        noisy = os.path.join(tmp, "noisy.featureXML")
        make_featurexml(noisy, [["0"]], peptide_refs=["0"],
                        precursor_mz=["510.050000000000011"])
        assert om.count_identifications(lib, noisy)["identified_peptides"] == 1

        wrong = os.path.join(tmp, "wrong.featureXML")
        make_featurexml(wrong, [["0"]], peptide_refs=["0"],
                        precursor_mz=["500.01"])
        try:
            om.count_identifications(lib, wrong)
        except om.OpenSwathMetricsError as exc:
            assert "PrecursorMZ" in str(exc), exc
        else:
            raise AssertionError("a mismatched PrecursorMZ was accepted")

        absent = os.path.join(tmp, "absent.featureXML")
        make_featurexml(absent, [["0"]], peptide_refs=["0"])
        assert om.count_identifications(lib, absent)["identified_peptides"] == 1
    print("PASS: PrecursorMZ consistency check (tolerant, redundant, skippable)")


def test_duplicate_declared_ids_are_rejected():
    """Duplicate <Peptide> / <Compound> declarations are schema-invalid (both
    ids are xs:ID), the upstream normalizer throws on them, and they would
    change the canonical precursor namespace - so they must raise instead of
    silently keeping the last declaration."""
    with tempfile.TemporaryDirectory() as tmp:
        dup_peptide = os.path.join(tmp, "dup_peptide.TraML")
        make_traml(dup_peptide,
                   proteins=[("ProtA", "uniprot_a")],
                   peptides=[("PepA", "AAAA", "2", "ProtA"),
                             ("PepA", "GGGG", "3", "ProtA")],
                   transitions=[("t1", "PepA", "400.5")])
        try:
            om.parse_traml(dup_peptide)
        except om.OpenSwathMetricsError as exc:
            assert "PepA" in str(exc), exc
        else:
            raise AssertionError("a duplicate <Peptide id> was silently merged")

        dup_compound = os.path.join(tmp, "dup_compound.TraML")
        make_traml(dup_compound,
                   proteins=[("ProtA", "uniprot_a")],
                   peptides=[("PepA", "AAAA", "2", "ProtA")],
                   compounds=[("LIG", "1"), ("LIG", "2")],
                   transitions=[("t1", "PepA", "400.5")])
        try:
            om.parse_traml(dup_compound)
        except om.OpenSwathMetricsError as exc:
            assert "LIG" in str(exc), exc
        else:
            raise AssertionError("a duplicate <Compound id> was silently merged")
    print("PASS: duplicate peptide/compound declarations are rejected")


def test_identified_precursor_mz_counts_used_windows():
    """M3 regression: a peptide may declare several precursor windows, but only
    the windows its picked transitions came from count as identified. Counting
    every window of an identified peptide would inflate the metric."""
    with tempfile.TemporaryDirectory() as tmp:
        traml = os.path.join(tmp, "lib.TraML")
        # one peptide, two precursor windows (400.5 and 600.5)
        transitions = [("t1", "PepA", "400.5"),
                       ("t2", "PepA", "400.5"),
                       ("t3", "PepA", "600.5"),
                       ("t4", "PepA", "600.5")]
        make_traml(traml,
                   proteins=[("ProtA", "uniprot_a")],
                   peptides=[("PepA", "AAAA", "2", "ProtA")],
                   transitions=transitions)
        lib = om.parse_traml(traml)
        assert lib["library_precursor_mz"] == 2, lib

        # feature picked from the 400.5 window only
        one_window = os.path.join(tmp, "one.featureXML")
        make_featurexml(one_window, [canonical(transitions, "t1", "t2")],
                        peptide_refs=["0"])
        counts = om.count_identifications(lib, one_window)
        assert counts["identified_peptides"] == 1, counts
        assert counts["transitions_used"] == 2, counts
        assert counts["identified_precursor_mz"] == 1, counts

        # feature using both windows reaches both
        both = os.path.join(tmp, "both.featureXML")
        make_featurexml(both, [canonical(transitions, "t1", "t2", "t3", "t4")],
                        peptide_refs=["0"])
        assert om.count_identifications(
            lib, both)["identified_precursor_mz"] == 2

        # the positional path keeps the same per-transition semantics
        positional = os.path.join(tmp, "positional.featureXML")
        make_featurexml(positional, [canonical(transitions, "t1", "t2")])
        counts = om.count_identifications(lib, positional, require_anchor=False)
        assert counts["identified_precursor_mz"] == 1, counts
    print("PASS: identified_precursor_mz counts only used precursor windows")


def test_missing_precursor_mz_is_fail_closed():
    """L2: only the supported TraML precursor term is read. A transition
    without it contributes NO precursor identity (never a fake (missing,
    charge) tuple), and a supplied PrecursorMZ that cannot be checked is
    refused rather than silently accepted."""
    body = """<?xml version="1.0" encoding="UTF-8"?>
<TraML version="1.0.0" xmlns="http://psi.hupo.org/ms/traml">
  <CompoundList>
    <Peptide id="PepA" sequence="AAAA">
      <cvParam cvRef="MS" accession="MS:1000041" name="charge state" value="2"/>
    </Peptide>
  </CompoundList>
  <TransitionList>
    <Transition id="t1" peptideRef="PepA"><Product/></Transition>
    <Transition id="t2" peptideRef="PepA"><Product/></Transition>
  </TransitionList>
</TraML>
"""
    with tempfile.TemporaryDirectory() as tmp:
        traml = os.path.join(tmp, "lib.TraML")
        with open(traml, "w", encoding="utf-8") as fh:
            fh.write(body)
        lib = om.parse_traml(traml)
        # no supported m/z -> no precursor identity is invented
        assert lib["library_transitions"] == 2, lib
        assert lib["library_precursor_mz"] == 0, lib

        without = os.path.join(tmp, "without.featureXML")
        make_featurexml(without, [["0", "1"]], peptide_refs=["0"])
        counts = om.count_identifications(lib, without)
        assert counts["identified_peptides"] == 1, counts
        assert counts["identified_precursor_mz"] == 0, counts

        # a declared PrecursorMZ that cannot be checked must not be accepted
        declared = os.path.join(tmp, "declared.featureXML")
        make_featurexml(declared, [["0"]], peptide_refs=["0"],
                        precursor_mz=["400.5"])
        try:
            om.count_identifications(lib, declared)
        except om.OpenSwathMetricsError as exc:
            assert "supported precursor m/z" in str(exc), exc
        else:
            raise AssertionError("an unverifiable PrecursorMZ was accepted")

        # A non-finite library m/z is invalid, not merely unsupported: it must
        # not enter the identity count, and it must never slip past the
        # consistency check (every comparison against NaN is False).
        for value in ("nan", "inf", "-inf"):
            bad = os.path.join(tmp, "nonfinite_%s.TraML" % value.strip("-"))
            with open(bad, "w", encoding="utf-8") as fh:
                fh.write(body.replace(
                    "</Transition>",
                    "<Precursor><cvParam cvRef=\"MS\" accession=\"MS:1000827\" "
                    "name=\"isolation window target m/z\" value=\"%s\"/>"
                    "</Precursor></Transition>" % value))
            try:
                om.parse_traml(bad)
            except om.OpenSwathMetricsError as exc:
                assert value in str(exc), exc
            else:
                raise AssertionError(
                    "a non-finite precursor m/z (%r) was accepted" % value)
    print("PASS: unsupported precursor m/z contributes no identity; non-finite m/z rejected")


# --- runner verdict path ----------------------------------------------------


def _runner_python_block():
    """The Python block embedded in run_openswath_benchmark.sh, verbatim."""
    with open(RUNNER, encoding="utf-8") as fh:
        script = fh.read()
    match = re.search(r"python3 - <<'PY'\n(.*?)\nPY\n", script, re.S)
    assert match, "runner no longer contains an embedded python3 <<'PY' block"
    return match.group(1)


def _write_run_inputs(results_dir):
    with open(os.path.join(results_dir, "meta.txt"), "w", encoding="utf-8") as fh:
        fh.write("openms_sha=testsha\nopenms_version=test\n")
    with open(os.path.join(results_dir, "stages.tsv"), "w", encoding="utf-8") as fh:
        # one REQUIRED stage that passed: stage status cannot be what fails
        fh.write("workflow\ttrue\t0\t0.1\t0.1\t100\tpass\t\n")
    with open(os.path.join(results_dir, "output.json"), "w", encoding="utf-8") as fh:
        fh.write("{}")


def _run_runner_block(block, results_dir, fixtures_dir, script_dir):
    env = dict(os.environ)
    env.update({"RESULTS_DIR": results_dir, "FIXTURES_DIR": fixtures_dir,
                "SCRIPT_DIR": script_dir})
    env.pop("GITHUB_RUN_ID", None)
    completed = subprocess.run([sys.executable, "-"], input=block, text=True,
                               capture_output=True, env=env)
    assert completed.returncode == 0, completed.stderr
    with open(os.path.join(results_dir, "openswath.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _shell_verdict_readback(json_path):
    """The exact read-back the runner uses for summary/exit status."""
    return subprocess.run(
        [sys.executable, "-c",
         "import json,sys; sys.exit(0 if json.load(open(sys.argv[1]))[\"verdict\"] == "
         "\"pass\" else 1)", json_path],
        capture_output=True).returncode


def test_runner_verdict_rejects_unvalidated_assignment():
    """The verdict must follow the EVIDENCE, not just the stage status.

    Executes the runner's own Python block (extracted from the script) against
    stubbed run directories, so this covers the real verdict/JSON path without
    an OpenMS binary:
      1. positional-only featureXML -> fail with an anchor error;
      2. anchored featureXML       -> pass with assignment_evidence="anchor";
      3. a metrics module reporting an unvalidated join -> still fail;
      4. an empty featureXML       -> fail the expected-feature gate, not pass;
      5. anchored features with no resolved MS2 transition -> fail, not pass.
    The shell's read-back is exercised too, because summary.txt and the exit
    status are derived from that JSON verdict.
    """
    block = _runner_python_block()
    with tempfile.TemporaryDirectory() as tmp:
        fixtures = os.path.join(tmp, "fixtures")
        os.makedirs(fixtures)
        # Six transitions, because the runner also gates on the expected
        # feature count (6 for the reference fixture); the scenarios below then
        # differ only in their evidence, not in completeness.
        six_transitions = [("t%d" % i, "PepA", "%0.2f" % (400.5 + i))
                           for i in range(6)]
        make_traml(os.path.join(fixtures, "OpenSwathWorkflow_1_input.TraML"),
                   [("ProtA", "uniprot_a")],
                   [("PepA", "AAAA", "2", "ProtA")],
                   six_transitions)
        results = os.path.join(tmp, "results")
        os.makedirs(results)
        _write_run_inputs(results)

        # 1. no anchor anywhere: the strict default raises, so the run fails.
        make_featurexml(os.path.join(results, "output.featureXML"),
                        [[str(i)] for i in range(6)])
        report = _run_runner_block(block, results, fixtures, HERE)
        assert report["verdict"] == "fail", report["verdict"]
        assert "anchor" in report["correctness"]["library_error"], report["correctness"]
        assert report["correctness"]["features_match"] is True, report["correctness"]
        assert _shell_verdict_readback(
            os.path.join(results, "openswath.json")) == 1

        # 2. anchored feature: passes, and the evidence is recorded.
        make_featurexml(os.path.join(results, "output.featureXML"),
                        [[str(i)] for i in range(6)],
                        peptide_refs=["0"] * 6)
        report = _run_runner_block(block, results, fixtures, HERE)
        assert report["verdict"] == "pass", report["correctness"]
        assert report["correctness"]["assignment_evidence"] == "anchor", \
            report["correctness"]
        assert report["correctness"]["features_without_anchor"] == 0, \
            report["correctness"]
        assert report["correctness"]["features_match"] is True, report["correctness"]
        assert _shell_verdict_readback(
            os.path.join(results, "openswath.json")) == 0

        # 3. even a module that RETURNS positional evidence (rather than
        # raising) must not produce a passing verdict.
        stub_dir = os.path.join(tmp, "stub")
        os.makedirs(stub_dir)
        with open(os.path.join(stub_dir, "openswath_metrics.py"), "w",
                  encoding="utf-8") as fh:
            fh.write(
                "class OpenSwathMetricsError(Exception):\n"
                "    pass\n\n"
                "def parse_traml(path):\n"
                "    return {}\n\n"
                "def count_identifications(library, featurexml_path):\n"
                "    return {'library_peptides': 1, 'identified_peptides': 0,\n"
                "            'assignment_evidence': 'positional',\n"
                "            'features_without_anchor': 1}\n")
        report = _run_runner_block(block, results, fixtures, stub_dir)
        assert report["verdict"] == "fail", report["verdict"]
        assert "unvalidated" in report["correctness"]["library_error"], \
            report["correctness"]
        assert _shell_verdict_readback(
            os.path.join(results, "openswath.json")) == 1

        # 4. an EMPTY featureXML: the metrics layer keeps zero counts and
        # assignment_evidence="none", but the run is incomplete, so the
        # expected-feature gate must keep it from passing.
        make_featurexml(os.path.join(results, "output.featureXML"), [])
        report = _run_runner_block(block, results, fixtures, HERE)
        assert report["verdict"] == "fail", report["verdict"]
        assert report["correctness"]["assignment_evidence"] == "none", \
            report["correctness"]
        assert report["correctness"]["features_match"] is False, report["correctness"]
        assert report["correctness"]["actual_features"] == 0, report["correctness"]
        assert "feature_count_error" in report["correctness"], report["correctness"]
        assert "0" in report["correctness"]["feature_count_error"], \
            report["correctness"]
        assert "6" in report["correctness"]["feature_count_error"], \
            report["correctness"]
        assert "library_error" not in report["correctness"], report["correctness"]
        assert _shell_verdict_readback(
            os.path.join(results, "openswath.json")) == 1

        # 5. six anchored features that resolve no MS2 transition: an anchor
        # alone must satisfy neither the join nor the completeness gate.
        make_featurexml(os.path.join(results, "output.featureXML"), [[]] * 6,
                        peptide_refs=["0"] * 6)
        report = _run_runner_block(block, results, fixtures, HERE)
        assert report["verdict"] == "fail", report["verdict"]
        assert "MS2" in report["correctness"]["library_error"], \
            report["correctness"]
        assert "assignment_evidence" not in report["correctness"], \
            report["correctness"]
        assert _shell_verdict_readback(
            os.path.join(results, "openswath.json")) == 1
    print("PASS: runner verdict follows evidence and completeness (JSON + read-back)")


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
    test_unusable_transition_keeps_its_position()
    test_duplicate_source_ids_across_peptides()
    test_duplicate_source_ids_within_peptide()
    test_compound_backed_feature_is_reported_not_counted()
    test_permuted_transitions_fail_membership()
    test_stale_one_based_pairing_fails()
    test_ms1_precursor_prefix_anchors_when_peptide_ref_absent()
    test_missing_and_mixed_anchors_fail_closed()
    test_sequence_check_uses_anchor_and_reports_ambiguity()
    test_modified_peptide_sequence_check_is_skipped()
    test_precursor_mz_consistency_check()
    test_duplicate_declared_ids_are_rejected()
    test_identified_precursor_mz_counts_used_windows()
    test_missing_precursor_mz_is_fail_closed()
    test_runner_verdict_rejects_unvalidated_assignment()
    print("\nAll openswath_metrics tests passed.")


if __name__ == "__main__":
    main()
