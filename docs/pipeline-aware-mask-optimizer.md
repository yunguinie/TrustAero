# Pipeline-aware Mask Optimizer V2

This development stage follows the negative isolated-mechanism formula. It
uses complete, result-equivalent DuckDB fragments from Phase 2I and Phase 2J;
it does not use or authorize the untouched Phase 2G workload.

## Bounded decision

The optimizer ranks only two candidates that have already passed TrustAero's
semantic and governance checks:

```text
early: scan -> hash all raw values -> materialize -> Join -> ordered output
late:  scan -> Join raw values -> hash matched values -> ordered output
```

The model cannot make Mask generally commutative. An illegal candidate or one
that exceeds the raw-value exposure limit is removed before latency ranking.
If neither candidate is legal, selection fails closed.

## Frozen physical formula

One shared non-negative ridge formula predicts the log latency of both
candidates from five interpretable quantities:

1. scanned raw payload;
2. payload actually hashed;
3. payload entering Join;
4. Join output cardinality;
5. whether an early materialization boundary is introduced.

The first four work quantities use `log1p`, allowing continuous scale effects
without hard-coding a width or row-count threshold. Coefficients are constrained
to be non-negative. The cost formula predicts both candidate latencies and
ranks their ratio; it is not a direct early/late classifier.

## Leakage and uncertainty rules

All seeds sharing the same `(rows, identifier width, Join match rate)` are
paired and aggregated into one family before fitting. Phase 2I and Phase 2J
replicates of the same physical family are also merged. Cross-validation holds
out that complete family, so no seed or duplicate grid point appears in both
training and testing.

The training paired-log-ratio RMSE defines the uncertainty margin. A ranking
inside that margin, or outside the observed rows/width/match support, falls
back to frozen governance-aware Optimizer V1. This fallback is reported; it is
not counted as a direct model decision.

## Predeclared development gate

Against frozen V1, guarded leave-one-family-out evaluation must simultaneously:

1. strictly improve the fraction within the fixed 3% practical-tie band;
2. not worsen mean regret;
3. not worsen P95 regret;
4. not worsen maximum regret;
5. make direct model decisions for at least 25% of families;
6. retain non-negative cost coefficients;
7. pass injected legality, exposure, and fail-closed checks.

Failure is frozen as a negative result. Passing only makes the model eligible
for a separate version-controlled freeze. It does not itself authorize Phase
2G, because Phase 2I/J informed the formula design.

## Reproduction with visible progress

Run from the repository root in `TrustAero_env`:

```powershell
python -u scripts/develop_pipeline_optimizer.py `
  --config experiments/configs/phase2k_pipeline_optimizer_development.json `
  --progress
```

The terminal reports completed family folds, elapsed seconds, and ETA. The run
writes family observations, every baseline/model prediction, the serialized
formula, a JSON summary, and a Markdown report.
