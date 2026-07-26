# Formal real-data development-partition results

Status: **PASS**. Source commit: `dc87ac9aad801fa08556d60ff329a3bec25207cc`.

The frozen January 2024 suite measured three governance-driven query families
on a clean source snapshot. Every legal candidate returned the same relation,
retained a distinct observed DuckDB plan, and carried checked lineage and
certificate evidence. All predeclared order, completeness, and timing-stability
gates passed; no candidate spilled to disk.

| Query | Candidate | Median (ms) | P95 (ms) | Interpretation |
| --- | --- | ---: | ---: | --- |
| BTS masked read | fused | 731.871 | 965.877 | legal under both profiles |
| BTS masked read | materialize raw after Filter | 781.015 | 1013.079 | slower; rejected by no-raw-materialization policy |
| BTS masked read | materialize after Mask | 638.116 | 866.282 | fastest; 1.147x versus fused |
| NYC zone aggregate | fused | 342.013 | 433.894 | fastest |
| NYC zone aggregate | materialize after Filter | 404.618 | 565.973 | slower |
| NYC zone aggregate | materialize after Join | 460.895 | 584.580 | slower and highest peak memory |
| BTS Mask/Join | early Mask | 761.672 | 1161.011 | only candidate legal under strict no-raw-Join policy |
| BTS Mask/Join | late Mask | 792.310 | 1082.635 | exposes 106,251 raw rows to Join |
| BTS natural multi-Join | fused | 61.341 | 88.811 | fastest across four legal routes |
| BTS natural multi-Join | materialize after Filter | 83.436 | 116.646 | 1.296x fused paired ratio |
| BTS natural multi-Join | materialize after first Join | 79.779 | 112.165 | 1.275x fused paired ratio |
| BTS natural multi-Join | materialize after second Join | 88.654 | 126.515 | 1.378x fused paired ratio |

The Mask/Join conclusion uses the paired within-block ratio, not the two
separate medians. Median `early / late = 1.0067`, which lies inside the frozen
3% tie band. Therefore early and late are reported as a performance tie under
the permissive policy; the strict policy nevertheless forces early Mask.

## What this establishes

- A fixed fused route is not universally best: BTS benefits from a legal
  post-Mask boundary, while NYC is fastest when fused.
- Governance can remove a physically executable route before cost comparison.
- Performance and governance must be represented separately: a tie can still
  have only one policy-feasible candidate.
- The timing and integrity infrastructure is ready for larger frozen workloads.
- Four-candidate queries use all 24 execution orders; the multi-Join run repeated
  each permutation twice and passed all balance and stability checks.

## What this does not establish

January was already used for integration and is not an independent holdout.
The suite times all legal candidates to compute a diagnostic Oracle; it does not
evaluate Optimizer V1 or V2 selection. Final optimizer claims still require
unseen months/query families, TPC-H, additional real workloads, and comparisons
against fixed baselines and Oracle.
