# Phase 2M compact policy-aware ablation result

The frozen compact matrix completed all 15 atomic units and all protocol
validations. Governance policy changed the legal optimum in every development
family, but the predeclared V2.1 hypothesis gate did not pass. This result is
retained as development evidence; it does not authorize Phase 2G.

## Execution integrity

- three physical families and five new seeds per family;
- four result-equivalent candidate plans per unit;
- 900 timed measurements and 180 analyzed physical profiles;
- 15 of 15 units with equal output, exact Join cardinality, distinct physical
  plans, and validated materialization boundaries;
- 60 of 60 component rows annotated with raw/Masked exposure;
- zero spilled profiles and zero spilled scenarios;
- 676.4 seconds of CPU execution time.

The analysis used the frozen 3% practical-tie band and required agreement in at
least four of five seeds. Illegal candidates were removed before computing the
legal oracle.

## Policy-conditioned result

| Development family | Raw permissive | No raw materialization | No raw Join | Median governance overhead: no raw materialization / no raw Join |
|---|---|---|---|---:|
| 100K rows, width 128, match 90% | Late Join-materialized | Stable tie | Early Hash-materialized | 65.98% / 71.92% |
| 100K rows, width 512, match 90% | Late Join-materialized | Mixed | Early Hash-materialized | 94.56% / 122.62% |
| 200K rows, width 128, match 100% | Late Join-materialized | Mixed | Early Hash-materialized | 19.38% / 24.61% |

Under the raw-permissive policy, `late_join_materialized` was the fastest legal
candidate in all 15 units. It materializes matched raw sensitive values and is
therefore removed by both stricter policies. Under `no_raw_join`, the bounded
fragment has only one legal candidate, `early_hash_materialized`; this is a
feasibility decision rather than an optimizer prediction.

The informative middle policy is `no_raw_materialization`, which leaves three
legal candidates. Its per-unit legal oracle was `late_hash_materialized` 11
times, `early_hash_materialized` three times, and `late_fused` once. However,
many comparisons fell inside the frozen 3% tie band. The three complete
families consequently classified as stable tie, mixed, and mixed; none produced
a four-of-five-seed stable practical winner.

## Gate decision

Four frozen checks passed:

1. every family had exactly five seeds;
2. policy changed the stable legal optimum;
3. every selected candidate was policy-legal;
4. no scenario spilled.

The required check that `no_raw_materialization` exhibit at least two different
stable winners across the three families failed. Therefore the combined V2.1
hypothesis gate failed and Phase 2G remains unauthorized. We do not lower the
agreement requirement, shrink the tie band, or add post-hoc boundary points.

## Interpretation

This experiment confirms an important database-system property: governance is
a hard constraint on physical plan feasibility, not a latency penalty added
after unconstrained optimization. The unconstrained fastest diagnostic can be
illegal, and removing it can impose a median cost of roughly 19% to 123% in
these development families.

It does not yet show a stable cost boundary among the remaining legal choices.
Consequently, these measurements cannot justify fitting or freezing a new
pipeline-aware cost model. The next defensible work is to keep candidate
legality as a verified optimizer stage and move to a separately frozen real-data
preparation protocol, while preserving Phase 2G values as unseen holdout data.

