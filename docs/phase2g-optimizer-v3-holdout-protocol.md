# Phase 2G independent Optimizer V3 holdout protocol

## Purpose

Phase 2G is the first independent test of the frozen Optimizer V3-v3. Phase 2F
was inspected during V2 development, and Phase 2I/J trained V3, so none of
their scenario values or seeds can serve as this holdout.

The holdout is frozen before its data are generated or its labels are read.
It can be started only once and resumed only with the same source commit,
configuration digest, run identifier, and model hashes.

## Unseen in-support matrix

The main matrix contains:

- rows: 75K, 125K, 175K;
- sensitive-field widths: 192, 384, 768, 1536 bytes;
- Join match rates: 25%, 70%, 95%;
- seeds: 2111, 2222, 2333, 2444, 2555.

The full factorial has 36 rows-width-match families and 180 seed-level units.
Every numeric value and seed is new, while all three axes remain inside the
frozen training support. This tests interpolation to unseen combinations
instead of constructing an out-of-support workload that would automatically
fall back to V1.

Each unit runs two warm-ups, 15 timed repetitions, and three untimed
`EXPLAIN ANALYZE` profiles per candidate. Candidate order alternates, outputs
must be identical, physical plans must differ, and DuckDB spill is forbidden.

## Frozen evaluation

Seed measurements are aggregated inside a complete rows-width-match family.
The evaluator compares frozen V3 against frozen V1, fixed early, fixed late,
and the experimental Oracle. It reports exact Top-1, within-3%, mean regret,
P95 regret, maximum regret, direct model coverage, and both decision types.

Two paper-facing improvement claims require paired 95% bootstrap confidence
intervals over complete families, stratified by input row count:

- within-3% improvement is authorized only when the lower bound of
  `V3 - V1` is above zero;
- mean-regret reduction is authorized only when the upper bound of
  `V3 - V1` is below zero.

Point estimates cannot authorize either claim by themselves. P95 and maximum
regret are reported as observed tail checks, not as significance claims.

## One-shot and failure rules

Before starting, the runner must verify a clean committed source tree, all
frozen SHA-256 bindings, the exact V3 model record, an explicit Phase 2G
authorization record, and absence of a previous Phase 2G run.

After a run begins, a second new run is forbidden. An interrupted run may only
resume the recorded run ID with the same source commit. The evaluator opens
labels only after all 180 units complete. Regardless of outcome, Phase 2G is
then consumed and cannot be changed or rerun as a new independent holdout.

An unfavorable result must be reported. It may motivate a future optimizer,
but that optimizer requires a different independently frozen holdout.
