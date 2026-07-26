# Approved real-data multi-candidate measurement

Integrity status: **PASS**

This is a frozen development-partition paper-candidate measurement; it is not held-out optimizer evidence.

| Unit | Candidate | Median (ms) | P95 (ms) | Peak memory (MiB) | Spill (MiB) |
|---|---|---:|---:|---:|---:|
| bts-full-2024-01 | fused | 731.871 | 965.877 | 11.77 | 0.00 |
| bts-full-2024-01 | materialize-after-bts-filter | 781.015 | 1013.079 | 21.92 | 0.00 |
| bts-full-2024-01 | materialize-after-gov-002-mask | 638.116 | 866.282 | 28.34 | 0.00 |
| nyc_tlc-full-2024-01 | fused | 342.013 | 433.894 | 55.77 | 0.00 |
| nyc_tlc-full-2024-01 | materialize-after-nyc-filter | 404.618 | 565.973 | 67.62 | 0.00 |
| nyc_tlc-full-2024-01 | materialize-after-nyc-zone-join | 460.895 | 584.580 | 113.66 | 0.00 |

## Interpretation boundary

- Full-month pre-experiment authorized: `True`
- Formal performance experiment authorized: `True`
- Timing stability diagnostic passed: `True`
- Policy-dependent legal Oracle observed: `False`
- Scale-dependent Oracle reversal observed: `False`
- Oracle is computed after running all legal candidates and is not deployable.
- A 3% band is treated as a tie; no winner is claimed inside that band.
