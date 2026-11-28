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
