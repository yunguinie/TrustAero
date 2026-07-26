# Governed TPC-H SF1 Q1 paired measurement

Status: **PASS**

| Candidate | Median (ms) | P95 (ms) | Peak memory (MiB) |
| --- | ---: | ---: | ---: |
| fused | 740.136 | 987.029 | 121.12 |
| materialize-after-q01-aggregate | 754.042 | 975.240 | 996.79 |
| materialize-after-q01-filter | 1639.878 | 2045.255 | 996.79 |

- Six-permutation balance: `True`
- Stability gates: `{'absolute_half_drift': True, 'paired_ratio_half_drift': True, 'paired_ratio_outlier_fraction': True}`
- Paired 3% diagnostic Oracle set: `['fused', 'materialize-after-q01-aggregate']`
- Optimizer selection evaluated: `False`.
