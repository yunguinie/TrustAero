# Phase 2I complete-fragment pilot analysis

This is development evidence, not an independent Phase 2G result.

- Units: 72
- Scenario families: 24
- Stable early families: 1
- Stable late families: 17
- Stable tie families: 1
- Mixed families: 5
- Stable reversal observed: True

| Rows | Width | Match | Early/Tie/Late seeds | Family | Early ms | Late ms |
|---:|---:|---:|---:|---|---:|---:|
| 150000 | 1024 | 10% | 0/0/3 | stable_late | 1811.05 | 220.44 |
| 150000 | 1024 | 50% | 0/0/3 | stable_late | 1897.78 | 1129.33 |
| 150000 | 1024 | 100% | 0/2/1 | mixed | 2128.20 | 2072.73 |
| 150000 | 2048 | 10% | 0/0/3 | stable_late | 3712.30 | 448.96 |
| 150000 | 2048 | 50% | 0/0/3 | stable_late | 3806.77 | 2269.68 |
| 150000 | 2048 | 100% | 0/3/0 | stable_tie | 3855.87 | 3845.13 |
| 150000 | 256 | 10% | 0/0/3 | stable_late | 702.21 | 100.29 |
| 150000 | 256 | 50% | 0/0/3 | stable_late | 751.67 | 469.37 |
| 150000 | 256 | 100% | 3/0/0 | stable_early | 782.53 | 825.14 |
| 150000 | 512 | 10% | 0/0/3 | stable_late | 1135.68 | 145.41 |
| 150000 | 512 | 50% | 0/0/3 | stable_late | 1177.33 | 726.35 |
| 150000 | 512 | 100% | 0/2/1 | mixed | 1292.24 | 1278.60 |
| 50000 | 1024 | 10% | 0/0/3 | stable_late | 853.28 | 87.86 |
| 50000 | 1024 | 50% | 0/0/3 | stable_late | 804.50 | 377.60 |
| 50000 | 1024 | 100% | 1/2/0 | mixed | 915.75 | 901.74 |
| 50000 | 2048 | 10% | 0/0/3 | stable_late | 1520.12 | 145.64 |
| 50000 | 2048 | 50% | 0/0/3 | stable_late | 1358.24 | 645.46 |
| 50000 | 2048 | 100% | 0/0/3 | stable_late | 1345.42 | 1189.05 |
| 50000 | 256 | 10% | 0/0/3 | stable_late | 245.07 | 30.18 |
| 50000 | 256 | 50% | 0/0/3 | stable_late | 256.78 | 131.86 |
| 50000 | 256 | 100% | 2/0/1 | mixed | 272.08 | 351.42 |
| 50000 | 512 | 10% | 0/0/3 | stable_late | 419.39 | 57.95 |
| 50000 | 512 | 50% | 0/0/3 | stable_late | 421.64 | 232.86 |
| 50000 | 512 | 100% | 0/1/2 | mixed | 466.26 | 476.96 |

The stable early region contains too few families to fit a new selector 
responsibly. Confirm the high-match boundary with a separately frozen 
matrix and new seeds before designing a pipeline-aware optimizer.
