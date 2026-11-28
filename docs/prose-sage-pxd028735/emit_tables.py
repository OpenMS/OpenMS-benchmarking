#!/usr/bin/env python3
"""Regenerate every results table of EXPERIMENT_REPORT.md from frozen outputs.

Inputs (read only): <runroot>/results/** and <runroot>/logs/**
Output: Markdown tables, to the file given as argv[2] or to stdout.

Exit status is non-zero if any internal consistency check fails, so the report
cannot silently drift from the raw data.  Checks are written to stderr.
"""
import csv
import os
import re
import sys
import xml.etree.ElementTree as ET

Q = 0.01
ENT = "ENTRAPMENT_"
DEC = "DECOY_"
DEC_ENT = "DECOY_ENTRAPMENT_"
PAREN_MOD = re.compile(r"\([^)]*\)")   # OpenMS embedded modification
BRACKET_MOD = re.compile(r"\[[^\]]*\]")  # Sage bracketed mass shift

CHECKS = []


def check(ok, label, detail=""):
    CHECKS.append((bool(ok), label, detail))
    print(("PASS  " if ok else "FAIL  ") + label + (("  " + detail) if detail else ""),
          file=sys.stderr)


def classify(accs, td):
    """One category per hit; order is the one used throughout the audit."""
    if not accs:
        return "no_protein_reference"
    ent = [a for a in accs if a.startswith(ENT)]
    dec_ent = [a for a in accs if a.startswith(DEC_ENT)]
    dec = [a for a in accs if a.startswith(DEC)]
    if dec_ent:
        return "decoy_entrapment"
    if dec and not any(not a.startswith(DEC) for a in accs):
        return "decoy"
    if ent and any(not a.startswith(ENT) and not a.startswith(DEC) for a in accs):
        return "target_shared_entrapment"
    if ent and all(a.startswith(ENT) for a in accs):
        return "entrapment_only"
    if td == "target+decoy":
        return "target_decoy_shared"
    return "pure_target"


def undecoy(a):
    """Strip the engine's decoy tag so the underlying accession can be tested."""
    for p in (DEC, "rev_"):
        if a.startswith(p):
            return a[len(p):]
    return a


def prose(root, cond):
    path = os.path.join(root, "results", "prose_%s" % cond, "ProSE_%s.idXML" % cond)
    el = ET.parse(path).getroot()
    id2acc = {}
    for pr in el.iter("ProteinIdentification"):
        for ph in pr.iter("ProteinHit"):
            id2acc[ph.get("id")] = ph.get("accession") or ""

    cls, seq, bare = {}, {}, {}
    total = 0
    ref_ids = set()
    for pid in el.iter("PeptideIdentification"):
        hit = pid.find("PeptideHit")
        total += 1
        q = float(hit.get("score"))
        refs = (hit.get("protein_refs") or "").split()
        ref_ids.update(refs)
        accs = [id2acc.get(r, r) for r in refs]
        up = hit.find('UserParam[@name="target_decoy"]')
        c = classify(accs, up.get("value") if up is not None else "")
        s = hit.get("sequence") or ""
        if q <= Q:
            cls[c] = cls.get(c, 0) + 1
            seq.setdefault(c, set()).add(s)
            bare.setdefault(c, set()).add(PAREN_MOD.sub("", s))

    union_mod = {x for st in seq.values() for x in st}
    union_bare = {x for st in bare.values() for x in st}

    accs_all = list(id2acc.values())
    log = open(os.path.join(root, "logs", "prose_%s.stdout.log" % cond)).read()
    m = re.search(r"Matched\s*:\s*(\d+) / (\d+) spectra", log)
    log_matched, spectra = int(m.group(1)), int(m.group(2))
    m = re.search(r"Peptides/prot:\s*(\d+) unique peptides \| (\d+) unique proteins", log)
    log_pep, log_prot = int(m.group(1)), int(m.group(2))
    m = re.search(r"search ([\d.]+)s \| fdr ([\d.]+)s \| index\(shared\) ([\d.]+)s", log)
    timings = [float(m.group(1)), float(m.group(2)), float(m.group(3))]

    check(total == sum(cls.values()), "ProSE %s: every row passes the q filter" % cond,
          "rows=%d kept=%d" % (total, sum(cls.values())))
    check(sum(cls.values()) == log_matched, "ProSE %s: kept rows == logged Matched" % cond,
          "%d vs %d" % (sum(cls.values()), log_matched))
    check(len(union_mod) == log_pep, "ProSE %s: peptide union == logged unique peptides" % cond,
          "%d vs %d" % (len(union_mod), log_pep))
    check(len(ref_ids) == log_prot, "ProSE %s: referenced proteins == logged proteins" % cond,
          "%d vs %d" % (len(ref_ids), log_prot))

    return {
        "total": total, "spectra": spectra,
        "cls": cls, "seq": seq, "bare": bare,
        "union_mod": union_mod, "union_bare": union_bare,
        "prot_total": len(accs_all),
        "prot_nonent": sum(1 for a in accs_all if not a.startswith((ENT, DEC))),
        "prot_ent": sum(1 for a in accs_all if a.startswith(ENT)),
        "prot_dec": sum(1 for a in accs_all if a.startswith(DEC)),
        "groups": len(list(el.iter("ProteinGroup"))),
        "log_pep": log_pep, "log_prot": log_prot, "log_matched": log_matched,
        "internal": timings,
    }


def sage(root, cond):
    path = os.path.join(root, "results", "sage_%s" % cond, "results.sage.tsv")
    sq_cls, sq_seq, sq_bare = {}, {}, {}
    pq_cls, pq_seq, pq_bare = {}, {}, {}
    all_label_at_q = 0
    dec_cls = {"decoy": 0, "decoy_entrapment": 0}
    prots, ent_prots, groups, ent_groups = set(), set(), set(), set()

    with open(path, newline="") as fh:
        for r in csv.DictReader(fh, delimiter="\t"):
            accs = [p for p in (r["proteins"] or "").split(";") if p]
            try:
                sq = float(r["spectrum_q"])
            except (TypeError, ValueError):
                sq = None
            if r["label"] == "1":
                c = classify(accs, "target")
                s = r["peptide"] or ""
                if sq is not None and sq <= Q:
                    all_label_at_q += 1
                    sq_cls[c] = sq_cls.get(c, 0) + 1
                    sq_seq.setdefault(c, set()).add(s)
                    sq_bare.setdefault(c, set()).add(BRACKET_MOD.sub("", s))
                try:
                    pq = float(r["peptide_q"])
                except (TypeError, ValueError):
                    pq = None
                if pq is not None and pq <= Q:
                    pq_cls[c] = pq_cls.get(c, 0) + 1
                    pq_seq.setdefault(c, set()).add(s)
                    pq_bare.setdefault(c, set()).add(BRACKET_MOD.sub("", s))
                try:
                    prq = float(r["protein_q"])
                except (TypeError, ValueError):
                    prq = None
                if prq is not None and prq <= Q:
                    prots.update(accs)
                    ent_prots.update(a for a in accs if a.startswith(ENT))
                try:
                    gq = float(r["protein_group_q"])
                except (TypeError, ValueError):
                    gq = None
                if gq is not None and gq <= Q:
                    gids = {g for g in (r["protein_groups"] or "").split(";") if g}
                    groups.update(gids)
                    if accs and all(a.startswith(ENT) for a in accs):
                        ent_groups.update(gids)
            else:
                if sq is not None and sq <= Q:
                    all_label_at_q += 1
                    if any(undecoy(a).startswith(ENT) for a in accs):
                        dec_cls["decoy_entrapment"] += 1
                    else:
                        dec_cls["decoy"] += 1

    pq_union = {x for st in pq_seq.values() for x in st}
    pq_union_bare = {x for st in pq_bare.values() for x in st}
    sq_union = {x for st in sq_seq.values() for x in st}

    log = open(os.path.join(root, "logs", "sage_%s.stderr.log" % cond)).read()
    def grab(pat):
        mm = re.search(pat, log)
        return int(mm.group(1)) if mm else None
    log_psm = grab(r"discovered (\d+) target peptide-spectrum matches")
    log_pep = grab(r"discovered (\d+) target peptides")
    log_prot = grab(r"discovered (\d+) target proteins")
    log_grp = grab(r"discovered (\d+) target protein groups")
    m = re.search(r"- search:\s+(\d+) ms", log)
    search_ms = int(m.group(1)) if m else None

    check(all_label_at_q == log_psm,
          "Sage %s: all-label rows at spectrum_q<=0.01 == logged PSM figure" % cond,
          "%d vs %d" % (all_label_at_q, log_psm))
    check(all_label_at_q == sum(sq_cls.values()) + sum(dec_cls.values()),
          "Sage %s: all-label count == target classes + decoy classes" % cond,
          "%d vs %d" % (all_label_at_q, sum(sq_cls.values()) + sum(dec_cls.values())))
    check(len(pq_union) == log_pep, "Sage %s: peptide_q set size == logged peptides" % cond,
          "%d vs %d" % (len(pq_union), log_pep))
    check(len(prots) == log_prot, "Sage %s: protein set == logged proteins" % cond,
          "%d vs %d" % (len(prots), log_prot))
    check(len(groups) == log_grp, "Sage %s: group set == logged groups" % cond,
          "%d vs %d" % (len(groups), log_grp))

    return {
        "spectra": None, "all_label_at_q": all_label_at_q, "dec": dec_cls,
        "sq_cls": sq_cls, "sq_seq": sq_seq, "sq_bare": sq_bare, "sq_union": sq_union,
        "pq_cls": pq_cls, "pq_seq": pq_seq, "pq_bare": pq_bare,
        "pq_union": pq_union, "pq_union_bare": pq_union_bare,
        "prot": prots, "ent_prot": ent_prots, "grp": groups, "ent_grp": ent_groups,
        "log_psm": log_psm, "log_pep": log_pep, "log_prot": log_prot, "log_grp": log_grp,
        "search_ms": search_ms,
    }


def times(root, prefix, cond):
    txt = open(os.path.join(root, "logs", "%s_%s.time.txt" % (prefix, cond))).read()
    def grab(pat, cast=str):
        m = re.search(pat, txt)
        return cast(m.group(1)) if m else None
    return {
        "wall": grab(r"Elapsed \(wall clock\) time \(h:mm:ss or m:ss\):\s*(\S+)"),
        "user": grab(r"User time \(seconds\):\s*([\d.]+)", float),
        "sys": grab(r"System time \(seconds\):\s*([\d.]+)", float),
        "cpu": grab(r"Percent of CPU this job got:\s*(\d+)", int),
        "rss": grab(r"Maximum resident set size \(kbytes\):\s*(\d+)", int),
    }


def num(v):
    return f"{v:,}" if isinstance(v, int) else ("—" if v is None else str(v))


def table(headers, rows):
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] + ["---:"] * (len(headers) - 1)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def get(d, cls_key, metric):
    """metric: 'psm' or 'pep' -> count for class cls_key, 0 when absent."""
    return d["cls" if metric == "psm" else "seq"].get(cls_key, 0)


def main():
    root = sys.argv[1]
    out_path = sys.argv[2] if len(sys.argv) > 2 else None
    P = {c: prose(root, c) for c in "AB"}
    S = {c: sage(root, c) for c in "AB"}
    T = {(e, c): times(root, e, c) for e in ("prose", "sage") for c in "AB"}

    L = []
    A = L.append

    # ---------- Table 1: PSMs ----------
    A("### Table 1 — PSM level\n")
    A("All engine-native q thresholds are 1% and inclusive. ProSE removes decoy PSMs and their "
      "protein references at protein-FDR finalization, so decoy rows cannot be observed in its output; "
      "Sage writes targets and decoys alike.\n")
    rows = [
        ["Engine log: PSM figure (as labelled by the engine)",
         num(P["A"]["total"]), num(P["B"]["total"]), num(S["A"]["log_psm"]), num(S["B"]["log_psm"])],
        ["Serialized rows, target class (engine-native threshold)",
         num(P["A"]["total"]), num(P["B"]["total"]),
         num(sum(S["A"]["sq_cls"].values())), num(sum(S["B"]["sq_cls"].values()))],
        ["Serialized rows, all labels at threshold",
         "not observable", "not observable", num(S["A"]["all_label_at_q"]), num(S["B"]["all_label_at_q"])],
        ["**Pure target**",
         num(P["A"]["cls"].get("pure_target", 0)), num(P["B"]["cls"].get("pure_target", 0)),
         num(S["A"]["sq_cls"].get("pure_target", 0)), num(S["B"]["sq_cls"].get("pure_target", 0))],
        ["Target+decoy shared",
         num(P["A"]["cls"].get("target_decoy_shared", 0)), num(P["B"]["cls"].get("target_decoy_shared", 0)),
         "—", "—"],
        ["Target-shared entrapment",
         num(P["A"]["cls"].get("target_shared_entrapment", 0)),
         num(P["B"]["cls"].get("target_shared_entrapment", 0)),
         num(S["A"]["sq_cls"].get("target_shared_entrapment", 0)),
         num(S["B"]["sq_cls"].get("target_shared_entrapment", 0))],
        ["Entrapment only",
         num(P["A"]["cls"].get("entrapment_only", 0)), num(P["B"]["cls"].get("entrapment_only", 0)),
         num(S["A"]["sq_cls"].get("entrapment_only", 0)), num(S["B"]["sq_cls"].get("entrapment_only", 0))],
        ["Target, no surviving protein reference",
         num(P["A"]["cls"].get("no_protein_reference", 0)),
         num(P["B"]["cls"].get("no_protein_reference", 0)),
         num(S["A"]["sq_cls"].get("no_protein_reference", 0)),
         num(S["B"]["sq_cls"].get("no_protein_reference", 0))],
        ["Decoy",
         "not observable", "not observable",
         num(S["A"]["dec"]["decoy"]), num(S["B"]["dec"]["decoy"])],
        ["Decoy-entrapment",
         "not observable", "not observable",
         num(S["A"]["dec"]["decoy_entrapment"]), num(S["B"]["dec"]["decoy_entrapment"])],
    ]
    A(table(["Metric", "ProSE A", "ProSE B", "Sage A", "Sage B"], rows))
    A("")

    # ---------- Table 2: peptides ----------
    A("### Table 2 — Distinct peptide sequences\n")
    A("A peptide is counted once per class it appears in; the two engines write different sequence "
      "syntaxes, so only within-engine comparisons are made. *Modified* is the engine's own string "
      "(OpenMS `M(Oxidation)` vs Sage `M[+15.9949]`); *bare* has modification tokens stripped.\n")
    rows = [
        ["Engine log: unique peptides", num(P["A"]["log_pep"]), num(P["B"]["log_pep"]),
         num(S["A"]["log_pep"]), num(S["B"]["log_pep"])],
        ["Recomputed set behind that figure", num(len(P["A"]["union_mod"])), num(len(P["B"]["union_mod"])),
         num(len(S["A"]["pq_union"])), num(len(S["B"]["pq_union"]))],
        ["Bare sequences in the same set", num(len(P["A"]["union_bare"])), num(len(P["B"]["union_bare"])),
         num(len(S["A"]["pq_union_bare"])), num(len(S["B"]["pq_union_bare"]))],
        ["**Pure target, modified**",
         num(len(P["A"]["seq"].get("pure_target", ()))), num(len(P["B"]["seq"].get("pure_target", ()))),
         num(len(S["A"]["pq_seq"].get("pure_target", ()))), num(len(S["B"]["pq_seq"].get("pure_target", ())))],
        ["Pure target, bare",
         num(len(P["A"]["bare"].get("pure_target", ()))), num(len(P["B"]["bare"].get("pure_target", ()))),
         num(len(S["A"]["pq_bare"].get("pure_target", ()))), num(len(S["B"]["pq_bare"].get("pure_target", ())))],
        ["Target+decoy shared",
         num(len(P["A"]["seq"].get("target_decoy_shared", ()))),
         num(len(P["B"]["seq"].get("target_decoy_shared", ()))), "—", "—"],
        ["Target-shared entrapment",
         num(len(P["A"]["seq"].get("target_shared_entrapment", ()))),
         num(len(P["B"]["seq"].get("target_shared_entrapment", ()))),
         num(len(S["A"]["sq_seq"].get("target_shared_entrapment", ()))),
         num(len(S["B"]["sq_seq"].get("target_shared_entrapment", ())))],
        ["Entrapment only",
         num(len(P["A"]["seq"].get("entrapment_only", ()))),
         num(len(P["B"]["seq"].get("entrapment_only", ()))),
         num(len(S["A"]["sq_seq"].get("entrapment_only", ()))),
         num(len(S["B"]["sq_seq"].get("entrapment_only", ())))],
        ["Target, no surviving protein reference",
         num(len(P["A"]["seq"].get("no_protein_reference", ()))),
         num(len(P["B"]["seq"].get("no_protein_reference", ()))),
         num(len(S["A"]["sq_seq"].get("no_protein_reference", ()))),
         num(len(S["B"]["sq_seq"].get("no_protein_reference", ())))],
    ]
    A(table(["Metric", "ProSE A", "ProSE B", "Sage A", "Sage B"], rows))
    A("")
    A("Rows 7–9 for Sage are taken at the PSM threshold (`spectrum_q ≤ 0.01`). Under Sage's own "
      "peptide threshold the same classes are smaller; the engine's reported peptide total and its "
      "pure-target part are unaffected by that difference:\n")
    rows = [
        ["Unique peptides, `peptide_q ≤ 0.01`", num(len(S["A"]["pq_union"])), num(len(S["B"]["pq_union"]))],
        ["Pure target", num(len(S["A"]["pq_seq"].get("pure_target", ()))),
         num(len(S["B"]["pq_seq"].get("pure_target", ())))],
        ["Target-shared entrapment", num(len(S["A"]["pq_seq"].get("target_shared_entrapment", ()))),
         num(len(S["B"]["pq_seq"].get("target_shared_entrapment", ())))],
        ["Entrapment only", num(len(S["A"]["pq_seq"].get("entrapment_only", ()))),
         num(len(S["B"]["pq_seq"].get("entrapment_only", ())))],
        ["Unique peptides, `spectrum_q ≤ 0.01`", num(len(S["A"]["sq_union"])), num(len(S["B"]["sq_union"]))],
    ]
    A(table(["Sage peptide set", "Sage A", "Sage B"], rows))
    A("")

    # ---------- Table 3: proteins ----------
    A("### Table 3 — Proteins and protein groups\n")
    A("ProSE's protein count is a picked-protein list over complete protein sets with no grouping "
      "step in the output; Sage's is the picked protein list restricted to proteotypic peptides, "
      "plus a separate IDPicker-style grouping. The two protein columns are different quantities "
      "and are not compared.\n")
    rows = [
        ["Protein identifications at 1% FDR",
         num(P["A"]["prot_total"] - P["A"]["prot_dec"]),
         num(P["B"]["prot_total"] - P["B"]["prot_dec"]),
         num(len(S["A"]["prot"])), num(len(S["B"]["prot"]))],
        ["— non-entrapment",
         num(P["A"]["prot_nonent"]), num(P["B"]["prot_nonent"]),
         num(len(S["A"]["prot"]) - len(S["A"]["ent_prot"])),
         num(len(S["B"]["prot"]) - len(S["B"]["ent_prot"]))],
        ["— entrapment",
         num(P["A"]["prot_ent"]), num(P["B"]["prot_ent"]),
         num(len(S["A"]["ent_prot"])), num(len(S["B"]["ent_prot"]))],
        ["Protein groups", "not emitted (0 elements)", "not emitted (0 elements)",
         num(len(S["A"]["grp"])), num(len(S["B"]["grp"]))],
        ["— entrapment groups", "—", "—",
         num(len(S["A"]["ent_grp"])), num(len(S["B"]["ent_grp"]))],
    ]
    A(table(["Metric", "ProSE A", "ProSE B", "Sage A", "Sage B"], rows))
    A("")

    # ---------- Table 4: runtime ----------
    A("### Table 4 — Runtime and resources\n")
    A("From `/usr/bin/time -v` on the recorded runs; nothing was re-timed. Peak RSS is shown in "
      "MiB; the raw figures are %s / %s / %s / %s kB.\n"
      % (num(T[("prose", "A")]["rss"]), num(T[("prose", "B")]["rss"]),
         num(T[("sage", "A")]["rss"]), num(T[("sage", "B")]["rss"])))
    rows = []
    for label, key in (("Wall clock", "wall"), ("User CPU (s)", "user"), ("System CPU (s)", "sys"),
                       ("CPU utilisation", "cpu"), ("Peak RSS (MiB)", "rss")):
        vals = []
        for e, c in (("prose", "A"), ("prose", "B"), ("sage", "A"), ("sage", "B")):
            t = T[(e, c)][key]
            if key == "rss":
                vals.append(num(round(t / 1024)) if t else "—")
            elif key == "cpu":
                vals.append(f"{t}%" if t else "—")
            elif key in ("user", "sys"):
                vals.append(f"{t:.2f}" if t else "—")
            else:
                vals.append(t or "—")
        rows.append([label] + vals)
    A(table(["Metric", "ProSE A", "ProSE B", "Sage A", "Sage B"], rows))
    A("")
    A("Engine-internal stages (from the same logs): ProSE index build %.1f s / %.1f s, search %.1f s / "
      "%.1f s, FDR %.1f s / %.1f s; Sage search %.1f s / %.1f s."
      % (P["A"]["internal"][2], P["B"]["internal"][2],
         P["A"]["internal"][0], P["B"]["internal"][0],
         P["A"]["internal"][1], P["B"]["internal"][1],
         S["A"]["search_ms"] / 1000.0, S["B"]["search_ms"] / 1000.0))
    A("")

    # ---------- Table 5: reconciliation ----------
    A("### Table 5 — Log-to-output reconciliation\n")
    A("Each engine summary figure is recomputed from the serialized output. A zero delta means the "
      "logged number is fully explained by the stated reconstruction.\n")
    rows = []

    def rec(label, logged, got):
        rows.append([label, num(logged), num(got), num(got - logged)])

    for c in "AB":
        rec("ProSE %s PSMs (logged `Matched`)" % c, P[c]["log_matched"],
            sum(P[c]["cls"].values()))
        rec("ProSE %s unique peptides" % c, P[c]["log_pep"], len(P[c]["union_mod"]))
        rec("ProSE %s proteins" % c, P[c]["log_prot"],
            P[c]["prot_total"] - P[c]["prot_dec"])
        rec("Sage %s PSM figure (all labels at threshold)" % c, S[c]["log_psm"],
            S[c]["all_label_at_q"])
        rec("Sage %s unique peptides (`peptide_q`)" % c, S[c]["log_pep"], len(S[c]["pq_union"]))
        rec("Sage %s proteins" % c, S[c]["log_prot"], len(S[c]["prot"]))
        rec("Sage %s protein groups" % c, S[c]["log_grp"], len(S[c]["grp"]))
    A(table(["Quantity", "Logged", "Reconstructed", "Δ"], rows))
    A("")

    txt = "\n".join(L)
    if out_path:
        with open(out_path, "w", newline="\n") as fh:
            fh.write(txt)
    else:
        sys.stdout.write(txt)

    bad = [c for c in CHECKS if not c[0]]
    print("\n%d checks, %d failed" % (len(CHECKS), len(bad)), file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
