# Decomposed Mask candidate-cost model

This development model implements a database-style alternative to predicting a
placement label directly. It estimates both legal physical candidates with one
shared set of non-negative operation coefficients and selects the lower
estimated latency only after legality and raw-exposure checks.

## Candidate formulas

For input cardinality `N`, estimated Join match rate `m`, raw identifier width
`w`, and 64-byte hash width `h`, the work terms are:

```text
early hash input       = N * w
early Join payload     = N * h
early materialization  = N * h

late hash input        = N * m * w
late Join payload      = N * w
late materialization   = 0
```

The model estimates log latency:

```text
log C(plan) =
    intercept
  + beta_rows * log1p(N / 100K)
  + beta_hash * log1p(hash_input_bytes / MiB)
  + beta_join * log1p(join_payload_bytes / MiB)
  + beta_mat  * log1p(materialized_bytes / MiB)
```

All operation coefficients are constrained to be non-negative. Row/width and
width/match interactions are represented through the physical byte-work terms,
not through hard-coded rules such as `width > 500`.

The output artifact retains both candidate estimates and every component
contribution. The model remains unable to choose an illegal candidate or trade
away an explicit raw-exposure limit.

## Grouped development result

The model was fitted and evaluated over 30 paired-seed workload observations
from Phase 2E, Phase 2F, and the V2 boundary calibration. The primary split
holds out a complete scenario family, which is stricter than separating random
seeds or only one scale.

| Model | Within 3% | Mean regret | P95 regret | Maximum regret | Match monotonicity |
|---|---:|---:|---:|---:|---:|
| Frozen V1 | 70.0% | 3.35% | 18.03% | 21.33% | not applicable |
| Linear latency-ratio V2 | 70.0% | 4.41% | 28.43% | 37.64% | 0 / 270 violations |
| Decomposed candidate cost | 80.0% | 4.03% | 37.64% | 39.20% | 0 / 270 violations |

The decomposed model improves selection count but fails the predeclared tail
regret gate. It is therefore saved with status
`development_only_rejected_by_tail_regret_gate` and is not eligible for a new
held-out claim.

The fitted materialization coefficient is zero under the non-negative
constraint. This does not mean materialization is free. It means the current
measurements cannot identify a separate materialization coefficient after
input, hash, and Join payload terms absorb the correlated variation.

## Decision

The two-cost structure is retained because it is auditable and database-like,
but its current estimator is only a baseline. The next development step must
target costly ranking mistakes, for example with a regret-aware residual model
or uncertainty-aware fallback, while keeping the decomposed base costs visible.

Phase 2G is not frozen yet. When a candidate passes the grouped accuracy,
tail-regret, legality, exposure, and monotonicity gates, Phase 2G must use
unseen widths, match rates, scales, and seeds. Values such as 384/640/1280/1536
bytes and 15%/35%/60%/90% match rates are suitable candidates, but the exact
matrix must be frozen only after the model and all parameters are committed.
