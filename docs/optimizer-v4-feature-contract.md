# Pipeline-aware Optimizer V4 feature contract

## Stage boundary

This stage freezes a candidate-work contract, not a fitted optimizer.  It does
not alter V3 and does not inspect February--December performance labels.  A V4
model may be designed only after January data collection proves that every
feature is available before execution and that the candidate work records are
stable and reproducible.

V4 remains scoped to the reviewed DuckDB fragment:

- one filtering many-to-one inner Join;
- one SHA-256 Mask on a non-key string field;
- a legal early Mask with an explicit materialized boundary;
- a legal late fused Mask;
- ordered, server-side materialized output;
- Parquet input, hot cache, fixed DuckDB version, thread count, and memory cap.

Changing backend, cache protocol, Join multiplicity, or Mask method creates a
new support domain rather than a silent extrapolation.

## Workload statistics available before ranking

The frozen statistics are:

- physical source rows scanned;
- rows entering the Join after common filters;
- estimated rows leaving the Join;
- dimension build rows;
- raw sensitive payload width after any controlled derivation;
- total physical source-scan payload width and fixed fact-side Join width;
- dimension build-side and projected-output payload widths (the Join key is
  not silently charged to the final projection);
- sort-key width;
- estimate provenance and hard governance feasibility.

Development may use exact controlled catalog counts, but the external
evaluation must produce the same fields from frozen catalog statistics or a
frozen estimator.  Candidate runtime, Oracle labels, and post-execution
operator timings are never optimizer inputs.

## Candidate-specific work

For early Mask, the contract records:

- hashing all Join-input sensitive values;
- carrying fixed-width hashes through Join;
- writing and reading the masked boundary;
- zero raw-sensitive Join exposure.

For late Mask, it records:

- carrying raw sensitive values through Join;
- hashing only estimated matched rows;
- no early boundary;
- raw-sensitive Join exposure equal to Join-input rows.

Both candidates also retain source scan payload, output payload, Join output
rows, dimension-build payload, controlled sensitive-value derivation payload,
and sort comparison work because DuckDB pipeline breaking can make common
logical work interact with placement.  Physical source scan bytes remain
separate from a wide value produced after filtering.  These quantities are not
assumed to be additive wall-clock milliseconds.

## Explicit non-goals

V4 must not:

- refit the rejected Phase 2K five-feature formula under a new name;
- sum Phase 2H microbenchmark milliseconds as if DuckDB operators were causal
  and additive;
- tune V3 thresholds or its V1 fallback after seeing January labels;
- include common policy-filter or source-lineage cost merely to improve fit;
- rank a candidate that governance feasibility has rejected;
- use February--December labels for feature design or parameter selection.

## Required next gate

The next implementation stage must extract the frozen statistics for January
development families and verify:

1. statistic values are identical across early and late candidates;
2. estimated Join rows match controlled development cardinalities;
3. raw and masked exposure accounting matches the approved physical plans;
4. candidate results and DuckDB plans remain equal/distinct as required;
5. multiple stable early and late families remain after grouping by complete
   time-window, width, match-rate, and dimension-subset scenario;
6. no future external partition is opened.

Only after that gate may alternative V4 formulas be compared by complete-
scenario cross-validation.  V1, V3, fixed early, fixed late, and Oracle remain
mandatory baselines.
