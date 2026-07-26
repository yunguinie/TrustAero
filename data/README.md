# External experiment data

Large raw and processed datasets are intentionally excluded from Git. This
directory retains only small, reviewable manifests and documentation needed to
reproduce downloads and deterministic transformations.

- `raw/`: immutable files fetched from authoritative sources;
- `processed/`: deterministic slices and derived experiment tables;
- `manifests/`: tracked URLs, hashes, schemas, row counts, and transform rules.
- `tmp/`: E-drive-only conversion scratch space and DuckDB spill.

Do not place data on the C drive. The approved workload portfolio and staged
acquisition gates are documented in `docs/real-data-selection.md`.

To inspect the acquisition registry from an activated `TrustAero_env` terminal:

```powershell
python scripts/download_datasets.py --list
```

To acquire only the approved integration artifacts with visible progress,
resumption, byte validation, and SHA-256 audit records:

```powershell
python scripts/download_datasets.py --stage smoke
```

After acquisition, prepare deterministic E-drive Parquet artifacts and run the
correctness-only real-data query smoke:

```powershell
python scripts/prepare_real_data_smoke.py
python scripts/run_real_data_query_smoke.py
```

These commands validate integration, exact result equivalence, and distinct
DuckDB physical plans. They intentionally do not collect paper performance
timings. The 100K/500K files are deterministic, evenly spaced source-order
samples spanning each fixed January file; final experiments use complete held-
out months.

To exercise the full TrustAero boundary on the prepared 100K slices, including
policy validation, deterministic rewrite, SQL compilation, DuckDB execution,
source lineage, certificate checking, and fail-closed fault injection, run:

```powershell
python scripts/run_real_data_governed_smoke.py
```

The command writes only an auditable semantic manifest to
`data/manifests/processed/real-data-governed-smoke.json`. It deliberately does
not publish latency, speedup, or optimizer claims; those belong to the later
frozen performance protocol over complete data.

The next infrastructure gate runs the canonical governed plan at 100K and
500K with warmups, repeated measurements, physical-plan profiling, intermediate
cardinalities, atomic checkpoints, and an in-terminal ETA:

```powershell
python -u scripts/run_real_data_pilot.py --progress
```

If the process is interrupted after a workload/size unit is checkpointed, use:

```powershell
python -u scripts/run_real_data_pilot.py --progress --resume
```

Outputs are placed under `results/real_data_pilot/<run_id>/`. The frozen label
`real_data_infrastructure_pilot_not_paper_performance_evidence` prevents these
small-slice timings from being accidentally presented as optimizer or final
paper results.

After completion, apply the integrity gates and create a readable report:

```powershell
python scripts/summarize_real_data_pilot.py
```

Before any multi-candidate timing, run the approved-candidate semantic gate:

```powershell
python scripts/run_real_data_candidate_smoke.py
```

It creates three approved physical candidates per workload, verifies exact
result equivalence, observes distinct DuckDB plans, binds each candidate to
lineage and a certificate, and demonstrates that a stricter no-raw-sensitive-
materialization profile rejects the BTS raw boundary before cost comparison.
Every real-data runner also rechecks the current Parquet byte size, row count,
and SHA-256 against the preparation manifest before opening trusted views.

Run the balanced 100K/500K candidate performance pilot with progress and safe
atomic checkpoints:

```powershell
python -u scripts/run_real_data_candidate_pilot.py --progress
python scripts/summarize_real_data_candidate_pilot.py
```

Use `--progress --resume` after an interruption. The report treats differences
within 3% as ties and reports only diagnostic Oracle opportunity; it does not
claim that an online optimizer selected the Oracle plan.

Generate the standard TPC-H SF1 database locally on E drive with a signed
DuckDB core extension, eight visible generation steps, row-count verification,
and an atomic manifest:

```powershell
python -u scripts/prepare_tpch_sf1.py
```

The extension is installed below `data/processed/duckdb_extensions/`, not the
default C-drive user directory. The generated database remains ignored under
`data/processed/tpch/sf1/`; its small manifest is tracked.

The full-month pre-experiment uses the same runner with the frozen full source
bindings:

```powershell
python -u scripts/run_real_data_candidate_pilot.py `
  --config experiments/configs/real_data_full_month_pilot.json --progress
```

Its first run is intentionally diagnostic: integrity passed, but the timing-
stability gate failed because early/late repetitions drifted substantially.
Do not use those latency values as final paper evidence.
