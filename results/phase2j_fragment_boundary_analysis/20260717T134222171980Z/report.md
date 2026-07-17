# Complete Mask-fragment family analysis

This is development evidence, not an independent Phase 2G result.

- Units: 90
- Scenario families: 18
- Stable early families: 5
- Stable late families: 3
- Stable tie families: 0
- Mixed families: 10
- Stable reversal observed: True
- Required seed agreement: 80%
- Optimizer-design gate passed: True

| Rows | Width | Match | Early/Tie/Late seeds | Family | Early ms | Late ms |
|---:|---:|---:|---:|---|---:|---:|
| 100000 | 128 | 90% | 0/2/3 | mixed | 464.83 | 453.67 |
| 100000 | 128 | 100% | 4/1/0 | stable_early | 476.03 | 468.95 |
| 100000 | 256 | 90% | 1/0/4 | stable_late | 628.34 | 573.22 |
| 100000 | 256 | 100% | 1/2/2 | mixed | 626.72 | 636.25 |
| 100000 | 512 | 90% | 0/0/5 | stable_late | 961.15 | 857.85 |
| 100000 | 512 | 100% | 0/1/4 | stable_late | 969.34 | 953.52 |
| 150000 | 128 | 90% | 4/1/0 | stable_early | 563.42 | 608.32 |
| 150000 | 128 | 100% | 3/1/1 | mixed | 586.82 | 631.45 |
| 150000 | 256 | 90% | 1/3/1 | mixed | 807.47 | 799.01 |
| 150000 | 256 | 100% | 2/3/0 | mixed | 775.49 | 803.95 |
| 150000 | 512 | 90% | 0/3/2 | mixed | 1219.52 | 1190.09 |
| 150000 | 512 | 100% | 1/2/2 | mixed | 1217.60 | 1207.97 |
| 200000 | 128 | 90% | 5/0/0 | stable_early | 580.98 | 646.16 |
| 200000 | 128 | 100% | 5/0/0 | stable_early | 587.72 | 657.34 |
| 200000 | 256 | 90% | 3/1/1 | mixed | 842.34 | 914.36 |
| 200000 | 256 | 100% | 4/1/0 | stable_early | 839.93 | 876.89 |
| 200000 | 512 | 90% | 2/1/2 | mixed | 1270.51 | 1287.82 |
| 200000 | 512 | 100% | 1/2/2 | mixed | 1283.77 | 1334.58 |

Passing this gate permits pipeline-aware model design only. It does not 
authorize Phase 2G or a held-out generalization claim.
