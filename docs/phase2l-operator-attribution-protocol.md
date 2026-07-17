# Phase 2L paired operator attribution protocol

Phase 2K showed that a global five-feature cost formula collapses to fixed late
Mask. Phase 2L uses existing Phase 2I/J profiles to determine which complete
pipeline role is associated with the observed early/late reversal. It does not
run a new workload, fit a placement model, or read Phase 2G.

## Frozen role mapping

Each early/late candidate is mapped to eight roles:

- SHA-256 projection;
- other supporting projections;
- hash Join;
- ordered output;
- CTE/CTE-scan materialization;
- fact/event scan;
- dimension scan;
- output-table sink.

The SHA-256 role is the highest-time `PROJECTION` in this bounded fragment,
which is verified to contain one Join, one sort, one sink, at least one
projection, and two scans. All non-wrapper operators must be accounted for;
unknown physical shapes fail closed.

## Paired analysis

For every frozen run and seed, early and late operator medians are paired before
their differences are calculated. The timed end-to-end winner retains the
existing 3% practical-tie rule. Replicates sharing `(rows, width, match rate)`
are merged; a stable family requires at least 80% agreement. This reproduces
3/3 for Phase 2I, 4/5 for Phase 2J, and 7/8 where both runs measured the same
physical point.

For each role Phase 2L reports:

1. sign agreement between its paired relative difference and the decisive
   end-to-end winner;
2. Spearman association with the observed early/late log-latency ratio;
3. the fraction of physical families where it has the largest absolute timing
   difference in milliseconds for at least one replicate;
4. whether its median relative difference is negative in stable-early regions
   and positive in stable-late regions.

A role is eligible to motivate a new interaction hypothesis only if all are
true: at least 65% sign agreement, absolute Spearman at least 0.4, dominance in
at least 20% of families, and the stable-region direction reversal. These are
development thresholds fixed before running the combined attribution.

## Scientific boundary

DuckDB `EXPLAIN ANALYZE` profiles are collected separately from timed runs.
Operator timing can contain parallel CPU effects and operator fusion, so role
times are not assumed to add to wall-clock latency. Phase 2L establishes
association and narrows the next physical hypothesis; it cannot prove that one
operator independently caused the performance reversal.

## Reproduction

From the repository root in `TrustAero_env`:

```powershell
python -u scripts/analyze_pipeline_attribution.py `
  --config experiments/configs/phase2l_pipeline_attribution.json
```

The command prints three progress checkpoints and writes paired unit, family,
role, summary, and Markdown report artifacts.
