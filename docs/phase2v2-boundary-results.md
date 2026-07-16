# Optimizer V2 boundary-calibration results

Run `results/phase2v2_boundary_calibration/20260716T012853745457Z`
completed on clean commit `6e17b28` in 36 minutes 25 seconds. It contains 32
atomic units and 448 measured executions. Both legal candidates returned
equivalent results in every unit, no temporary-directory spill occurred, and
profiled peak buffer memory was 3,923,054,592 bytes.

This is two-seed development data. None of the following results qualifies for
the five-seed stable-reversal label or an independent paper claim.

## Observed boundaries

Positive values mean early Mask is faster than late/fused under the paired
seed-median comparison.

| Width | Match | Rows | Early-Mask speedup | Paired seed interval |
|---:|---:|---:|---:|---:|
| 256 | 100% | 200K | -8.50% | -56.55% to 39.55% |
| 256 | 100% | 400K | -26.55% | -37.34% to -15.76% |
| 512 | 100% | 150K | 6.75% | 5.93% to 7.57% |
| 512 | 100% | 350K | 21.33% | 21.03% to 21.62% |
| 768 | 100% | 150K | 15.57% | 15.32% to 15.82% |
| 768 | 100% | 350K | 18.12% | 13.42% to 22.83% |
| 1024 | 25% | 250K | -41.44% | -41.99% to -40.90% |
| 1024 | 25% | 450K | -16.34% | -19.33% to -13.35% |
| 1024 | 75% | 250K | 9.00% | 8.42% to 9.58% |
| 1024 | 75% | 450K | 14.99% | 11.57% to 18.41% |
| 2048 | 10% | 350K | -54.04% | -56.64% to -51.44% |
| 2048 | 10% | 450K | -40.14% | -44.55% to -35.73% |
| 2048 | 25% | 350K | -11.40% | -14.03% to -8.76% |
| 2048 | 25% | 450K | 5.95% | -3.81% to 15.71% |
| 2048 | 75% | 250K | 20.20% | 12.21% to 28.18% |
| 2048 | 75% | 450K | 49.51% | 41.86% to 57.16% |

The matrix shows three interacting boundaries rather than one byte-work
threshold: a width transition between 256 and 512 bytes at high match, a match
transition between 25% and 75%, and a scale transition for very wide fields at
intermediate match.

## Frozen V1 versus current linear V2

Phase 2E, Phase 2F, and this run produce 30 paired-seed workload observations.
The primary comparison holds out an entire scenario family.

| Model | Within 3% | Mean regret | P95 regret | Maximum regret | Monotonic violations |
|---|---:|---:|---:|---:|---:|
| Frozen V1 | 70.0% | 3.35% | 18.03% | 21.33% | not applicable |
| Linear V2 | 70.0% | 4.41% | 28.43% | 37.64% | 0 / 270 |

Linear V2 is rejected: it does not improve selection rate and materially
worsens tail regret. Passing the match-rate monotonicity audit is necessary but
not sufficient; the learned boundary is still in the wrong location.

## Decision

Do not freeze Phase 2G yet. The next development candidate must represent the
observed piecewise width, match, and scale interactions and optimize regret,
not only squared latency-ratio error. It must be compared under the same
scenario-family split and retain legality, exposure, and monotonicity checks.

Any piecewise structure or confidence fallback designed after this run is
development-time tuning. Its first generalization claim requires a new Phase
2G matrix with unseen widths, match rates, scales, and seeds.
