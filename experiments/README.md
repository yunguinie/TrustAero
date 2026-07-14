# Phase 0: repeatable semantic evaluation

Phase 0 is the first experiment layer for TrustAero. It evaluates the validator,
rewrite logic, fail-closed behavior, and certificate/physical-plan structural
checks. It is not a database-system performance experiment.

Current Phase 0 questions:

- Does each case return the expected validation status?
- Do diagnostics include the expected stable reason codes?
- Do governed execution certificates detect injected event, lineage, and
  physical-DAG violations?
- How much core validation latency does each case add after inputs are loaded?
- Which commit and environment produced the result files?

Run from the repository root:

```powershell
python scripts/run_phase0.py
python scripts/run_phase0.py --config experiments/configs/phase0.json
python scripts/summarize_phase0.py
```

The runner writes:

```text
results/phase0/<run_id>/
  cases.csv
  summary.json
  environment.json
  config.json
  failures/
```

`cases.csv` contains both expected and actual outcomes. Reason-code columns are
pipe-separated because a case may produce multiple diagnostics. Latency columns
separate one cold end-to-end measurement from repeated preloaded core
validation measurements.

The input matrix uses two important columns:

- `case_kind=validation` runs the candidate plan through the validator.
- `case_kind=certificate` first obtains a validated/rewrite plan, derives an
  approved physical plan, then verifies a deterministic certificate scenario.

`scenario` names a deterministic mutation such as `unknown_dataset`,
`masked_filter`, `expression_type_mismatch`, `weak_lineage`,
`missing_lineage_event`, `snapshot_mismatch`, `event_order_invalid`, or
`dependency_violation`. These are fixed fault injections, not random fuzzing.

This phase can support paper results about semantic correctness, failure
classification, and validator overhead. It cannot support claims about DuckDB
execution latency, physical optimization quality, or real lineage-capture
runtime overhead.

`summarize_phase0.py` reads one or more run folders and writes:

```text
results/phase0_summary/
  phase0_summary.csv
  phase0_summary.json
  phase0_category_summary.csv
  phase0_category_summary.json
  phase0_reason_code_summary.csv
  phase0_reason_code_summary.json
```

The summary includes status accuracy, reason-code accuracy, detection rate over
negative/injected cases, false reject rate over legal cases, and latency
aggregates across each run. The category summary groups cases by experiment
dimension, while the reason-code summary counts expected, observed, and matched
diagnostics without collapsing multi-code cases into a single label.

When a case does not match its expected status or expected reason codes, the
runner writes `failures/<case_id>.json` with the case metadata, flattened result
row, input paths, and serialized diagnostics. Passing cases do not create
failure artifacts, which keeps successful result directories compact.

## Phase 1: minimal DuckDB execution smoke

Phase 1 is the first real execution layer. It runs one validated single-table
plan through the trusted SQL compiler, executes it in an in-memory DuckDB
database, computes a result digest, and verifies that digest against a governed
execution certificate.

Run from the repository root after installing the optional DuckDB extra:

```powershell
python scripts/run_phase1.py
python scripts/run_phase1.py --config experiments/configs/phase1.json
```

The runner writes:

```text
results/phase1/<run_id>/
  cases.csv
  summary.json
  environment.json
  config.json
  failures/
```

This phase can support claims that the minimal validated-plan-to-certificate
execution path is wired end to end. It still cannot support claims about
cost-based optimization, large-scale DBMS performance, or malicious database
protection. In the current certificate result, `physical_plan_execution` remains
unverified because the physical execution trace is trusted-executor evidence,
not a cryptographic proof.
