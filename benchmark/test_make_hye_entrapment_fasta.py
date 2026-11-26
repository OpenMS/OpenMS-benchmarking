#!/usr/bin/env python3
"""Tests for make_hye_entrapment_fasta.py (SoCe-only PXD028735 entrapment).

Synthetic FASTA files are written on the fly; no dataset, no OpenMS source
tree and no ProSE build is needed. Each test invokes the generator exactly as
prepare_pxd028735_inputs.sh does and asserts on its exit code, its stderr and
the generated FASTA.

Run:  python3 benchmark/test_make_hye_entrapment_fasta.py
"""

import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "make_hye_entrapment_fasta.py")

SO_OS = "OS=Sorangium cellulosum (strain So ce56)"
ENTRAPMENT_PREFIX = "ENTRAPMENT_"


def write_fasta(path, entries):
    with open(path, "w", newline="\n") as fh:
        for header, seq in entries:
            fh.write(f">{header}\n{seq}\n")


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


def soce(accession, name, seq, db="sp"):
    return (f"{db}|{accession}|{name} Description {SO_OS} GN=x PE=3 SV=1", seq)


def target(accession, name, seq):
    """HYE target entry (ProteoBench contaminants are Cont_ accessions)."""
    return (f"sp|{accession}|{name}", seq)


def run_generator(tmp, hye_entries, soce_entries, run_id="1"):
    hye = os.path.join(tmp, f"hye-{run_id}.fasta")
    soce_path = os.path.join(tmp, f"soce-{run_id}.fasta")
    out = os.path.join(tmp, f"out-{run_id}.fasta")
    summary = os.path.join(tmp, f"summary-{run_id}.txt")
    write_fasta(hye, hye_entries)
    write_fasta(soce_path, soce_entries)
    proc = subprocess.run(
        [sys.executable, SCRIPT, "--hye", hye, "--soce", soce_path,
         "--out", out, "--summary", summary, "--force"],
        capture_output=True, text=True)
    return proc, out, summary


def summary_dict(path):
    data = {}
    with open(path) as fh:
        for line in fh:
            key, _, value = line.rstrip("\n").partition("=")
            data[key] = value
    return data


def expect_failure(proc, needle):
    assert proc.returncode != 0, \
        f"generator unexpectedly succeeded:\n{proc.stdout}\n{proc.stderr}"
    assert needle in proc.stderr, proc.stderr


def test_database_prefixed_soce_retained():
    """sp| and tr| So ce56 entries are kept verbatim, ENTRAPMENT_-prefixed."""
    with tempfile.TemporaryDirectory() as tmp:
        hye = [target("Cont_Q00001", "TARGET_HUMAN", "MKWVTFISLLFLFSSAYSR")]
        src = [
            soce("A9F596", "ACCA_SORC5", "MAASILPFEKPVAELIEKVRELRALAAADPR"),
            soce("B1ABC2", "TRX_SORC5", "MSKGEELFTGVVPILVELDGDVNGHKFSVSG",
                 db="tr"),
        ]
        proc, out, summary = run_generator(tmp, hye, src)
        assert proc.returncode == 0, proc.stderr
        entries = read_entries(out)
        assert len(entries) == 3, entries
        assert entries[0] == hye[0]
        for got, (want_header, want_seq) in zip(entries[1:], src):
            assert got[0] == ENTRAPMENT_PREFIX + want_header, got[0]
            assert got[1] == want_seq
        data = summary_dict(summary)
        assert data["retained_entries"] == "2", data
        assert data["excluded_entries"] == "0", data
        assert data["hye_entries"] == "1", data
        assert data["overlap_accessions"] == "0", data
        assert data["overlap_names"] == "0", data
        assert data["overlap_sequences"] == "0", data
        assert "shared_peptides" in data and "entrapment_peptides" in data, data
    print("PASS: database-prefixed Soce entries retained and prefixed")


def test_bare_acc_name_contaminant_excluded():
    """Bare ACC|NAME headers have no OS= and are excluded, even when their
    accession also exists as a Cont_ accession in the HYE targets."""
    with tempfile.TemporaryDirectory() as tmp:
        hye = [target("Cont_P00760", "TRY1_BOVIN", "MKTLLLTLVVAA")]
        src = [
            ("P00760|TRY1_BOVIN Trypsin - Bos taurus (Bovine).", "MKWVTFISLL"),
            soce("A9F596", "ACCA_SORC5", "MAASILPFEKPVAELIEKVRELRALAAADPR"),
        ]
        proc, out, summary = run_generator(tmp, hye, src)
        assert proc.returncode == 0, proc.stderr
        entries = read_entries(out)
        assert len(entries) == 2, entries
        assert entries[1][0] == ENTRAPMENT_PREFIX + src[1][0]
        data = summary_dict(summary)
        assert data["retained_entries"] == "1", data
        assert data["excluded_entries"] == "1", data
    print("PASS: bare ACC|NAME contaminant excluded (no false overlap failure)")


def test_non_soce_database_entry_excluded():
    """Order-preserving exclusion of a db-prefixed foreign organism."""
    with tempfile.TemporaryDirectory() as tmp:
        hye = [target("Cont_Q00001", "TARGET_HUMAN", "MKWVTFISLL")]
        src = [
            soce("A9F596", "ACCA_SORC5", "MAASILPFEKPV"),
            ("sp|P12345|FOREIGN_HUMAN Description OS=Homo sapiens GN=x", "MSKGEELFT"),
            soce("B1ABC2", "TRX_SORC5", "MAASILPFEKPVAELIEK"),
        ]
        proc, out, summary = run_generator(tmp, hye, src)
        assert proc.returncode == 0, proc.stderr
        entries = read_entries(out)
        assert [e[0].split()[0] for e in entries] == [
            "sp|Cont_Q00001|TARGET_HUMAN",
            ENTRAPMENT_PREFIX + "sp|A9F596|ACCA_SORC5",
            ENTRAPMENT_PREFIX + "sp|B1ABC2|TRX_SORC5",
        ], entries
        assert summary_dict(summary)["excluded_entries"] == "1"
    print("PASS: non-Soce database-prefixed entry excluded, order preserved")


def test_target_contaminant_accession_collision():
    """A So ce56 entry exposing a Cont_pin accession fails closed."""
    with tempfile.TemporaryDirectory() as tmp:
        hye = [target("Cont_P00760", "TRY1_BOVIN", "MKTLLLTLVVAA")]
        src = [soce("P00760", "TRYX_SORC5", "MAASILPFEKPV")]
        proc, _, _ = run_generator(tmp, hye, src)
        expect_failure(proc, "accession")
    print("PASS: target Cont_ accession collision fails closed")


def test_protein_name_collision():
    """Distinct accessions but the same protein name fails closed."""
    with tempfile.TemporaryDirectory() as tmp:
        hye = [target("Cont_Q11111", "ACCA_SORC5", "MKWVTFISLL")]
        src = [soce("P22222", "ACCA_SORC5", "MAASILPFEKPV")]
        proc, _, _ = run_generator(tmp, hye, src)
        expect_failure(proc, "protein name")
    print("PASS: protein-name collision fails closed")


def test_identical_sequence_collision():
    """Distinct accession and name but an identical sequence fails closed."""
    with tempfile.TemporaryDirectory() as tmp:
        hye = [target("Cont_Q22222", "OTHER_HUMAN",
                      "MKWVTFISLLFLFSSAYSRGVFRR")]
        src = [soce("P33333", "UNIQ_SORC5", "MKWVTFISLLFLFSSAYSRGVFRR")]
        proc, _, _ = run_generator(tmp, hye, src)
        expect_failure(proc, "sequence")
    print("PASS: identical-sequence collision fails closed")


def test_decoy_marked_source_fails():
    """A _rev-suffix decoy entry in the source aborts generation."""
    with tempfile.TemporaryDirectory() as tmp:
        hye = [target("Cont_Q00001", "TARGET_HUMAN", "MKWVTFISLL")]
        src = [("P33333_rev Some protein " + SO_OS, "MAASILPFEKPV")]
        proc, _, _ = run_generator(tmp, hye, src)
        expect_failure(proc, "decoy-marker")
    print("PASS: decoy-marked source fails closed")


def test_all_foreign_source_fails():
    """A source with no So ce56 entry at all aborts generation."""
    with tempfile.TemporaryDirectory() as tmp:
        hye = [target("Cont_Q00001", "TARGET_HUMAN", "MKWVTFISLL")]
        src = [
            ("sp|P12345|FOREIGN_HUMAN Description OS=Homo sapiens GN=x",
             "MSKGEELFT"),
            ("P00760|TRY1_BOVIN Trypsin - Bos taurus (Bovine).", "MKWVTFISLL"),
        ]
        proc, _, _ = run_generator(tmp, hye, src)
        expect_failure(proc, "no S. cellulosum")
    print("PASS: all-foreign source fails closed")


def test_duplicate_output_identifiers_fail():
    """Duplicate identifiers in the written FASTA abort generation."""
    with tempfile.TemporaryDirectory() as tmp:
        hye = [target("Cont_Q00001", "TARGET_HUMAN", "MKWVTFISLL")]
        src = [
            soce("A9F596", "ACCA_SORC5", "MAASILPFEKPV"),
            soce("A9F596", "ACCA_SORC5", "MSKGEELFTGVV"),
        ]
        proc, _, _ = run_generator(tmp, hye, src)
        expect_failure(proc, "duplicate output identifier")
    print("PASS: duplicate output identifiers fail closed")


def test_deterministic_output():
    """Two generations from identical inputs are byte-identical."""
    with tempfile.TemporaryDirectory() as tmp:
        hye = [
            target("Cont_Q00001", "TARGET_HUMAN", "MKWVTFISLL"),
            ("tr|Q00002|TARGET2_HUMAN", "MSKGEELFTGVVP"),
        ]
        src = [
            soce("A9F596", "ACCA_SORC5", "MAASILPFEKPV"),
            soce("B1ABC2", "TRX_SORC5", "MAASILPFEKPVAELIEK", db="tr"),
            ("P00760|TRY1_BOVIN Trypsin - Bos taurus (Bovine).", "MKWVTFISLL"),
        ]
        proc1, out1, summary1 = run_generator(tmp, hye, src, run_id="1")
        proc2, out2, summary2 = run_generator(tmp, hye, src, run_id="2")
        assert proc1.returncode == 0, proc1.stderr
        assert proc2.returncode == 0, proc2.stderr
        with open(out1, "rb") as fh:
            first = fh.read()
        with open(out2, "rb") as fh:
            second = fh.read()
        assert first == second, "generated FASTA is not byte-identical"
        with open(summary1, "rb") as fh:
            first_summary = fh.read()
        with open(summary2, "rb") as fh:
            second_summary = fh.read()
        assert first_summary == second_summary, "summary is not deterministic"
    print("PASS: deterministic output (byte-identical FASTA and summary)")


def test_hye_target_entries_unchanged():
    """HYE entries stay verbatim, in order, with their original headers."""
    with tempfile.TemporaryDirectory() as tmp:
        hye = [
            target("Cont_P00722", "BGAL_ECOLI", "MKWVTFISLL"),
            ("tr|A0A123|YEAST_PROT Description OS=Saccharomyces cerevisiae",
             "MSKGEELFTGVVP"),
            ("Biognosys|iRT_11|iRT-pep11", "LGGNEQVTR"),
        ]
        src = [soce("A9F596", "ACCA_SORC5", "MAASILPFEKPV")]
        proc, out, _ = run_generator(tmp, hye, src)
        assert proc.returncode == 0, proc.stderr
        entries = read_entries(out)
        assert entries[:len(hye)] == hye, entries[:len(hye)]
        assert len(entries) == len(hye) + 1, entries
    print("PASS: HYE target entries unchanged and first in output")


def main():
    test_database_prefixed_soce_retained()
    test_bare_acc_name_contaminant_excluded()
    test_non_soce_database_entry_excluded()
    test_target_contaminant_accession_collision()
    test_protein_name_collision()
    test_identical_sequence_collision()
    test_decoy_marked_source_fails()
    test_all_foreign_source_fails()
    test_duplicate_output_identifiers_fail()
    test_deterministic_output()
    test_hye_target_entries_unchanged()
    print("\nAll make_hye_entrapment_fasta tests passed.")


if __name__ == "__main__":
    main()
