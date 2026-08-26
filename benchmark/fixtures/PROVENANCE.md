# Fixture provenance

These fixtures are copies of small files from the OpenMS repository's own test
data, vendored here so the benchmarks are fully self-contained (no network
download in CI) and deterministic.

## Smoke fixture (DDA)

| File | Source (OpenMS repo) | Size | Content |
|---|---|---|---|
| `smoke_input.mzML` | `src/tests/topp/THIRDPARTY/CometAdapter_3.mzML` | 2.0 MB | real search-engine test spectra that match **both** target and decoy sequences |
| `smoke_input.fasta` | `src/tests/topp/THIRDPARTY/CometAdapter_3.fasta` (DECOY_ prefix stripped) | 816 B | 3 real protein sequences (Q5QTS3, P61313, SRSF3) |

Copied from OpenMS commit `f1768367fa66f7901b4fa78a9ebece64b2ce9024`
(upstream develop, 2026-08-12).

### Why this pair

The previous fixture (`SimpleSearchEngine_1.mzML` + `DecoyDatabase_1.fasta`)
let `DecoyDatabase` and `CometAdapter` execute, but the spectra were unrelated
to the FASTA, so Comet found no PSMs and `PercolatorAdapter` was always
skipped. The real gap in the smoke test was that Percolator never ran.

`CometAdapter_3.mzML` is the input of OpenMS' own CometAdapter test
(`TOPP_CometAdapter_3` in `third_party_tests.cmake`). Its spectra were recorded
from all three proteins, so after `DecoyDatabase` appends reversed decoys,
`CometAdapter` finds **both target and decoy PSMs** (at `-num_hits 5
-missed_cleavages 3`, the exact parameters OpenMS' test uses) and
`PercolatorAdapter` can actually train on the mixture.

The FASTA is vendored with the `DECOY_` prefix stripped: in OpenMS' test data
the prefix marks *real* proteins as decoys for its own diff-based test; here
`DecoyDatabase` must be free to append its own reversed decoys, so the input
must be decoy-free.

## OpenSwath fixture (DIA)

| File | Source (OpenMS repo) | Size | Content |
|---|---|---|---|
| `OpenSwathWorkflow_1_input.mzML` | `src/tests/topp/OpenSwathWorkflow_1_input.mzML` | 288 KB | DIA (SWATH-MS) spectra with 5 SWATH windows (400-525 m/z) |
| `OpenSwathWorkflow_1_input.TraML` | `src/tests/topp/OpenSwathWorkflow_1_input.TraML` | 26 KB | Transition library for 7 peptides |
| `OpenSwathWorkflow_1_input.trafoXML` | `src/tests/topp/OpenSwathWorkflow_1_input.trafoXML` | 376 B | Pre-computed RT transformation |
| `swath_windows.txt` | `src/tests/topp/swath_windows.txt` | 52 B | SWATH window definitions |

### Reference outputs (for correctness validation)

| File | Source | Size | Content |
|---|---|---|---|
| `OpenSwathWorkflow_1_output.featureXML` | `src/tests/topp/OpenSwathWorkflow_1_output.featureXML` | 36 KB | 6 identified features (24 feature elements total) |
| `OpenSwathWorkflow_1_output.json` | `src/tests/topp/OpenSwathWorkflow_1_output.json` | 145 B | QC metrics (charge distribution) |
| `OpenSwathWorkflow_1_output.chrom.mzML` | `src/tests/topp/OpenSwathWorkflow_1_output.chrom.mzML` | 47 KB | Extracted chromatograms |

### Why this fixture

- Exercises DIA (Data-Independent Acquisition) proteomics — completely
  different from the DDA smoke benchmark
- Test data is already committed in OpenMS (topp test `TOPP_OpenSwathWorkflow_1`)
- 7 peptides × 3 transitions each = 21 transitions, producing 6 features
- Includes pre-computed RT transformation for reproducibility
- Deterministic: same inputs produce same outputs

### What this benchmarks

- SWATH data reading and window handling
- Chromatogram extraction (OpenSwathChromatogramExtractor)
- MRM scoring (OpenSwathScoring)
- Feature picking (MRMTransitionGroupPicker)
- QC metrics generation

## Reproducing locally

```bash
# from a checkout of OpenMS at the pinned SHA
cp src/tests/topp/OpenSwathWorkflow_1_input.mzML <this dir>/
cp src/tests/topp/OpenSwathWorkflow_1_input.TraML <this dir>/
cp src/tests/topp/OpenSwathWorkflow_1_input.trafoXML <this dir>/
cp src/tests/topp/swath_windows.txt <this dir>/
cp src/tests/topp/OpenSwathWorkflow_1_output.featureXML <this dir>/
cp src/tests/topp/OpenSwathWorkflow_1_output.json <this dir>/
cp src/tests/topp/OpenSwathWorkflow_1_output.chrom.mzML <this dir>/
```

The full PXD028735 dataset replaces these fixtures in the extended benchmark
milestone.
