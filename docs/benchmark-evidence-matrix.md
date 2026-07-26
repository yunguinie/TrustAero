# Cross-workload candidate evidence matrix

TrustAero normalizes four accepted experiment records into one descriptive
matrix: TPC-H Q1, TPC-H Q6, BTS January 2024 and NYC TLC January 2024. Every
source summary and acceptance record is bound by SHA-256 before aggregation.

The matrix uses ratios of source-reported candidate medians so unrelated raw
latencies are not compared directly. It also freezes and rechecks each source's
formally accepted 3% Oracle set. This is a descriptive cross-workload summary;
it does not replace the paired statistics inside each source experiment.

## Current result

- Q1: fused and aggregation-boundary materialization are tied within 3%.
- Q6: fused is the only candidate in the Oracle set.
- BTS: materialization after Mask strictly beats fused; fixed fused has 14.69%
  pooled-median regret.
- NYC TLC: fused is the only candidate in the Oracle set.

Thus an alternative legal boundary enters the tie band in two of four
workloads, while one real workload exhibits a strict reference-plan reversal.
The geometric post-hoc Oracle opportunity over fixed fused is about 1.035x.

## Scientific boundary

Q1 and Q6 are standard-benchmark method evidence. BTS and NYC TLC use already
inspected January development partitions. The matrix is paper-performance
evidence about candidate costs, but it is not held-out optimizer evidence.

The previously rejected Mask Optimizer V2 remains rejected. This aggregation
does not authorize Phase 2G and does not claim that TrustAero can yet predict
the best boundary.

Rebuild the content-addressed matrix from the repository root:

```powershell
python -u scripts/build_benchmark_evidence.py
```

The next model-development stage must first define a shared boundary-feature
contract covering cardinality, selectivity, row width, boundary cardinality,
operator family, Mask work and Join work. It must use grouped development
partitions and keep future holdout months and query templates unseen.
