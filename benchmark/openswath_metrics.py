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
- compound    a distinct TraML <Compound> (small molecule / metabolomics
              entry). It shares the canonical precursor id namespace with
              peptides but is NOT a peptide and never enters a peptide metric.

So "identified_peptides" means: distinct library peptides reached by the

    featureXML subordinate <UserParam name="native_id">   (canonical id)
        -> library["transitions"][canonical id]                 (position key)
        -> Transition@peptideRef
        -> TraML <Peptide id="...">
        -> Peptide/ProteinRef@ref
        -> TraML <Protein id="...">

join, validated against the feature's canonical precursor anchor. It does NOT
mean peptide-spectrum matches.

TRANSITION ID CONTRACT - two namespaces, never interchangeable:

- source id     the TraML <Transition id="..."> attribute, e.g. "1" or
                "TIC_x". It is METADATA here: OpenMS replaces it with the
                canonical id before the workflow runs, so it survives only in
                diagnostics (transition["source_id"],
                transition_canonical_to_source).
- canonical id  the zero-based position of that <Transition> in TraML
                document order, as a decimal string ("0", "1", ...).
                OpenSwathLibraryIDNormalizer::normalizeSourceIDs() writes this
                value as the feature's native_id.

`transitions` is therefore keyed by CANONICAL id, one record per <Transition>
element. Source ids are never unique by contract - the upstream normalizer
header states "Source transition IDs are not required to be unique, so only
the canonical-to-source direction is retained", and the TraML 1.0.0 schema
types TransitionType/@id as xs:string rather than xs:ID - so keying by source
id would silently merge distinct transitions (and, when the duplicates belong
to different peptides, mis-attribute one peptide's transitions to another).
Duplicate source ids are accepted; duplicates of a <Peptide>/<Compound> id are
not (both are xs:ID, and the normalizer rejects them).

CANONICAL PRECURSOR IDS (feature anchors) are reconstructed exactly as the
normalizer assigns them: sort ALL declared <Peptide> and <Compound> ids and
use the zero-based index as the id, including entries no transition refers to
(an orphan still reserves its slot). Declaration order is irrelevant because
the assignment is sorted; the union matters because upstream converts both
lists into one TargetedExperiment::compounds vector.

ASSIGNMENT EVIDENCE - a count is only as good as the join behind it:

- anchor        PeptideRef, else the MS1 precursor prefix of a native_id
                ("<canonical precursor>_Precursor_i<k>"). Both encode the same
                upstream transition-group id, so they are redundant encodings,
                not independent witnesses: when both are present they must
                agree, and disagreement is a corrupt feature record.
- positional    no anchor at all. The join still runs, but nothing ties the
                feature to a library peptide beyond its native_ids, so the
                counts are UNVALIDATED. Asking for this requires
                require_anchor=False; the result is stamped
                assignment_evidence="positional" (or "mixed") and the runner
                refuses to pass it. Features without an anchor fail by default.

WHAT IS DELIBERATELY NOT COUNTED HERE:

- PSMs / peptide-spectrum matches. DIA produces no spectrum-level
  identifications: OpenSwathWorkflow registers no idXML or mzTab output, and
  its featureXML contains no ProteinIdentification or
  IdentificationConfidence element. Any such count would be fabricated.
- FDR / q-values. There is no per-spectrum score and no decoy axis in DIA
  feature output, so neither is defined here.

Only MS2-level subordinates take part in the join. OpenSwath also writes MS1
precursor-extraction subordinates whose native_id is a precursor id of the form
"<canonical precursor>_Precursor_i<k>" (OpenSwathHelper::computePrecursorId).
They never denote a TraML <Transition>, so they are excluded from the join by
their FeatureLevel userParam - never by the shape of the id. That FeatureLevel
is also what gates the one supported shape parse: when a feature carries no
PeptideRef, its MS1 native_ids are scanned for exactly that documented upstream
prefix format, and the prefix is used as the canonical precursor anchor. An id
of any other shape is not an anchor.

FEATURE-LEVEL METADATA CONTRACT: PeptideRef and PrecursorMZ are read only from
the top-level <feature>'s direct children, which is where the pinned writer puts
them (PeptideRef is the transition-group id; PrecursorMZ is set by
MRMFeatureFinderScoring::prepareScoredFeatureOutput_ via
setMetaValue("PrecursorMZ", ...)). Values nested on a subordinate are never
treated as authoritative: a nested PeptideRef leaves the feature unanchored
(which fails closed under the default), and a nested PrecursorMZ is not read.

The join is fail-loud: a feature whose canonical transition native_id has no
entry in the library, whose transition belongs to a different peptide than the
feature's anchor, that resolves no MS2 transition at all, or whose
sequence/precursor m/z contradicts that peptide raises OpenSwathMetricsError
rather than being counted, so a run can never silently inflate, shift or fake
its own identification counts.

Stdlib only, matching the rest of the benchmarking repo.
"""

import math
import re
import xml.etree.ElementTree as ET

# TraML is a namespaced PSI-MS format; featureXML is not. _local() strips any
# namespace so both documents are walked by plain local tag name.
TRAML_NS = "http://psi.hupo.org/ms/traml"

# library_coverage is a ratio, rounded like the runner's other ratio/sum
# scalars (overall_quality_sum rounds to 6 decimals).
COVERAGE_DECIMALS = 6

# Absolute tolerance, in Da, for the REDUNDANT PrecursorMZ consistency check.
# Upstream writes PrecursorMZ from the same in-memory value as the library's
# isolation-window target m/z (MRMFeatureFinderScoring.cpp), so a matched pair
# differs only by decimal formatting - the committed fixture deviates by
# ~1.4e-14 (510.05 vs 510.050000000000011). 0.01 Da therefore tolerates
# formatting while still catching a feature attributed to an unrelated
# peptide, whose precursor is several Da away. It cannot separate isobaric
# peptides; the transition-membership and sequence checks do that work, which
# is why this check is documented as redundant rather than decisive.
#
# Two limits are deliberate:
#
# - The comparison is theoretical-vs-theoretical. It models the MS2 feature path
#   (PrecursorMZ := the detection group's library precursor m/z). With
#   MRMFeatureFinderScoring::scorePeakgroups(..., ms1only=true) - reachable via
#   pyOpenMS/the library API, NOT via the OpenSwathWorkflow tool, which never
#   sets it - PrecursorMZ is instead the MEASURED apex m/z of the MS1 feature
#   (selected_feature.getMZ()). This tolerance does not model that mode; a
#   benchmark enabling it would need a ppm-based redefinition, not a wider
#   absolute window.
# - Only the supported TraML term is read ("isolation window target m/z",
#   MS:1000827), matching TraMLHandler.cpp, which maps no other precursor term.
#   A transition without it contributes NO precursor identity (never a fake
#   "(missing, charge)" one), and a feature that still supplies PrecursorMZ is
#   refused rather than accepted unvalidated: see _check_precursor_mz.
PRECURSOR_MZ_TOLERANCE = 0.01

# MS1 precursor-extraction native_id. The prefix is the canonical PRECURSOR id
# (a decimal string, because canonical ids are positional integers).
_MS1_PRECURSOR_NATIVE_ID = re.compile(r"^(?P<prefix>\d+)_Precursor_i\d+$")

# A sequence that is plain residues only. Modified/annotated sequences (OpenMS
# AASequence::toString writes them as e.g. "PEPTIDEK(Phospho)") are not
# comparable with a TraML @sequence attribute, which carries residues only.
_PLAIN_SEQUENCE = re.compile(r"^[A-Za-z]+$")

# library_peptides_identifiable is the base of library_coverage. A TraML
# peptide with no transitions is still declared in the library (it counts
# towards library_peptides, matching upstream TargetedExperiment::getSummary())
# but it has no precursor, so no transition, so no native_id a feature could
# carry: it can never be identified. Dividing by such an entry would cap
# coverage at a ceiling fixed by library construction rather than by the tool,
# so it is excluded from the ratio and reported separately instead.


class OpenSwathMetricsError(Exception):
    """Raised for a structurally unusable TraML/featureXML, or a feature whose
    transition assignment cannot be validated against the library."""


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


def _subordinate_native_ids(feature, level):
    """native_id values of the feature's subordinates at the given level.

    MS2 (the default join) also keeps a subordinate that declares no
    FeatureLevel at all: dropping it would make a library that never writes the
    field silently report zero identifications instead of failing loudly, which
    is a worse failure than joining it. MS1 (used only for the anchor fallback)
    requires the level explicitly, so a stray id is never promoted to an anchor.
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
        declared = None if enclosing is None else _cv(enclosing, "FeatureLevel")
        if level == "MS2":
            if declared is not None and declared != "MS2":
                continue
        elif declared != level:
            continue
        native_ids.append(up.get("value"))
    return native_ids


def _feature_native_ids(feature):
    """The transition ids a picked feature is built from.

    OpenSwath writes them as <UserParam name="native_id"> on the feature's
    subordinate features; iterating the whole subtree also covers a library
    that puts them on the top-level feature. The values are CANONICAL ids
    (zero-based TraML document positions), not TraML source ids - see the
    module docstring. Resolving them is count_identifications()'s job.
    """
    return _subordinate_native_ids(feature, "MS2")


def _feature_meta(feature, name):
    """Value of a feature-level userParam, e.g. PeptideRef or PrecursorMZ.

    Read from the top-level <feature>'s direct children only. The pinned writer
    puts both there (PeptideRef = the transition-group id; PrecursorMZ set in
    MRMFeatureFinderScoring::prepareScoredFeatureOutput_), and a subordinate's
    value is never authoritative - scanning the subtree could silently adopt a
    nested value that no supported writer produces. A nested PeptideRef leaves
    the feature unanchored (fail-closed by default); a nested PrecursorMZ is
    simply not read.
    """
    for child in feature:
        if (_local(child).lower() in ("userparam", "cvparam")
                and child.get("name") == name):
            return child.get("value")
    return None


def _hit_sequences(feature):
    """Distinct non-empty PeptideHit sequences, in document order."""
    sequences = []
    for element in feature.iter():
        if _local(element) == "PeptideHit":
            sequence = element.get("sequence")
            if sequence and sequence not in sequences:
                sequences.append(sequence)
    return sequences


def parse_traml(path):
    """Parse a TraML spectral library.

    Returns a dict with:
        proteins    set of TraML Protein ids
        peptides    {peptide_id: {"sequence": str|None, "modified": bool,
                                  "charge": str|None,
                                  "proteins": set of Protein ids}}
        compounds   {compound_id: {"charge": str|None}} - small molecules,
                    never counted as peptides
        precursor_ids
                    {peptide/compound id -> canonical precursor id}
        precursor_canonical_to_id
                    {canonical precursor id -> peptide/compound id}
        transitions {canonical transition id -> {"source_id": str|None,
                                                 "peptide": peptide_id|None,
                                                 "compound": compound_id|None,
                                                 "precursor": (mz, charge)}},
                    ONE RECORD PER <Transition> ELEMENT in document order.
        transition_canonical_to_source
                    {canonical id -> source id}, for diagnostics/messages.
        library_peptides / library_transitions / library_transitions_total /
        library_compound_transitions / library_precursor_mz /
        library_proteins   sizes

    Duplicate <Transition id> values are accepted: the canonical position, not
    the source label, is the key, so two transitions sharing a label stay two
    distinct records with their own peptideRef. library_transitions counts
    USABLE transition elements (those resolving to a declared <Peptide>);
    library_transitions_total counts every element, and
    library_compound_transitions the compound-backed ones, which consume a
    canonical position but never enter a peptide metric - so library_peptides,
    library_precursor_mz and library_coverage all ignore them.

    Duplicate PE / COMPOUND declarations are rejected rather than merged: both
    ids are xs:ID, the upstream normalizer throws on duplicates, and the
    canonical precursor ids are assigned over the declared set - so a silent
    last-one-wins overwrite would both hide an invalid library and change the
    sequence a feature is validated against.

    A peptide with no transitions still counts towards library_peptides (it is
    in the library) but contributes no precursor and can never be identified.

    A non-finite precursor m/z (NaN, +/-infinity) is rejected as an invalid
    library: it would otherwise enter the precursor identity count and, worse,
    disable the per-feature PrecursorMZ consistency check, since every
    comparison against NaN is False.
    """
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise OpenSwathMetricsError("cannot parse TraML %s: %s" % (path, exc))

    proteins = set()
    peptides = {}
    compounds = {}
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
                if pid in peptides:
                    raise OpenSwathMetricsError(
                        "TraML %s declares <Peptide id=%r> more than once; "
                        "peptide ids are xs:ID and the canonical precursor "
                        "namespace is undefined for a duplicate" % (path, pid))
                peptides[pid] = {
                    "sequence": element.get("sequence"),
                    # TraML keeps modifications as separate elements while the
                    # @sequence attribute stays residue-only, so this flag
                    # decides whether a featureXML AASequence is comparable.
                    "modified": any(_local(c) == "Modification"
                                     for c in element),
                    "charge": _cv(element, "charge state"),
                    "proteins": {c.get("ref") for c in element
                                  if _local(c) == "ProteinRef" and c.get("ref")},
                }
        elif tag == "Compound":
            cid = element.get("id")
            if cid:
                if cid in compounds:
                    raise OpenSwathMetricsError(
                        "TraML %s declares <Compound id=%r> more than once; "
                        "compound ids are xs:ID and the upstream normalizer "
                        "rejects duplicates" % (path, cid))
                compounds[cid] = {"charge": _cv(element, "charge state")}
        elif tag == "Transition":
            canonical_id = str(transition_index)
            transition_index += 1
            source_id = element.get("id")
            if source_id is not None:
                # Many-to-one is legal: duplicate labels are allowed upstream.
                canonical_to_source[canonical_id] = source_id
            peptide_ref = element.get("peptideRef")
            compound_ref = element.get("compoundRef")
            precursor_mz = None
            for child in element:
                if _local(child) == "Precursor":
                    precursor_mz = _cv(child, "isolation window target m/z")
            if precursor_mz is not None:
                try:
                    numeric_mz = float(precursor_mz)
                except ValueError:
                    numeric_mz = None  # non-numeric; never a checkable value
                if numeric_mz is not None and not math.isfinite(numeric_mz):
                    raise OpenSwathMetricsError(
                        "TraML %s transition %r declares a non-finite precursor "
                        "m/z %r (isolation window target m/z); refusing an "
                        "invalid library" % (path, source_id, precursor_mz))
            # Only refs that resolve to a DECLARED entry are usable; an
            # unknown ref keeps its position but can never be counted.
            peptide_id = peptide_ref if peptide_ref in peptides else None
            compound_id = None
            if peptide_id is None and compound_ref in compounds:
                compound_id = compound_ref
            entry = peptides.get(peptide_id) or compounds.get(compound_id) or {}
            transitions[canonical_id] = {
                "source_id": source_id,
                "peptide": peptide_id,
                "compound": compound_id,
                "precursor": (precursor_mz, entry.get("charge")),
            }

    # Peptide and Compound ids are both xs:ID, so a valid TraML cannot declare
    # the same id twice; the canonical precursor namespace depends on it.
    overlap = set(peptides) & set(compounds)
    if overlap:
        raise OpenSwathMetricsError(
            "TraML %s declares id %r as both <Peptide> and <Compound>; the "
            "canonical precursor namespace requires one id per entry"
            % (path, min(overlap)))

    # Upstream sorts the union of ALL declared compounds (TraML <Peptide> and
    # <Compound> are converted into one list) and numbers it from zero, so
    # declaration order is irrelevant, unreferenced entries still reserve
    # their slot, and a compound shifts every peptide id after it.
    declared_precursors = sorted(set(peptides) | set(compounds))
    precursor_ids = {entry_id: str(i)
                     for i, entry_id in enumerate(declared_precursors)}
    precursor_canonical_to_id = {canonical: entry_id
                                 for entry_id, canonical in precursor_ids.items()}

    usable = {canonical: transition for canonical, transition
              in transitions.items() if transition["peptide"] is not None}
    # Only a supported precursor m/z creates a precursor identity: a transition
    # without the term must not collapse several peptides into one
    # "(missing, charge)" pseudo-precursor.
    precursors = {transition["precursor"] for transition in usable.values()
                  if transition["precursor"][0] is not None}
    return {
        "proteins": proteins,
        "peptides": peptides,
        "compounds": compounds,
        "precursor_ids": precursor_ids,
        "precursor_canonical_to_id": precursor_canonical_to_id,
        "transitions": transitions,
        "transition_canonical_to_source": canonical_to_source,
        "library_peptides": len(peptides),
        "library_precursor_mz": len(precursors),
        "library_transitions": len(usable),
        "library_transitions_total": len(transitions),
        "library_compound_transitions": sum(
            1 for transition in transitions.values()
            if transition["compound"] is not None),
        "library_proteins": len(proteins),
    }


def _resolve_transition(library, feature_id, native_id):
    """The library transition a feature's MS2 native_id denotes, or raise.

    `native_id` is a canonical id; it is never used as a source id and never
    arithmetic-converted (source ids are opaque and may collide with canonical
    ones). Compound-backed and unusable transitions are rejected with their own
    message so an unsupported assignment is reported, never silently counted.
    """
    transition = library["transitions"].get(native_id)
    if transition is None:
        raise OpenSwathMetricsError(
            "feature %s references canonical transition native_id %r, which "
            "the library does not define" % (feature_id, native_id))
    if transition["compound"] is not None:
        raise OpenSwathMetricsError(
            "feature %s references canonical transition native_id %r (source "
            "id %r), which is compound-backed (%r); compound assignments are "
            "outside the peptide identification metrics"
            % (feature_id, native_id, transition["source_id"],
               transition["compound"]))
    if transition["peptide"] is None:
        raise OpenSwathMetricsError(
            "feature %s references canonical transition native_id %r (source "
            "id %r), which resolves to no declared TraML <Peptide>"
            % (feature_id, native_id, transition["source_id"]))
    return transition


def _check_precursor_mz(feature, feature_id, peptide_id, expected_mz, tolerance):
    """Redundant consistency check: PrecursorMZ must match the peptide's Q1.

    Upstream writes PrecursorMZ from the detection transition group's first
    transition, i.e. the same library value passed in as `expected_mz`, so a
    mismatched featureXML/library pair is a real inconsistency. Because the
    value is a projection of the anchor it cannot prove the library matches; it
    only catches a feature record glued to the wrong peptide.

    `expected_mz` is the precomputed list of supported library precursor m/z
    values for the anchored peptide (so no per-feature rescan of the library).

    When the feature supplies PrecursorMZ but the library defines no supported
    precursor m/z (TraML <Precursor> "isolation window target m/z", MS:1000827)
    for that peptide, the supplied value cannot be validated: it is REFUSED
    rather than silently accepted, because "could not check" must not read as
    "checked out". A feature that supplies no PrecursorMZ is unaffected.

    See PRECURSOR_MZ_TOLERANCE for the theoretical-vs-measured limit: this
    comparison does not model ms1only=true, which the OpenSwathWorkflow tool
    never enables.
    """
    declared = _feature_meta(feature, "PrecursorMZ")
    if declared is None:
        return
    try:
        declared_mz = float(declared)
    except ValueError:
        raise OpenSwathMetricsError(
            "feature %s declares PrecursorMZ %r, which is not a number"
            % (feature_id, declared))
    if declared_mz != declared_mz:  # NaN would defeat the comparison below
        raise OpenSwathMetricsError(
            "feature %s declares a NaN PrecursorMZ" % (feature_id,))
    if not expected_mz:
        raise OpenSwathMetricsError(
            "feature %s declares PrecursorMZ %r, but the library defines no "
            "supported precursor m/z (TraML <Precursor> 'isolation window "
            "target m/z') for peptide %r, so the declared value cannot be "
            "validated; refusing to accept an unverifiable precursor m/z"
            % (feature_id, declared, peptide_id))
    if min(abs(declared_mz - mz) for mz in expected_mz) > tolerance:
        raise OpenSwathMetricsError(
            "feature %s declares PrecursorMZ %r, but peptide %r has no library "
            "precursor m/z within %g Da (%s); the library does not match this "
            "featureXML"
            % (feature_id, declared, peptide_id, tolerance,
               ", ".join("%.6f" % mz for mz in expected_mz)))


def _check_sequence(feature, feature_id, peptide_id, peptide):
    """Assay-peptide sequence check; returns True when hits are ambiguous.

    Upstream builds the feature's PeptideIdentification from the assay peptide
    itself (MRMFeatureFinderScoring::prepareScoredFeatureOutput_ copies
    pep.sequence into the single PeptideHit), so a sequence that disagrees with
    the anchor means the featureXML and the library are not a matched pair.

    VALIDATION IS PER HIT, never per feature: the hit sequences are partitioned
    into plain-residue and annotated ones, and the comparison uses only the
    plain subset. An annotated hit (e.g. "PEPTIDEK(Phospho)") is never compared
    - a TraML @sequence carries residues only - but it must not switch off
    validation of the plain hits beside it. Whenever at least one plain hit
    exists and the library side is comparable, the anchored sequence must be
    among the plain hits or this raises.

    The library side is comparable only when the peptide declares no
    <Modification> child and its @sequence is itself plain residues. Otherwise
    no comparison is possible and only ambiguity is reported. Ambiguity counts
    ALL distinct non-empty hit sequences - plain and annotated - because it
    describes the candidate list, not what could be compared.

    Skipping a comparison never skips the transition-membership validation
    around it, and a modified or annotated peptide elsewhere in the library
    never disables this peptide's comparison.
    """
    sequences = _hit_sequences(feature)
    if not sequences:
        return False
    ambiguous = len(sequences) > 1

    library_sequence = peptide.get("sequence")
    if not library_sequence or peptide.get("modified"):
        return ambiguous
    if not _PLAIN_SEQUENCE.match(library_sequence):
        # The library sequence itself is annotated, so residue-only hits are
        # not directly comparable with it.
        return ambiguous

    plain = [sequence for sequence in sequences
             if _PLAIN_SEQUENCE.match(sequence)]
    if not plain:
        return ambiguous
    if library_sequence not in plain:
        raise OpenSwathMetricsError(
            "feature %s is anchored to peptide %r (library sequence %r) but "
            "its plain PeptideHit sequence(s) %s do not include it (all hits: "
            "%s); the library does not match this featureXML"
            % (feature_id, peptide_id, library_sequence,
               ", ".join(repr(sequence) for sequence in plain),
               ", ".join(repr(sequence) for sequence in sequences)))
    # Several distinct candidates: the anchor decides, and the caller records
    # that the feature was ambiguous instead of silently picking a sequence.
    return ambiguous


def count_identifications(library, featurexml_path, require_anchor=True,
                          precursor_mz_tolerance=PRECURSOR_MZ_TOLERANCE):
    """Join a featureXML against a parsed library and return the counts.

    Returns (all integers unless noted):
        library_peptides, library_peptides_identifiable,
        library_peptides_orphan, library_precursor_mz, library_transitions,
        library_transitions_total, library_compound_transitions,
        identified_peptides, identified_precursor_mz, identified_proteins,
        transitions_used, library_coverage (float), assignment_evidence,
        features_without_anchor, ambiguous_hit_features

    library_peptides stays the full declared count so an orphan library entry
    stays visible; library_coverage is divided by the identifiable subset
    only, because an orphan is structurally incapable of being identified and
    would otherwise impose a ceiling no tool result can exceed.

    Each feature is anchored by its canonical PRECURSOR id - PeptideRef, or the
    MS1 precursor prefix of a native_id when PeptideRef is absent - and its MS2
    native_ids are then required to be transitions of that anchored peptide.
    This makes the join order-independent and self-checking: a permuted or
    stale library fails with the offending id named rather than shifting every
    assignment onto a neighbour. Only MS2-level subordinates take part in the
    join; MS1 precursor subordinates serve only as the anchor fallback.

    `require_anchor=True` (default) fails closed: a feature with no anchor, or
    a file mixing anchored and unanchored features, raises instead of returning
    counts nothing vouches for. `require_anchor=False` is the explicit
    legacy/test opt-out for a purely positional join; the result is then
    stamped assignment_evidence="positional" (or "mixed" when some features
    were anchored) and must not be treated as a validated result.

    Precursor identities (library_precursor_mz / identified_precursor_mz) count
    distinct (precursor m/z, charge) tuples. A transition without a supported
    precursor m/z contributes NO identity, so an unsupported library cannot
    collapse all peptides onto one pseudo-precursor; identified_precursor_mz
    counts the tuples of the transitions a feature actually used, not every
    window its identified peptides happen to declare. Consistently, a feature
    that supplies PrecursorMZ while its anchored peptide has no supported
    library precursor m/z is refused: an unverifiable value is not accepted.

    Raises OpenSwathMetricsError if the featureXML cannot be parsed, an anchor
    cannot be resolved, a feature references a transition the library does not
    define or one belonging to another peptide, an anchored feature resolves
    no MS2 transition of its peptide at all, an identification contradicts the
    anchored sequence, or PrecursorMZ contradicts (or cannot be checked
    against) the anchored peptide.
    """
    try:
        root = ET.parse(featurexml_path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise OpenSwathMetricsError(
            "cannot parse featureXML %s: %s" % (featurexml_path, exc))

    feature_list = root.find("featureList")
    features = [] if feature_list is None else feature_list.findall("feature")

    transitions = library["transitions"]
    precursor_of = library["precursor_canonical_to_id"]
    peptides = library["peptides"]
    compounds = library["compounds"]

    # Supported library precursor m/z per peptide, parsed once: the per-feature
    # consistency check must not rescan every transition. A transition without
    # the supported term contributes nothing here; a non-numeric value is
    # skipped as well (so a declared PrecursorMZ for it is refused as
    # unverifiable), and non-finite values were already rejected by
    # parse_traml.
    precursor_mz_by_peptide = {}
    for transition in transitions.values():
        peptide_id = transition["peptide"]
        if peptide_id is None:
            continue
        mz = transition["precursor"][0]
        if mz is None:
            continue
        try:
            value = float(mz)
        except ValueError:
            continue
        precursor_mz_by_peptide.setdefault(peptide_id, []).append(value)

    identified_peptides = set()
    identified_precursor_mz = set()
    identified_proteins = set()
    transitions_used = set()
    features_without_anchor = 0
    anchored_features = 0
    ambiguous_hit_features = 0

    for feature in features:
        feature_id = feature.get("id")
        native_ids = _feature_native_ids(feature)

        peptide_ref = _feature_meta(feature, "PeptideRef")
        ms1_prefixes = {match.group("prefix") for match in
                        (_MS1_PRECURSOR_NATIVE_ID.match(value)
                         for value in _subordinate_native_ids(feature, "MS1"))
                        if match}
        if len(ms1_prefixes) > 1:
            raise OpenSwathMetricsError(
                "feature %s declares inconsistent MS1 precursor ids %s; a "
                "single canonical precursor anchor is required"
                % (feature_id, sorted(ms1_prefixes)))
        ms1_prefix = next(iter(ms1_prefixes), None)
        # PeptideRef and the MS1 prefix are two encodings of the same upstream
        # transition-group id, so they must agree; agreement is consistency,
        # not independent proof of the library mapping.
        if (peptide_ref is not None and ms1_prefix is not None
                and peptide_ref != ms1_prefix):
            raise OpenSwathMetricsError(
                "feature %s has PeptideRef %r but MS1 precursor id %r; the "
                "feature's two encodings of the assay identity disagree"
                % (feature_id, peptide_ref, ms1_prefix))
        anchor = peptide_ref if peptide_ref is not None else ms1_prefix

        if anchor is None:
            features_without_anchor += 1
            if require_anchor:
                raise OpenSwathMetricsError(
                    "feature %s carries no canonical precursor anchor (no "
                    "PeptideRef and no MS1 precursor native_id), so its "
                    "transition assignment cannot be validated; pass "
                    "require_anchor=False only for an explicit positional "
                    "(unvalidated) join" % (feature_id,))
            # Explicit opt-out: count positionally and let the evidence stamp
            # below make the missing validation visible to every consumer.
            for native_id in native_ids:
                transition = _resolve_transition(library, feature_id, native_id)
                transitions_used.add(native_id)
                identified_peptides.add(transition["peptide"])
                # Only a transition actually used contributes its precursor. A
                # transition without a supported m/z contributes no identity.
                if transition["precursor"][0] is not None:
                    identified_precursor_mz.add(transition["precursor"])
                identified_proteins.update(
                    peptides[transition["peptide"]]["proteins"])
            continue

        anchored_features += 1
        peptide_id = precursor_of.get(anchor)
        if peptide_id is None:
            raise OpenSwathMetricsError(
                "feature %s has canonical precursor anchor %r, which is not a "
                "declared peptide/compound id of the library (its peptide or "
                "compound set differs)" % (feature_id, anchor))
        if peptide_id in compounds:
            raise OpenSwathMetricsError(
                "feature %s is anchored to compound %r (canonical precursor "
                "%r); compound-backed assignments are outside the peptide "
                "identification metrics" % (feature_id, peptide_id, anchor))

        resolved = 0
        for native_id in native_ids:
            transition = _resolve_transition(library, feature_id, native_id)
            if transition["peptide"] != peptide_id:
                raise OpenSwathMetricsError(
                    "feature %s is anchored to peptide %r (canonical precursor "
                    "%r) but transition native_id %r (source id %r) belongs "
                    "to peptide %r; refusing to attribute it"
                    % (feature_id, peptide_id, anchor, native_id,
                       transition["source_id"], transition["peptide"]))
            transitions_used.add(native_id)
            if transition["precursor"][0] is not None:
                identified_precursor_mz.add(transition["precursor"])
            resolved += 1

        _check_precursor_mz(feature, feature_id, peptide_id,
                            precursor_mz_by_peptide.get(peptide_id, []),
                            precursor_mz_tolerance)
        if _check_sequence(feature, feature_id, peptide_id,
                           peptides[peptide_id]):
            ambiguous_hit_features += 1

        if not resolved:
            # An anchor establishes WHICH peptide the feature should belong to,
            # never that any transition was attributed to it. A feature that
            # resolved nothing would otherwise pass the join (and a
            # feature-count gate) while contributing zero identifications, so
            # it is refused per feature rather than counted as an empty
            # identification.
            raise OpenSwathMetricsError(
                "feature %s is anchored to peptide %r (canonical precursor "
                "%r) but carries no usable MS2 transition native_id belonging "
                "to it, so no identification can be attributed to the feature; "
                "every anchored feature must resolve at least one of its "
                "peptide's MS2 transitions"
                % (feature_id, peptide_id, anchor))
        # Counted through the anchor, so a feature carrying another peptide's
        # subordinate ids can never inflate this peptide's score. Precursor
        # identities were added per USED transition above, so a peptide with
        # several windows reports only the windows reached.
        identified_peptides.add(peptide_id)
        identified_proteins.update(peptides[peptide_id]["proteins"])

    library_peptides = library["library_peptides"]
    # Identifiable = declared peptides that at least one transition references.
    # Intersecting with the declared set keeps a transition pointing at an
    # undeclared peptide out of the denominator; the join above still fails
    # loudly if a feature reaches such a peptide.
    identifiable = {transition["peptide"] for transition in transitions.values()
                    if transition["peptide"] in peptides}
    identifiable_count = len(identifiable)
    coverage = (len(identified_peptides) / identifiable_count
                if identifiable_count else 0.0)

    if not features:
        assignment_evidence = "none"
    elif anchored_features == len(features):
        assignment_evidence = "anchor"
    elif anchored_features:
        assignment_evidence = "mixed"
    else:
        assignment_evidence = "positional"

    return {
        "library_peptides": library_peptides,
        "library_peptides_identifiable": identifiable_count,
        "library_peptides_orphan": library_peptides - identifiable_count,
        "library_precursor_mz": library["library_precursor_mz"],
        "library_transitions": library["library_transitions"],
        "library_transitions_total": library["library_transitions_total"],
        "library_compound_transitions": library["library_compound_transitions"],
        "identified_peptides": len(identified_peptides),
        "identified_precursor_mz": len(identified_precursor_mz),
        "identified_proteins": len(identified_proteins),
        "transitions_used": len(transitions_used),
        "library_coverage": round(coverage, COVERAGE_DECIMALS),
        "assignment_evidence": assignment_evidence,
        "features_without_anchor": features_without_anchor,
        "ambiguous_hit_features": ambiguous_hit_features,
    }
