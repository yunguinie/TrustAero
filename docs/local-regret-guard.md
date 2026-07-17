# Nested local-regret guard

The local-regret guard addresses a specific failure of the residual Mask
optimizer: a workload can lie inside every global feature range while still
being far from a reliably evaluated scenario family. A rectangular
minimum/maximum support test cannot identify that sparse interpolation.

## Selector, not another boundary model

The guard compares two already valid selectors:

- the regret-aware residual selector;
- the frozen, explainable V1 selector.

It does not create a rule such as `width > 500`. Workloads are represented by
continuous log input rows, log identifier width, and Join match rate. Distance
is standardized with statistics computed only from the current training fold.

For a query, the guard finds the three nearest distinct scenario families. It
computes each selector's unweighted mean regret over those families and uses
the lower-regret selector; a tie returns to frozen V1. Three neighbors is a
frozen design constant and is not selected by trying several values on the
outer evaluation results.

## Nested grouping prevents leakage

The primary evaluation has two levels:

```text
outer fold: hold out one complete scenario family for evaluation
  inner folds: within outer training data, hold out each scenario family
               to produce calibration regrets
  fit final residual model on outer training data
  select residual or V1 using only inner out-of-fold local regrets
  evaluate once on the untouched outer family
```

Thus no seed, scale, timing, winner, or regret from the outer family enters the
guard. Calibration records retain the inner held-out group ID, workload ID,
feature vector, residual regret, and V1 regret for auditability.

## Governance remains a hard constraint

Both underlying selectors filter illegal candidates and enforce the raw-row
exposure limit before ranking. The guard only chooses between their feasible
outputs. It cannot re-enable an illegal placement.

## Promotion gate

The guard is compared with frozen V1 using the same predeclared requirements:

1. strictly higher within-3% selection rate;
2. no worse P95 regret;
3. no worse maximum regret;
4. zero corrected residual-score match monotonicity violations.

Passing this gate would authorize freezing Phase 2G; it would not turn this
development matrix into held-out evidence. The evaluation result and artifact
status are recorded after the implementation is frozen.

## Recorded development result

The frozen three-neighbor guard was evaluated over the same 30 observations as
the residual development study. The full nested run took about 7.5 minutes on
the development machine.

| Model | Within 3% | Mean regret | P95 regret | Maximum regret |
|---|---:|---:|---:|---:|
| Frozen V1 | 70.0% | 3.35% | 18.03% | 21.33% |
| Unguarded residual | 80.0% | 3.25% | 28.43% | 37.64% |
| Nested local-regret guard | 70.0% | 4.65% | 28.43% | 37.64% |

Only the monotonicity check passes. The guard is serialized with status
`development_only_rejected_by_predeclared_gate`, and Phase 2G remains
unfrozen. Across 30 outer predictions, 23 selectors agreed, the guard selected
V1 three times, and it selected the residual model four times.

The critical `w256_match100` family remains wrong. Its nearest groups are
512/768-byte high-match families, whose inner-fold regret favors the residual
selector; geometric proximity therefore transfers the wrong execution regime.
The guard also falls back to V1 on some 512-byte cases where early Mask is
actually faster. This demonstrates that Euclidean workload proximity is not a
sufficient proxy for shared physical execution behavior.

Trying neighbor counts 1, 2, or 4 after seeing this outer result would be
post-hoc tuning and is deliberately not performed. A successor should use
mechanism-level observables such as measured hash throughput, payload-copy
cost, materialization cost, and operator cardinalities, rather than another
distance heuristic over the same sparse coordinates.

The development CLI supports `--progress`; future nested runs print one line
after every completed outer fold so a VS Code terminal shows visible progress.
