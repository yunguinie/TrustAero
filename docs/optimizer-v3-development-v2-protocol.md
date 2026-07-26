# Optimizer V3 bounded-interaction development protocol

## Versioning reason

The frozen V3-v1 feasibility preflight showed that a three-term shared physical
formula collapses to a match-rate-only signal and cannot make a direct early
decision after its uncertainty guard. No formal V3-v1 evaluation or Phase 2G
run was performed. The rejected basis is retained as negative development
evidence.

V3-v2 addresses only the identified missing capability: interactions among
input scale, sensitive-field width, and Join match rate. These dimensions
control how many values are hashed, how much payload crosses the Join, and how
much work reaches the safe materialization boundary.

## Fixed continuous basis

The three inputs available before execution are normalized continuously:

- `rows_log = log1p(join_input_rows / 100000)`;
- `width_log = log1p(identifier_width_bytes / 64)`;
- `match_rate = estimated Join match rate`.

The formula contains the three base terms, their three pairwise interactions,
one three-way interaction, and the square of each base term. The basis is
fixed before formal evaluation. It contains no learned split point, width
threshold, family identifier, seed, or lookup table.

Ridge regression predicts the paired log latency ratio between the complete
early and late routes. This is a continuous cost-difference surface: it can
represent a performance reversal without asserting that Mask is freely
commutative or memorizing the existing experimental grid.

## Nested family evaluation

The outer loop leaves out one complete rows-width-match family. All seeds in
that family remain outside fitting and hyperparameter selection. Inside each
outer training set, another complete-family leave-one-out loop selects the
ridge coefficient and residual-quantile uncertainty threshold from the frozen
grids.

The selection objective is lexicographic and frozen in the JSON protocol. No
outer-family label may influence its fold's model or threshold.

## Safety and uncertainty

Candidate legality and raw-exposure limits are checked before the model. Zero
legal candidates fail closed; one legal candidate is selected without a cost
comparison. Out-of-support or uncertain predictions fall back to frozen V1 and
record the reason.

The final development gate is unchanged from V3-v1: strictly improve the 3%
acceptable rate without worsening mean, P95, or maximum regret, retain at least
25% direct coverage, make both direct early and direct late decisions, and pass
all governance audits. Passing authorizes a separate model freeze, not Phase 2G
or a paper claim.
