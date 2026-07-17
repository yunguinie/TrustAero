# Phase 2J high-match boundary confirmation result

Run `20260717T134222171980Z` executed the frozen 90-unit protocol on commit
`86a3c55`. It completed 2,700 timed candidate executions and 1,800 operator
summaries in about 53.7 minutes. All 90 early/late pairs were result equivalent,
all 90 actual DuckDB plan pairs were distinct, all Join cardinalities were
exact, and no profile spilled to temporary storage.

This is development boundary confirmation, not Phase 2G.

## Predeclared 4/5 result

With the unchanged 3% practical-tie band and at least four of five seeds
required for a stable family label:

| Family classification | Count |
|---|---:|
| Stable early | 5 |
| Stable late | 3 |
| Stable tie | 0 |
| Mixed | 10 |

The stable-early families and paired median advantage are:

| Rows | Width | Match | Early/Tie/Late seeds | Early advantage |
|---:|---:|---:|---:|---:|
| 100K | 128 | 100% | 4/1/0 | 4.26% |
| 150K | 128 | 90% | 4/1/0 | 10.28% |
| 200K | 128 | 90% | 5/0/0 | 8.93% |
| 200K | 128 | 100% | 5/0/0 | 8.73% |
| 200K | 256 | 100% | 4/1/0 | 5.56% |

Three stable-late families remain at 100K rows: 256 bytes/90%, 512 bytes/90%,
and 512 bytes/100%. Their paired median late advantage ranges from about 4.0%
to 11.7%. Stable early and stable late therefore coexist in a focused,
result-equivalent physical fragment.

## Gate decision

All predeclared optimizer-design checks pass:

- at least two stable-early families: pass;
- at least one stable-late family: pass;
- adjacent stable-early grid families: pass;
- no spilled unit: pass.

The adjacency condition is materially important. For example, 200K/128B/90%
and 200K/128B/100% are both stable early, as are neighboring scale points at
128B/90%. The early result is therefore no longer a single isolated point.

The gate permits design of a pipeline-aware optimizer. It does not show that
such an optimizer is accurate, and it does not authorize Phase 2G. Ten of 18
families are still mixed, so the next model must expose uncertainty and retain
the frozen V1 or a conservative fallback near the boundary.

One analysis-only bug was caught before freezing: family relative differences
were initially displayed as a ratio of separate early/late medians. Commit
`bcbc170` changes this to the scientifically correct paired-seed ratio before
aggregation. Unit classifications and the predeclared gate were unaffected.

Exact source files, analysis files, representative stable-early/stable-late
plans, and SHA-256 digests are listed in
`experiments/frozen/phase2j_boundary_confirmation_record.json`.
