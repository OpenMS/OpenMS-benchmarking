# Smoke fixture provenance

These fixtures are copies of small files from the OpenMS repository's own test
data, vendored here so the smoke benchmark is fully self-contained (no network
download in CI) and deterministic.

| File | Source (OpenMS repo) | Size | Content |
|---|---|---|---|
| `smoke_input.mzML` | `src/tests/topp/AssayGeneratorMetabo_decoy_generation_input.mzML` | 191 KB | 51 spectra (17 MS1 / 34 MS2), real indexed mzML |
| `smoke_input.fasta` | `src/tests/topp/FileInfo_17_input.fasta` | 2.8 KB | 11 protein sequences (crab crystallin) |

Copied from OpenMS commit `f1768367fa66f7901b4fa78a9ebece64b2ce9024`
(upstream develop, 2026-08-12).

Both files are intentionally tiny. The mzML has enough MS2 spectra for
`CometAdapter` to perform a real search (the point of the smoke test), but
too few PSMs for `PercolatorAdapter` to train on — which is why the Percolator
stage is optional in the smoke benchmark. The real PXD028735 dataset replaces
these fixtures in the extended benchmark milestone.
