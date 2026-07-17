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
which fact rows use a key present in that dimension. The paired queries compare
a matched-row payload baseline with an inner Join that consumes the same
sensitive value plus a dimension marker. This avoids changing the Join build
side when match rate changes.

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
