# Phase 0: repeatable semantic evaluation

Phase 0 is the first experiment layer for TrustAero. It evaluates the validator,
rewrite logic, fail-closed behavior, and certificate/physical-plan structural
checks. It is not a database-system performance experiment.

Current Phase 0 questions:

- Does each case return the expected validation status?
- Do diagnostics include the expected stable reason codes?
- How much core validation latency does each case add after inputs are loaded?
- Which commit and environment produced the result files?

Run from the repository root:

```powershell
python scripts/run_phase0.py
python scripts/run_phase0.py --config experiments/configs/phase0.json
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

This phase can support paper results about semantic correctness, failure
classification, and validator overhead. It cannot support claims about DuckDB
execution latency, physical optimization quality, or real lineage-capture
runtime overhead.
