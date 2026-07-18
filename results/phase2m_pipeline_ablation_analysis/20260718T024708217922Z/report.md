# Phase 2M compact policy-aware ablation

This is development evidence, not Phase 2G or a final optimizer result.

- Scenarios: 15
- Families: 3
- Timed measurements: 900
- Policy changes stable optimum: True
- V2.1 hypothesis gate: False

| Region | Policy | Stable practical winner | Median governance overhead |
|---|---|---|---:|
| phase2l_mixed | no_raw_join | early_hash_materialized | 71.92% |
| phase2l_mixed | no_raw_materialization | stable_tie | 65.98% |
| phase2l_mixed | raw_permissive | late_join_materialized | 0.00% |
| phase2l_stable_late | no_raw_join | early_hash_materialized | 122.62% |
| phase2l_stable_late | no_raw_materialization | mixed | 94.56% |
| phase2l_stable_late | raw_permissive | late_join_materialized | 0.00% |
| phase2l_stable_early | no_raw_join | early_hash_materialized | 24.61% |
| phase2l_stable_early | no_raw_materialization | mixed | 19.38% |
| phase2l_stable_early | raw_permissive | late_join_materialized | 0.00% |

> Raw-intermediate materialization is a hard policy constraint. An 
> illegal diagnostic is excluded before the legal oracle is computed.
