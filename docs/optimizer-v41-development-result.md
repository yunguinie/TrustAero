# Optimizer V4.1 grouped-development result

## Frozen outcome

Optimizer V4.1 was evaluated once on the 48-family January development
matrix after its implementation and gates were frozen in commit `e4f5ee5`.
The run is `20260721T122941014452Z`.  February--December data were not
accessed.

V4.1 replaces V4's failed residual-magnitude uncertainty threshold with a
complete-training-group sign-consensus rule.  Across the 44 pre-audited stable
families, it made 35 direct decisions.  Thirty-four were correct: direct
precision was 97.1%, coverage was 79.5%, mean direct regret was 0.37%, and
maximum direct regret was 13.0%.  It made both early and late decisions and
never selected a governance-illegal plan.

These direct-selector results pass every predeclared direct-selection gate.
They are a real improvement over V4's 40.9% direct coverage, but they do not
make the overall V4.1 protocol pass.

## Why the overall gate failed

V4.1 failed three frozen checks:

- none of the four pre-audited unstable families was marked uncertain;
- conservative-early deployment had 5.47% mean regret, above the 5% gate;
- its P95 regret was 23.54%, above the 20% gate.

All four unstable families are the 192-byte, 70%-match boundary family across
the four complete January windows.  Their measured early/late ratio changes
direction across windows, while every V4.1 deletion model still predicts
early.  This shows that sign consensus among the current models is not a valid
boundary detector.

The only stable direct error is `jan22-31-w1536-target0.25`; V4.1 predicts
early while late is 13.0% faster.  Nine stable late-preferred families are
sent to uncertainty.  A learned match-rate fallback handles those cases well,
but its contribution is reported separately and cannot be credited to the
pipeline model.

## Workload-sufficiency consequence

The learned match-rate-only baseline remains perfect on all 44 stable January
families.  Therefore this development fragment cannot demonstrate that the
pipeline-aware feature surface is better than a simple rule.  It is not
scientifically sound to hide that baseline in a fallback, tune a threshold to
the single remaining error, or open the external months for model repair.

Before another optimizer version is justified, the development workload must
contain predeclared governance-driven query families in which the preferred
legal placement is not determined by match rate alone.  In particular, at
least one fixed match-rate stratum must contain stable early and late winners
caused by other cost drivers such as input scale, sensitive-width work,
policy-filter work, lineage work, or a physical materialization boundary.

