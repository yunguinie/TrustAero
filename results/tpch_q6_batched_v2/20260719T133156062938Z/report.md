# Governed TPC-H SF1 Q6 paired measurement

Status: **FAIL**

| Candidate | Median (ms) | P95 (ms) | Peak memory (MiB) |
| --- | ---: | ---: | ---: |
| fused | 224.144 | 287.529 | 53.46 |
| materialize-after-q06-predicate | 243.307 | 301.614 | 116.28 |
| materialize-after-q06-time | 644.646 | 765.318 | 116.28 |

- Six-permutation balance: `True`
- Stability gates: `{'absolute_half_drift': True, 'paired_ratio_half_drift': True, 'paired_ratio_outlier_fraction': False}`
- Paired 3% diagnostic Oracle set: `['fused']`
- Optimizer selection evaluated: `False`.
