# Phase 2I complete-fragment pilot result

Run `20260717T120435635390Z` executed the frozen Phase 2I protocol on commit
`58ced0e`. It completed 72 units, 2,160 timed candidate executions, and 1,440
operator summaries in about 58 minutes. All 72 early/late pairs produced the
same content digest and distinct actual DuckDB plan fingerprints. Every Join
cardinality was exact, every required Join/sort operator was present, and no
profile used DuckDB temporary-directory spill.

This is development evidence. It is not Phase 2G and does not authorize a
paper generalization claim.

## Fixed 3% classification

The committed analyzer groups all three seeds for each `(rows, width, match)`
family and calls a seed early or late only when it beats the other plan by more
than the existing 3% practical-tie band.

| Classification | Units | Scenario families |
|---|---:|---:|
| Early | 6 | 1 stable-early |
| Late | 56 | 17 stable-late |
| Tie | 10 | 1 stable-tie |
| Mixed | — | 5 mixed families |

The only stable-early family is 150K rows, a 256-byte identifier, and 100%
Join match rate. All three new seeds select early Mask outside the tie band:

| Seed | Early ms | Late ms | Early relative difference |
|---:|---:|---:|---:|
| 707 | 782.53 | 825.14 | -5.16% |
| 808 | 749.03 | 811.14 | -7.66% |
| 909 | 795.65 | 848.31 | -6.21% |

Low and medium match rates strongly favor late Mask because it hashes only the
matched rows. Several 100%-match families are ties or mixed across seeds. In
particular, wider payloads do not monotonically expand the early-Mask winning
region in this controlled CTAS-and-sort fragment. That is a useful correction
to any simplistic “wide means early” rule.

## Interpretation

The experiment establishes a real performance reversal under two validated,
result-equivalent physical fragments: stable-late and stable-early families
both exist. It also shows why another threshold or linear formula would be
premature. Only one of 24 families is stably early, while five high-match
families are mixed and one is a stable tie.

The correct next step is a separately frozen boundary confirmation around the
high-match region using new seeds and nearby development points. We should not
fit a pipeline-aware optimizer until that confirmation shows whether the early
region is repeatable and structured rather than a single isolated point.

The complete analysis is reproducible with:

```powershell
python -u scripts/analyze_phase2i.py `
  results/phase2i_fragment_pilot/20260717T120435635390Z `
  --output-dir results/phase2i_fragment_analysis/20260717T120435635390Z
```

File hashes and representative early/late analyzed plans are recorded in
`experiments/frozen/phase2i_fragment_pilot_record.json`.
