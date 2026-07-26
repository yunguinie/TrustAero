# Optimizer V3 independent holdout results

Status: **PASS; one-shot holdout consumed**. Source commit:
`62c7305c0fc869aa17b8d4e78c03ba7c618ce560`.

Phase 2G evaluated the frozen Optimizer V3 exactly once on 36 previously unseen
Mask-placement families (180 seeded units). All 5,400 timed executions passed
semantic validation; paired candidates returned equivalent results, retained
distinct observed DuckDB plan fragments, passed the governance audit, and did
not spill to disk.

| Scheme | Exact fastest | Within 3% of Oracle | Mean regret | P95 regret | Maximum regret |
| --- | ---: | ---: | ---: | ---: | ---: |
| Fixed early Mask | 8.33% | 16.67% | 96.349% | 314.215% | 325.182% |
| Fixed late Mask / frozen V1 | 91.67% | 97.22% | 0.193% | 1.461% | 4.388% |
| Frozen Optimizer V3 | 94.44% | 100.00% | 0.071% | 1.109% | 1.461% |
| Experimental Oracle | 100.00% | 100.00% | 0% | 0% | 0% |

V3 made a direct model decision in 28 of 36 families. It correctly selected
early Mask for the held-out `n175000-w192-m0950` family, where frozen V1 chose
late Mask and incurred 4.388% regret. V3 missed the exact fastest placement in
two families, but both selections remained inside the predeclared 3% tie band.

## Authorized interpretation

The holdout gate passed, so the results support the claim that the frozen V3
generalizes to unseen in-support Mask-placement settings without an observed
regression under the frozen metrics. In particular, every held-out family was
within 3% of the experimental Oracle and the observed maximum regret fell from
4.388% for V1 to 1.461% for V3.

The result does **not** authorize a statistical-superiority claim over V1. The
paired 95% interval for the within-3% improvement is `[0, 0.0833]`, and the
paired interval for the V3-minus-V1 mean-regret difference is
`[-0.3657%, 0]`. Both touch zero under the predeclared strict rule.

This holdout is permanently consumed. It cannot be used to tune another V3
variant or presented again as an independent test. The evaluated model applies
to the frozen early/late Mask-placement fragment; it is not a general-purpose
Join-order or materialization optimizer.
