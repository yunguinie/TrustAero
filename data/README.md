# External experiment data

Raw datasets and generated databases are not stored in Git. The repository
contains their acquisition and preparation manifests so that each timed input
can be identified by source, snapshot, byte size, row count, and SHA-256 digest.

| Path | Contents |
|---|---|
| `manifests/dataset-registry.json` | Public source URLs and dataset stages |
| `manifests/downloads/` | Download metadata for fixed source snapshots |
| `manifests/processed/` | Row counts, schemas, hashes, and transformations |
| `raw/` | Downloaded source files (ignored) |
| `processed/` | Prepared Parquet and DuckDB inputs (ignored) |
| `tmp/` | Temporary conversion and spill files (ignored) |

The evaluated public sources are the U.S. Bureau of Transportation Statistics
On-Time Performance data, NYC TLC trip records, USGS earthquake data, FAA
airport data, NYSDEC oil and gas records, U.S. Census city data, and generated
TPC-H databases. Provider terms continue to apply to downloaded data.

List the registry and available stages:

```bash
python scripts/download_datasets.py --list
```

Prepare the 2024 BTS and NYC TLC snapshots used by the primary real-workload
evaluation:

```bash
python scripts/download_datasets.py --stage main_2024
python scripts/prepare_real_data_2024.py --months 1-12
```

Prepare the BTS 2025 temporal holdout:

```bash
python scripts/download_datasets.py --stage bts_2025_temporal_holdout_v1
python scripts/prepare_bts_2025_temporal_holdout.py --months 1-12
```

Generate TPC-H locally:

```bash
python scripts/prepare_tpch.py --scale-factor 1 --progress
python scripts/prepare_tpch.py --scale-factor 10 --progress
```

Preparation is deterministic and verifies the resulting artifacts against the
tracked manifests before experiment runners open them. See
[`artifact/README.md`](../artifact/README.md) for the paper-to-command map.
