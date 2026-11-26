#!/usr/bin/env python3
"""Assemble the PXD028735 (ProteoBench HYE) target + entrapment FASTA.

target:     ProteoBench mixed-species HYE FASTA, entries verbatim
            (human + yeast + E. coli + Cont_ contaminants - the sample's true
            protein contents)
entrapment: S. cellulosum So ce56 targets ONLY, from the OpenMS in-tree
            doc/pyopenms/src/data/18Protein_SoCe_Tr_detergents_trace.fasta.
            That source has 9,439 entries: 9,320 S. cellulosum So ce56
            proteins and 119 contaminant/foreign entries. 104 of the 119 are
            bare ACC|NAME headers with no OS= annotation at all (human
            keratins, trypsins, BSA, ...); the rest carry a different OS=.
            Those 119 are excluded for one reason only: they are not
            annotated as the exact So ce56 organism - bare ACC|NAME headers
            with no OS=, or a different OS= - so they cannot serve as
            entrapment proteins. A separate audit of that source against the
            HYE target FASTA found 39 accession, 37 protein-name and 29
            exact-sequence matches: evidence that excluded entries would have
            collided with targets, not a claim that each of the 119 overlaps
            in all three ways. Only headers whose OS= annotation is exactly
            "Sorangium cellulosum (strain So ce56)" are kept. Source order
            and sequences are preserved verbatim; accessions are prefixed
            with ENTRAPMENT_; the organism is provably absent from the sample.

Decoy handling: fail closed. The target_decoy variant of the SoCe file marks
decoys with a _rev SUFFIX (e.g. ACCA_SORC5_rev), which a rev_-prefix check
misses - copying it would silently put reversed sequences into the
entrapment set (the Phase-2a pilot did exactly that). This generator refuses
any source carrying decoy markers (rev_/DECOY_/REV_/decoy_ prefixes or
_rev/_REV identifier suffixes) and points at the targets-only file.
ProSE generates decoys itself (-Search:decoys auto).

Overlap handling: fail closed. Every retained entry is checked against the
HYE target entries by normalized accession, protein name and exact sequence.
Zero overlap is required; any hit aborts generation instead of silently
producing an entrapment set that shares target proteins. Accession
normalization strips sp|/tr| database prefixes, a leading Cont_ (ProteoBench
contaminant marker) and takes the accession field of a bare ACC|NAME header;
full-header equality is never relied on.

Shared-peptide handling: unchanged conservative policy (ENTRAPMENT_ prefix
wins at classification time; shared tryptic peptides are counted as
entrapment). This script MEASURES the overlap under one fixed reference
definition (trypsin KR|P, 0-2 missed cleavages, 7-50 aa) and reports it, so
the number lands in PROVENANCE.txt instead of being asserted from memory.

That definition is a measurement convention only. It is ENGINE-INDEPENDENT:
it is not the search space of ProSE or Sage, none of its parameters is
passed to either engine, and the reported percentage is a property of the
two FASTA sets (how much of the entrapment peptide space could also be
produced by the target proteins), not of the search configuration.

Interpretation notes (documentation only - none of these is applied):
  - ProSE's actual defaults (trypsin KR|P, 7-40 aa, 1 missed cleavage,
    100-9000 Da) measure 716 of 477,176 = 0.150% on these same two sets.
  - Sage's DEFAULT digestion (trypsin KR|P, 5-50 aa, 1 missed cleavage, no
    mass window) measures 20,061 of 559,448 = 3.586%. That is Sage's shipped
    default, NOT the peptide space of the explicitly configured one-off Sage
    run used for the exploratory comparison; the two must not be quoted
    interchangeably.
  - Residue policy does not move the 7-50/0-2 number: X/B/Z are kept in the
    peptide space here, and dropping them leaves the shared count unchanged
    (no shared peptide contains them), so the same value is obtained under
    either residue convention.

Usage:
  make_hye_entrapment_fasta.py --hye HYE.fasta --soce SOCE.fasta
      --out OUT.fasta [--summary summary.txt] [--force]

--summary writes the measured counts as key=value lines (consumed by
prepare_pxd028735_inputs.sh for PROVENANCE.txt).

Deterministic: input order is preserved; regeneration is byte-identical.
"""
import argparse
import os
import re
import sys

DECOY_PREFIXES = ("rev_", "DECOY_", "REV_", "decoy_")
DECOY_SUFFIX_RE = re.compile(r"_rev(\s|$)|_REV(\s|$)")
ENTRAPMENT_PREFIX = "ENTRAPMENT_"
SO_ORGANISM = "Sorangium cellulosum (strain So ce56)"
DB_PREFIXES = ("sp|", "tr|")
CONTAMINANT_PREFIX = "Cont_"

# OS= value ends at the next UniProt field (GN=/PE=/SV=/OX=) or end of header.
ORGANISM_RE = re.compile(r"(?:^|\s)OS=(.*?)(?=\s+(?:GN|PE|SV|OX)=|$)")

# Reference peptide space for the shared-peptide measurement: a fixed
# convention, independent of ProSE/Sage settings. Changing these constants
# changes only the reported percentage, never the FASTA that is written.
PEPTIDE_MIN_LEN = 7
PEPTIDE_MAX_LEN = 50
PEPTIDE_MAX_MISSED_CLEAVAGES = 2
TRYPSIN_SPLIT_RE = re.compile(r"(?<=[KR])(?!P)")


def read_entries(path):
    entries = []
    name, seq = None, []
    with open(path) as fh:
        for line in fh:
            line = line.rstrip("\r\n")
            if line.startswith(">"):
                if name is not None:
                    entries.append((name, "".join(seq)))
                name, seq = line[1:], []
            elif line:
                seq.append(line)
    if name is not None:
        entries.append((name, "".join(seq)))
    return entries


def ident_of(name):
    """First whitespace token of a FASTA header (everything before the
    description)."""
    parts = name.split()
    return parts[0] if parts else name


def decoy_marker_count(entries):
    n = 0
    for name, _ in entries:
        ident = ident_of(name)
        if ident.startswith(DECOY_PREFIXES) or DECOY_SUFFIX_RE.search(ident):
            n += 1
    return n


def organism_of(name):
    """The OS= annotation, or None when the header has none."""
    match = ORGANISM_RE.search(name)
    return match.group(1).strip() if match else None


def split_accession_name(ident):
    """Normalized (accession, protein_name) for a database-style identifier.

    Handles sp|ACC|NAME, tr|ACC|NAME, bare ACC|NAME and plain ACC. A leading
    Cont_ (ProteoBench contaminant marker, e.g. sp|Cont_P00722|BGAL_ECOLI) is
    stripped from the accession so it compares equal to the same protein
    without the marker. Returns ("", "") only for an empty identifier.
    """
    token = ident
    for prefix in DB_PREFIXES:
        if token.startswith(prefix):
            token = token[len(prefix):]
            break
    if "|" in token:
        accession, protein_name = token.split("|", 1)
    else:
        accession, protein_name = token, ""
    if accession.startswith(CONTAMINANT_PREFIX):
        accession = accession[len(CONTAMINANT_PREFIX):]
    return accession, protein_name


def tryptic_peptides(seq):
    """Reference tryptic peptide space: KR|P cleavage, 0-2 missed cleavages,
    7-50 aa. A measurement convention, not any engine's search settings."""
    segments = [s for s in TRYPSIN_SPLIT_RE.split(seq) if s]
    peptides = set()
    for start in range(len(segments)):
        peptide = ""
        for end in range(start, min(start + PEPTIDE_MAX_MISSED_CLEAVAGES + 1,
                                   len(segments))):
            peptide += segments[end]
            if len(peptide) > PEPTIDE_MAX_LEN:
                break
            if len(peptide) >= PEPTIDE_MIN_LEN:
                peptides.add(peptide)
    return peptides


def describe_overlap(kind, values):
    examples = ", ".join(sorted(values)[:5])
    print(f"ERROR: {len(values)} entrapment {kind}(s) overlap the HYE target "
          f"FASTA (e.g. {examples}). The entrapment set must be S. cellulosum "
          "So ce56 only - check that the SoCe source is the pinned file.",
          file=sys.stderr)
    return 4


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hye", required=True, help="ProteoBench HYE FASTA (targets)")
    ap.add_argument(
        "--soce", required=True,
        help="in-tree S. cellulosum FASTA, targets only "
             "(doc/pyopenms/src/data/18Protein_SoCe_Tr_detergents_trace.fasta)")
    ap.add_argument("--out", required=True, help="combined output FASTA")
    ap.add_argument("--summary", help="write measured counts as key=value lines")
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing output")
    args = ap.parse_args()

    missing = [p for p in (args.hye, args.soce) if not os.path.isfile(p)]
    if missing:
        print("ERROR: missing input files: " + ", ".join(missing), file=sys.stderr)
        return 2
    if os.path.exists(args.out) and not args.force:
        print(f"ERROR: refusing to overwrite existing {args.out} (use --force)",
              file=sys.stderr)
        return 2

    hye = read_entries(args.hye)
    soce = read_entries(args.soce)
    if not hye or not soce:
        print("ERROR: empty input FASTA (hye=%d, soce=%d entries)"
              % (len(hye), len(soce)), file=sys.stderr)
        return 2

    # Fail closed on decoys: the targets-only source must carry none. The
    # _target_decoy variant (9,439 _rev-suffix decoys) must never be passed.
    for label, entries in (("HYE", hye), ("SoCe", soce)):
        n = decoy_marker_count(entries)
        if n:
            print(
                "ERROR: %s source contains %d decoy-marker entries "
                "(rev_/DECOY_ prefixes or _rev/_REV suffixes). Use the "
                "targets-only SoCe file "
                "doc/pyopenms/src/data/18Protein_SoCe_Tr_detergents_trace."
                "fasta; ProSE generates decoys itself (-Search:decoys auto)."
                % (label, n), file=sys.stderr)
            return 3

    # SoCe-only entrapment: keep entries whose OS= annotation is exactly the
    # absent organism, in source order. Contaminants and foreign proteins
    # (including bare ACC|NAME headers with no OS= field) are dropped.
    retained = []
    for name, seq in soce:
        if organism_of(name) == SO_ORGANISM:
            retained.append((name, seq))

    excluded = len(soce) - len(retained)
    if not retained:
        print(
            "ERROR: no S. cellulosum (strain So ce56) entries left after "
            f"filtering {len(soce)} source entries; the SoCe FASTA appears to "
            "contain only foreign/contaminant proteins.",
            file=sys.stderr)
        return 4

    # Fail closed if the retained set is not provably the expected organism.
    for name, _ in retained:
        if organism_of(name) != SO_ORGANISM:
            print("ERROR: retained entry lacks the expected So ce56 OS= "
                  f"annotation: {ident_of(name)}", file=sys.stderr)
            return 4

    # Collision checks against the HYE targets (accession/name/sequence).
    target_acc, target_names, target_seqs = set(), set(), set()
    for name, seq in hye:
        accession, protein_name = split_accession_name(ident_of(name))
        if accession:
            target_acc.add(accession)
        if protein_name:
            target_names.add(protein_name)
        if seq:
            target_seqs.add(seq)

    ent_acc, ent_names, ent_seqs = set(), set(), set()
    for name, seq in retained:
        accession, protein_name = split_accession_name(ident_of(name))
        if accession:
            ent_acc.add(accession)
        if protein_name:
            ent_names.add(protein_name)
        if seq:
            ent_seqs.add(seq)

    for kind, overlap in (
        ("accession", ent_acc & target_acc),
        ("protein name", ent_names & target_names),
        ("sequence", ent_seqs & target_seqs),
    ):
        if overlap:
            return describe_overlap(kind, overlap)

    # Fail closed on duplicate identifiers in the written FASTA: duplicates
    # make protein inference and entrapment classification ambiguous.
    seen, duplicates = set(), set()
    for name, _ in hye:
        ident = ident_of(name)
        if ident in seen:
            duplicates.add(ident)
        seen.add(ident)
    for name, _ in retained:
        ident = ENTRAPMENT_PREFIX + ident_of(name)
        if ident in seen:
            duplicates.add(ident)
        seen.add(ident)
    if duplicates:
        examples = ", ".join(sorted(duplicates)[:5])
        print(f"ERROR: {len(duplicates)} duplicate output identifier(s) "
              f"(e.g. {examples})", file=sys.stderr)
        return 4

    with open(args.out, "w") as out:
        for name, seq in hye:                                   # targets verbatim
            out.write(f">{name}\n{seq}\n")
        for name, seq in retained:                              # entrapment prefixed
            out.write(f">{ENTRAPMENT_PREFIX}{name}\n{seq}\n")

    target_peps, ent_peps = set(), set()
    for _, seq in hye:
        target_peps |= tryptic_peptides(seq)
    for _, seq in retained:
        ent_peps |= tryptic_peptides(seq)
    shared = target_peps & ent_peps
    shared_pct = 100.0 * len(shared) / len(ent_peps) if ent_peps else 0.0

    print(f"HYE target entries (verbatim): {len(hye)}")
    print(f"SoCe source entries: {len(soce)}")
    print(f"retained S. cellulosum So ce56 entries: {len(retained)}")
    print(f"excluded non-SoCe source entries: {excluded}")
    print(f"entrapment entries written ({ENTRAPMENT_PREFIX}-prefixed): "
          f"{len(retained)}")
    print(f"total entries written: {len(hye) + len(retained)}")
    print(f"target overlap removed: accessions 0, protein names 0, sequences 0")
    print(f"shared tryptic peptides entrapment vs target: {len(shared)} of "
          f"{len(ent_peps)} ({shared_pct:.2f}% of entrapment space)")
    print(f"  reference definition (engine-independent; NOT the ProSE/Sage "
          f"search settings): trypsin KR|P, "
          f"0-{PEPTIDE_MAX_MISSED_CLEAVAGES} missed cleavages, "
          f"{PEPTIDE_MIN_LEN}-{PEPTIDE_MAX_LEN} aa")

    if args.summary:
        with open(args.summary, "w") as summary:
            summary.write(f"hye_entries={len(hye)}\n")
            summary.write(f"source_entries={len(soce)}\n")
            summary.write(f"retained_entries={len(retained)}\n")
            summary.write(f"excluded_entries={excluded}\n")
            summary.write("overlap_accessions=0\n")
            summary.write("overlap_names=0\n")
            summary.write("overlap_sequences=0\n")
            summary.write(f"entrapment_peptides={len(ent_peps)}\n")
            summary.write(f"shared_peptides={len(shared)}\n")
            summary.write(f"shared_peptide_pct={shared_pct:.2f}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
