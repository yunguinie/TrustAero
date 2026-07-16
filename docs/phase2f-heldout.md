# Phase 2F held-out Optimizer V1 record

This document freezes run
`results/phase2f_optimizer_holdout/20260715T152953290778Z` as the first unseen
workload test of Mask Optimizer V1. The run is bound to clean commit `35c3d03`,
completed 24 scenario/scale/seed units and 480 measured candidate executions in
44 minutes 5 seconds, produced equivalent results, and observed no DuckDB
temporary-directory spill.

## Frozen V1 result

V1 selected the exact fastest candidate in 18/24 units (75%). The same 18/24
fell within the predeclared 3% tie band. Median regret was 0%, mean regret was
2.73%, P95 regret was 12.72%, and maximum regret was 15.77%.

Across the heterogeneous workload set, V1 achieved a 1.088x geometric-mean
speedup over always using late Mask and 1.150x over always using early Mask.
These aggregates complement, rather than replace, the per-scenario results.

All six V1 errors occurred in the previously unseen 512-byte, high-match
scenario. Early Mask was actually 10.94% faster at 200K rows and 9.82% faster
at 500K rows. The error is diagnostic: total raw bytes alone cannot represent
both per-row Join payload overhead and materialization cost.

The remaining held-out scenario/scale groups were selected correctly:

- 2048-byte/high-match: early Mask at 200K and 500K;
- 2048-byte/low-match: late Mask at 200K and 500K;
- 2048-byte/50%-match: late Mask at 200K and early Mask at 500K.

## Scientific status

Phase 2F remains a valid held-out result for the already frozen V1. After this
inspection it becomes development data for V2 and must never be presented as
an independent V2 test. V2 feature design and fitting use Phase 2E/F only to
prepare a newly frozen Phase 2G protocol.

The run has three seeds, synthetic data, two legal Mask placements, one
machine, and one DuckDB version. Profiled peak buffer memory reached about
4.37 GB without spilling, so a larger protocol must monitor memory carefully.
