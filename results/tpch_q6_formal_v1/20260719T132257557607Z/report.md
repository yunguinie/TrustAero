# Governed TPC-H SF1 Q6 paired measurement

Status: **FAIL**

| Candidate | Median (ms) | P95 (ms) | Peak memory (MiB) |
| --- | ---: | ---: | ---: |
| fused | 222.427 | 293.666 | 53.46 |
| materialize-after-q06-predicate | 253.963 | 328.391 | 116.38 |
| materialize-after-q06-time | 639.328 | 763.842 | 116.38 |

- Six-permutation balance: `True`
- Stability gates: `{'absolute_half_drift': True, 'paired_ratio_half_drift': True, 'paired_ratio_outlier_fraction': False}`
- Paired 3% diagnostic Oracle set: `['fused']`
- Optimizer selection evaluated: `False`.
