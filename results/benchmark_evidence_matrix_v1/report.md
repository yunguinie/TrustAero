# Cross-workload governed candidate evidence

This is derived paper-performance evidence, not optimizer evaluation.

| Workload | Query family | Candidates | Reference regret | Oracle set |
| --- | --- | ---: | ---: | --- |
| tpch-q01-sf1 | filter_group_aggregate_sort | 3 | 0.00% | fused, materialize-after-q01-aggregate |
| tpch-q06-sf1 | filter_aggregate | 3 | 0.00% | fused |
| bts-2024-01-governed | filter_mask_aggregate | 3 | 14.69% | materialize-after-gov-002-mask |
| nyc-tlc-2024-01-governed | filter_join_aggregate | 3 | 0.00% | fused |

- Workloads: `4`
- Alternative boundary in 3% tie band: `2`
- Reference outside Oracle set: `1`
- Held-out optimizer evidence: `False`
- Optimizer selection evaluated: `False`
