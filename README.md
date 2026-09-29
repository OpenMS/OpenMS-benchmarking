# OpenMS Benchmarking

[![CI](https://github.com/OpenMS/OpenMS-benchmarking/actions/workflows/benchmark.yml/badge.svg)](https://github.com/OpenMS/OpenMS-benchmarking/actions/workflows/benchmark.yml)

OpenMS Benchmarking is the benchmarking infrastructure for
[OpenMS](https://github.com/OpenMS/OpenMS), developed in the context of
[OpenMS Issue #8788](https://github.com/OpenMS/OpenMS/issues/8788). It builds
OpenMS from a pinned commit in GitHub Actions, runs the generated binaries
through small, fully self-contained mass-spectrometry benchmarks - currently a
DDA smoke pipeline and a DIA OpenSwath workflow - and records machine-readable,
schema-versioned results for every run. A generic report generator compares
each run against the stored baseline and flags regressions, so that OpenMS
developers can see the performance impact of a change, not just its test
status. The repository is intended for OpenMS developers, maintainers and
contributors evaluating changes to OpenMS.

Table of Contents
--------

- [Features](#features)
- [Benchmarks](#benchmarks)
  - [Smoke benchmark](#smoke-benchmark)
  - [OpenSwath DIA benchmark](#openswath-dia-benchmark)
  - [ProteoBench reference results](#proteobench-reference-results)
- [Architecture](#architecture)
- [Running benchmarks](#running-benchmarks)
- [Results and reporting](#results-and-reporting)
- [Continuous integration](#continuous-integration)
- [Development](#development)
- [Testing](#testing)
- [Repository structure](#repository-structure)
- [Scope](#scope)
- [Resources](#resources)

Features
--------

- Reproducible Linux builds of OpenMS from any pinned commit SHA, with the
  binaries and search engines shipped as a CI artifact
- Compiler cache (ccache) keyed by the pinned SHA, so repeated benchmarks of
  the same SHA skip the cold build
- DDA smoke benchmark: `DecoyDatabase` -> `CometAdapter` -> `PercolatorAdapter`
- DIA OpenSwath benchmark of `OpenSwathWorkflow`, validated against OpenMS'
  own reference outputs
- Per-stage performance measurement: wall time, CPU time and peak RSS
- Schema-versioned JSON results; older v1 files are promoted automatically
- Benchmark-agnostic HTML report comparing the current run against the stored
  baseline, with a regression verdict
- Standard-library-only Python tooling

Benchmarks
--------

Benchmark configurations live in `benchmark/definitions/`; each benchmark is
executed by a runner script that writes raw results to a results directory.

### Smoke benchmark

The smoke benchmark is a small but real DDA pipeline - `DecoyDatabase` ->
`CometAdapter` -> `PercolatorAdapter` - executed with the binaries built from
the pinned OpenMS SHA. It proves that the generated binaries are functional
and records per-stage wall time, CPU time and peak RSS. The Percolator stage
is optional: the smoke fixture produces too few PSMs for Percolator to train
on, so a skip is recorded rather than a failure. Runner:
`benchmark/run_smoke_benchmark.sh` (definition: `smoke.json`).

### OpenSwath DIA benchmark

The OpenSwath benchmark exercises `OpenSwathWorkflow` - DIA spectral library
search with retention-time calibration - on fixtures vendored from OpenMS'
own test data (mzML spectra, TraML transition library, RT transformation and
SWATH window list). The vendored fixtures include OpenMS' reference outputs,
which the benchmark uses to validate correctness alongside the performance
measurements. It runs in CI as a separate job after the OpenMS build. Runner:
`benchmark/run_openswath_benchmark.sh` (definition: `openswath.json`). Raw
results are normalized with `normalize openswath` and stored under
`benchmark/results/openswath_dia/openms/`, giving OpenSwath the same
baseline/history/comparison lifecycle as the smoke benchmark.

### ProteoBench reference results

ProteoBench results - produced externally by a local ProteoBench scorer
(quantms on the PXD028735 dataset) - are stored under
`benchmark/results/tools/` and pulled into the HTML report as external
reference context. They are not an OpenMS version-to-version baseline, and the
report does not rank tools or workflows. Definition: `proteobench.json` (no
runner - results are generated externally and normalized locally).

Architecture
--------

```
OpenMS @ pinned SHA   (built in CI, ccache-accelerated)
        |
        v
benchmark/definitions/*.json       benchmark configuration
        |    validated by benchmark/validate_definitions.py
        v
benchmark/run_*_benchmark.sh       benchmark execution
        |
        v
raw benchmark results              smoke.json / openswath.json, stages.tsv, logs
        |
        v
report_generate.py normalize       convert raw output to the common schema
        |
        v
benchmark/results/                 stored, schema-versioned results
        |
        v
report_generate.py render          compare current run vs stored baseline
        |
        v
report.html                        self-contained HTML report
```

Definitions describe benchmark configuration; runner scripts execute the
benchmarks and emit raw results; normalization converts benchmark output into
the common result format; rendering produces the human-readable report.

Running benchmarks
--------

Both runners require a local OpenMS build (the same artifacts CI produces).

Smoke benchmark (DDA):

```bash
OPENMS_BIN=/path/to/openms/build/bin \
ENGINES_DIR=/path/to/openms/_thirdparty \
OPENMS_SHA=$(git -C /path/to/openms rev-parse HEAD) \
bash benchmark/run_smoke_benchmark.sh
```

OpenSwath benchmark (DIA):

```bash
OPENMS_BIN=/path/to/openms/build/bin \
LD_LIBRARY_PATH=/path/to/openms/build/lib \
OPENMS_DATA_PATH=/path/to/openms/share/OpenMS \
OPENMS_SHA=$(git -C /path/to/openms rev-parse HEAD) \
bash benchmark/run_openswath_benchmark.sh
```

`OPENMS_BIN` points at the built TOPP binaries; `ENGINES_DIR` at the staged
search engines (smoke only); `LD_LIBRARY_PATH` and `OPENMS_DATA_PATH` provide
the shared libraries and shared data the binaries need at runtime (OpenSwath
only). `OPENMS_SHA` is recorded in the results. Each script writes to
`RESULTS_DIR/` (default `./results`): a machine-readable JSON, a
human-readable `summary.txt`, raw per-stage `stages.tsv`, full stage logs,
and pipeline outputs.

Results and reporting
--------

Results are machine-readable JSON, stored under `benchmark/results/`
(schema `openms-benchmarking/report/v2`; older v1 files are promoted
automatically):

```
benchmark/results/
  openms/   smoke-<run-id>.json     # OpenMS benchmark runs (the comparison history)
  openswath_dia/openms/             # stored OpenSwath DIA runs (comparison history)
  tools/    proteobench-*.json      # reference results from other tools/sources
```

`benchmark/report/report_generate.py` has two stages: `normalize` converts a
raw result into the common schema, adding run metadata the runner cannot know
(build wall time, ccache state, run id); `render` discovers all stored
results, compares the most recent OpenMS run against the previous one
(`--current` picks a specific run) and writes one self-contained HTML report.
The generator is benchmark-agnostic: it renders whatever metrics the current
run and the baseline actually share, and detects regressions (required-stage
pass -> fail).

Normalize a raw CI `smoke.json`:

```bash
python3 benchmark/report/report_generate.py normalize smoke smoke.json \
    --run-id 31881021123 --cache warm --build-time 363 \
    --artifact-bytes 149265170 --run-at 2026-08-15T11:01:59Z \
    --out benchmark/results/openms/smoke-31881021123.json
```

Add a ProteoBench result as a reference:

```bash
python3 benchmark/report/report_generate.py normalize proteobench \
    proteobench_local_exp2.json --label "Exp 2 (Comet->MS2Rescore->Percolator)" \
    --out benchmark/results/tools/proteobench-exp2.json
```

Normalize a raw CI `openswath.json` (the build time and artifact size come
from the same CI run that produced the raw result):

```bash
python3 benchmark/report/report_generate.py normalize openswath openswath.json \
    --run-id 32995654115 --build-time 3412 --artifact-bytes 149265164 \
    --out benchmark/results/openswath_dia/openms/openswath_dia-32995654115.json
```

Render the report:

```bash
python3 benchmark/report/report_generate.py render
# -> benchmark/reports/report.html
```

The report answers: which OpenMS SHA, which benchmark/dataset, when, cold or
warm build, the stage results, how they compare with the stored baseline, and
whether anything regressed. Wall-time deltas are reported but deliberately not
treated as regressions - at smoke scale they are dominated by runner load.

Continuous integration
--------

The `Benchmark` workflow (`.github/workflows/benchmark.yml`) runs on
`workflow_dispatch` with the following inputs:

- `runtime_source` - `source` (default) or `package` (see below)
- `openms_sha` - the OpenMS commit SHA to build and benchmark in source mode
  (default: a pinned develop SHA; the full SHA is resolved and recorded in
  every result). In package mode this input is optional: if set, the
  package's embedded OpenMS SHA must match it or the run fails loudly (a
  package can only satisfy an SHA request by actually being that revision).
- `package_date` - package mode only: nightly archive date (`YYYY.MM.DD`,
  the upload-day directory; the .deb inside is the prior night's build).
  Empty selects the newest available at dispatch.
- `use_ms2rescore` - `false` (default). The choice is recorded in the
  results, but MS2Rescore is not executed; rescoring belongs to the extended
  benchmark, which this repository does not run yet.
- `nightly_deb_probe` - opt-in packaging-health probe (independent of
  `runtime_source`).

**Runtime modes.** With `runtime_source=source` (the default), OpenMS is
built from the pinned SHA exactly as before: exact-SHA semantics, all
benchmarks (Smoke, OpenSwath, ProSE/PeptDeep, PXD028735). With
`runtime_source=package`, the published Debian nightly .deb is downloaded,
its declared dependencies satisfied, and the extracted package provides the
benchmark runtime: Smoke and OpenSwath run against the packaged binaries via
the same runner scripts and the same `OPENMS_BIN`/`ENGINES_DIR`/`OPENMS_SHA`
contract the source path uses. Package provenance (filename, SHA256, archive
date, and the OpenMS revision embedded in the package's
`openms_package_version.h`) is recorded in every result, and the report
labels package runs explicitly so they can never be mistaken for exact-SHA
builds. ProSE/PeptDeep do not run in package mode today: published packages
contain no ONNX Runtime, and benchmark support is claimed only for runners
that have actually executed - it is never inferred from binary availability.

Jobs, in dependency order:

1. **build-openms** - checks out OpenMS at the pinned SHA, installs
   dependencies with OpenMS' own `tools/ci/deps-ubuntu.sh --skip-gui-deps`,
   stages the THIRDPARTY search engines and builds headless (Release, Ninja,
   no GUI, no docs). Every compile goes through ccache keyed by the pinned
   SHA, so repeated benchmarks of the same SHA skip the ~70-minute cold build.
   Uploads the `openms-bin-<sha>` artifact.
2. **run-smoke-benchmark** - on a fresh runner, installs the runtime
   dependencies, downloads the build artifact, restores the executable bits
   and runs the smoke benchmark. Uploads `benchmark-results-<sha>`.
3. **run-openswath-benchmark** - same artifact handoff, runs the OpenSwath
   benchmark. Uploads `benchmark-openswath-<sha>`.

Artifacts per run: `openms-bin-<sha>` (TOPP binaries, shared libraries, OpenMS
shared data, search engines), `benchmark-results-<sha>` (`smoke.json`,
`summary.txt`, `stages.tsv`, per-stage logs, pipeline outputs) and
`benchmark-openswath-<sha>` (`openswath.json`, `summary.txt`, `stages.tsv`,
logs, featureXML/chrom.mzML outputs, QC metrics).

Development
--------

### Benchmark definitions

Each benchmark is described by a JSON definition in `benchmark/definitions/`
declaring its `id`, `name`, `description`, the `runner` script, its `fixtures`
and how its raw output is mapped into the report schema (`normalization`).
The conceptual flow is:

```
definition -> runner -> raw JSON -> normalization -> stored result -> report
```

### Adding a benchmark

1. Add a definition under `benchmark/definitions/<id>.json` (see the existing
   `smoke.json` / `openswath.json` for the shape).
2. Add the runner script that executes the benchmark and writes raw results
   (a machine-readable JSON plus `stages.tsv` and logs) to `RESULTS_DIR`.
3. If the raw output needs mapping into the common report schema, add a
   `normalize` subcommand in `benchmark/report/report_generate.py` (see
   `normalize_smoke` / `normalize_proteobench`).
4. Validate the definition and run the tests (below).

### Validation

`benchmark/validate_definitions.py` checks that every definition is valid
JSON, that ids are unique, and that every referenced runner, fixture and
normalization file exists:

```bash
python3 benchmark/validate_definitions.py
```

Testing
--------

```bash
python3 benchmark/report/test_v2_schema.py         # currently 5 tests:
                                                   # v1 -> v2 promotion, result layout loading
python3 benchmark/report/test_generic_renderer.py  # currently 19 tests: rendering all
                                                   # benchmark types, dynamic stage/metric
                                                   # discovery, regression detection,
                                                   # OpenSwath normalization end-to-end
```

Repository structure
--------

```
.github/workflows/benchmark.yml   # CI: build + smoke + OpenSwath benchmarks
benchmark/
  definitions/                    # per-benchmark definitions (smoke, openswath,
                                  #   proteobench); validated by validate_definitions.py
  run_smoke_benchmark.sh          # DDA smoke benchmark runner
  run_openswath_benchmark.sh      # DIA OpenSwath benchmark runner
  validate_definitions.py         # checks definitions reference real files
  fixtures/                       # vendored benchmark inputs + reference outputs
                                  #   (sources and rationale: fixtures/PROVENANCE.md)
  report/
    report_generate.py            # report normalizer/renderer (stdlib only)
    test_v2_schema.py             # v1 -> v2 promotion + schema tests
    test_generic_renderer.py      # generic renderer tests
  results/
    openms/                       # stored OpenMS run results (comparison baseline)
    tools/                        # reference results from other tools
  reports/                        # generated report.html
```

Scope
--------

This repository deliberately does not (yet):

- run the full PXD028735 / ProteoBench LFQ DDA benchmark;
- execute MS2Rescore (the workflow input is recorded, rescoring is not run);
- run ProteoBench scoring in CI - ProteoBench results enter the report as
  reference results generated externally (see [Benchmarks](#benchmarks));
- run on a schedule (nightly).

Resources
--------

- [OpenMS](https://github.com/OpenMS/OpenMS) - the main repository
- [OpenMS Issue #8788](https://github.com/OpenMS/OpenMS/issues/8788) - the benchmarking effort this repository serves
- [`benchmark/fixtures/PROVENANCE.md`](benchmark/fixtures/PROVENANCE.md) - sources, sizes and selection rationale for every vendored fixture
