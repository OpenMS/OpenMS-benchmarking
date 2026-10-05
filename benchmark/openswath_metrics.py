#!/usr/bin/env python3
"""Library-level identification counts for the OpenSwath DIA benchmark.

Pure extraction module, no output policy: parse one TraML spectral library and
one featureXML of picked features and return the library-level counts used by
the benchmark. The runner (run_openswath_benchmark.sh) and the tests both use
this module. Mirrors the shape of prose_metrics.py for the DDA benchmark.

TERMINOLOGY (these are not interchangeable):

- transition  one MRM ion pair: a TraML <Transition>.
- precursor   one isolatable window: the tuple (precursor isolation-window
              target m/z, peptide charge state), taken over all transitions. A
              TraML carries no <TransitionGroup> element, so this is derived
              structurally. It is deliberately NOT the peptide: upstream
              OpenSwath keys MRM transition groups by Transition@peptideRef
              alone (MRMFeatureFinderScoring.cpp), so an isobaric pair (e.g.
              Leu/Ile) shares one precursor here but is two transition groups
              there. On a library where every peptide has exactly one charge
              state and one precursor m/z this coincides with a per-peptide
              count, but the two are never assumed equal. Hence the explicit
              `_precursor_mz` metric names.
- feature     one picked chromatographic feature (a top-level <feature>). The
              runner owns the feature count; this module does not duplicate it.
- peptide     a distinct TraML <Peptide>.  A declared peptide with at least
              one transition is "identifiable"; one with none is an "orphan".
              library_peptides counts BOTH (it is the declared population);
              library_peptides_identifiable and library_peptides_orphan split
              it, and library_coverage is measured over the identifiable half.
- protein     a distinct TraML <Protein> reached through a peptide's
              ProteinRef.

So "identified_peptides" means: distinct library peptides reached by the

    featureXML subordinate <UserParam name="native_id">
        -> TraML <Transition id="...">
        -> Transition@peptideRef
        -> TraML <Peptide id="...">
        -> Peptide/ProteinRef@ref
        -> TraML <Protein id="...">

join. It does NOT mean peptide-spectrum matches.

WHAT IS DELIBERATELY NOT COUNTED HERE:

- PSMs / peptide-spectrum matches. DIA produces no spectrum-level
  identifications: OpenSwathWorkflow registers no idXML or mzTab output, and
  its featureXML contains no PeptideIdentification, ProteinIdentification or
  IdentificationConfidence element. Any such count would be fabricated.
- FDR / q-values. There is no per-spectrum score and no decoy axis in DIA
  feature output, so neither is defined here.

The join is fail-loud: a feature whose transition native_id is absent from the
library raises OpenSwathMetricsError rather than being counted, so a run can
never silently inflate its own identification counts.

Stdlib only, matching the rest of the benchmarking repo.
"""

import xml.etree.ElementTree as ET

# TraML is a namespaced PSI-MS format; featureXML is not. _local() strips any
# namespace so both documents are walked by plain local tag name.
TRAML_NS = "http://psi.hupo.org/ms/traml"

# library_coverage is a ratio, rounded like the runner's other ratio/sum
# scalars (overall_quality_sum rounds to 6 decimals).
COVERAGE_DECIMALS = 6

# library_peptides_identifiable is the base of library_coverage. A TraML
# peptide with no transitions is still declared in the library (it counts
# towards library_peptides, matching upstream TargetedExperiment::getSummary())
# but it has no precursor, so no transition, so no native_id a feature could
# carry: it can never be identified. Dividing by such an entry would cap
# coverage at a ceiling fixed by library construction rather than by the tool,
# so it is excluded from the ratio and reported separately instead.


class OpenSwathMetricsError(Exception):
    """Raised for a structurally unusable TraML/featureXML, or a feature whose
    transition native_id is not present in the library."""


def _local(element):
    """Element's tag without any '{namespace}' prefix."""
    tag = element.tag
    return tag.split("}", 1)[1] if tag.startswith("{") else tag


def _cv(element, name):
    """Value of the direct child cvParam/userParam with the given name."""
    for child in element:
        if _local(child) in ("cvParam", "userParam") and child.get("name") == name:
            return child.get("value")
    return None


def parse_traml(path):
    """Parse a TraML spectral library.

    Returns a dict with:
        proteins    set of TraML Protein ids
        peptides    {peptide_id: {"charge": str|None,
                                  "proteins": set of Protein ids}}
        transitions {transition_id: {"peptide": peptide_id,
                                     "precursor": (precursor_mz, charge)}}
        library_peptides / library_precursor_mz / library_transitions /
        library_proteins   integer sizes

    A peptide with no transitions still counts towards library_peptides (it is
    in the library) but contributes no precursor and can never be identified.
    """
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise OpenSwathMetricsError("cannot parse TraML %s: %s" % (path, exc))

    proteins = set()
    peptides = {}
    transitions = {}

    for element in root.iter():
        tag = _local(element)
        if tag == "Protein":
            if element.get("id"):
                proteins.add(element.get("id"))
        elif tag == "Peptide":
            pid = element.get("id")
            if pid:
                peptides[pid] = {
                    "charge": _cv(element, "charge state"),
                    "proteins": {c.get("ref") for c in element
                                 if _local(c) == "ProteinRef" and c.get("ref")},
                }
        elif tag == "Transition":
            tid = element.get("id")
            peptide_id = element.get("peptideRef")
            if not tid or not peptide_id:
                continue
            precursor_mz = None
            for child in element:
                if _local(child) == "Precursor":
                    precursor_mz = _cv(child, "isolation window target m/z")
            charge = peptides.get(peptide_id, {}).get("charge")
            transitions[tid] = {
                "peptide": peptide_id,
                "precursor": (precursor_mz, charge),
            }

    precursors = {t["precursor"] for t in transitions.values()}
    return {
        "proteins": proteins,
        "peptides": peptides,
        "transitions": transitions,
        "library_peptides": len(peptides),
        "library_precursor_mz": len(precursors),
        "library_transitions": len(transitions),
        "library_proteins": len(proteins),
    }


def _feature_native_ids(feature):
    """The transition ids a picked feature is built from.

    OpenSwath writes them as <UserParam name="native_id"> on the feature's
    subordinate features; iterating the whole subtree also covers a library
    that puts them on the top-level feature.
    """
    return [up.get("value") for up in feature.iter()
            if _local(up) == "UserParam"
            and up.get("name") == "native_id" and up.get("value")]


def count_identifications(library, featurexml_path):
    """Join a featureXML against a parsed library and return the counts.

    Returns (all integers unless noted):
        library_peptides, library_peptides_identifiable,
        library_peptides_orphan, library_precursor_mz, library_transitions,
        identified_peptides, identified_precursor_mz, identified_proteins,
        transitions_used, library_coverage (float, identified_peptides /
        library_peptides_identifiable rounded to COVERAGE_DECIMALS)

    library_peptides stays the full declared count so an orphan library entry
    stays visible; library_coverage is divided by the identifiable subset
    only, because an orphan is structurally incapable of being identified and
    would otherwise impose a ceiling no tool result can exceed.

    Raises OpenSwathMetricsError if the featureXML cannot be parsed or a
    feature references a transition the library does not define.
    """
    try:
        root = ET.parse(featurexml_path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise OpenSwathMetricsError(
            "cannot parse featureXML %s: %s" % (featurexml_path, exc))

    feature_list = root.find("featureList")
    features = [] if feature_list is None else feature_list.findall("feature")

    transitions = library["transitions"]
    peptides = library["peptides"]
    identified_peptides = set()
    identified_precursor_mz = set()
    identified_proteins = set()
    transitions_used = set()

    for feature in features:
        for native_id in _feature_native_ids(feature):
            transition = transitions.get(native_id)
            if transition is None:
                # Fail loudly: an unknown transition must never be counted as
                # an identification.
                raise OpenSwathMetricsError(
                    "feature %s references transition native_id %r, which the "
                    "library does not define" % (feature.get("id"), native_id))
            transitions_used.add(native_id)
            peptide_id = transition["peptide"]
            identified_peptides.add(peptide_id)
            identified_precursor_mz.add(transition["precursor"])
            peptide = peptides.get(peptide_id)
            if peptide is None:
                raise OpenSwathMetricsError(
                    "transition %s references peptide %r, which the library "
                    "does not define" % (native_id, peptide_id))
            identified_proteins.update(peptide["proteins"])

    library_peptides = library["library_peptides"]
    # Identifiable = declared peptides that at least one transition references.
    # Intersecting with the declared set keeps a transition pointing at an
    # undeclared peptide out of the denominator; the join above still fails
    # loudly if a feature reaches such a peptide.
    identifiable = {t["peptide"] for t in transitions.values()} & set(peptides)
    identifiable_count = len(identifiable)
    coverage = (len(identified_peptides) / identifiable_count
                if identifiable_count else 0.0)
    return {
        "library_peptides": library_peptides,
        "library_peptides_identifiable": identifiable_count,
        "library_peptides_orphan": library_peptides - identifiable_count,
        "library_precursor_mz": library["library_precursor_mz"],
        "library_transitions": library["library_transitions"],
        "identified_peptides": len(identified_peptides),
        "identified_precursor_mz": len(identified_precursor_mz),
        "identified_proteins": len(identified_proteins),
        "transitions_used": len(transitions_used),
        "library_coverage": round(coverage, COVERAGE_DECIMALS),
    }