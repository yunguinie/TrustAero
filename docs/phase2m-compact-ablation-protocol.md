# Phase 2M compact policy-aware ablation protocol

The Phase 2M smoke validated four distinct result-equivalent DuckDB plans and
found that the Join-materialized diagnostic can be fast. That diagnostic also
writes matched raw sensitive values to an intermediate, so the compact matrix
must analyze physical cost and governance feasibility together.

## Frozen exposure semantics

| Variant | Raw rows enter Join | Raw rows materialized | Masked rows materialized |
|---|---:|---:|---:|
| Late fused | input rows | 0 | 0 |
| Late Join-materialized | input rows | matched rows | 0 |
| Late Hash-materialized | input rows | 0 | matched rows |
| Early Hash-materialized | 0 | 0 | input rows |

These annotations are produced before runtime comparison. A policy-infeasible
variant is excluded even when it has the lowest measured latency.

## Frozen policy profiles

1. `raw_permissive`: raw Join and raw intermediate materialization are allowed;
2. `no_raw_materialization`: raw Join is allowed, raw intermediate storage is not;
3. `no_raw_join`: raw sensitive values may not enter Join or be materialized.

The third policy leaves early Hash-materialized as the single supported
candidate in this bounded fragment. This is a feasibility outcome, not a cost
model prediction.

## Compact matrix

The matrix does not add boundary points. It reuses the smoke's three development
families and introduces five new development seeds:

- `1777`, `1888`, `1999`, `2111`, `2222`;
- three physical families × five seeds = 15 atomic units;
- four variants per unit;
- two warm-ups, 15 measured repetitions, three analyzed profiles;
- 900 timed measurements and 180 physical profiles when complete.

Candidate order rotates deterministically. Every unit retains output digests,
fingerprints, exact cardinalities, boundary checks, memory, spill, exposure,
checkpoint, progress, and ETA information.

## Frozen analysis and V2.1 hypothesis gate

Within each seed and policy, the analyzer filters illegal candidates first. It
then reports the fastest legal candidate, candidates within the fixed 3% band,
and governance overhead relative to the unconstrained fastest diagnostic.
A family needs at least four of five seeds to share a practical winner.

The compact matrix permits design of a final V2.1 hypothesis only if:

1. all three families contain exactly five seeds;
2. at least one family changes stable optimum across policy profiles;
3. the `no_raw_materialization` profile has at least two different stable
   winners across the three data families;
4. every selected candidate is policy-legal;
5. no scenario spills to temporary storage.

Failure is retained and stops V2.1 design from this matrix. Passing permits one
new version-controlled optimizer hypothesis; it does not authorize Phase 2G.

## Commands

Run with visible progress:

```powershell
python -u scripts/run_pipeline_ablation.py `
  --config experiments/configs/phase2m_pipeline_ablation_compact.json `
  --progress
```

Resume after interruption:

```powershell
python -u scripts/run_pipeline_ablation.py `
  --config experiments/configs/phase2m_pipeline_ablation_compact.json `
  --resume `
  --progress
```

Analyze only after the run completes:

```powershell
python -u scripts/analyze_pipeline_ablation.py <RUN_DIR> `
  --output-dir results/phase2m_pipeline_ablation_analysis/<RUN_ID> `
  --tie-threshold 0.03 `
  --required-seed-agreement 0.8
```
