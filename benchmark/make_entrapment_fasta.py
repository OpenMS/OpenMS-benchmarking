#!/usr/bin/env python3
"""Build the target+entrapment FASTA for the ProSE entrapment benchmark.

Target:     the benchmark target FASTA (P02769, bovine serum albumin),
            copied verbatim.
Entrapment: the real E. coli K-12 *target* proteins from OpenMS's in-tree
            target_decoy_Ecoli_K12_TaxID_83333.proteomes.fasta, their
            identifiers prefixed with ENTRAPMENT_. The file's rev_ decoy
            entries are NOT copied: ProSE generates decoys itself from the
            combined database (-Search:decoys auto).

No sequences are fabricated or modified: entries are preserved in input
order with their sequence lines byte-identical (only line trailing
whitespace is stripped); E. coli identifiers merely gain the prefix.
Output is deterministic for given inputs. Stdlib only.

Usage:
  make_entrapment_fasta.py --target P02769.fasta \
      --ecoli target_decoy_Ecoli_K12_TaxID_83333.proteomes.fasta \
      --out entrapment_target.fasta [--force]
"""

import argparse
import sys

ENTRAPMENT_PREFIX = "ENTRAPMENT_"
DECOY_MARKER = "rev_"
MIN_ENTRAPMENT_ENTRIES = 100


def read_fasta(path):
    """[(identifier, header_rest, [sequence lines])] in file order.

    header_rest is the header text after the identifier (" desc..." or "").
    """
    entries = []
    ident = rest = None
    seqs = []
    with open(path) as fh:
        for line in fh:
            line = line.rstrip("\r\n")
            if line.startswith(">"):
                if ident is not None:
                    entries.append((ident, rest, seqs))
                header = line[1:]
                parts = header.split(None, 1)
                ident, rest = parts[0], (" " + parts[1] if len(parts) > 1 else "")
                seqs = []
            elif ident is not None and line.strip():
                seqs.append(line.strip())
    if ident is not None:
        entries.append((ident, rest, seqs))
    return entries


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--target", required=True, help="target FASTA (P02769)")
    ap.add_argument("--ecoli", required=True, help="E. coli target_decoy FASTA")
    ap.add_argument("--out", required=True, help="combined output FASTA")
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing output file")
    args = ap.parse_args()

    for name, path in (("--target", args.target), ("--ecoli", args.ecoli)):
        try:
            open(path).close()
        except OSError as e:
            sys.exit("ERROR: %s input not readable: %s" % (name, e))
    if not args.force:
        try:
            open(args.out).close()
            sys.exit("ERROR: %s exists (use --force to overwrite)" % args.out)
        except OSError:
            pass

    target = read_fasta(args.target)
    ecoli = read_fasta(args.ecoli)
    if not target:
        sys.exit("ERROR: no entries in %s" % args.target)
    if any(i.startswith(ENTRAPMENT_PREFIX) for i, _, _ in ecoli):
        sys.exit("ERROR: %s already carries %s entries"
                 % (args.ecoli, ENTRAPMENT_PREFIX))
    entrap = [(ENTRAPMENT_PREFIX + i, rest, s) for i, rest, s in ecoli
              if not i.startswith(DECOY_MARKER)]
    if len(entrap) < MIN_ENTRAPMENT_ENTRIES:
        sys.exit("ERROR: only %d non-decoy E. coli entries in %s (expected >= %d)"
                 % (len(entrap), args.ecoli, MIN_ENTRAPMENT_ENTRIES))

    with open(args.out, "w") as fh:
        for ident, rest, seqs in target + entrap:
            fh.write(">%s%s\n" % (ident, rest))
            for line in seqs:
                fh.write(line + "\n")

    print("wrote %s: %d target + %d ENTRAPMENT_ entries "
          "(%d E. coli rev_ decoys skipped)"
          % (args.out, len(target), len(entrap), len(ecoli) - len(entrap)))


if __name__ == "__main__":
    main()
