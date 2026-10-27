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

    featureXML subordinate <UserParam name="native_id">   (canonical id)
        -> transition_canonical_to_source[canonical id]
        -> TraML <Transition id="...">                     (source id)
        -> Transition@peptideRef
        -> TraML <Peptide id="...">
        -> Peptide/ProteinRef@ref
        -> TraML <Protein id="...">

join. It does NOT mean peptide-spectrum matches.

TRANSITION ID CONTRACT - two namespaces, never interchangeable:

- source id     the TraML <Transition id="..."> attribute, e.g. "1" or
                "TIC_x". `transitions` is keyed by it, because everything
                downstream of the join (peptideRef, precursor, protein) lives
                on the TraML element itself.
- canonical id  the zero-based position of that <Transition> in TraML
                document order, as a decimal string ("0", "1", ...).
                OpenMS replaces every source id with this value in
                OpenSwathLibraryIDNormalizer::normalizeSourceIDs() before the
                workflow runs, so OpenSwathWorkflow writes the CANONICAL id
                as the feature's native_id. The source id survives only in an
                in-memory map that featureXML never carries.

The mapping between the two is therefore positional and is rebuilt here from
TraML document order (transition_canonical_to_source). It is derived, not
inferred from the numeric value: source ids are opaque strings and may be
non-numeric, non-contiguous or collide with a canonical id, so a native_id is
never arithmetic-converted and never looked up as a source id directly.

WHAT IS DELIBERATELY NOT COUNTED HERE:

- PSMs / peptide-spectrum matches. DIA produces no spectrum-level
  identifications: OpenSwathWorkflow registers no idXML or mzTab output, and
  its featureXML contains no PeptideIdentification, ProteinIdentification or
  IdentificationConfidence element. Any such count would be fabricated.
- FDR / q-values. There is no per-spectrum score and no decoy axis in DIA
  feature output, so neither is defined here.

Only MS2-level subordinates take part in the join. OpenSwath also writes MS1
precursor-extraction subordinates whose native_id is a precursor id of the form
"<canonical precursor>_Precursor_i<k>"; those never denote a TraML <Transition>,
so they are excluded by their FeatureLevel userParam - never by matching the
shape of the id.

The join is fail-loud: a feature whose canonical transition native_id has no
entry in transition_canonical_to_source - or whose resolved source id is
absent from the library - raises OpenSwathMetricsError rather than being
counted, so a run can never silently inflate its own identification counts.

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
    """Value of the direct child cvParam/userParam with the given name.

    Matched case-insensitively because the two formats spell it differently:
    TraML writes <cvParam>/<userParam>, featureXML writes <UserParam>.
    """
    for child in element:
        if (_local(child).lower() in ("cvparam", "userparam")
                and child.get("name") == name):
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
        transition_canonical_to_source
                    {canonical id -> source id}, where the canonical id is
                    the transition's zero-based position in TraML document
                    order - the id OpenSwathWorkflow writes as native_id.
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
    canonical_to_source = {}
    # Zero-based position of the next <Transition> in document order. OpenMS
    # assigns exactly this value as the transition's canonical id, so it is
    # counted over EVERY <Transition> element - including one this parser
    # cannot use - otherwise an unusable transition would silently shift the
    # canonical ids of every transition after it.
    transition_index = 0

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
            canonical_id = str(transition_index)
            transition_index += 1
            tid = element.get("id")
            peptide_id = element.get("peptideRef")
            if not tid:
                continue
            canonical_to_source[canonical_id] = tid
            if not peptide_id:
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
        "transition_canonical_to_source": canonical_to_source,
        "library_peptides": len(peptides),
        "library_precursor_mz": len(precursors),
        "library_transitions": len(transitions),
        "library_proteins": len(proteins),
    }


def _feature_native_ids(feature):
    """The transition ids a picked feature is built from.

    OpenSwath writes them as <UserParam name="native_id"> on the feature's
    subordinate features; iterating the whole subtree also covers a library
    that puts them on the top-level feature. The values are CANONICAL ids
    (zero-based TraML document positions), not TraML source ids - see the
    module docstring. Resolving them is count_identifications()'s job.

    Only TRANSITION-level subordinates belong to this join. OpenSwath also
    emits MS1 precursor-extraction subordinates whose native_id is a PRECURSOR
    id ("<canonical precursor>_Precursor_i<k>", OpenSwathHelper.h); those have
    no TraML <Transition> to resolve against. Subordinates are classified by
    their FeatureLevel userParam - never by the shape of the id - so an MS1
    subordinate is dropped even if its id happens to look canonical, and an
    MS2 subordinate is kept even if its id looks exotic.

    A subordinate with no FeatureLevel at all is KEPT: dropping it would make a
    library that never writes the field silently report zero identifications
    instead of failing loudly, which is a worse failure than joining it.
    """
    # nearest enclosing <feature> for every node, so each native_id can be
    # classified by the feature that actually declares it.
    owner = {child: node for node in feature.iter() for child in node}

    native_ids = []
    for up in feature.iter():
        if (_local(up).lower() != "userparam" or up.get("name") != "native_id"
                or not up.get("value")):
            continue
        enclosing = owner.get(up)
        while enclosing is not None and _local(enclosing) != "feature":
            enclosing = owner.get(enclosing)
        level = None if enclosing is None else _cv(enclosing, "FeatureLevel")
        if level is not None and level != "MS2":
            # Explicitly a non-transition level (MS1 precursor extraction).
            continue
        native_ids.append(up.get("value"))
    return native_ids


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

    Every featureXML native_id is resolved as a CANONICAL transition id
    through library["transition_canonical_to_source"] before any
    peptide/precursor/protein lookup, so a library whose TraML source ids are
    "1".."18" joins against a featureXML written with canonical "0".."17".
    Only MS2-level subordinates take part in the join; MS1 precursor
    subordinates are excluded by FeatureLevel (see _feature_native_ids).

    Raises OpenSwathMetricsError if the featureXML cannot be parsed or a
    feature references a canonical transition the library does not define (or
    one that resolves to a transition the library cannot use).
    """
    try:
        root = ET.parse(featurexml_path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise OpenSwathMetricsError(
            "cannot parse featureXML %s: %s" % (featurexml_path, exc))

    feature_list = root.find("featureList")
    features = [] if feature_list is None else feature_list.findall("feature")

    transitions = library["transitions"]
    canonical_to_source = library["transition_canonical_to_source"]
    peptides = library["peptides"]
    identified_peptides = set()
    identified_precursor_mz = set()
    identified_proteins = set()
    transitions_used = set()

    for feature in features:
        for native_id in _feature_native_ids(feature):
            # native_id is the canonical id OpenSwath wrote, so resolve it to
            # the TraML source id first; it is never used as a source id and
            # never arithmetic-converted.
            source_id = canonical_to_source.get(native_id)
            if source_id is None:
                # Fail loudly: an unknown canonical transition must never be
                # counted as an identification.
                raise OpenSwathMetricsError(
                    "feature %s references canonical transition native_id %r, "
                    "which the library does not define"
                    % (feature.get("id"), native_id))
            transition = transitions.get(source_id)
            if transition is None:
                # The canonical id exists but the underlying TraML transition
                # carries no usable peptideRef. Still fail loudly.
                raise OpenSwathMetricsError(
                    "feature %s references transition native_id %r (canonical "
                    "id %r), which the library does not define"
                    % (feature.get("id"), source_id, native_id))
            transitions_used.add(source_id)
            peptide_id = transition["peptide"]
            identified_peptides.add(peptide_id)
            identified_precursor_mz.add(transition["precursor"])
            peptide = peptides.get(peptide_id)
            if peptide is None:
                raise OpenSwathMetricsError(
                    "transition %s references peptide %r, which the library "
                    "does not define" % (source_id, peptide_id))
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