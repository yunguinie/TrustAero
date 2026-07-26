# Optimizer V3 unanimous-stability development protocol

## Single change from V3-v2

V3-v2 passed every frozen development gate except maximum regret. Its worst
family was selected directly even though models fitted with the five frozen
ridge values disagreed on which placement was faster. A single selected ridge
therefore overstated confidence in a structurally unstable prediction.

V3-v3 keeps the V3-v2 feature basis, family splits, hyperparameter grids,
uncertainty quantiles, fallback, and success gate unchanged. It adds one
label-independent protection before a direct cost decision:

> all five ridge models trained without the outer family must predict the same
> early/late direction.

Any sign disagreement falls back to frozen V1 and records
`MASK_INTERACTION_RIDGE_DISAGREEMENT_FALLBACK`. The guard uses no observed
latency from the held-out family and has no tunable agreement threshold.

## Nested application

The same unanimous guard is applied inside hyperparameter selection and in the
outer complete-family evaluation. Thus the outer family cannot decide whether
its prediction is considered stable. Candidate legality and raw-exposure
limits still run before every model prediction.

## Scientific boundary

This remains development on Phase 2I/J, not Phase 2G. Passing permits an
immutable model freeze only. The independent holdout remains unopened until
the frozen model, protocol, implementation, and all governance audits are
bound to hashes and a clean commit.
