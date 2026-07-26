# TPC-H support and governance boundary

TrustAero reports the complete 22-query TPC-H denominator. A query is not
called supported merely because DuckDB can execute its raw SQL.

The audit separately checks whether every official query executes on the
frozen SF1 artifact and whether the trusted IR can express its semantics without
a raw-SQL escape hatch. The reviewed IR v1 fragment supports Q1 and Q6. The
other 20 queries remain visible with explicit blockers such as subqueries,
CASE, LIMIT, LIKE, derived columns and nested arithmetic.

## Why Q1 is admitted

Q1 adds grouping over two raw string keys, SUM/AVG/COUNT, two exact fixed-point
formulas and a two-key sort. The arithmetic is not general SQL: one formula may
multiply only two or three factors, and each factor is either a raw numeric
field or exactly `decimal constant +/- raw numeric field`. This covers
`extendedprice * (1 - discount) * (1 + tax)` while still rejecting division,
functions and nested expression trees. Sort accepts only validated raw fields;
masked presentation values cannot silently regain semantic use.

The Q1 smoke compares three governed candidates with DuckDB's ordered official
schema and result, then checks source-lineage evidence at the current PARTIAL
certificate boundary. PARTIAL is intentional: structure, snapshots, events and
source lineage are checked, while unavailable database-internal facts are not
treated as verified. Like Q6 semantic smoke, this contains no latency and is
not performance evidence.

## Why Q6 is admitted

Q6 contains a scan, a half-open date interval, numeric predicates and one
`SUM(l_extendedprice * l_discount)`. TrustAero lowers `BETWEEN` to inclusive
comparisons and supports exactly one non-nested product of two raw numeric
fields as a SUM/AVG input. Constants, division, nesting and arbitrary functions
remain outside this rule. Masked or nonnumeric inputs fail closed.

The semantic smoke runs fused execution plus materialization after the temporal
filter and after all predicates. Each candidate must exactly equal the official
fixed-point result, retain a distinct DuckDB physical plan, capture source
lineage and pass certificate verification. It records no performance timing.

```powershell
python -u scripts/audit_tpch_sf1.py
python -u scripts/run_tpch_q1_smoke.py
python -u scripts/run_tpch_q6_smoke.py
```

Both commands require a clean commit and use the database and extension stored
inside the project on the E drive.

## Frozen Q1 timing protocol

Q1 uses the corrected Q6 measurement discipline without changing its gates:
UTC, one hot DuckDB connection, all six candidate orders, six warm-up blocks,
30 paired measured blocks and five executions per candidate position. The run
therefore performs 450 timed queries, while each block median remains one
paired observation. The runner validates the official 10-column ordered result
before starting its stopwatch and prints live percentage, elapsed time and ETA.

```powershell
python -u scripts/run_tpch_q1_formal.py --progress
```

This is standard-benchmark method evidence, not held-out optimizer evidence.
The post-hoc Oracle is reported only as a diagnostic upper bound.

### Q1 accepted result

The frozen run completed 450 timed queries and passed every integrity and
stability gate. All candidates reproduced the official 10-column, four-row
result, retained three distinct DuckDB plans and avoided disk spill.

Fused had a 740.136 ms median. Materializing after aggregation had a 754.042 ms
median (1.016× fused), so both belong to the predeclared 3% diagnostic Oracle
set. Materializing after the broad date filter had a 1639.878 ms median
(2.211× fused). The filter retains 98.59% of 6,001,215 rows, explaining why
materializing that intermediate is expensive. Materialized routes also reached
about 997 MiB peak buffer memory versus about 121 MiB for fused execution.

This proves a stable cost consequence of governance-compatible boundary
placement for Q1. It does not prove that an optimizer can predict the choice.

## Frozen Q6 timing protocol

After the semantic record was committed, a separate protocol froze the timing
rules before observing performance: three candidates, all six candidate-order
permutations, six warm-up blocks, and thirty measured blocks. Thus each
candidate receives thirty paired observations and the run contains ninety
formal timed executions.

The main protocol is a hot-cache repeated-query experiment on one DuckDB
connection. It fixes threads, memory and the E-drive temporary directory, and
checks absolute drift, paired-ratio drift, paired-ratio outliers, order/position
balance, plan uniqueness, result identity, lineage certificates, memory and
spill. A difference below three percent is treated as a tie.

```powershell
python -u scripts/run_tpch_q6_formal.py --progress
```

Passing this gate authorizes SF1 Q6 as method-level standard-benchmark evidence.
It does not evaluate Optimizer V1/V2, and its diagnostic Oracle is not a
deployable baseline because it is computed after running every candidate.

### First timing outcome

The first 90-execution run completed but failed the predeclared paired-ratio
outlier gate. All semantic, plan-uniqueness, certificate, order-balance,
position-balance, resource, absolute-drift and ratio-drift checks passed. The
failure was retained as a frozen negative result instead of being overwritten.

Individual fused observations ranged from about 155 to 313 ms even though the
first/second-half median drift was only 1.6%. No material position effect was
found. This means one very fast or slow fused observation can distort two
single-query values divided inside a block. The observed candidate medians are
diagnostic only and are not authorized as paper performance evidence.

The next protocol revision must be frozen independently. Its justified change
is to execute a small fixed batch per candidate position and compare the batch
medians, while preserving all six outer candidate-order permutations. It may
not relax the failed threshold after seeing these results.

That V2 protocol is now frozen with five timed executions per candidate
position. It retains the same 30 outer paired blocks, all six permutations,
the 3% tie band and every V1 stability threshold. Therefore it produces 450
timed queries, but the inferential unit remains 30 independently ordered block
medians per candidate. The change addresses noisy individual stop-watch values;
it does not change the definition of a passing result.

```powershell
python -u scripts/run_tpch_q6_formal.py `
  --config experiments/configs/tpch_q6_batched_v2.json --progress
```

## Execution-timezone correction

A post-run cardinality cross-check found that both formal timing runs processed
114,189 rows while official Q6 processes 114,160. The first diagnosis blamed
binary FLOAT parameters, but a controlled reproduction disproved it: with
DuckDB set to UTC, both exact decimal and float parameters select 114,160 rows
and produce the official 123,141,078.2283 result.

The confirmed cause was a missing execution-timezone control. The semantic
smoke explicitly set UTC and remains valid. The timing runner inherited this
machine's `Asia/Shanghai` timezone; converting DATE columns to TIMESTAMPTZ then
shifted their boundary relative to UTC parameters. Both timing runs are invalid
for performance interpretation and also independently failed stability gates.

Future timing must declare UTC in the protocol, record it in the environment,
and set it before creating the trusted view. Separately, TrustAero now has an
exact DECIMAL logical type whose JSON literals are canonical base-10 strings.
This is defense-in-depth rather than a misreported root-cause fix.

The corrected clean-source semantic run now passes: all three candidates return
the exact fixed-point value `123141078.2283`, share the same result digest,
retain three distinct DuckDB plan fingerprints, and produce checked source
lineage certificates. A fresh complete audit also executed all 22 official
queries and retained Q6 as the only exact IR-v1 query. These are semantic
results only; neither invalid timing run has been rehabilitated.

V3 timing is separately frozen against this corrected record. It keeps the V2
median-of-five design and every original threshold, adds explicit UTC to the
configuration and environment, and requires each candidate's preflight scalar
to equal the official Q6 fixed-point value before the first stopwatch sample.

### V3 accepted result

The corrected V3 run completed 450 timed queries and passed every integrity and
stability gate. The governed preflight processed the same 114,160 rows as
official Q6 and returned the exact `123141078.2283` fixed-point value for all
three candidates. No candidate spilled to disk.

Fused had a 152.245 ms median. Materializing after all predicates had a 168.395
ms median (1.169× fused), while materializing after the time filter had a
683.658 ms median (4.672× fused). The paired three-percent diagnostic Oracle set
contains only fused.

This result demonstrates a stable case where preserving the DuckDB pipeline is
important: materializing 909,455 temporally selected rows is much more expensive
than fusing the remaining predicates and aggregate. It is one standard query,
not optimizer accuracy evidence; the Oracle was determined after timing all
candidates.
