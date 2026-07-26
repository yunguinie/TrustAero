# Governed TPC-H SF1 Q6 paired measurement

Status: **PASS**

| Candidate | Median (ms) | P95 (ms) | Peak memory (MiB) |
| --- | ---: | ---: | ---: |
| fused | 152.245 | 196.622 | 53.48 |
| materialize-after-q06-predicate | 168.395 | 223.545 | 116.44 |
| materialize-after-q06-time | 683.658 | 893.107 | 116.44 |

- Six-permutation balance: `True`
- Stability gates: `{'absolute_half_drift': True, 'paired_ratio_half_drift': True, 'paired_ratio_outlier_fraction': True}`
- Paired 3% diagnostic Oracle set: `['fused']`
- Optimizer selection evaluated: `False`.
