# Smoke fixture provenance

These fixtures are copies of small files from the OpenMS repository's own test
data, vendored here so the smoke benchmark is fully self-contained (no network
download in CI) and deterministic.

| File | Source (OpenMS repo) | Size | Content |
|---|---|---|---|
| `smoke_input.mzML` | `src/tests/topp/THIRDPARTY/CometAdapter_3.mzML` | 2.0 MB | real search-engine test spectra that match **both** target and decoy sequences |
| `smoke_input.fasta` | `src/tests/topp/THIRDPARTY/CometAdapter_3.fasta` (DECOY_ prefix stripped) | 816 B | 3 real protein sequences (Q5QTS3, P61313, SRSF3) |

Copied from OpenMS commit `f1768367fa66f7901b4fa78a9ebece64b2ce9024`
(upstream develop, 2026-08-12).

## Why this pair

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

## Reproducing locally

```bash
# from a checkout of OpenMS at the pinned SHA
cp src/tests/topp/THIRDPARTY/CometAdapter_3.mzML  <this dir>/smoke_input.mzML
sed 's/^>DECOY_/>/' src/tests/topp/THIRDPARTY/CometAdapter_3.fasta \
  > <this dir>/smoke_input.fasta
```

Both files are still small (2 MB total). The full PXD028735 dataset replaces
these fixtures in the extended benchmark milestone.
