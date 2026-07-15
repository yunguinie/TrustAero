# Phase 2E confirmation record

This document freezes the interpretation of run
`results/phase2e_confirmation/20260715T140108314442Z`. The run is bound to
commit `d25ffa0`, has a clean worktree marker, contains 30 completed units and
1,800 measured executions, and reports equivalent results for both legal
physical candidates. No DuckDB temporary-directory spill was observed.

## What the run established

The two candidates implement the same validated query and the same required
`hash(event_id)`, but place that Mask on opposite sides of a Join that does not
semantically use `event_id`:

- early Mask: raw identifiers do not enter the Join, but every input row is
  hashed and an explicit materialization boundary is paid;
- late Mask (`fused`): raw identifiers enter the Join, but only matching rows
  are hashed.

The stable result is `wide_high_match` at 300K rows. Early Mask won all five
independent seeds. Its paired median speedup over late Mask was 16.94%, with a
seed-bootstrap interval of 9.62% to 19.13%. Median governed latency was
2,668.31 ms for early Mask and 3,120.44 ms for late Mask.

The low-match control reversed strongly. At 100K and 300K rows, early Mask was
respectively 82.81% and 65.16% slower by the paired comparison because it
hashed every row even though only 10% survived the Join. Narrow identifiers
and the 100K wide/high-match case did not satisfy the frozen stability rule.

This is the desired qualitative result: governance-aware placement has no
universally best fixed order. Data scale, identifier width, and Join match rate
change both runtime work and raw-data exposure.

## What the run does not establish

The evidence is synthetic, comes from one machine and one DuckDB version, and
covers only two legal placements in a restricted semantic fragment. It does
not prove that early Mask is generally faster, and it does not yet validate an
optimizer on unseen workloads or real data.

Optimizer V1 uses this confirmation boundary to calibrate its transparent
proxy model. Consequently, its score on this same run is a calibration
(resubstitution) score, not a held-out generalization result. Phase 2F freezes
new seeds, scales, identifier widths, and match rates before measuring V1.

The earlier Phase 2D negative result remains useful as an ablation: forcing
ordinary filter permutations did not beat DuckDB reliably, so TrustAero should
focus on governance choices the underlying database cannot infer by itself.

## Optimizer V1 calibration result

The frozen V1 proxy selects early Mask only for the five `wide_high_match`
300K units and late Mask for the other 25 units. On this calibration run it
selects the exact fastest candidate in 23/30 units (76.7%) and a candidate
within the frozen 3% tie band in 26/30 units (86.7%). Median regret is 0%, P95
regret is 4.94%, and maximum regret is 11.10%.

These numbers show that the implementation follows the intended Phase 2E
boundary; they are not a publishable optimizer-accuracy claim because the
proxy's fixed setup term was calibrated from this run. The untouched Phase 2F
workloads provide the first valid generalization check.
