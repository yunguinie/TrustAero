# Validation semantics

TrustAero separates checks by the information they require:

1. **L1 structural parsing** uses strict Pydantic models to reject malformed
   fields, unknown operator tags, extra fields, and invalid scalar values.
2. **L2 plan semantics** checks operator IDs, input arity, references,
   reachability, and graph acyclicity. These properties cannot be expressed by
   JSON Schema alone.
3. **L3 governance** resolves datasets and snapshots, evaluates policy, and
   inserts deterministic obligation-enforcement operators.

The layers are a security boundary. A plan that fails an earlier layer never
advances to policy evaluation, and uncertain authorization never becomes
permission. `PolicyDecision` records the policy fragment's result, while
`ValidationStatus` records how TrustAero handles the complete request; the two
enums are intentionally not aliases.

## Current limitation

IR v1 does not yet prove full field lineage or relational type compatibility
through every operator. It therefore must not be described as a complete query
type checker. Those checks will be added with explicit operator semantics and
adversarial tests, rather than inferred from JSON shape alone.

Obligations without a defined IR v1 enforcement are rejected. This prevents a
validator response from claiming that an ignored obligation was satisfied.

## Query scope versus disclosed precision

`SpatialFilter.radius_km` and `GeneralizeLocation.precision_km` have different
meanings and must never be substituted for one another:

- `radius_km` is a selection predicate. It determines which records satisfy
  the user's geographic query.
- `precision_km` is an output-governance constraint. It limits how precisely
  the selected records' locations may be disclosed.

For example, applying `GeneralizeLocation(precision_km=5)` after a
`SpatialFilter(radius_km=50)` keeps the 50 km result membership unchanged. It
requires the trusted executor to replace the named precise coordinate fields
with deterministic fixed-grid representations at approximately 5 km
resolution. The original precise coordinates must not survive in hidden or
auxiliary output fields.

IR v1 specifies this logical contract but does not yet implement the physical
coordinate transformation. The future executor must define the CRS, grid
origin, boundary handling, and output field representation before this
operator can be described as physically enforced.
