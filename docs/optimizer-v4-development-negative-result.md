# Optimizer V4 grouped-development negative result

## Frozen outcome

The first Pipeline-aware V4 selector failed its predeclared January development
gate.  The run is `20260721T120615014628Z`, bound to implementation commit
`5f8f45b`.  No February--December partition was accessed.

Across 44 pre-audited stable held-out families, V4 achieved 72.7% top-1 and
within-3% rates, 6.12% mean regret, 23.54% P95 regret, 77.64% maximum regret,
and only 40.9% direct coverage.  It selected no governance-illegal plan, but
failed every frozen performance and coverage gate except unstable-family
uncertainty capture.

The learned match-rate-only baseline selected all 44 stable families correctly
with zero regret.  V1 and frozen V3 both behaved like fixed late, with 28.76%
mean, 64.62% P95, and 67.90% maximum regret.  Fixed early had 8.59% mean,
42.38% P95, and 77.64% maximum regret.

## Root cause

The pipeline-work ranking direction is not the main failure.  All 18 direct V4
decisions are correct.  Ignoring the uncertainty gate, the predicted sign is
correct for 43 of 44 stable held-out families.

The failure is the group-residual magnitude used as an uncertainty scale.  Fold
thresholds range from 0.123 to 4.246 log-ratio units.  They send 29 of 48
families to conservative early fallback, including 12 stable late-preferred
families.  Those incorrect fallbacks create the large regret tail.

This distinction matters: lowering the residual quantile after seeing the
answer would be post-hoc tuning, while declaring the raw sign result a success
would erase the frozen fallback semantics.  Both are rejected.

## Scientific consequence

The current Mask/Join fragment establishes that adaptive selection is needed,
but it does not establish that a four-feature pipeline model is superior to a
learned match-rate-only rule.  The simple baseline must remain visible in every
future comparison.

The only authorized next design question is whether sign stability across
complete training groups can serve as a separate uncertainty signal.  A future
selector must report how much performance comes from the pipeline model versus
its fallback; it may not hide the perfect January match-rate baseline inside a
fallback and claim the combined result as pipeline-model gain.
