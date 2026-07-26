# TrustAero hierarchical planner

## Why this planner exists

TrustAero must not let the fastest SQL candidate waive a governance rule. The
planner therefore makes decisions in a fixed order:

1. remove policy-incompatible candidates;
2. remove candidates that are mechanically no better than an equivalent plan;
3. select the only survivor, use an explicit governance fallback, or return
   `DEFER` when an authorized performance ranker is still required.

In plain language, TrustAero checks whether a route is allowed before comparing
how fast it might be.

## Example

The governed-checkpoint fragment currently has three result-equivalent shapes:

| Candidate | Durable checkpoint | Raw sensitive checkpoint |
|---|---:|---:|
| fused | no | no |
| policy-first | yes | no |
| query-first | yes | yes |

The legal set changes with the policy:

| Policy | Legal planning outcome |
|---|---|
| checkpoint optional, raw allowed | fused is the conservative fallback |
| checkpoint required, raw allowed | fused is rejected; policy-first is the conservative fallback |
| checkpoint optional, raw forbidden | query-first is rejected; fused remains |
| checkpoint required, raw forbidden | only policy-first remains |

The fallback is a governance choice, not a claim that the selected plan is
always fastest.

## Safe dominance

Candidate A can prune candidate B only when:

- both candidates have the same result-equivalence identifier;
- both expose the same named physical-work dimensions;
- A is no worse in every work and governance-exposure dimension;
- A is strictly better in at least one dimension.

Different units are never added together. Missing metrics are not silently
treated as zero. Incomparable candidates remain in the plan space.

## Current scientific boundary

The required-checkpoint boundary experiment found real bidirectional
performance reversals, but its labels did not pass the pre-registered gate for
training a more complex optimizer. Therefore this planner:

- does not hard-code the development-only `q=0.16` value;
- does not load a failed learned cost model;
- exposes `performance_model_used = false`;
- can return `DEFER` if several plans survive and no explicit fallback exists.

Future performance ranking must be added as a separate authorized layer and
evaluated against fixed plans, simple baselines, and a Legal Oracle.
