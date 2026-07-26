# DuckDB execution semantics for IR v1

This document freezes the executable fragment used by TrustAero experiments.
It is intentionally smaller than the logical IR. An operator outside this
contract fails closed at compilation or physical binding.

IR datetime values denote instants and always include a UTC offset. DuckDB
bindings therefore use `TIMESTAMPTZ`; a naive `TIMESTAMP` is outside the V1
contract because its comparison result depends on the session timezone.

## Join

- Exactly two inputs.
- `inner` and `left` equi-joins only.
- Both keys must exist, have the same logical type, and remain raw.
- Input output names must be disjoint; IR v1 has no implicit aliasing.
- SQL NULL semantics apply: NULL keys do not match.

## Aggregate

- Raw group keys only.
- `COUNT`, `SUM`, `AVG`, `MIN`, and `MAX` only.
- `COUNT` without an input is `COUNT(*)`; other functions require an input.
- The validator freezes numeric/comparable input restrictions and output types.
- Aggregation does not automatically declassify sensitive data.
- `SUM` and `AVG` additionally accept one non-nested product of two raw numeric
  fields. This is the complete arithmetic fragment: constants, division,
  nested arithmetic and SQL functions are not silently admitted.
- Exact `decimal` values use canonical base-10 strings in JSON and Python
  `Decimal` bindings. JSON numeric tokens are rejected for this type because a
  binary float can change an inclusive database boundary such as `0.05`.

## Mask

The methods are independent contracts, not an ordered privacy scale.

- `redact`: replace the value with the VARCHAR token `[REDACTED]`.
- `hash`: lowercase SHA-256 hexadecimal over a raw VARCHAR. Non-string hashing
  is rejected because IR v1 does not define canonical numeric/datetime bytes.
- `null`: replace the value with a SQL NULL cast to the original logical type.

Masked fields remain projectable presentation values. They cannot feed Filter,
Join, Aggregate, SpatialFilter, or TemporalFilter unless a future
operation-specific compatibility rule is added.

Consequently, moving Mask earlier is not an unconditional optimizer rewrite.
Every proposed placement must pass normal schema/capability validation. A Mask
may move before a later Project that only displays the field, but not before a
Join or predicate that still needs its raw semantics.

## GeneralizeLocation

- One complete catalog-declared EPSG:4326 latitude/longitude pair.
- `fixed_grid` only.
- Angular step: `precision_km / 111.045` degrees on both axes.
- Grid anchor: latitude -90 degrees, longitude -180 degrees.
- Each coordinate becomes its deterministic grid-cell center.
- Row membership is preserved. Generalization never changes the radius or
  meaning of a preceding SpatialFilter.

The grid is an approximate geographic disclosure resolution, not a geodesic
distance operator. Alternative projections or grids require a new method name.

## Lineage instrumentation

DuckDB V1 implements source-level evidence only. It records edges from resolved
dataset snapshots to each required output target, binds the evidence to the
logical plan and result digest, and measures capture latency separately.

Record-level lineage is not downgraded or estimated. It remains unsupported and
fails closed until row identities and transformations are carried through Join
and Aggregate.

## Optimizer gate

Candidate generation and cost modeling may use only backend-bound operators
whose `implementation_status` is `executable`. Every candidate must be run
through the normal TrustAero validator after reordering. The cost model must use
measured lineage instrumentation costs rather than assuming lineage is free.
