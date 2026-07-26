# Optimizer V5 real-candidate calibration V1 result

Run `20260721T132242419765Z` completed all four development units and all 360
measured candidate executions.  Every unit produced equivalent results, three
distinct DuckDB plans, complete balanced permutations, positive latency, real
memory observations, and zero spill.  The repository remained clean and no
new data were downloaded.

The run is structurally valid but does not pass the frozen timing-stability
gate.  Absolute half-run drift and paired-ratio half drift pass for all four
units.  The maximum paired-ratio outlier fraction exceeds the 10% limit for
BTS 100K (13.3%), NYC 100K (16.7%), and NYC 500K (16.7%).  Therefore median
Oracle choices from this run are diagnostic and must not become V5 training
labels.

A post-result diagnosis using the repository's existing pollution-safe paired
method finds only five safe blocks per materialized-candidate comparison.  All
carryover confidence intervals are inconclusive.  Some candidate-vs-fused
comparisons look decisive, but five blocks are below the already established
ten-block formal minimum and cannot rescue this run.

The authorized correction is predeclared rather than post-hoc: retain both
materialized candidates as possible carryover sources, use 60 complete
permutation blocks so every candidate-vs-fused comparison has ten pollution-
safe blocks, and authorize conclusions only with a 95% permutation-stratified
paired bootstrap confidence interval.  The 3% practical tie band remains
unchanged.  This run is never overwritten or relabelled as passed inference.

