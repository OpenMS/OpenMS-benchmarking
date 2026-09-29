#!/usr/bin/env python3
"""PSM metrics for the ProSE + PeptDeep entrapment benchmark.

Pure extraction module, no output policy: parse one idXML and return the
PSM records and headline counts used by the benchmark. The runner
(run_prose_benchmark.sh) and the tests both use this module.

Definitions used here (established in PROSE_PEPTDEEP_DESIGN.md):

- PSM   the single (top) hit of each PeptideIdentification element. ProSE
        reports report:top_hits=1 and the Percolator stage runs
        keepNBestHits(1), so every PeptideIdentification in the ProSE output
        carries exactly one hit; the parser asserts that.
- q     the hit's q-value: the main score when the run's score_type is
        "q-value" (what ProSE+Percolator produce), otherwise the
        percolator_q_value UserParam.
- target hit whose target_decoy UserParam is exactly "target" AND none of its
        protein accessions start with the entrapment prefix. Anything else
        (decoy, target+decoy, entrapment, entrapment-shared) is not counted
        as a target: shared peptides conservatively count as entrapment.
- peptide  the hit's OpenMS sequence notation (modifications embedded, e.g.
        "M(Oxidation)PEPTIDE"), counted once per distinct value.

Stdlib only, matching the rest of the benchmarking repo.
"""

import xml.etree.ElementTree as ET

Q_THRESHOLD = 0.01

# The five PeptDeep rescoring features registered in extra_features by
# PR #9975 (OpenMS Constants::UserParam). Internal Percolator features --
# never headline metrics -- but the peptdeep arm must produce them, or the
# arm silently degraded to a baseline run (e.g. a build without ONNX).
PEPTDEEP_FEATURES = (
    "ms2_cosine",
    "ms2_spectral_angle",
    "ms2_pearson",
    "ms2_frac_pred_found",
    "rt_abs_error",
)


class ProseMetricsError(Exception):
    """Raised for structurally unusable idXML or violated expectations."""


def _hit_q(hit, score_type):
    """The hit's q-value.

    ProSE+Percolator output uses score_type="q-value" with the q-value as
    the main score; a fallback reads the percolator_q_value UserParam.
    """
    if score_type == "q-value":
        return float(hit.get("score"))
    qv = hit.find('UserParam[@name="percolator_q_value"]')
    if qv is None:
        raise ProseMetricsError(
            'no q-value: run score_type is %r and the hit has no '
            '"percolator_q_value" UserParam' % score_type)
    return float(qv.get("value"))


def _hit_accessions(hit, protein_id_to_accession):
    refs = (hit.get("protein_refs") or "").split()
    # idXML stores ProteinHit ids like PH_3; map id -> accession.
    return [protein_id_to_accession.get(r, r) for r in refs]


def parse_idxml(path, entrapment_prefix):
    """Parse one ProSE idXML into PSM records.

    Returns a dict with the protein accession map, the raw PSM records, and
    the run-level score_type. A PSM record is:

        {"q": float, "target_decoy": str, "sequence": str,
         "entrapment": bool, "target": bool}

    Classification policy: a PSM is entrapment if ANY mapped accession has
    the entrapment prefix; it is a target only if target_decoy == "target"
    AND it is not entrapment. Shared target/entrapment peptides therefore
    count as entrapment (conservative, see PROSE_PEPTDEEP_DESIGN.md 4/5).
    """
    tree = ET.parse(path)
    root = tree.getroot()

    protein_id_to_accession = {}
    score_type = None
    psms = []

    for prot_run in root.iter("ProteinIdentification"):
        for phit in prot_run.iter("ProteinHit"):
            protein_id_to_accession[phit.get("id")] = phit.get("accession")

    for pid in root.iter("PeptideIdentification"):
        st = pid.get("score_type")
        if score_type is None:
            score_type = st
        elif st != score_type:
            raise ProseMetricsError(
                "mixed score_types in one idXML: %r vs %r" % (score_type, st))
        hits = pid.findall("PeptideHit")
        if len(hits) != 1:
            raise ProseMetricsError(
                "expected exactly 1 hit per PeptideIdentification "
                "(ProSE top_hits=1), found %d" % len(hits))
        hit = hits[0]
        decoy = hit.find('UserParam[@name="target_decoy"]')
        accessions = _hit_accessions(hit, protein_id_to_accession)
        entrapment = any(a.startswith(entrapment_prefix) for a in accessions)
        psms.append({
            "q": _hit_q(hit, st),
            "target_decoy": decoy.get("value") if decoy is not None else "",
            "sequence": hit.get("sequence") or "",
            "entrapment": entrapment,
            "target": decoy is not None and decoy.get("value") == "target"
                      and not entrapment,
        })

    if score_type is None:
        raise ProseMetricsError("no PeptideIdentification elements in %s" % path)
    return {"score_type": score_type, "psms": psms}


def count_psms(parsed, q_threshold=Q_THRESHOLD):
    """Headline counts from parse_idxml() output.

    target_psms_at_1pct_fdr / target_peptides_at_1pct_fdr are the headline
    metrics; the entrapment_* counts are diagnostics (the target:entrapment
    ratio is the entrapment false-discovery proxy). A PSM is kept at
    q <= threshold (inclusive; ties at exactly the threshold count).
    Peptides are the distinct hit sequence notations among kept PSMs of the
    respective class.
    """
    kept = [p for p in parsed["psms"] if p["q"] <= q_threshold]
    targets = [p for p in kept if p["target"]]
    entrap = [p for p in kept if p["entrapment"]]
    return {
        "q_threshold": q_threshold,
        "psms_total": len(parsed["psms"]),
        "target_psms_at_1pct_fdr": len(targets),
        "target_peptides_at_1pct_fdr": len({p["sequence"] for p in targets}),
        "entrapment_psms_at_1pct_fdr": len(entrap),
        "entrapment_peptides_at_1pct_fdr": len({p["sequence"] for p in entrap}),
    }


def extract_extra_features(path):
    """The SearchParameters extra_features list of one idXML (or [])."""
    root = ET.parse(path).getroot()
    sp = root.find("SearchParameters")
    if sp is None:
        return []
    up = sp.find('UserParam[@name="extra_features"]')
    if up is None:
        return []
    return [f for f in (up.get("value") or "").split(",") if f]


def check_peptdeep_features(path, extra_features=None):
    """Assert the five PeptDeep features landed in the idXML.

    Returns the list of missing features; empty means the arm ran PeptDeep.
    Checks the extra_features registration AND (when hits carry UserParams)
    that at least one hit actually has each value -- a registered but never
    written feature would otherwise pass silently.
    """
    features = extra_features if extra_features is not None \
        else extract_extra_features(path)
    missing = [f for f in PEPTDEEP_FEATURES if f not in features]
    if missing:
        return missing
    root = ET.parse(path).getroot()
    hit_users = set()
    for hit in root.iter("PeptideHit"):
        for up in hit.findall("UserParam"):
            hit_users.add(up.get("name"))
    return [f for f in PEPTDEEP_FEATURES if f not in hit_users]
