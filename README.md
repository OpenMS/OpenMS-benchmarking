# OpenMS Benchmarking

Benchmarking infrastructure for [OpenMS](https://github.com/OpenMS/OpenMS)
(Issue #8788). Work in progress.

## Current milestone (Milestone 1: fixed-SHA build + smoke benchmark)

The goal is to prove the architecture before adding any scientific benchmark:

```
fixed OpenMS SHA
      |
      v
Linux OpenMS build in GitHub Actions
      |
      v
generated OpenMS binaries
      |
      v
smoke benchmark (Comet -> Percolator)
      |
      v
results + execution/resource information
```

- **Build**: OpenMS is checked out at a pinned commit SHA and built on
  `ubuntu-24.04` with the same dependency script OpenMS' own CI uses
  (`tools/ci/deps-ubuntu.sh`), plus the `THIRDPARTY` search engines
  (Comet, Percolator, ...). The generated `build/bin` binaries and engines are
  uploaded as a CI artifact.
- **Smoke benchmark**: `benchmark/run_smoke_benchmark.sh` runs a real but tiny
  pipeline (`DecoyDatabase` -> `CometAdapter` -> `PercolatorAdapter`) against
  small fixtures and records per-stage wall time, CPU time and peak RSS. The
  Percolator stage is optional: the smoke fixture has too few PSMs for it, but
  its outcome is still recorded.
- **MS2Rescore**: the `use_ms2rescore` workflow input is plumbed through and
  recorded in the results, but rescoring is NOT executed in this milestone.
  The extended (Comet -> MS2Rescore -> Percolator) benchmark, the real
  PXD028735 dataset, and the local ProteoBench scorer come in later
  milestones.

## How to run

Trigger the `Benchmark` workflow manually (Actions tab) with:

- `openms_sha` — OpenMS commit SHA to build (default: a pinned develop SHA)
- `use_ms2rescore` — `false` (default) or `true` (recorded, not executed yet)

Artifacts produced:

- `openms-bin-<sha>` — the built TOPP binaries + search engines
- `benchmark-results-<sha>` — `smoke.json` (machine-readable metrics),
  `summary.txt`, `stages.tsv`, per-stage logs, and the pipeline outputs
  (`decoy.fasta`, `comet.idXML`, `rescored.idXML`)

## Running the smoke benchmark locally

```bash
OPENMS_BIN=/path/to/openms/build/bin \
ENGINES_DIR=/path/to/openms/_thirdparty \
OPENMS_SHA=$(git -C /path/to/openms rev-parse HEAD) \
bash benchmark/run_smoke_benchmark.sh
```

## Benchmark report

`benchmark/report/report_generate.py` turns machine-readable result JSONs into
one self-contained HTML report (current run vs the stored baseline, plus
reference results from other tools such as the local ProteoBench scorer):

```
benchmark run  ->  JSON result  ->  report_generate.py render  ->  report.html
```

The generator discovers whatever results exist; nothing is hard-coded to a
specific run. Result layout (schema `openms-benchmarking/report/v1`):

```
benchmark/results/
  openms/   smoke-<run-id>.json     # OpenMS benchmark runs (the comparison history)
  tools/    proteobench-*.json      # reference results from other tools/sources
```

Normalize a raw CI `smoke.json` (adding the run metadata the smoke script
cannot know: build wall time, ccache state, run id):

```bash
python3 benchmark/report/report_generate.py normalize smoke smoke.json \
    --run-id 31881021123 --cache warm --build-time 363 \
    --artifact-bytes 149265170 --run-at 2026-08-15T11:01:59Z \
    --out benchmark/results/openms/smoke-31881021123.json
```

Add a local ProteoBench result as a reference:

```bash
python3 benchmark/report/report_generate.py normalize proteobench \
    proteobench_local_exp2.json --label "Exp 2 (Comet->MS2Rescore->Percolator)" \
    --out benchmark/results/tools/proteobench-exp2.json
```

Render the report (compares the most recent OpenMS run against the previous
one; `--current` picks a specific run):

```bash
python3 benchmark/report/report_generate.py render
# -> benchmark/reports/report.html
```

The report answers: which OpenMS SHA, when, cold or warm build, the stage
results, how they compare with the stored baseline, and whether anything
regressed (verdict / required-stage pass->fail). Wall-time deltas are reported
but deliberately not treated as regressions - at smoke scale they are
dominated by runner load.

## Repository layout

```
.github/workflows/benchmark.yml   # Milestone 1 CI workflow
benchmark/
  run_smoke_benchmark.sh          # smoke benchmark script
  fixtures/                       # tiny vendored inputs (see PROVENANCE.md)
  report/
    report_generate.py            # HTML report generator (stdlib only)
  results/
    openms/                       # stored OpenMS run results (comparison baseline)
    tools/                        # reference results from other tools
  reports/                        # generated report.html
```

## Out of scope (later milestones)

- The full PXD028735 / ProteoBench LFQ DDA benchmark.
- MS2Rescore execution and the regular-vs-extended benchmark distinction.
- ProteoBench scoring in CI (the local scorer already reproduces results
  bit-exactly; see the GSOC notebook, Day-10 Task-12). ProteoBench *results*
  can already be pulled into the HTML report as reference results.
- Scheduled (nightly) runs.
