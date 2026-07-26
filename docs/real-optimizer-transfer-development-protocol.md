# Real-data Optimizer V3 transfer gate

## Question and boundary

This development gate asks whether the already frozen two-candidate Mask
optimizer remains usable when the synthetic event relation is replaced by the
January 2024 BTS On-Time flight distribution.  January is a development
partition and **not** an independent paper holdout.  The V3 coefficients,
uncertainty threshold, and fallback policy are immutable during this gate.

The native flight rows, dates, airport identifiers, distance filter, Join
frequency skew, and airport attributes remain real.  Public BTS data do not
contain long confidential payloads, so a deterministic padded `Tail_Number`
is used to control only the sensitive-field width.  Dimension rows are chosen
deterministically to create target Join match rates; the achieved row-weighted
rate, rather than the requested rate, is passed to the optimizer and reported.

## Legal candidates

Both candidates originate from one validated TrustAero logical plan:

1. `late_mask`: filter, Join, SHA-256 Mask, non-sensitive Sort;
2. `early_mask_materialized`: filter, SHA-256 Mask, an explicit materialized
   boundary, Join, the same non-sensitive Sort.

The physical planner independently proves that `Tail_Number` is not a Join
key before moving its Mask.  The combined strategy can materialize only the
moved Mask.  The timing harness creates the final result as a temporary table
for both candidates, matching the server-side materialization convention used
by the frozen Phase 2G fragment.  This common measurement wrapper is not a
third optimizer decision.

Phase 2G sorted by the hash itself, which the conservative IR V1 does not allow
as semantic reuse.  This gate instead sorts by raw, non-sensitive `Distance`
and `airport_code`.  Therefore it is a mechanism-transfer check, not a claim
that the real query is byte-for-byte identical to the synthetic workload.

## Matrix and measurement

- full January fact input with the frozen 8--22 January governance filter;
- widths: 192, 384, 768, and 1536 bytes;
- target row-weighted Join rates: 25%, 70%, and 95%;
- two warm-up paired blocks and twenty measured paired blocks per family;
- balanced early/late order, one DuckDB connection, hot cache, four threads,
  4 GiB memory limit, and no permitted spill.

Each family must have equal result checksums, distinct DuckDB physical-plan
fingerprints, successful validation, in-support features, and no spill.  V3 is
compared with fixed early, fixed late, frozen V1, and the experimental Oracle.
The gate requires at least 75% of families within 3% of Oracle, mean regret at
most 3%, maximum regret at most 10%, and direct V3 coverage at least 25%.

Passing authorizes a separate frozen February--December external evaluation.
It does not authorize a superiority statement.  Failure is retained as a
negative transfer result and must not be repaired by tuning V3 on January and
then calling the later run an unchanged-model test.
