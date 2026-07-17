# Phase 2L operator attribution result

Phase 2L completed its predeclared paired attribution on the frozen Phase 2I/J
profiles. No individual operator role passes all four interaction-hypothesis
checks. This is a negative attribution result; Phase 2G remains unauthorized.

## Validated scope

- 162 early/late seed pairs;
- 40 unique physical workload families;
- five stable-early, twenty stable-late, one stable-tie, and fourteen mixed
  families under the frozen 3% and 80%-agreement rules;
- all non-wrapper physical operators mapped to eight fixed semantic roles.

## Main result

| Role | Sign agreement | Spearman | Dominant families | Stable-early delta | Stable-late delta |
|---|---:|---:|---:|---:|---:|
| Hash projection | 71.9% | 0.685 | 100.0% | +1.6% | +57.0% |
| Event scan | 64.8% | 0.586 | 0.0% | -0.2% | +43.0% |
| Sort | 66.4% | 0.353 | 12.5% | +1.2% | +15.8% |
| Hash Join | 66.4% | 0.212 | 0.0% | +75.9% | +78.3% |
| Materialization | 66.4% | 0.000 | 17.5% | +100% | +100% |

The hash projection is the strongest single association: it agrees with the
decisive end-to-end direction for 71.9% of replicates, has Spearman 0.685, and
produces the largest absolute profiled timing difference in at least one
replicate of every family. However, its median early-plan time remains 1.6%
higher in stable-early families. It therefore fails the required stable-region
direction reversal and cannot independently explain why the complete early
fragment wins.

The event scan is the only role whose regional median changes sign, but it
misses the frozen 65% sign threshold by 0.16 percentage points and is never the
largest absolute timing difference. Other roles fail association, dominance,
or reversal checks. No role is eligible to seed a single-role interaction
model.

## Interpretation

The evidence is consistent with a cross-operator effect such as pipeline
fusion, parallel CPU scheduling, expression placement relative to sorting, or
a materialization boundary changing downstream execution. It is not evidence
that any one of those mechanisms is already proven. Because operator profiles
are separate from timed executions and can accumulate parallel CPU time, their
medians are not an additive wall-clock decomposition.

The next defensible step is a small factorial ablation of complete,
result-equivalent SQL fragments that isolates boundary and fusion choices while
verifying that DuckDB produces genuinely different physical plans. Retuning
the failed Phase 2K formula or lowering Phase 2L thresholds after inspection is
not allowed.
