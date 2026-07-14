# Phase 0 experiments

Phase 0 is TrustAero's repeatable semantic evaluation layer. It checks whether
the validator, deterministic rewrite logic, certificate checker, and failure
taxonomy behave as specified. It is the first experimental layer, but it is not
yet the full database-system performance study.

## What Phase 0 can answer

Phase 0 is designed to support early paper claims such as:

- TrustAero accepts, rewrites, clarifies, or rejects fixed benchmark cases with
  the expected status.
- Fail-closed violations are detected with stable reason codes.
- Deterministic governance rewrites insert the expected obligations.
- Certificate checks detect lineage, event-order, and physical-DAG faults.
- Validator overhead is small on the bounded IR cases used in this phase.
- Results are reproducible because each run records commit, environment,
  configuration, and per-case outputs.

These claims map naturally to the first semantic-correctness experiments. They
also provide part of the evidence for governed execution certificates.

## What Phase 0 cannot answer

Phase 0 does not yet support claims about:

- DuckDB or other DBMS execution latency;
- physical query-plan quality;
- cost-based optimization benefits;
- real lineage backend overhead;
- true result-byte recomputation;
- robustness against a malicious DBMS.

Those claims require later phases with an actual executor, physical operators,
data loading, and optimizer experiments. In other words, Phase 0 tells us
whether TrustAero's safety logic is behaving, not whether a database execution
plan is fastest.

## Case matrix

The current matrix lives in:

```text
experiments/cases/phase0_cases.csv
```

Each case fixes:

- `case_id`
- `case_category`
- `case_kind`
- `scenario`
- plan, policy, and catalog paths
- expected status
- expected reason codes

`case_kind=validation` runs the candidate plan through the validator.
`case_kind=certificate` first obtains a validated/rewrite plan, derives an
approved physical plan, and then verifies a deterministic certificate scenario.

Current scenario examples include:

| Scenario | Purpose |
|---|---|
| `baseline` | expected legal or governed behavior |
| `unknown_dataset` | fail-closed catalog resolution |
| `masked_filter` | masked field used semantically after masking |
| `weak_lineage` | lineage evidence weaker than the policy requirement |
| `missing_lineage_event` | certificate evidence without the required event |
| `dependency_violation` | physical operator starts before an input completes |

These are deterministic fault injections, not random fuzzing. Randomized or
coverage-guided testing can be added later, but the Phase 0 matrix stays fixed
so paper results are reproducible.

## Running Phase 0

From the repository root:

```powershell
python scripts/run_phase0.py --config experiments/configs/phase0.json
python scripts/summarize_phase0.py
```

For quick smoke tests, reduce repetitions:

```powershell
python scripts/run_phase0.py --warmup-runs 1 --measured-runs 2
```

Full Phase 0 runs should use the checked-in config unless a paper experiment
explicitly reports a different configuration.

## Output files

Each run creates:

```text
results/phase0/<run_id>/
  cases.csv
  summary.json
  environment.json
  config.json
  failures/
```

The summary step creates:

```text
results/phase0_summary/
  phase0_summary.csv
  phase0_summary.json
```

`results/` is intentionally ignored by git. Commit the experiment code and
matrix, not ad-hoc local outputs.

## Metrics

Per-case outputs include:

- `expected_status` and `actual_status`
- `status_correct`
- `expected_reason_codes` and `actual_reason_codes`
- `reason_code_correct`
- cold and preloaded latency measurements
- operator and edge counts
- rewrite rounds and inserted-operator count
- pending and verified obligation counts
- certificate event count
- validated plan digest
- run ID and commit hash

Run summaries include:

| Metric | Meaning |
|---|---|
| `status_accuracy` | fraction of cases with the expected status |
| `reason_code_accuracy` | fraction of cases whose expected reason codes are present |
| `detection_rate` | fraction of negative or injected cases detected as expected |
| `false_reject_rate` | fraction of legal cases incorrectly rejected |
| `median_latency_ms` | median of per-case median validation/check latency |
| `p95_latency_ms` | maximum per-case P95 latency in the run |
| `all_correct` | whether every case matched status and reason-code expectations |

Latency is split into:

- cold latency, including JSON reads and typed policy/catalog loading;
- repeated preloaded measurements, which better represent validator micro-cost.

The runner supports warm-up and repeated measurements because individual Python
timings are noisy at this scale.

## Failure artifacts

If a case does not match its expected status or reason codes, the runner writes:

```text
results/phase0/<run_id>/failures/<case_id>.json
```

That file contains the case metadata, actual result row, diagnostics, and input
paths. Passing cases do not create failure artifacts. This keeps successful run
directories compact while preserving enough information to debug failures.

## How to report Phase 0

When reporting Phase 0 in a paper or artifact appendix, prefer statements like:

> Phase 0 evaluates TrustAero's semantic validation, deterministic rewrite, and
> certificate-structure checks over a fixed case matrix.

Avoid statements like:

> Phase 0 proves the DBMS execution is correct.

The second statement would overclaim. Phase 0 verifies TrustAero's current
IR-level safety behavior and execution-certificate structure; physical database
execution is a later phase.
