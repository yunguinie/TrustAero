# Optimizer V3 development protocol

## Scope

Optimizer V3 remains inside the bounded fragment already supported by the
project: hash Mask, equality Join, ordered projection, and an optional safe
materialization boundary. The model ranks only candidates that the semantic
validator has already proved legal. It does not make Mask freely commutative.

This is Phase 2N development, not the independent Phase 2G holdout.

## Why V2 is not reused

V2 fitted absolute end-to-end latency for two routes. Shared scan and output
costs dominated the fit, so the non-negative formula collapsed to the same
decisions as V1. The failed result remains frozen.

V3 removes only that identified ambiguity. It estimates the *paired cost
difference* between the two complete routes using shared physical-work terms:

- bytes hashed by each route;
- bytes carried into Join by each route;
- bytes written at the early safe materialization boundary.

For each placement, the model builds a cost score from the same non-negative
operator coefficients. Subtracting the two scores produces a predicted
log-latency ratio. This is still an explainable comparison of candidate costs,
not a classifier that memorizes width or match-rate thresholds.

## Leakage prevention

Only input row count, identifier width, estimated Join match rate, candidate
legality, and raw-exposure limits are available before selection. Observed
latency, actual winner, regret, and execution-time cardinalities are labels or
evaluation outputs and cannot become model inputs.

All seeds belonging to the same rows-width-match family stay together. The
outer evaluation leaves out one complete family. Hyperparameters and the
uncertainty threshold are selected using a second complete-family cross
validation entirely inside the outer training fold.

## Conservative decisions

The optimizer first removes illegal candidates. If no candidate remains, it
fails closed. If only one remains, it selects that candidate without consulting
the cost model.

When a legal workload is outside training support, or the predicted advantage
does not exceed the inner-fold residual threshold, V3 returns to the frozen V1
selector and records the reason. The model must make both direct early and
direct late decisions; a disguised fixed selector cannot pass.

## Frozen success gate

Relative to frozen V1 on the same 40 complete development families, V3 must:

- strictly improve the fraction within 3% of Oracle;
- not worsen mean, P95, or maximum regret;
- make direct, non-fallback decisions on at least 25% of families;
- make at least one direct early and one direct late decision;
- retain non-negative shared operator coefficients;
- pass every governance legality and exposure audit.

Failure freezes another negative development result and keeps Phase 2G closed.
Passing the gate permits freezing the model; it still does not turn development
cross-validation into a paper holdout result.
