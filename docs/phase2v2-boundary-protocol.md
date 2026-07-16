# Optimizer V2 boundary-calibration protocol

This protocol is development data, not a held-out or paper-confirmation run.
Its purpose is to identify the decision boundaries that Phase 2E/F sampled too
sparsely before any V2 model is promoted.

## Fractional boundary matrix

The runner now permits each scenario to declare its own `row_counts`. A
scenario-specific list overrides the legacy global list, avoiding a full
scenario-by-scale Cartesian product while preserving deterministic unit IDs,
checkpoints, resume behavior, and provenance.

The frozen configuration contains 16 scenario/scale groups:

- high match at widths 256, 512, and 768 bytes;
- 25% and 75% match at width 1024;
- 10%, 25%, and 75% match at width 2048;
- targeted scales from 150K to 450K rows.

Two new seeds and seven measured repetitions produce 32 atomic units and 448
measured candidate executions. With two warmups, the run performs 576 total
candidate executions. The maximum scale is capped at 450K because the previous
500K/2048-byte run already approached the configured memory limit.

## Predeclared model comparison

After completion, Phase 2E, Phase 2F, and this calibration run become V2
development data. Compare the frozen V1 proxy, the current ridge latency-ratio
model, and a physically constrained analytical candidate using the same paired
workload observations.

The primary split is leave-one-scenario-family-out. A replacement model may be
considered for a new holdout only if it:

1. improves within-3% selection rate over V1;
2. does not worsen P95 regret;
3. does not worsen maximum regret materially;
4. has no match-rate monotonicity violations on the audit grid;
5. never overrides semantic legality or a raw-exposure limit.

For fixed rows and identifier width, increasing Join match rate must not make
early Mask less attractive: late Mask hashes more matched rows, while early
Mask already hashes all input rows. This directional requirement is audited
separately from predictive accuracy.

Passing this development gate does not establish generalization. It only
authorizes freezing a completely new Phase 2G configuration with unseen seeds
and feature combinations.
