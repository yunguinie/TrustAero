# TrustAero

TrustAero is a research prototype for validating **untrusted agent-generated
query plans** over governed spatio-temporal data. An agent may propose a plan,
but it cannot execute arbitrary SQL or bypass deterministic validation.

## Current scope (0.1.0)

- strict typed Candidate Plan and Validated Logical Plan models;
- versioned JSON Schemas exported from the typed models;
- deterministic L1 structural, L2 plan-graph, and L3 governance validation;
- stable reason codes and fail-closed outcomes;
- a policy-before-cost physical-candidate feasibility gate for bounded raw Join
  and raw-intermediate exposure;
- minimal policy decisions and obligation-driven safe rewrites;
- approved physical-plan specifications that bind to validated logical plans;
- governed execution-certificate structure, event, DAG, and lineage-evidence
  checks under a trusted-executor assumption;
- a minimal optional DuckDB execution boundary for a validated single-relation
  SQL fragment;
- a local CLI with no online API dependency.

Not implemented yet: general database execution, cost-based physical
optimization, LLM access, or cryptographic proof generation. The minimal DuckDB
path only covers a small validated fragment for smoke experiments. The Governed
Execution Certificate model is a structured record under a trusted-executor
assumption, not a proof against a malicious DBMS.

## Development setup

Open this repository as the VS Code workspace, select the Conda interpreter
named `TrustAero_env`, and run all commands from the repository root:

```powershell
conda activate TrustAero_env
python -m pip install -e ".[dev]"
python -u scripts/run_tests.py -q
python scripts/export_json_schemas.py
python scripts/check_schema_sync.py
```

The test wrapper disables unrelated global pytest plugins and keeps temporary
test files inside this E-drive workspace. In a VS Code terminal, pytest prints
its normal progress while the suite runs.

To run the optional DuckDB execution smoke path, install the extra dependency
inside the same environment:

```powershell
python -m pip install -e ".[dev,duckdb]"
python scripts/run_duckdb_smoke.py
```

The first paper-scale standard benchmark artifact is TPC-H SF10. Generate it
locally with visible, newline-delimited progress (no C-drive staging is used):

```powershell
python -u scripts/prepare_tpch.py --scale-factor 10 --progress
```

The generator requires a clean commit, at least 12 GiB of free E-drive space,
and records the eight exact table counts, DuckDB/TPC-H extension versions,
database size, and SHA-256 digest. It is safe to rerun: a verified complete
artifact is reused, and an interrupted build resumes after its last completed
partition. The unpublished `.building` file is never accepted as formal input.
After generation, run the scale-specific semantic gates before freezing any
performance configuration:

```powershell
python -u scripts/run_tpch_q1_smoke.py --scale-factor 10
python -u scripts/run_tpch_q6_smoke.py --scale-factor 10
python scripts/freeze_tpch_sf10_protocol.py
```

The freeze command creates two deterministic, content-addressed config files
but performs no timing and does not inspect a candidate winner. Review and
commit those configs, re-run the source-freeze check, and then start the paired
formal measurements with progress and ETA:

```powershell
python -u scripts/run_tpch_q1_formal.py `
  --config experiments/configs/tpch_sf10_q1_paired_ci_v2.json --progress
python -u scripts/run_tpch_q6_formal.py `
  --config experiments/configs/tpch_sf10_q6_paired_ci_v2.json --progress
```

Formal Q1/Q6 measurements persist every complete paired block atomically. A
stopped run can be completed with `--resume-run-id`, but the resumed result is
deliberately classified as diagnostic because it spans multiple DuckDB
processes and therefore no longer satisfies the frozen single-connection cache
protocol. Final paper-table evidence must complete in one uninterrupted VS Code
terminal process; run long experiments there so progress and ETA remain visible.
The current inference protocol also tests whether each predeclared heavy
materialization route changes a later candidate's latency. Performance claims
use only blocks in which that possible polluter has not yet run, and each claim
must be authorized by a deterministic 95% permutation-stratified paired
bootstrap interval. A point-estimate winner or diagnostic Oracle is never
enough to authorize paper text.

The core dependency list intentionally stays small. Optional libraries are
added only when production code uses them; a package already present in a
developer's Conda environment is not automatically a TrustAero dependency.

Validate an example:

```powershell
trustaero validate examples/plans/rewrite_precision.json `
  --policy examples/policies/research_policy.json `
  --catalog examples/catalogs/minimal_catalog.json
```

## Security model

- The LLM/agent and its output are untrusted.
- Unknown or indeterminate authorization is never treated as permission.
- Only a validated logical plan may proceed to later optimization.
- The current prototype assumes the validator, catalog, policy store, executor,
  and event log are trusted.

For the current system pipeline, see [docs/architecture.md](docs/architecture.md).
The bounded physical-candidate governance gate is documented in
[docs/candidate-feasibility.md](docs/candidate-feasibility.md).
The optimizer development rationale is documented in
[docs/decomposed-mask-cost-model.md](docs/decomposed-mask-cost-model.md) and
[docs/regret-aware-mask-residual-model.md](docs/regret-aware-mask-residual-model.md).
The nested uncertainty policy is described in
[docs/local-regret-guard.md](docs/local-regret-guard.md).
The next mechanism-calibration stage is specified in
[docs/mechanism-microbenchmarks.md](docs/mechanism-microbenchmarks.md).
For the first repeatable semantic evaluation layer, see
[docs/phase0-experiments.md](docs/phase0-experiments.md).
Before any publication-facing performance run, apply the reproducibility gate
described in [docs/source-freeze.md](docs/source-freeze.md).

## Repository status

Research prototype. Public API stability is not guaranteed before package
version 1.0.0. The IR specification version and package version are separate.
