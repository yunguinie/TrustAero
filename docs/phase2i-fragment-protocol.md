# Phase 2I complete Mask-fragment protocol

Phase 2I follows the rejected additive mechanism formula. It does not tune a
new optimizer. It measures the two complete, result-equivalent physical
fragments whose interaction the isolated SHA-256, materialization, and Join
microbenchmarks could not explain.

## Bounded physical fragments

Both candidates produce the same temporary output table with the schema
`(row_id, masked_value, marker)` and sort by `(masked_value, row_id)`.

```text
early Mask
  scan -> sha256(raw identifier) -> MATERIALIZED CTE -> HASH_JOIN -> ORDER_BY

late Mask
  scan -> HASH_JOIN -> sha256(matched identifier) -> ORDER_BY
```

The fragment deliberately uses only the already supported `hash` Mask and an
equality Join that does not consume the masked field. It does not claim Mask
is generally commutative. In the real planner, both candidates still have to
pass semantic validation and raw-exposure constraints before entering the
cost comparison.

Each timed execution creates an identical final DuckDB table. A database-side
content digest checks cardinality, 64-byte hash width, row IDs, dimension
markers, and masked values without transferring every result row to Python.
The runner rejects a unit unless:

- early and late result digests are identical;
- their analyzed physical-plan fingerprints are different;
- both analyzed trees contain `HASH_JOIN` and `ORDER_BY`;
- each `HASH_JOIN` produces exactly the configured matched-row count;
- the physical shape remains stable over all profile repetitions.

Wall-clock timing is kept separate from `EXPLAIN ANALYZE`. Operator profiles
record timings, cardinalities, scanned rows, peak buffer memory, temporary
directory use, and the complete JSON plan. Candidate order rotates
deterministically per round. Every unit is atomically checkpointed and can be
resumed without reusing a partially measured unit.

## Frozen smoke and pilot matrices

The smoke matrix contains four small units and only verifies the protocol.
The pilot deliberately reuses development-axis values already seen in Phase
2H rather than consuming possible Phase 2G holdout values.

| Matrix | Rows | Widths | Match rates | Seeds | Units |
|---|---|---|---|---|---:|
| smoke | 2K | 64, 512 | 25%, 75% | 707 | 4 |
| pilot | 50K, 150K | 256, 512, 1024, 2048 | 10%, 50%, 100% | 707, 808, 909 | 72 |

The pilot uses two warm-ups, 15 measured repetitions, and five analyzed
profiles per candidate. Its matrix and order seed are frozen in
`experiments/configs/phase2i_fragment_pilot.json` before any pilot result is
observed.

## Commands and visible progress

From the repository root in `TrustAero_env`:

```powershell
python -u scripts/run_mechanism_microbench.py `
  --config experiments/configs/phase2i_fragment_smoke.json `
  --progress
```

After interruption, use the same committed code and configuration:

```powershell
python -u scripts/run_mechanism_microbench.py `
  --config experiments/configs/phase2i_fragment_smoke.json `
  --resume `
  --progress
```

The terminal prints completed units, elapsed seconds, and ETA. Resume ETA uses
only units completed in the resumed session, fixing the bias caused by mixing
old completed units with the current session time.

## Scientific boundary and next gate

Smoke results cannot support a performance claim. The 72-unit pilot is still
development evidence, not Phase 2G and not a paper generalization result. A
performance reversal is an observation, not a required outcome; absence of a
reversal must be reported rather than repaired by changing the matrix.

Only after the pilot is complete may we decide whether a pipeline-aware model
is identifiable. Any new model structure and gate must then be committed
before evaluation. The untouched Phase 2G widths, match rates, scales, and
seeds must be frozen only after a development model passes its declared gate.
