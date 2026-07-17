# DuckDB mechanism microbenchmarks

These microbenchmarks measure mechanisms that the early/late Mask cost formula
previously estimated from correlated end-to-end observations. They are V2/V3
development data, not a Phase 2G holdout and not a paper performance claim.

## Three isolated measurements

### SHA-256 work

The production Mask SQL uses DuckDB `sha256(value)`. Each unit balances two
queries over the same exact-width deterministic strings:

- scan consumption: `sum(length(value))`;
- SHA-256 consumption: `sum(length(sha256(value)))`.

The paired difference estimates incremental hash work while retaining both raw
latencies and actual physical profiles. It is a diagnostic rather than a claim
that subtraction perfectly removes every vectorized scan effect.

### Join payload

The dimension table size and Join key type stay fixed. Match rate changes only
which fact rows use a key present in that dimension. The initial pilot compared
scalar payload consumers, but showed that `length(string)` can be satisfied
largely from string metadata and does not force the full payload through the
Join. The refined paired queries instead materialize identical output schemas:

- baseline: materialize matched fact rows without a Join;
- Join: materialize the same rows and payload after the dimension Join.

The paired subtraction controls common output materialization while forcing
real payload production. This avoids changing the Join build side when match
rate changes.

DuckDB may use vector references or late payload consumption instead of copying
strings through the hash table. That is a result to measure, not an assumption
to hide. Every unit therefore retains `EXPLAIN ANALYZE` operator names,
cardinalities, timings, memory, spill, and plan fingerprint.

### Materialization

Materialization is measured as a dependency-ordered pair:

1. write `row_id` and the sensitive value to a temporary table;
2. read and validate the complete temporary payload.

Write and read latencies are separate raw components. Their paired sum is the
round-trip materialization cost; unlike the other two benchmarks it is not a
subtraction.

## Reproducibility controls

Each mechanism/scale/width/match/seed unit is atomic. The runner provides:

- exact-width deterministic strings;
- balanced component order where dependencies permit it;
- warmups excluded from recorded measurements;
- fixed DuckDB threads and memory;
- commit, dirty-worktree, Python, DuckDB, CPU, and OS provenance;
- per-unit JSON checkpoints and safe resume;
- result checksums and expected row/cardinality validation;
- raw measurements, component summaries, paired costs, plans, and memory/spill;
- repeated per-operator timing/cardinality summaries;
- visible unit progress, elapsed time, and ETA.

An interrupted unit is repeated in full. A completed unit is never measured
again on resume. A changed configuration or Git commit cannot resume an old
run.

## Pilot protocol

The committed pilot deliberately remains smaller than the eventual mechanism
calibration:

- rows: 50K and 150K;
- widths: 256, 512, 1024, and 2048 bytes;
- match rates: 10%, 50%, and 100%;
- two new seeds;
- two warmups and seven measured repetitions.

This expands to 80 atomic units without crossing match rate into hash or
materialization units. Run it from the activated `TrustAero_env` environment:

```powershell
python -u scripts/run_mechanism_microbench.py `
  --config experiments/configs/phase2h_mechanism_pilot.json `
  --progress
```

## First pilot diagnosis

The first 80-unit run completed with 1120 measurements and no failed
validation, negative median paired diagnostic, or disk spill. It produced
clear scale/width signals for two mechanisms:

- incremental SHA-256 diagnostic: about 221 ms to 3696 ms;
- materialization round trip: about 27 ms to 442 ms.

The original scalar Join diagnostic ranged only from about 1.3 ms to 7.9 ms.
Its median relative gap between the two seeds was about 31%, with a maximum
above 100%, and it had no stable width or match-rate trend. Therefore it is not
eligible for a cost formula. This is a benchmark-design finding, not evidence
that Join payload is free.

The refined Join-only protocol uses new seeds and the materialized paired
queries described above:

```powershell
python -u scripts/run_mechanism_microbench.py `
  --config experiments/configs/phase2h_join_payload_refinement.json `
  --progress
```

Do not combine the original and refined Join measurements as if they were the
same target. Hash and materialization pilot observations remain useful
development diagnostics; Join must be recollected under the refined protocol.

The refined wall-clock subtraction also proved unsuitable: 17 of 36 median
differences were negative because DuckDB's joined and filtered pipelines do not
form two additive, otherwise identical executions. The saved plans do expose a
separate `HASH_JOIN` operator timing and actual cardinality. The next frozen
calibration therefore repeats `EXPLAIN ANALYZE` five times per unit and reports
median/P95 timing for every physical operator in `operator_summary.csv`:

```powershell
python -u scripts/run_mechanism_microbench.py `
  --config experiments/configs/phase2h_join_operator_calibration.json `
  --progress
```

This is not a third attempt to tune a winner threshold. It changes the measured
quantity from an invalid whole-query subtraction to DuckDB's directly observed
`HASH_JOIN` operator cost, with a predeclared repeat count and new seeds.

The operator calibration completed all 36 units with 360 wall-clock samples,
324 operator summaries, exact Join cardinalities, stable physical shapes, and
no spill. Across scenario groups, the relative gap between the two seeds for
median `HASH_JOIN` time has a median of about 9% and a maximum of about 45%.
The within-unit max-minus-min range remains large for some sub-millisecond
operators, so the median of five profiles must be used rather than one sample.

The resulting `HASH_JOIN` timing primarily follows input and matched-output
cardinality. Width is weak and non-monotone, consistent with DuckDB passing
vectors/references instead of copying the entire string inside the hash table.
Therefore the next explainable formula should assign:

- raw byte hashing to the measured SHA-256 term;
- explicit/intermediate payload movement to the materialization term;
- Join build/probe work to input and matched-output rows, not a forced width
  coefficient.

This mechanism allocation is a development hypothesis. It must still face
complete scenario-family cross-validation against V1, linear V2, the residual
model, and the rejected local guard before any Phase 2G freeze.

After interruption, use the exact same committed code and configuration:

```powershell
python -u scripts/run_mechanism_microbench.py `
  --config experiments/configs/phase2h_mechanism_pilot.json `
  --resume `
  --progress
```

## Scientific boundary

The runner does not choose a governed plan, so it cannot relax governance.
When these measurements enter a later optimizer, semantic legality and the
maximum raw-value exposure limit remain feasibility filters before cost. A plan
that violates exposure constraints is excluded even if its measured mechanism
cost is lower.

Do not change Phase 2G from these pilot results. First determine whether the
three costs are identifiable and stable across seeds. Then freeze one
explainable formula and evaluate it with complete scenario-family holdouts
against V1, linear V2, the residual model, and the rejected local guard. Only a
model that passes the predeclared mean, P95, maximum-regret, monotonicity, and
governance gates may authorize a separately frozen Phase 2G.
