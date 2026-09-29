# Design report: ProSE + PeptDeep entrapment benchmark

Status: design only — no code implemented yet. Established from
`OpenMS-benchmarking` (branch `feat/openswath-normalization-and-cleanup`) and
OpenMS PR #9975 fetched locally as branch `pr-9975` (HEAD `e4b9609c95`,
`PeptDeepRescoring` + `peptdeep:*` ProSE parameters + `WITH_ONNX` build plumbing).

## 1. Benchmark architecture conventions (what the new runner must follow)

- Runners are standalone bash scripts: `benchmark/run_smoke_benchmark.sh`,
  `benchmark/run_openswath_benchmark.sh`. Both share the same helper block:
  `log`, `to_seconds`, `record_stage`, and `run_stage`.
- **Timing/RSS convention (both runners):** every stage runs under
  `/usr/bin/time -v`; wall = `Elapsed (wall clock) time`, cpu = user+system,
  peak RSS = `Maximum resident set size` in **KB** (fallback: `date +%s.%N`
  wall only, cpu 0, RSS 0 when GNU time is absent). Per-stage rows go to
  `stages.tsv`; an inline `python3` heredoc converts `meta.txt` + `stages.tsv`
  (+ pipeline outputs) into one benchmark JSON; verdict = all required stages
  passed. `run_stage <name> <required> <cmd...>` aborts the script only when a
  required stage fails.
- Definitions live in `benchmark/definitions/*.json` (`id`, `name`,
  `description`, `runner`, `fixtures`, `normalization`), validated by
  `benchmark/validate_definitions.py` (runner path, fixture paths, and
  normalization function must exist).
- Normalization: `benchmark/report/report_generate.py normalize <kind> ...`
  produces v2 results (`schema openms-benchmarking/report/v2`,
  `identity/performance/metrics`); `performance.wall_time_s` = **sum of stage
  walls**, `performance.peak_rss_kb` = **max stage peak**. `normalize_openswath`
  is the precedent for a runner that emits raw outputs later normalized.
- CI: `.github/workflows/benchmark.yml` builds OpenMS at a pinned SHA
  (`deps-ubuntu.sh --skip-gui-deps`, no GUI, no docs), stages
  `THIRDPARTY` engines into `_thirdparty`, uploads `build/bin`, `build/lib`,
  `share/OpenMS`, `_thirdparty` as one artifact, then runs benchmarks with
  `OPENMS_BIN`, `ENGINES_DIR`, `LD_LIBRARY_PATH=build/lib`,
  `OPENMS_DATA_PATH=share/OpenMS`.

## 2. PR #9975 — exact CLI/configuration

### Baseline ProSE

```
ProSE -in <run.mzML> -database <target+entrapment.fasta> \
      -out_idxml baseline.idXML \
      -percolator_executable <ENGINES_DIR>/percolator \
      -threads N \
      -Search:<common search params> \
      -Search:peptdeep:enable false        # the default; set explicitly for the record
```

- `-percolator_executable` non-empty triggers rescoring: ProSE shells out to its
  sibling `PercolatorAdapter` with `-score_type q-value -train_best_positive
  -post_processing_tdc`; PercolatorAdapter defaults to its **in-process**
  backend (`-use_subprocess false`), so the external `percolator` binary is only
  needed as the trigger argument. Requires decoys in the DB (`-Search:decoys`
  default `auto` generates `DECOY_`-prefixed decoys if none present) and
  ≥ 100 PSMs (below that, ProSE warns and falls back to HyperScore).
- With Percolator enabled, ProSE defers `FDR:PSM`/`FDR:protein` to
  post-rescoring (both default 0 anyway). The output idXML therefore contains
  **all** PSMs with q-values and decoys retained — the 1 % cut is applied at
  metric-extraction time, not by the tool.
- Search params (identical in both arms): `-Search:precursor:mass_tolerance_*`,
  `-Search:fragment:mass_tolerance[_unit]`, `-Search:enzyme`,
  `-Search:peptide:missed_cleavages`, `-Search:modifications:*`,
  `-Search:report:top_hits 1`, `-Search:annotate:PSM ALL` (default; required —
  PeptDeep MS2 features come from the PSM's own peak annotations).

### ProSE + PeptDeep (MS2 + RT)

Identical command plus:

```
      -Search:peptdeep:enable true \
      -Search:peptdeep:ms2_model models/peptdeep_ms2_dynamic.onnx \
      -Search:peptdeep:rt_model models/peptdeep_rt_dynamic.onnx \
      -Search:peptdeep:instrument QE
```

- `peptdeep:enable=true` is the gate (default `false`); the step also requires
  both model paths, so baseline needs no models and no ONNX.
- Relative model names resolve via `File::find(name, {exePath + "../share/OpenMS"})`
  → `<build>/share/OpenMS/models/…`. CMake (`WITH_ONNX=ON`) downloads
  `peptdeep_{ccs,ms2,rt}_dynamic.onnx` from
  `http://archive.openms.de/openms/models` (SHA256-pinned) into that dir.
- Remaining knobs stay at defaults: `nce -1` (auto NCE grid around the mzML
  value), `rt_model_type b_spline`, `batch_size 500`; ONNX inference threads =
  ProSE's OpenMP thread count. Only `instrument` should be pinned per dataset
  (`QE` for the Q Exactive HF/Astral runs used in the PR evaluation; options
  Lumos/QE/timsTOF/SciexTOF).
- Requires an ONNX build: `cmake -DWITH_ONNX=ON` (vcpkg manifest feature
  `onnxruntime`, or system ONNX Runtime via `cmake/Modules/FindONNXRuntime.cmake`).
  Without ONNX, the peptdeep arm warns and silently produces a baseline result —
  the runner must fail the run if `extra_features` lacks the five features.

### Outputs

- Exactly one idXML per input file via `-out_idxml` (e.g. `baseline.idXML`,
  `peptdeep.idXML`). Optional `-out_merged` is **not** used: PeptDeep NCE/RT
  calibration is per identification run and must see unmerged runs (PR note).
- Optional machine-readable side outputs (free to use):
  `-summary_out <file>.yaml` + `-no_summary` (ProSE run summary, see §6),
  `-out_pin`, `-out_parquet`.

## 3. Where q-values live

Per `PeptideHit` (in-process backend, `Percolator.cpp` L1305-07 /
`PercolatorAdapter.cpp` L1088-1133):

- meta values `percolator_score`, `percolator_q_value`, `percolator_pep`;
  serialized in idXML as `UserParam`s;
- CV params `MS:1001492` (score), `MS:1001491` (q-value), `MS:1001493` (PEP);
- because ProSE calls PercolatorAdapter with `-score_type q-value`, the hit's
  **main score is the q-value** and the run has
  `score_type="q-value"`, `higher_score_better="false"`.

Extraction rule: read the run's `score_type`; if it is `q-value`, `q =
hit/@score`; otherwise fall back to `UserParam percolator_q_value`. Include
`q <= 0.01`.

## 4. Identifying target vs entrapment PSMs

- Standard OpenMS marker: `target_decoy` UserParam on each hit =
  `target` / `decoy` / `target+decoy` (`PeptideHit::setTargetDecoyType`, set by
  PeptideIndexing). Decoys are excluded by `target_decoy != target`
  (`target+decoy` treated as non-target — conservative).
- **Entrapment has no first-class support anywhere in OpenMS** (no code, no
  CHANGELOG mention). Reliable identification must therefore be accession-based
  on the benchmark side:
  1. The search FASTA contains target proteins + entrapment proteins whose
     accessions are distinguishable by convention (e.g. `ENTRAPMENT_` prefix or
     a recorded accession set). The entrapment proteins themselves must be
     generated outside OpenMS (shuffled/perturbed sequences, seeded &
     versioned) — **open prerequisite, see §8**.
  2. idXML peptide hits carry their protein mapping as `<ProteinHit
     accession="…"/>` children. Build the entrapment accession set from the
     FASTA once; a PSM is *entrapment* if any mapped accession is in the set,
     *target* only if all mapped accessions are targets. (Treating
     target∩entrapment shared peptides as entrapment is the conservative
     choice; flagged in §8.)
- Sequence-based membership checks are unnecessary; accession sets are exact
  and O(1).

## 5. Counting target PSMs / unique peptides at q ≤ 0.01

Per arm, from the arm's idXML (stdlib `xml.etree`, like the openswath runner):

- PSM = top hit of each `PeptideIdentification` (ProSE reports
  `top_hits=1`; Percolator stage runs `keepNBestHits(…, 1)`).
- Keep hits with `q <= 0.01` and `target_decoy == "target"` (target =
  all mapped accessions outside the entrapment set).
- `target_psms_at_1pct_fdr` = count of kept PSMs.
- `target_peptides_at_1pct_fdr` = distinct `sequence` attribute values
  (OpenMS notation includes modifications) among kept PSMs.
- Diagnostic (same parse, near-free): `entrapment_psms_at_1pct_fdr` and
  `entrapment_peptides_at_1pct_fdr` — the target:entrapment ratio is the
  entrapment false-discovery proxy. Not headline metrics.
- The five `peptdeep:*` scores (`ms2_cosine`, `ms2_spectral_angle`,
  `ms2_pearson`, `ms2_frac_pred_found`, `rt_abs_error`) appear as UserParams on
  each hit and in the SearchParameters `extra_features` list. They are
  **internal Percolator features only** — never reported as benchmark metrics.
  Their presence in `extra_features` of the peptdeep arm's idXML is the
  assertion that the arm actually ran (see §7 guard).

## 6. Timing: ProSE summary vs `/usr/bin/time`

ProSE already writes a run summary (`-summary_out file.yaml`, `-no_summary`):
`shared.seconds_total`, `seconds_index_build`, per-file
`timing_seconds.{calibration,search,fdr}`, `target_psms`, `decoy_psms`,
`achieved_psm_fdr`, `unique_peptides` (#9642, present on develop and on the PR
branch). **Reuse it as auxiliary evidence, not for headline metrics:**

- it has **no RSS**;
- it is unclear whether `seconds_total` covers the external
  PercolatorAdapter invocation and ONNX model load (uncertainty §8.6);
- the repo convention measures whole stage invocations, which is exactly what
  the headline `wall_time_s`/`peak_rss_mb` should mean.

**Decision: `/usr/bin/time -v` around each ProSE invocation is the headline
measurement** (`wall_time_s`, `peak_rss_mb = peak_rss_kb / 1024`), recorded per
arm via the existing `run_stage` helper; the summary YAML is captured as an
artifact and its `seconds_*` fields recorded as informational only.

## 7. Planned minimal implementation

| File | Change |
|---|---|
| `benchmark/run_prose_benchmark.sh` | New runner, copied helper-for-helper from `run_openswath_benchmark.sh`. Env: `OPENMS_BIN`, `ENGINES_DIR`, `OPENMS_SHA`, `RESULTS_DIR`, `PROSE_INPUT` (mzML), `PROSE_FASTA` (target+entrapment), `PROSE_ENTRAPMENT_PREFIX` (default `ENTRAPMENT_`), `PROSE_THREADS` (default 4; pinned identically for both arms). Stages: `prose_baseline` → `baseline.idXML`; `prose_peptdeep` → `peptdeep.idXML` (identical args except the four `peptdeep:*` flags); then inline Python parses both idXMLs, asserts the peptdeep arm's `extra_features` contains the five features, and writes `prose.json` with the four headline metrics per arm + entrapment diagnostics + stage metrics. |
| `benchmark/definitions/prose_peptdeep.json` | Definition (`id: prose_peptdeep`), runner path, normalization pointing at `report_generate.py::normalize_prose_peptdeep`. |
| `benchmark/report/report_generate.py` | One new `normalize prose-peptdeep` subcommand mirroring `normalize_openswath` (reads the runner's results dir, emits v2). Deferred if the report integration is not wanted yet. |
| `.github/workflows/benchmark.yml` | New job (or inputs on the existing one): build with `-DWITH_ONNX=ON` for the peptdeep arm, upload `share/OpenMS/models` with the artifact, install runtime onnxruntime. Dataset inputs via workflow inputs. Only touched when CI execution is wanted. |

No OpenMS core changes. The benchmark pins: same mzML, same FASTA, same search
params, same `percolator_executable`, same `threads`; the only difference
between arms is `peptdeep:enable` (+ model paths/instrument).

## 8. Uncertainties / decisions to confirm

1. **Entrapment DB generation** — not in OpenMS; must be produced benchmark-side
   (seeded shuffle/perturbation per entrapment convention, accession prefix).
   Which construction, and the accession marker?
2. **Shared peptides** (map to both target and entrapment proteins): count as
   entrapment (recommended, conservative) or exclude from both?
3. **ONNX build in benchmark CI** — the current workflow builds default flags.
   Options: vcpkg manifest (`onnxruntime` feature, matches PR CI) vs system
   onnxruntime + `FindONNXRuntime.cmake`; plus model download requires network
   access to `archive.openms.de` at build time.
4. **Benchmark dataset** — PR evaluated two archive.openms.de DDA runs (Q
   Exactive HF; Orbitrap Astral). Which run(s) become `PROSE_INPUT`, and are
   they vendored (like fixtures) or downloaded at run time? Entrapment FASTA
   follows from the dataset's target FASTA.
5. **PR branch volatility** — `pr-9975` HEAD is `e4b9609c95` (12 commits, still
   open, includes unrelated CI/vcpkg refactors vs develop). The benchmark SHA
   input should pin the PR branch tip used for the first run.
6. **ProSE summary coverage** — `seconds_total` may not include the
   Percolator subprocess/ONNX load; informational only (§6), verify once
   against `/usr/bin/time` on the first run.
7. **q ≤ 0.01 vs < 0.01** — recommend inclusive `<=` (ties at exactly 0.01
   kept); ProteoBench-style comparability to be confirmed.
