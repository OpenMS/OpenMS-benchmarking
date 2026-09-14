#!/usr/bin/env python3
"""Assemble the PXD028735 (ProteoBench HYE) target + entrapment FASTA.

target:     ProteoBench mixed-species HYE FASTA, entries verbatim
            (human + yeast + E. coli + Cont_ contaminants - the sample's true
            protein contents)
entrapment: OpenMS in-tree Sorangium cellulosum So ce56 proteome, targets
            only - doc/pyopenms/src/data/18Protein_SoCe_Tr_detergents_trace
            .fasta (9,439 entries, verified identical to the non-decoy subset
            of 18Protein_SoCe_Tr_detergents_trace_target_decoy.fasta at
            e4b9609c95). Sequences verbatim, accessions prefixed with
            ENTRAPMENT_; an organism provably absent from the sample.

Decoy handling: fail closed. The target_decoy variant of the SoCe file marks
decoys with a _rev SUFFIX (e.g. ACCA_SORC5_rev), which a rev_-prefix check
misses - copying it would silently put 9,439 reversed sequences into the
entrapment set (the Phase-2a pilot did exactly that). This generator refuses
any source carrying decoy markers (rev_/DECOY_/REV_/decoy_ prefixes or
_rev/_REV identifier suffixes) and points at the targets-only file.
ProSE generates decoys itself (-Search:decoys auto).

Shared-peptide handling: unchanged conservative policy (ENTRAPMENT_ prefix
wins at classification time; shared tryptic peptides are counted as
entrapment). This script only MEASURES the overlap (tryptic, 6-40 aa,
0 missed cleavages - the Day-34 definition) and reports it, so the number
lands in PROVENANCE.txt instead of being asserted from memory.

Usage:
  make_hye_entrapment_fasta.py --hye HYE.fasta --soce SOCE.fasta
      --out OUT.fasta [--force]

Deterministic: input order is preserved; regeneration is byte-identical.
"""
import argparse
import os
import re
import sys

DECOY_PREFIXES = ("rev_", "DECOY_", "REV_", "decoy_")
DECOY_SUFFIX_RE = re.compile(r"_rev(\s|$)|_REV(\s|$)")
ENTRAPMENT_PREFIX = "ENTRAPMENT_"


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


def decoy_marker_count(entries):
    n = 0
    for name, _ in entries:
        ident = name.split()[0] if name.split() else name
        if ident.startswith(DECOY_PREFIXES) or DECOY_SUFFIX_RE.search(ident):
            n += 1
    return n


def tryptic_peptides(seq):
    """Tryptic peptides, 0 missed cleavages, 6-40 aa (Day-34 definition)."""
    return {
        p
        for p in re.split(r"(?<=[KR])(?!P)", seq)
        if 6 <= len(p.strip("XUBZ*")) <= 40
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hye", required=True, help="ProteoBench HYE FASTA (targets)")
    ap.add_argument(
        "--soce", required=True,
        help="in-tree S. cellulosum FASTA, targets only "
             "(doc/pyopenms/src/data/18Protein_SoCe_Tr_detergents_trace.fasta)")
    ap.add_argument("--out", required=True, help="combined output FASTA")
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

    with open(args.out, "w") as out:
        for name, seq in hye:                                   # targets verbatim
            out.write(f">{name}\n{seq}\n")
        for name, seq in soce:                                  # entrapment prefixed
            out.write(f">{ENTRAPMENT_PREFIX}{name}\n{seq}\n")

    target_peps, ent_peps = set(), set()
    for _, seq in hye:
        target_peps |= tryptic_peptides(seq)
    for _, seq in soce:
        ent_peps |= tryptic_peptides(seq)
    shared = target_peps & ent_peps

    print(f"targets (HYE, verbatim): {len(hye)}")
    print(f"entrapment (SoCe targets, {ENTRAPMENT_PREFIX}-prefixed): {len(soce)}")
    print(f"total entries written: {len(hye) + len(soce)}")
    print(f"shared tryptic peptides entrapment∩target: {len(shared)} "
          f"({100.0 * len(shared) / len(ent_peps):.2f}% of entrapment space; "
          f"conservative classification applies)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
