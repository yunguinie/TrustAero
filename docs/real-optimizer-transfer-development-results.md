# Real-data Optimizer V3 transfer result

## Outcome

The January BTS development transfer gate completed all 12 families and 480
timed executions.  Every logical and physical safety check passed: both legal
candidates returned the same result, DuckDB retained distinct physical plans,
all feature vectors were inside the frozen V3 support bounds, the strict policy
forced early Mask, and no run spilled to disk.

The optimizer transfer gate nevertheless failed.  Frozen V3 and frozen V1
selected late Mask for all 12 families.  Only 6/12 selections were within 3%
of the experimental Oracle.  Mean regret was 24.665%, and maximum regret was
66.653%.

The generated summary originally reported P95 regret as 54.756% because the
nearest-rank index used a floor-like expression.  Analysis commit `5080286`
corrected the implementation and retained the original summary unchanged.  At
12 families, nearest-rank P95 is the twelfth observation, so corrected P95 is
66.653%.

## Paired stability audit

The values below are median paired `early / late` latency ratios.  Values below
1 favor the early materialized Mask; values above 1 favor late Mask.

| controlled width | 25% target | 70% target | 95% target |
|---:|---:|---:|---:|
| 192 bytes | 1.923 | 1.221 (unstable) | 1.095 |
| 384 bytes | 1.118 | 0.727 | 0.652 |
| 768 bytes | 1.149 | 0.772 | 0.601 |
| 1536 bytes | 1.108 | 0.706 | 0.625 |

Eleven families passed the paired stability audit.  Among them, six favored
early Mask and five favored late Mask, providing clear real-data plan
performance reversals.  The 192-byte, 70%-target family was excluded from
strong conclusions because early-first versus late-first strata and the two
temporal halves disagreed.  Removing it does not change the negative optimizer
transfer conclusion.

## Scientific interpretation

This is a useful negative development result, not a failed data collection.
It demonstrates that a boundary learned from the synthetic Mask fragment does
not transfer automatically to the real governed BTS pipeline.  The result also
strengthens the motivation for an optimizer: no fixed early or late route is
best across the stable real families.

The result cannot support an Optimizer V3 superiority claim and is not an
independent paper holdout.  V3 must remain a frozen baseline.  The next model
may use January only as development data, must incorporate real operator
statistics or a pipeline-transfer feature, and must be frozen before the
February--December external evaluation is opened.
