# Phase 2K pipeline-aware optimizer result

Phase 2K completed its single predeclared development evaluation on the
combined Phase 2I/J complete-fragment data. The result is negative: the model
does not pass the frozen development gate, and Phase 2G remains unauthorized.

## Validated input

- 40 unique `(rows, identifier width, Join match rate)` families;
- 162 paired seed replicates after merging duplicate physical grid points;
- every source unit previously proved result equivalence, distinct DuckDB
  physical plans, exact Join cardinality, and no spill;
- complete-family leave-one-out evaluation, with no seed crossing a fold.

## Result

| Selector | Top-1 | Within 3% | Mean regret | P95 regret | Max regret |
|---|---:|---:|---:|---:|---:|
| Fixed late | 77.5% | 77.5% | 1.54% | 9.56% | 11.46% |
| Frozen V1 | 75.0% | 77.5% | 1.55% | 9.56% | 11.46% |
| Pipeline direct | 77.5% | 77.5% | 1.54% | 9.56% | 11.46% |
| Pipeline + fallback | 75.0% | 77.5% | 1.55% | 9.56% | 11.46% |
| Oracle | 100.0% | 100.0% | 0.00% | 0.00% | 0.00% |

The direct model selected late Mask for every held-out family, making it
identical to the fixed-late baseline. The uncertainty guard made direct model
decisions for 40% of families, but its final choices and regret metrics were
effectively the same as frozen V1.

Six of seven gate checks pass: non-negative coefficients, all governance
audits, at least 25% direct coverage, and no worsening of mean, P95, or maximum
regret. The required strict improvement over V1 within the 3% practical-tie
band fails because both achieve 77.5%. The overall gate therefore fails.

## Interpretation

The result does not refute the observed plan reversals. It shows that this
five-feature additive log-cost formula cannot identify the small stable-early
region while remaining conservative across the broader Phase 2I workload.
Most workload families strongly favor late Mask, so a global non-negative
formula collapses to the majority route.

This negative result is retained rather than repaired by changing the ridge
constant, uncertainty multiplier, or gate after inspection. Phase 2G was not
read or run. Any next model must begin with a new version-controlled hypothesis
and cannot relabel this evaluation as held-out evidence.
