# Frozen dataset protocol for TrustAero experiments

Status: **approved for staged acquisition on 2026-07-18**.

This protocol freezes five complementary workload roles. They are not treated
as five interchangeable performance datasets: each one supports a different
claim, and no workload may be removed merely because its results are
unfavourable.

## Frozen workload portfolio

| Workload ID | Role | Fixed scope | Claim supported |
| --- | --- | --- | --- |
| `controlled_synthetic` | controlled workload | deterministic generators already in the repository | causal effects of rows, width, match rate, skew, policy strength, and legal Mask placement |
| `bts_on_time_2024` | primary real domain | BTS Reporting Carrier On-Time Performance, January--December 2024 | real aviation time predicates and natural airport/carrier joins |
| `nyc_tlc_yellow_2024` | independent real domain | NYC TLC Yellow Taxi, January--December 2024, plus Taxi Zone lookup | cross-domain generality, real skew, zone joins, and large Parquet scans |
| `tpch_sf1_sf10` | standard DB benchmark | TPC-H SF1 and SF10 | reproducible supported multi-table Join and Aggregate queries |
| `multisource_case_study` | end-to-end case | earthquake, well, airport, and city sources after source audit | policy, rewrite, snapshot, lineage, and certificate integration |

The machine-readable registry is
[`data/manifests/dataset-registry.json`](../data/manifests/dataset-registry.json).
Large source files and derived tables are never committed to Git.

## Authoritative sources

### BTS Airline On-Time Performance

- [BTS field selector and dictionary](https://transtats.bts.gov/DL_SelectFields.aspx?QO_fu146_anzr=&gnoyr_VQ=FGJ)
- [BTS official pre-zipped file directory](https://transtats.bts.gov/PREZIP/)

The frozen table is **Reporting Carrier On-Time Performance (1987-present)**,
restricted to calendar year 2024. It must not be silently substituted with the
similarly named Marketing Carrier table. January is used for integration
smoke testing before the remaining months are acquired.

Natural relational structure includes flight facts joined to airport and
carrier lookup tables. Useful real columns include flight date, reporting
carrier, tail number, origin/destination airport identifiers, scheduled and
actual times, delay groups, cancellation/diversion indicators, and distance.

### NYC TLC Yellow Taxi

- [NYC TLC trip-record portal](https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page)
- [Yellow Taxi data dictionary](https://www.nyc.gov/assets/tlc/downloads/pdf/data_dictionary_trip_records_yellow.pdf)
- [Taxi Zone Lookup](https://d37ci6vzurychx.cloudfront.net/misc/taxi_zone_lookup.csv)

The frozen scope is all twelve 2024 monthly Yellow Taxi Parquet files. January
is the integration month. The fact-to-dimension Join maps `PULocationID` or
`DOLocationID` to the zone lookup's `LocationID`.

### TPC-H

- [DuckDB TPC-H extension](https://duckdb.org/docs/current/core_extensions/tpch)

TPC-H data is generated deterministically through DuckDB `dbgen`, first at
SF1 and only then at SF10. Query inclusion is determined by the frozen
TrustAero IR/operator fragment, never by whether a query makes TrustAero look
fast. Every supported query is reported; unsupported queries receive an
operator-level explanation.

TPC-H SF1 was generated on 2026-07-19 from clean source commit `9f0fe4a` using
DuckDB/extension v1.5.4 in eight deterministic partitions. The resulting E-drive
database contains 8,661,245 rows across eight tables, exposes all 22 standard
queries, occupies 278,933,504 bytes (266.0 MiB), and has SHA-256
`64a709fa8f995aeae23ffe6305a47e77c97113c823974a3f6a6933b8d05a0b22`.
The tracked manifest is `data/manifests/processed/tpch-sf1.json`; the database
and signed extension remain below ignored `data/processed/` paths.

### Controlled and end-to-end workloads

The controlled synthetic generator remains the source of experiments that
need exact selectivity, payload width, match rate, correlation, or skew.

The earthquake/well/airport/city collection is one composed case study, not
four independent performance benchmarks. Its registered downloads, raw
checksums, deterministic transformations, prepared-table checksums, executable
query, and controlled governance augmentation have passed the end-to-end
semantic smoke. The registry therefore records
`end_to_end_semantic_validated`. This status does not make the case a
performance benchmark and does not imply that the public sources supplied the
synthetic sensitivity policy.

The source selection is frozen in
`experiments/frozen/multisource_case_study_source_protocol_v1_20260725.json`.
It uses a common New York State spatial scope and four authoritative
publishers: USGS earthquakes, NYSDEC regulated wells, the FAA NASR airport
subscription, and the U.S. Census Bureau Places Gazetteer. Raw files are
downloaded only through the registered, resumable acquisition command and are
stored below `data/raw/multisource/`; byte counts and SHA-256 digests are
recorded below `data/manifests/downloads/`.

The deterministic preparation manifest is
`data/manifests/processed/multisource-case-v1.json`, and the frozen executable
query contract is
`experiments/frozen/multisource_case_study_query_protocol_v1_20260725.json`.

## Original versus generated governance attributes

Public transportation records do not carry the purpose policies needed by
TrustAero. The evaluation therefore keeps two layers explicit:

1. **Observed layer:** real rows, timestamps, locations, Join relationships,
   match rates, correlations, and skew come from the official dataset.
2. **Controlled governance layer:** owner, sensitivity, permitted purpose,
   original-value materialization rules, and optional 128--1024 byte payloads
   are generated deterministically.

The paper and manifests must never imply that generated policies or payloads
were present in the original public source.

## Storage and path policy

All dataset activity is rooted below:

```text
<project-root>\data\
|-- raw\              # immutable official downloads; ignored by Git
|-- processed\        # deterministic Parquet/slices; ignored by Git
|-- manifests\        # tracked registry and small audit records
`-- tmp\              # DuckDB spill and short-lived conversion files
```

The downloader rejects paths escaping this data root. Partial downloads use a
`.part` suffix beside the intended E-drive file, support HTTP Range resumption,
and are atomically promoted only after verification.

Target free space is **50 GB**; 40 GB is the minimum working budget. Raw and
processed data should normally occupy less than 10 GB, leaving the remainder
for controlled wide-payload tables, profiles, and DuckDB spill.

## Reproducibility record

For every external artifact, retain:

- official URL and fixed period;
- retrieval time in UTC;
- exact byte size and SHA-256;
- raw and processed row counts and schemas;
- transformation code version and parameters;
- development/integration versus final-evaluation membership;
- source limitations and usage terms.

Raw data is immutable. A changed upstream file is a new snapshot and must not
overwrite the previous manifest silently.

## Staged execution gates

1. List the tracked artifacts and review their destinations.
2. Download only BTS January 2024, NYC Yellow January 2024, and the zone lookup.
3. Verify byte counts and record SHA-256 before parsing.
4. Inspect schemas and exact row counts; convert BTS selected columns to
   partitioned Parquet without retaining expanded CSV.
5. Run deterministic 100K and 500K semantic/execution smoke tests.
6. Confirm equal result digests and genuinely different DuckDB physical plans.
7. Freeze real-data query templates and final month partitions.
8. Acquire the remaining months, generate TPC-H SF1, then scale to SF10.
9. Run final held-out real-data measurements only after Optimizer V2 and its
   uncertainty thresholds are frozen.

The registry now exposes the remaining February--December BTS and NYC files as
the `main_2024` acquisition stage. The BTS byte counts are frozen from the
official PREZIP directory. NYC artifacts are accepted only after the downloader
records their server-reported size and SHA-256; their first verified retrieval
therefore establishes the immutable local snapshot. Acquisition itself does
not authorize performance claims or reveal optimizer labels.

From an activated `TrustAero_env` terminal at the repository root, acquisition
and preparation are intentionally separate commands:

```powershell
python -u scripts/download_datasets.py --stage main_2024
python -u scripts/prepare_real_data_2024.py --months 2-12
```

Both commands print progress. Downloads preserve resumable `.part` files, while
preparation publishes one hash-bound manifest per completed month and verifies
those manifests before skipping work after an interruption. Neither command
runs a performance experiment. The native BTS/NYC distributions and Join
relationships are prepared first; any controlled sensitive payload added later
must be labelled as generated governance data rather than an original field.

Integration months validate engineering and do not count as independent final
evidence. Final partitions are split by complete month or query family, never
by randomly mixing rows from the same scenario across development and test.

The initial query-family design is now frozen in
`experiments/configs/real_data_query_families_v1.json`.  This is a design freeze,
not a performance authorization: four templates are currently
`semantic_ready` after the BTS natural airport/carrier multi-Join and native
Tail_Number Mask/Join placement passed their semantic smokes. All templates
remain `performance_eligible=false` until a clean, separately hashed
measurement specification is created.

TPC-H SF1 is now generated and hash-verified on the E drive. All 22 official
queries execute on the artifact, while the exact TrustAero IR v1 support audit
currently admits only Q6 and reports blockers for the other 21. Governed Q6 has
three result-equivalent, physically distinct candidates with source-lineage and
certificate checks. This is semantic readiness only; TPC-H timing remains
unauthorized until its measurement and optimizer-comparison protocol is frozen.
