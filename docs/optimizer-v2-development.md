# Optimizer V2 development checkpoint

This checkpoint combines the 14 paired-seed workload observations from Phase
2E confirmation and Phase 2F. Phase 2F remains the frozen held-out result for
V1, but became development data as soon as its outcomes informed V2.

V2 predicts `log(early Mask latency / late Mask latency)` using five
pre-execution statistics: input rows, identifier width, Join match rate, raw
input work, and matched work. It is a standardized ridge model with a frozen
development coefficient of 0.01. Semantic legality and raw-exposure limits are
checked before the model score and cannot be traded for predicted speed.

## Development cross-validation

| Scheme | Exact / within 3% | Mean regret | P95 regret | vs fixed late | vs fixed early |
|---|---:|---:|---:|---:|---:|
| Frozen V1 on 14 aggregated workloads | 78.6% | 1.74% | 10.94% | 1.065x | 1.333x |
| V2 leave-one-workload-out | 85.7% | 1.56% | 15.27% | 1.067x | 1.336x |
| V2 leave-one-scenario-family-out | 78.6% | 1.81% | 15.27% | 1.065x | 1.333x |

The stricter scenario-family split shows no reliable improvement over V1 and
has worse tail regret. V2 therefore remains a development artifact and should
not yet be tested on a newly frozen holdout. The result is useful because it
prevents an under-supported linear model from being promoted based only on the
more favorable workload-level split.

The fitted ridge model passes all 80 comparisons in the initial match-rate
monotonicity audit. This is necessary but not sufficient: a model can follow
the expected direction and still place the decision boundary incorrectly.

The scenario-family split misses three boundaries: narrow/high-match at 300K,
wide/high-match at 100K, and very-wide/low-match at 500K. These failures imply
that the existing grid is too sparse to separate fixed materialization cost,
per-row overhead, byte width, and unmatched-row hashing cost.

## Next evidence needed

Before freezing Phase 2G, collect a development-only boundary matrix with
additional widths between 256 and 1024 bytes, match rates between 0.1 and 1.0,
and intermediate scales. Model selection must use grouped cross-validation,
and Phase 2G must use entirely new seeds and feature combinations.

Current controlled workloads provide realized cardinalities to the model.
Later experiments must inject estimation error because a deployable optimizer
will receive estimates rather than true post-execution row counts.
