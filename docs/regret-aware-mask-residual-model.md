# Regret-aware Mask residual ranking

This development model keeps the decomposed early/late candidate-cost formula
as an auditable base and learns only its remaining paired ranking error. It is
not a replacement for semantic legality checks and it cannot make an illegal
Mask placement executable.

## Score decomposition

For workload features `x`, the decision score is:

```text
base(x)      = log estimated_cost(early) - log estimated_cost(late)
residual(x)  = constrained continuous regression features
score(x)     = base(x) + residual(x)
```

A negative score chooses early Mask; a non-negative score chooses late Mask.
Every decision artifact retains all three values. The residual basis uses
continuous logarithms of rows and identifier width, their low-order
interactions, and match-rate interactions. It has no learned or hand-written
cut such as `width > 500`.

## Regret-aware fitting

The regression target is the observed paired log latency ratio minus the base
ratio. A workload receives the deterministic weight:

```text
1 + min((exp(abs(observed_log_ratio)) - 1) / tie_band, 10)
```

Thus a plan reversal with a large possible slowdown matters more than a noisy
near tie, while the cap prevents one observation from dominating the fit. The
ridge constant, weight cap, and confidence multiplier are fixed CLI defaults;
they must not be selected by repeatedly inspecting the same grouped-CV score.

All match-dependent residual coefficients are non-positive. Together with the
decomposed base model, this encodes the physical expectation that increasing
Join match rate cannot make early Mask less attractive for fixed rows and
width. A separate grid audit checks the corrected learned score before the
out-of-distribution fallback policy is applied.

## Confidence fallback and governance

The residual is statistical rather than a safety proof. The model retains the
base choice when a feature lies outside the complete training support or when
a residual sign flip ends within one weighted residual RMSE of the decision
boundary. This is a continuous uncertainty rule, not a width-specific answer.

Before any score comparison, the optimizer removes semantically illegal
candidates and enforces the maximum raw-exposure-row limit. Performance never
overrides governance.

## Development gate

The primary evaluation holds out a complete scenario family. Relative to the
frozen V1 baseline, promotion requires all of the following:

1. strictly better within-3% selection rate;
2. no worse P95 regret;
3. no worse maximum regret;
4. zero corrected-score match-rate monotonicity violations.

Passing these checks would only authorize freezing a new Phase 2G workload. It
would not itself be held-out evidence. Failing any check leaves the artifact as
a documented negative development result.

## Recorded development result

The implementation was evaluated over the same 30 paired-seed observations
used by the preceding V2 development study. No former result directory was
overwritten. The primary split leaves out a complete scenario family.

| Model | Within 3% | Mean regret | P95 regret | Maximum regret | Monotonicity |
|---|---:|---:|---:|---:|---:|
| Frozen V1 | 70.0% | 3.35% | 18.03% | 21.33% | not applicable |
| Decomposed candidate cost | 80.0% | 4.03% | 37.64% | 39.20% | 0 / 270 |
| Regret-aware residual | 80.0% | 3.25% | 28.43% | 37.64% | 0 / 270 |

The residual reduces mean regret and retains the base model's improved
selection count, but it fails both tail-risk checks. Its serialized status is
therefore `development_only_rejected_by_predeclared_gate`.

The two worst errors occur when the complete `w256_match100` family is held
out: early Mask is selected although late Mask is actually 28.43% and 37.64%
faster at the two scales. The point lies inside the global per-feature range,
so a rectangular support check incorrectly treats interpolation across sparse
scenario families as reliable. Seven of 30 predictions used the explicit base
fallback, but these two costly errors did not.

This is useful negative evidence: bounding-box support is not a sufficient
uncertainty estimate. A successor must estimate local, scenario-group-aware
uncertainty and validate any fallback with nested grouped folds. Choosing a
distance threshold after looking at these outer-fold mistakes would merely
encode the development answers and is not allowed. Phase 2G remains unfrozen.
