# Phase 2J high-match boundary confirmation

Phase 2I found one stable-early family at 150K rows, 256-byte identifiers,
and 100% Join match. Phase 2J tests whether that point belongs to a repeatable
region or is isolated noise. This is still development evidence; it neither
fits an optimizer nor consumes a future Phase 2G holdout.

## Frozen matrix

| Axis | Values |
|---|---|
| Rows | 100K, 150K, 200K |
| Identifier width | 128, 256, 512 bytes |
| Join match rate | 90%, 100% |
| New seeds | 1111, 1222, 1333, 1444, 1555 |

The Cartesian product contains 90 atomic units. Each unit uses two warm-ups,
15 measured repetitions, and three analyzed profiles for both complete
fragments. The same result-equivalence, plan-distinctness, exact-cardinality,
required-operator, memory, and spill checks from Phase 2I remain mandatory.

These values are boundary-development points. Future Phase 2G must use
different scales, widths, match rates, and seeds.

## Predeclared classification

- The practical tie band remains 3%.
- A family receives a stable early/late/tie label only when at least four of
  its five seeds receive that same label.
- A performance reversal is not required; absence must be reported.
- The matrix, 4/5 rule, and gate cannot change after seeing results.

## Optimizer-design gate

Phase 2J permits design of a pipeline-aware optimizer only if all are true:

1. at least two families are stable early;
2. at least one family is stable late;
3. at least two stable-early families are adjacent on the frozen grid while
   equal on the other two axes;
4. no unit spills to DuckDB temporary storage.

Passing this gate does not validate an optimizer and does not authorize Phase
2G. It only establishes enough structured development signal to design the
next model. Failing it means optimizer training remains deferred.

## Commands

Run with visible progress from `TrustAero_env`:

```powershell
python -u scripts/run_mechanism_microbench.py `
  --config experiments/configs/phase2j_fragment_boundary_confirmation.json `
  --progress
```

Resume safely after interruption:

```powershell
python -u scripts/run_mechanism_microbench.py `
  --config experiments/configs/phase2j_fragment_boundary_confirmation.json `
  --resume `
  --progress
```

After completion, run the predeclared analysis:

```powershell
python -u scripts/analyze_phase2i.py <RUN_DIR> `
  --output-dir results/phase2j_fragment_boundary_analysis/<RUN_ID> `
  --tie-threshold 0.03 `
  --required-seed-agreement 0.8 `
  --evaluation-label phase2j_fragment_boundary_confirmation
```
