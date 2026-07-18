# Phase 2M complete-pipeline ablation smoke

Phase 2K rejected a global additive pipeline cost formula, and Phase 2L found
that no individual profiled operator explains the early/late reversal. Phase
2M therefore changes one complete SQL boundary at a time while preserving the
same governed result. It is a bounded mechanism experiment, not another search
for workload reversal points.

## Frozen SQL variants

All variants return `(row_id, sha256(sensitive_value), marker)` with the same
ordered output and equality Join:

```text
late_fused
  Join -> Hash -> Sort

late_join_materialized
  Join -> materialize raw matched rows -> Hash -> Sort

late_hash_materialized
  Join -> Hash -> materialize masked rows -> Sort

early_hash_materialized
  Hash all input -> materialize masked rows -> Join -> Sort
```

These fragments do not claim that Mask is generally movable. They instantiate
only the already supported hash-Mask fragment where the Join key is separate
from the masked sensitive field. In a real plan, TrustAero's legality and
raw-exposure checks still run before candidate generation.

## Actual-plan checks

The runner parses DuckDB's physical JSON tree and fails closed unless:

- every variant contains exactly one `HASH_JOIN` and one `ORDER_BY`;
- the SHA-256 output projection exists;
- fused late has no CTE boundary;
- Join-materialized has Join in the CTE producer and Hash in the consumer;
- Hash-materialized has Join and Hash in the producer and Sort in the consumer;
- early materialized has Hash in the producer and Join/Sort in the consumer;
- all four fingerprints are distinct and stable across profile repetitions;
- the Join cardinality equals the configured exact match count.

The database-side result digest covers output count, hash width, row IDs,
markers, and masked values. All variants must share one digest. Any temporary
directory spill rejects the smoke authorization gate.

## Frozen smoke scenarios

The three scenarios reuse existing Phase 2L regions with a new development
seed `1666`:

| Region | Rows | Width | Match |
|---|---:|---:|---:|
| Stable early | 200K | 128 B | 100% |
| Stable late | 100K | 512 B | 90% |
| Mixed | 100K | 128 B | 90% |

Each scenario uses one warm-up, three timed repetitions, and one analyzed
profile per variant. The few smoke timings cannot support a performance claim.
They only decide whether the four-way protocol is technically valid enough for
a separately frozen compact matrix.

## Command and visible progress

From the repository root in `TrustAero_env`:

```powershell
python -u scripts/run_pipeline_ablation.py `
  --config experiments/configs/phase2m_pipeline_ablation_smoke.json `
  --progress
```

After interruption, safely resume the same committed protocol:

```powershell
python -u scripts/run_pipeline_ablation.py `
  --config experiments/configs/phase2m_pipeline_ablation_smoke.json `
  --resume `
  --progress
```

Phase 2G remains untouched and unauthorized regardless of the smoke result.
