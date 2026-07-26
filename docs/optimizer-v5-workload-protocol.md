# Optimizer V5 legal-candidate workload protocol

## Why V5 changes the development workload

The frozen V4.1 result shows that the current Mask/Join grid is separable by
Join match rate alone.  It is therefore inadequate for claiming that a
pipeline-aware model adds value.  V5 does not tune another threshold on that
grid.  It broadens the optimization task to the already implemented
governance-aware legal candidate space.

V5 remains bounded.  It ranks only candidates generated and approved by
TrustAero; it does not optimize arbitrary SQL or general Join ordering.
Governance feasibility is resolved before performance ranking, and an illegal
candidate can never win because of latency.

## Reused development evidence

The following January-only results already exist and must not be rerun merely
to obtain a more favorable answer:

- BTS governed masked read, full month, three physical candidates;
- NYC zone aggregate, full month, three physical candidates;
- BTS natural multi-Join, full month, four physical candidates;
- BTS Mask placement, 48 paired families over four complete January windows.

They cover fused execution, materialization after Filter, Mask, and Join,
multi-Join boundaries, Mask-before/after-Join work, hard raw-value exposure
constraints, and source-lineage requirements.  Existing full-month and
multi-Join timings are development evidence, not held-out optimizer evidence.

## New bounded calibration

The only new measurement run adds stable scale points for the existing BTS and
NYC templates:

| Workload | Rows | Candidates | Warm-up blocks | Measured blocks |
|---|---:|---:|---:|---:|
| BTS governed read | 100,000 | 3 | 6 | 30 |
| BTS governed read | 500,000 | 3 | 6 | 30 |
| NYC zone aggregate | 100,000 | 3 | 6 | 30 |
| NYC zone aggregate | 500,000 | 3 | 6 | 30 |

Each three-candidate permutation occurs equally often.  All candidates in a
block share one DuckDB process and connection.  Results must be semantically
identical, physical plans must remain distinct, execution order must be
balanced, and the paired stability audit must pass.  The run is resumable and
prints progress.

The new calibration uses only existing E-drive artifacts and does not download
data.  It is labelled development-only and cannot be reported as held-out
optimizer performance.

## Gate before any V5 model

Model fitting is prohibited until the merged development manifest proves:

1. every compared candidate is governance-feasible for its policy profile;
2. every query unit has equivalent results and distinct DuckDB plans;
3. the new four-unit paired timing audit passes;
4. at least three governance-driven query templates are represented;
5. at least two different legal candidate classes are stable winners;
6. the Mask-only match-rate baseline remains reported but is not treated as a
   baseline for unrelated materialization choices;
7. complete query-template families, rather than individual repetitions, are
   kept together during cross-validation;
8. February--December and future real-data holdouts remain unopened.

Only after these conditions pass may a generic candidate-work cost model be
implemented.  V4, V4.1, fixed fused, fixed safe, template-local simple rules,
and Oracle remain mandatory baselines.

