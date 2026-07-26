# BTS full-month natural multi-Join measurement

Status: **PASS**

| Candidate | Median (ms) | P95 (ms) | Peak memory (MiB) |
| --- | ---: | ---: | ---: |
| fused | 61.341 | 88.811 | 46.89 |
| materialize-after-bts-mj-carrier-join | 88.654 | 126.515 | 50.41 |
| materialize-after-bts-mj-filter | 83.436 | 116.646 | 50.41 |
| materialize-after-bts-mj-origin-join | 79.779 | 112.165 | 50.41 |

- Four-candidate 24-permutation balance: `True`
- Stability gates: `{'absolute_half_drift': True, 'paired_ratio_half_drift': True, 'paired_ratio_outlier_fraction': True}`
- Paired 3% Oracle set: `['fused']`
- Optimizer selection evaluated: `False`.
