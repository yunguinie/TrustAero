# Phase 2M complete-pipeline ablation smoke result

The frozen Phase 2M smoke completed successfully. All three representative
development scenarios produced equal governed output across four distinct
DuckDB physical plans, retained the requested boundaries, matched exact Join
cardinalities, and used no temporary-directory spill. A compact ablation matrix
is technically authorized; Phase 2G remains unauthorized.

## Protocol validation

- three scenarios and four variants;
- 36 timed measurements and 12 analyzed physical plans;
- three of three result-equivalent scenarios;
- three of three scenarios with four distinct fingerprints;
- three of three scenarios with every requested physical boundary validated;
- three of three exact Join-cardinality checks;
- zero spilled profiles and zero spilled scenarios.

The smoke completed in 41.7 seconds on CPU. One warm-up, three measurements,
and one profile per variant are deliberately insufficient for a performance
claim.

## Timing diagnostic

| Region | Late fused | Join→raw materialize→Hash | Join→Hash→materialize | Early Hash→materialize | Smoke fastest |
|---|---:|---:|---:|---:|---|
| Stable early | 634.90 ms | 476.48 ms | 525.93 ms | 566.15 ms | Join materialized |
| Stable late | 870.74 ms | 536.31 ms | 851.62 ms | 1185.68 ms | Join materialized |
| Mixed | 456.87 ms | 259.26 ms | 472.84 ms | 530.03 ms | Join materialized |

Relative to the faster of the two previously studied early/late candidates,
the Join-materialized diagnostic is 1.19x, 1.62x, and 1.76x faster in these
three smoke instances. These ratios are mechanism clues, not paper results.

## Governance interpretation

`late_join_materialized` writes matched raw sensitive values into an explicit
intermediate. This has a stronger exposure footprint than carrying raw values
through a fused Join. It cannot become an optimizer candidate merely because
it is faster. The compact matrix must record at least:

- `raw_rows_exposed_to_join`;
- `raw_rows_materialized`;
- whether raw intermediate materialization is policy-permitted;
- the masked rows materialized by the other variants.

When policy prohibits raw intermediate storage, the Join-materialized variant
must be removed before cost ranking. The remaining legal variants still provide
the controlled ablation needed to distinguish Join/Hash and Hash/Sort boundary
effects.

## Decision

The smoke authorizes one separately frozen compact Phase 2M matrix over the
same three development regions and new development seeds. The matrix must use
more repetitions, randomized rotation, repeated physical profiles, complete
family aggregation, and the new exposure annotations. It must not introduce
new row-count, width, or match-rate boundary points and must not consume Phase
2G values.
