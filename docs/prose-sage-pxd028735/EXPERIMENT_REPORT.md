# ProSE vs Sage on PXD028735: a pinned, audited two-condition benchmark

**Status.** Experiment complete: four primary searches, one per engine per condition, no repeats.
Metrics independently audited and reconciled against source and raw output. Results frozen; this
report is generated from the frozen outputs, not transcribed from them.

**Date.** Runs and audit 2026-11-27; report 2026-11-28.

**Scope.** A descriptive comparison of two search engines on one public dataset under a shared
parameter set. It reports what each engine output and why the raw summary figures differ. It does
not establish which engine is more sensitive or more accurate.

---

## 1. Question

Two engines, ProSE (OpenMS) and Sage, were run on the two conditions of PXD028735 with the same
database, the same digestion/enzyme rules, the same tolerances, the same fixed and variable
modifications, and the same 1% FDR targets. The questions were:

1. How many PSMs, peptides and proteins does each engine report at 1% FDR?
2. Do the engines' own summary figures agree with their serialized output?
3. Where they differ, is the difference a property of the search or a property of how the number is
   counted and reported?

Question 3 turned out to be the substantive one: two of the three headline discrepancies are
reporting artefacts, and one is a classification error in the first version of the comparison.

## 2. Data

| Item | Value |
|---|---|
| Dataset | PXD028735, LFQ Orbitrap DDA, *Sample Alpha 01* |
| Conditions | A and B, one mzML each |
| MS2 spectra | 109,507 (A), 111,088 (B) |
| Database | `entrapment_hye_soce.fasta`, 41,209 entries |
| Entrapment | HYE proteins, accessions prefixed `ENTRAPMENT_` (PR #34, merged) |
| Replicates | none — one injection per condition |

The entrapment sequences are present in the search database and are treated by both engines as
ordinary target entries; they are separated at analysis time by accession prefix. Decoys were
generated internally by each engine and never supplied as a file.

Because there is one injection per condition, every number here is a single observation. No
run-to-run variance can be estimated, and no significance test is meaningful.

## 3. Engines and pins

| Engine | Version | Binary SHA-256 |
|---|---|---|
| ProSE | OpenMS `ProSE`, PR #9975 build | `56958d4e428fa3c7af4b8501f63d08075d0d59bea5e371b3a77b580cf7a263dd` |
| Sage | 0.15.0-beta.2, prebuilt `x86_64-unknown-linux-gnu` | `d1a96a7d14836f0846a55cede74456595169399abdc6a12e468f234eb4fcf7c0` |

Sage was installed as a release binary with no bundled source. Its behaviour was interpreted from
the upstream tag `v0.15.0-beta.2`; the tag-to-binary correspondence is inferred from the version
string the binary prints, not established cryptographically. This matters only for explanations
that cite Sage source (Section 8.1); every *count* in this report comes from Sage's own output.

Environment: WSL2 Ubuntu 24.04, 4 vCPU, 11 GiB RAM. Both engines were given 4 threads.

## 4. Parameter alignment

The ProSE invocation was built flag by flag against the Sage configuration. Alignment is exact for
every parameter both engines expose.

| Parameter | ProSE setting | Sage setting |
|---|---|---|
| Enzyme | `Trypsin` | `cleave_at = KR`, `c_terminal = true` |
| Specificity | `enzyme_specificity full` | `semi_enzymatic = false` |
| Peptide length | `min_size 7`, `max_size 50` | `min_len 7`, `max_len 50` |
| Missed cleavages | `2` | `2` |
| Peptide mass | `500`–`5000` | `500.0`–`5000.0` |
| Precursor tolerance | `±10 ppm` | `ppm [-10, 10]` |
| Precursor charge | `2`–`4` | `[2, 4]` |
| Isotope error | `-1`–`2` | `[-1, 2]` |
| Fragment tolerance | `0.02 Da` | `da [-0.02, 0.02]` |
| Fragment m/z window | `150`–`2000` | `fragment_min_mz 150`, `fragment_max_mz 2000` |
| Min. ion index | `2` | `2` |
| Max. fragment charge | `3` | `3` |
| Deisotope | `true` | `true` |
| Min. matched ions | `4` | `min_matched_peaks 4` |
| Fixed modification | `Carbamidomethyl (C)` | `C: 57.02146` |
| Variable modifications | `Oxidation (M)`, `Acetyl (Protein N-term)` | `M: 15.9949`, `[^: 42.010565` |
| Max. variable mods | `2` | `2` |
| Report top hits | `1` | `report_psms 1` |
| Decoys | internal, `DECOY_` prefix | internal, `rev_` tag |
| PSM FDR | `0.01` | 1% (`spectrum_q`) |
| Protein FDR | `0.01` | 1% (proteotypic peptides) |
| Protein grouping | not produced | `protein_grouping = true` at 1% peptide FDR |
| Quantification | none | LFQ disabled |

Residual, non-alignable differences are internal to each engine: ProSE's automatic
resolution-aware peak cap versus Sage's `min_peaks 15 / max_peaks 150`, and each engine's own
scoring function (OpenMS HyperScore versus `SageHyperScore` plus an LDA discriminant). These are
part of what is being compared, not configuration drift.

## 5. Execution protocol

* Strictly sequential: ProSE A → Sage A → ProSE B → Sage B, one process at a time.
* A guard re-verified the 11 pinned input/config/binary/script hashes before the first run and
  aborted on any mismatch; it also refused to start if `results/` was non-empty, so no run could
  overwrite another.
* Every run captured its exact command line, stdout, stderr, exit code and `/usr/bin/time -v`
  record. All four exited 0.
* Sage was invoked with `--disable-telemetry-i-dont-want-to-improve-sage`. The flag is accepted and
  documented by the binary, but this build prints no acknowledgement line; nothing in the logs
  confirms the opt-out beyond the accepted argument.
* The FASTA hash was re-checked after the last run and was unchanged.

## 6. Counting definitions

Every figure in Tables 1–5 is recomputed from the serialized outputs by `emit_tables.py`, which
also asserts the identities in Table 5; no table entry is copied from an engine's summary
line without being reconstructed. The few numbers quoted only in the text come from the audit
scripts and are marked as such.

* **PSM** — one `PeptideIdentification` element (ProSE idXML) or one TSV row (Sage).
* **Threshold** — 1% FDR, inclusive (`q ≤ 0.01`), at each engine's native PSM-level score
  (`score` under `score_type="q-value"` for ProSE; `spectrum_q` for Sage).
* **Peptide** — distinct sequence string as the engine writes it. ProSE embeds modifications in
  parentheses (`M(Oxidation)`); Sage uses bracketed mass shifts (`M[+15.9949]`). "Bare" means the
  modification token is stripped. Bare sequences still differ in N-terminal annotation conventions,
  so peptides are only ever compared within an engine.
* **Protein** — a surviving `ProteinHit` accession (ProSE) or an accession with
  `protein_q ≤ 0.01` (Sage). ProSE protein hits and Sage protein groups are different objects and
  are never pooled or ranked against each other.

Each hit is assigned to exactly one class, evaluated in this order:

| Class | Definition |
|---|---|
| `pure_target` | ≥1 accession, no entrapment, no decoy; ProSE `target_decoy = target` |
| `target_decoy_shared` | ProSE only: references both target and decoy proteins, none entrapment |
| `target_shared_entrapment` | ≥1 entrapment accession and ≥1 non-entrapment accession |
| `entrapment_only` | every accession is entrapment |
| `decoy` | every accession is a decoy (non-entrapment) |
| `decoy_entrapment` | every accession is a decoy of an entrapment protein |
| `no_protein_reference` | zero surviving accessions after protein-FDR finalization |

**"Not observable" is not zero.** ProSE deletes decoy PSMs and then removes dangling protein
references during protein-FDR finalization, so decoy and decoy-entrapment evidence cannot be
counted in its output at all. Those cells are marked *not observable*; only Sage's decoy columns
are measurements.

## 7. Results

### Table 1 — PSM level

All engine-native q thresholds are 1% and inclusive. ProSE removes decoy PSMs and their protein references at protein-FDR finalization, so decoy rows cannot be observed in its output; Sage writes targets and decoys alike.

| Metric | ProSE A | ProSE B | Sage A | Sage B |
|---|---:|---:|---:|---:|
| Engine log: PSM figure (as labelled by the engine) | 45,723 | 47,252 | 55,377 | 56,312 |
| Serialized rows, target class (engine-native threshold) | 45,723 | 47,252 | 54,830 | 55,756 |
| Serialized rows, all labels at threshold | not observable | not observable | 55,377 | 56,312 |
| **Pure target** | 44,807 | 46,352 | 54,654 | 55,614 |
| Target+decoy shared | 4 | 1 | — | — |
| Target-shared entrapment | 31 | 25 | 49 | 43 |
| Entrapment only | 19 | 18 | 127 | 99 |
| Target, no surviving protein reference | 862 | 856 | 0 | 0 |
| Decoy | not observable | not observable | 409 | 417 |
| Decoy-entrapment | not observable | not observable | 138 | 139 |

### Table 2 — Distinct peptide sequences

A peptide is counted once per class it appears in; the two engines write different sequence syntaxes, so only within-engine comparisons are made. *Modified* is the engine's own string (OpenMS `M(Oxidation)` vs Sage `M[+15.9949]`); *bare* has modification tokens stripped.

| Metric | ProSE A | ProSE B | Sage A | Sage B |
|---|---:|---:|---:|---:|
| Engine log: unique peptides | 31,991 | 32,465 | 36,875 | 37,269 |
| Recomputed set behind that figure | 31,991 | 32,465 | 36,875 | 37,269 |
| Bare sequences in the same set | 30,991 | 31,206 | 35,732 | 35,835 |
| **Pure target, modified** | 31,094 | 31,586 | 36,767 | 37,190 |
| Pure target, bare | 30,094 | 30,328 | 35,624 | 35,756 |
| Target+decoy shared | 4 | 1 | — | — |
| Target-shared entrapment | 17 | 14 | 25 | 25 |
| Entrapment only | 19 | 17 | 122 | 95 |
| Target, no surviving protein reference | 857 | 847 | 0 | 0 |

Rows 7–9 for Sage are taken at the PSM threshold (`spectrum_q ≤ 0.01`). Under Sage's own peptide threshold the same classes are smaller; the engine's reported peptide total and its pure-target part are unaffected by that difference:

| Sage peptide set | Sage A | Sage B |
|---|---:|---:|
| Unique peptides, `peptide_q ≤ 0.01` | 36,875 | 37,269 |
| Pure target | 36,767 | 37,190 |
| Target-shared entrapment | 24 | 22 |
| Entrapment only | 84 | 57 |
| Unique peptides, `spectrum_q ≤ 0.01` | 38,181 | 38,455 |

### Table 3 — Proteins and protein groups

ProSE's protein count is a picked-protein list over complete protein sets with no grouping step in the output; Sage's is the picked protein list restricted to proteotypic peptides, plus a separate IDPicker-style grouping. The two protein columns are different quantities and are not compared.

| Metric | ProSE A | ProSE B | Sage A | Sage B |
|---|---:|---:|---:|---:|
| Protein identifications at 1% FDR | 5,709 | 5,755 | 5,276 | 5,225 |
| — non-entrapment | 5,680 | 5,727 | 5,262 | 5,218 |
| — entrapment | 29 | 28 | 14 | 7 |
| Protein groups | not emitted (0 elements) | not emitted (0 elements) | 5,380 | 5,336 |
| — entrapment groups | — | — | 14 | 6 |

### Table 4 — Runtime and resources

From `/usr/bin/time -v` on the recorded runs; nothing was re-timed. Peak RSS is shown in MiB; the raw figures are 5,636,512 / 5,635,720 / 8,237,792 / 7,913,656 kB.

| Metric | ProSE A | ProSE B | Sage A | Sage B |
|---|---:|---:|---:|---:|
| Wall clock | 2:47.23 | 2:55.83 | 3:28.38 | 4:49.09 |
| User CPU (s) | 500.82 | 558.20 | 536.65 | 824.97 |
| System CPU (s) | 30.57 | 21.79 | 33.62 | 35.45 |
| CPU utilisation | 317% | 329% | 273% | 297% |
| Peak RSS (MiB) | 5,504 | 5,504 | 8,045 | 7,728 |

Engine-internal stages (from the same logs): ProSE index build 44.3 s / 38.8 s, search 86.9 s / 104.3 s, FDR 0.6 s / 0.9 s; Sage search 94.8 s / 163.0 s.

### Table 5 — Log-to-output reconciliation

Each engine summary figure is recomputed from the serialized output. A zero delta means the logged number is fully explained by the stated reconstruction.

| Quantity | Logged | Reconstructed | Δ |
|---|---:|---:|---:|
| ProSE A PSMs (logged `Matched`) | 45,723 | 45,723 | 0 |
| ProSE A unique peptides | 31,991 | 31,991 | 0 |
| ProSE A proteins | 5,709 | 5,709 | 0 |
| Sage A PSM figure (all labels at threshold) | 55,377 | 55,377 | 0 |
| Sage A unique peptides (`peptide_q`) | 36,875 | 36,875 | 0 |
| Sage A proteins | 5,276 | 5,276 | 0 |
| Sage A protein groups | 5,380 | 5,380 | 0 |
| ProSE B PSMs (logged `Matched`) | 47,252 | 47,252 | 0 |
| ProSE B unique peptides | 32,465 | 32,465 | 0 |
| ProSE B proteins | 5,755 | 5,755 | 0 |
| Sage B PSM figure (all labels at threshold) | 56,312 | 56,312 | 0 |
| Sage B unique peptides (`peptide_q`) | 37,269 | 37,269 | 0 |
| Sage B proteins | 5,225 | 5,225 | 0 |
| Sage B protein groups | 5,336 | 5,336 | 0 |

## 8. The three discrepancies, and how each resolves

### 8.1 Sage's logged PSM figure includes decoys

Sage logs `discovered 55377 target peptide-spectrum matches at 1% FDR` (A) and `56312` (B). The
serialized output contains 54,830 and 55,756 target rows at `spectrum_q ≤ 0.01`. The gap is exactly
the decoy rows that fall inside the same q block: 547 (A) and 556 (B).

The mechanism is in `spectrum_q_value` (`crates/sage/src/ml/qvalue.rs`, tag `v0.15.0-beta.2`): the
returned counter is incremented for every feature whose cumulative-minimum q is ≤ 0.01, with no
test of `label == 1`.

```rust
for score in scores.iter_mut().rev() {
    q_min = q_min.min(score.spectrum_q);
    score.spectrum_q = q_min;
    if q_min <= 0.01 { passing += 1; }   // no label test
}
passing
```

`runner.rs` logs that return value verbatim as "target peptide-spectrum matches". The arithmetic
closes to the unit on both runs (Table 5), so this is a reconciliation rather than a coincidence.

**Consequence.** The logged figure is a counter over the target+decoy block. It is named here
**Sage logged PSM counter (includes decoys)** and is never used as a target count. The target
count — **Sage target PSMs from serialized TSV (label == 1, `spectrum_q ≤ 0.01`)** — is
54,830 / 55,756; the **Sage pure-target PSMs** figure, which also excludes entrapment-mapped
PSMs, is 54,654 / 55,614.

### 8.2 ProSE target identifications without surviving protein references

ProSE logs `45723 target / 0 decoy PSMs`. Of those 45,723, **862 (A) / 856 (B)** reference no
surviving protein: their proteins failed picked-protein FDR or were decoys, and
`removeDanglingProteinReferences` cleared the refs. They retain `target_decoy = "target"` and
q ≤ 0.01 (max 0.009995 (A) / 0.009968 (B), median 0.005033 / 0.004531, per
`audit/counts2.csv`), so they are genuine
target-classified peptide identifications — but no protein in the output supports them.

The first version of this comparison counted them as protein-supported targets. They are now
reported separately as **target PSMs without surviving protein references**. They are not
discarded: the peptide evidence exists. They are not counted as pure target either: the output does
not attribute them to a protein. On the protein side they are already absent, which is why
ProSE's 5,709 / 5,755 protein hits do not shrink.

The same correction applies at peptide level: 857 (A) / 847 (B) distinct sequences.

### 8.3 ProSE's logged unique-peptide figure is a union over classes

ProSE logs 31,991 (A) / 32,465 (B) unique peptides. That is the size of the union of *all*
post-FDR classes, not the pure-target set:

| Run | Pure target | No protein ref | Shared entrapment | Entrapment only | Target+decoy | Sum |
|---|---:|---:|---:|---:|---:|---:|
| A | 31,094 | 857 | 17 | 19 | 4 | 31,991 |
| B | 31,586 | 847 | 14 | 17 | 1 | 32,465 |

Both sums are exact, matching source: `updateFinalStats` accumulates every top hit without a class
filter. The engine summary is therefore internally consistent; it simply answers a different
question from "how many pure-target peptides were identified".

### 8.4 A fourth difference, in structure rather than arithmetic

Sage runs a separate peptide-level FDR stage (`peptide_q`) before protein inference. Its reported
36,875 / 37,269 peptides come from that stage, which is not a subset of the PSM-level set: it admits
3,200 (A) / 3,412 (B) rows that fail `spectrum_q ≤ 0.01` and excludes 1,318 / 1,199 that pass it
(measured by `audit/cross_threshold.py`, which is where those four numbers are reproduced).
ProSE has no peptide-level stage; its peptides inherit the PSM q-value. The two peptide totals are
therefore produced by different procedures even though both are "unique peptides at 1% FDR".

## 9. What the numbers do and do not show

Three descriptive observations survive the audit:

1. Sage reports more pure-target PSMs and more pure-target peptides than ProSE in both conditions.
2. ProSE reports more non-entrapment protein identifications than Sage in both conditions.
3. The two engines attribute different amounts of evidence to the entrapment proteins.

These are observations about two specific pipelines on two specific injections. They are not
evidence of superior sensitivity: the engines differ in scoring, decoy generation, q-value
estimation, peptide-level FDR, protein inference and grouping, and no ground truth for the
endogenous peptides exists in this design.

Two comparisons are explicitly *not* made:

* **ProSE protein hits vs Sage protein groups.** ProSE serializes no grouping (0 `ProteinGroup`
  elements) and counts any supporting peptide; Sage counts proteotypic-only support and then groups.
  5,709 / 5,755 and 5,380 / 5,336 are different quantities.
* **Peptide totals across engines.** The sequences use different modification syntaxes and different
  N-terminal conventions; no cross-engine peptide identity was computed.

Entrapment counts are in the tens to low hundreds of PSMs. They are not precise false-discovery
estimates: with one injection per condition and a single decoy generation scheme per engine, they
support only the statement that entrapment evidence was detected and how much of it each engine
reported.

## 10. Limitations

1. **Sage source-to-binary correspondence is inferred.** The binary ships without source; the tag
   was chosen from the version string. The numeric reconciliation makes a mismatch unlikely, but it
   is not cryptographic.
2. **Sage telemetry suppression is unconfirmed by the binary.** The flag was passed and is
   documented in `--help`; 0.15.0-beta.2 prints no acknowledgement.
3. **ProSE decoy evidence is unrecoverable.** Finalization deletes decoys and dangling references,
   so decoy-level classes are *not observable*, not zero.
4. **The 862 / 856 unreferenced ProSE PSMs cannot be re-attributed** without rerunning the search
   with protein inference retained, which was out of scope.
5. **One injection per condition, no repeats.** The experiment establishes observed results, not
   run-to-run reproducibility.
6. **Entrapment denominators are small**, so entrapment ratios are coarse.
7. **Residual parameter differences remain** in peak handling and scoring, by construction.

None of these invalidate the comparison; each bounds what it can be used to claim.

## 11. Reproducing this report without rerunning any search

Everything in this report is derived from the frozen outputs by one script. The outputs themselves
stay in the run root used for the experiment, which is not part of this repository:

```bash
bash reproduce.sh /path/to/prose-sage-pxd028735   # defaults to ~/prose-sage-pxd028735
```

`reproduce.sh` performs three independent checks and exits non-zero on any mismatch:

1. `sha256sum -c hashes.sha256` over the four result files, two Sage JSON sidecars, the FASTA, both
   mzML files, both Sage configs and both binaries — 13 items, no writes. Eleven entries are
   relative to the run root; the last two are absolute paths to the pinned ProSE and Sage
   binaries, which are not redistributed, so on another machine those two entries fail unless
   the identical builds are present.
2. Re-runs `emit_tables.py`, which recomputes every table and every class count from the raw idXML
   and TSV, asserts the 18 log-to-output identities, and diffs the regenerated tables against the
   ones printed in Section 7.
3. Rebuilds this document from `EXPERIMENT_REPORT.template.md` plus the regenerated tables and diffs
   it against the file on disk.

To regenerate the report after an intentional change, rebuild the table block and then the report:

```bash
python3 emit_tables.py /path/to/prose-sage-pxd028735 tables.md
python3 build_report.py EXPERIMENT_REPORT.template.md tables.md EXPERIMENT_REPORT.md
```

Rerunning the searches themselves is neither required nor supported by this document; the run
scripts and the guard are archived in the run root under `scripts/` and `audit/` for provenance
only.

## 12. Artifacts and hashes

The experimental artifacts — results, logs, data, configurations and run scripts — live outside
the git worktree, which neither the experiment nor the audit modified: HEAD
`86ea502f1035990dc4f6eaa50abbafe89bdb6c39` at audit time and again on re-verification
(2026-11-28), six status entries that are CRLF-only, zero substantive changes. This report and
its generation scripts are the only additions to the repository; they read the frozen outputs
and never produce them. The raw outputs and the mzML inputs are not part of this repository.

Raw results (SHA-256):

```
1ce35721a4dedc9878a76f0c753cc4ee5bf0ec0c6248d0948581ef348415c30b  results/prose_A/ProSE_A.idXML
19f64705513e3d4cf904346c4e1f46018267a08d0d654f3e387a397ef600cb7f  results/prose_B/ProSE_B.idXML
bf72afde43d0f495539bfc1dcefe8cf1281f824639889b3e6bf9d5447c034c1a  results/sage_A/results.sage.tsv
1d9630969c788c9e2a093c20017f3280949357bf16434cb97a10ad1e9c3ae41f  results/sage_B/results.sage.tsv
f144a889c8e100d166447e3373a91723ce3a2777a62156d975c368ff6ca74424  results/sage_A/results.json
8b3f8bfde3b21b4df0249ed8f19e698d67159b8e2f6d91243f80036c021389fd  results/sage_B/results.json
```

Paths other than "this directory" are relative to the run root (`$HOME/prose-sage-pxd028735` on
the audit machine); they are provenance, not repository content.

| Location | Contents |
|---|---|
| this directory | this report, its template, `emit_tables.py`, `build_report.py`, `reproduce.sh`, `hashes.sha256`, `tables.md` (the generated Tables 1–5 block) |
| `audit/REPORT.md` | the independent read-only audit, with per-claim evidence tags |
| `audit/counts2.csv` | authoritative per-class counts, one row per metric |
| `audit/counts.csv` | first audit pass, superseded classification, retained for provenance |
| `audit/freeze_manifest.txt` | 13 expected-vs-actual hashes, file sizes, mtimes, worktree state |
| `audit/COMMANDS.md` | exact audit commands |
| `logs/` | per-run command line, stdout, stderr, exit code, `/usr/bin/time -v` |
| `results/` | the six raw outputs listed above |
| `scripts/` | run scripts, parameter set, preflight guard |
