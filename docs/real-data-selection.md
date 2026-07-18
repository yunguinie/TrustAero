# Proposed real-data track: NYC TLC Yellow Taxi

Status: **proposed, not downloaded**. No external data may be fetched until the
dataset choice is confirmed by the project owner.

## Recommended fixed dataset

Use the New York City Taxi and Limousine Commission (NYC TLC) Yellow Taxi Trip
Records for **January 2024**, together with the Taxi Zone Lookup Table.

Official sources:

- [NYC TLC trip-record portal](https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page)
- [January 2024 Yellow Taxi Parquet](https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2024-01.parquet)
- [Taxi Zone Lookup CSV](https://d37ci6vzurychx.cloudfront.net/misc/taxi_zone_lookup.csv)
- [Yellow Taxi data dictionary](https://www.nyc.gov/assets/tlc/downloads/pdf/data_dictionary_trip_records_yellow.pdf)

The fixed historical month avoids silently following a changing "latest"
dataset. The raw Parquet is expected to contain roughly three million trips and
be on the order of tens of megabytes. Exact byte size, row count, schema, and
SHA-256 must be measured and recorded after an approved download.

## What the data describes

Each trip record contains real pickup/drop-off timestamps, pickup/drop-off taxi
zone IDs, distance, passenger count, rate and payment categories, and itemized
fare fields. The zone lookup maps `LocationID` to borough, named taxi zone, and
service-zone attributes.

This gives a natural database shape:

- fact table: individual time-stamped taxi trips;
- dimension table: taxi-zone lookup entries keyed by `LocationID`;
- natural Join: `PULocationID` or `DOLocationID` to `LocationID`;
- temporal predicates: pickup/drop-off time windows;
- spatial predicates: zone, borough, or service-zone membership;
- aggregation: trip counts, distances, or fare summaries by time and zone.

## Recognition and suitability

The source is an official New York City agency rather than a repackaged Kaggle
copy. DuckDB includes an NYC Taxi benchmark in its own benchmark suite and
describes the dataset as influential across database benchmarks, machine
learning, and visualization. Peer-reviewed geospatial database comparisons
have also used NYC taxi records with taxi-zone and census-block joins.

This makes the dataset recognizable to database reviewers and directly useful
for verifying real selection distributions, Join cardinalities, skew, Parquet
scans, and physical-plan changes.

## Usage terms and limitations

NYC Open Data states that its public data has no use restrictions, while the
NYC terms disclaim completeness, accuracy, and fitness for a particular use.
The project must retain source attribution, the fixed version, checksums, and a
description of every transformation. This is public-use data, not an
OSI-licensed software dependency, so it must not be copied into the Git
repository.

The data does not contain passenger identity, and the TLC cautions that vendor
submissions may contain inaccuracies. Therefore it would be misleading to call
an existing column private personal data. For controlled Mask experiments, we
will add a deterministic synthetic governance payload while preserving the
real timestamps, zones, match rates, correlations, and skew. The paper must
clearly distinguish original columns from the added governance field.

## Planned project locations

All large files remain on the E drive:

```text
E:\ProjectAll\codex\PVLDB\data\
├── raw\nyc_tlc\2024-01\
│   ├── yellow_tripdata_2024-01.parquet
│   └── taxi_zone_lookup.csv
├── processed\nyc_tlc\2024-01\
│   ├── yellow_taxi_100k.parquet
│   ├── yellow_taxi_500k.parquet
│   └── yellow_taxi_full.parquet
└── manifests\
    └── nyc_tlc_2024-01.json
```

`raw/` and `processed/` are ignored by Git. The small manifest is tracked and
records official URLs, retrieval time, SHA-256, byte size, row count, schema,
slice rules, and transformation version.

## Approval and execution sequence

After explicit approval of this choice:

1. add a checkpointed downloader that shows byte progress and never writes to C;
2. download only the January 2024 Parquet and zone CSV into `data/raw/`;
3. compute checksums and inspect the schema before transforming anything;
4. freeze deterministic 100K and 500K slices plus a full-data reference;
5. run a 100K semantic smoke before any larger performance experiment;
6. predeclare the real-data experiment matrix before collecting timings.

The first real-data smoke validates legal candidates, equal query results,
actual DuckDB plan differences, and observed exposure. It is not yet a paper
performance claim and does not consume the Phase 2G synthetic holdout.
