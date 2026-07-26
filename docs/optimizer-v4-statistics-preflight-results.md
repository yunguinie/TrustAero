# Optimizer V4 January statistics preflight

## Outcome

The label-free preflight passed for all 12 January BTS development families.
It is bound to commit `994018a` and run
`20260721T095310817400Z`.  The run did not execute or time either candidate,
read an Oracle label, fit a model, or access February--December partitions.

Two consecutive extractions produced byte-identical `families.json` files
(SHA-256 `77fd5f78a8b848ed10b029409f82a62356dc724c44ba7cd392b5039d19f3e60d`),
including one run before and one after the implementation commit.  Only the
clean, committed run is retained.

## Extracted cardinalities

All families use 547,271 January source rows and 106,251 fact rows after the
common time, distance, and cancellation filters.  The controlled dimension
subsets produce the following exact pre-ranking Join estimates:

| Target | Selected airports | Join output rows | Achieved rate |
| ---: | ---: | ---: | ---: |
| 0.25 | 51 | 28,129 | 0.264741 |
| 0.70 | 135 | 76,266 | 0.717791 |
| 0.95 | 192 | 100,979 | 0.950853 |

The native logical source-scan payload is 31.049 bytes per row.  Controlled
sensitive widths of 192, 384, 768, and 1,536 bytes are recorded as derived
post-filter payload rather than incorrectly charged to Parquet scanning.

## Meaning of the two candidate records

For every family, early Mask hashes all 106,251 Join-input values, carries a
64-byte digest through Join, and creates a 106,251-row materialized boundary.
Its estimated raw-sensitive Join exposure is zero.

Late Mask has no early boundary and hashes only the estimated Join outputs,
but carries the raw 192--1,536-byte value through Join.  Its estimated
raw-sensitive Join exposure is 106,251 rows.  Common scan, derivation,
dimension-build, output, and sort quantities are preserved rather than
pretended to cancel in a non-additive parallel DuckDB pipeline.

## Scientific boundary

This result proves feature availability and deterministic accounting only.  It
does not prove that V4 predicts runtimes, improves regret, or beats a fixed
plan.  The frozen January timing audit separately contains six stable
early-preferred and five stable late-preferred families; one unstable family
must remain excluded from strong direction claims.

The next authorized step is a development-only operator-profile collection
for the same January families, followed by complete-family cross-validation of
a small number of predeclared V4 structures.  A model may not consume profile
timings at inference, and February--December remain unopened until the full V4
selector, fallback, and gates are frozen.
