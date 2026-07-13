# Validation semantics

TrustAero separates checks by the information they require:

1. **L1 structural parsing** uses strict Pydantic models to reject malformed
   fields, unknown operator tags, extra fields, and invalid scalar values.
2. **L2 plan semantics** checks operator IDs, input arity, references,
   reachability, graph acyclicity, and relation-schema transfer rules. These
   properties cannot be expressed by JSON Schema alone.
3. **L3 governance** resolves datasets and snapshots, evaluates policy, and
   inserts deterministic obligation-enforcement operators.

The layers are a security boundary. A plan that fails an earlier layer never
advances to policy evaluation, and uncertain authorization never becomes
permission. `PolicyDecision` records the policy fragment's result, while
`ValidationStatus` records how TrustAero handles the complete request; the two
enums are intentionally not aliases.

## Relation-schema propagation

The checker evaluates the plan in topological order. `ScanSource` obtains its
field types and spatial/temporal roles from the Catalog; later operators derive
their output from already checked input schemas. A `Project` therefore removes
both fields and any relation capability that depended on those fields. If one
coordinate of a Catalog-declared pair is removed, a later `SpatialFilter`
fails closed rather than guessing a replacement from field names.

| Operator | Input requirement | Output schema | Governance effect |
|---|---|---|---|
| `ScanSource` | registered dataset and version | Catalog schema | binds source metadata |
| `Project` | every requested field exists | selected fields in request order | may remove sensitive/capability fields |
| `SpatialFilter` | complete spatial pair with matching CRS | unchanged | none |
| `TemporalFilter` | temporal `DATETIME` field and `start < end` | unchanged | none |
| `Filter` | structured, well-typed boolean predicate | unchanged | none |
| `Join` | two existing, equal-type keys; unambiguous names | concatenated inputs | none |
| `SpatialJoin` | both inputs spatial with a shared CRS | concatenated inputs | none |
| `Aggregate` | existing group/input fields and supported function types | group fields followed by named aggregate results | aggregation is not automatic declassification |
| `GeneralizeLocation` | complete spatial pair | fields with coarser precision metadata | limits disclosed precision |
| `LineageCapture` | one valid relation | unchanged | requests lineage capture |

## Structured expression fragment

`Filter.expression` is no longer an opaque SQL-like string. The accepted IR v1
fragment compares a bound field with a typed literal and combines at least two
comparisons using one flat `and` or `or` group. Equality supports identical
logical types, numeric comparisons allow integer/float compatibility, and
ordering supports numeric or timezone-qualified datetime values. String
ordering is rejected because the IR does not yet carry collation semantics.

`Aggregate.aggregates` contains named calls to `count`, `sum`, `avg`, `min`, or
`max`. `sum` and `avg` require numeric inputs; `min` and `max` accept numeric or
datetime inputs. `count` may omit its input to represent `COUNT(*)`. Grouping
fields and aggregate aliases form a new relation schema, so an ungrouped source
field does not silently survive the aggregate.

This bounded fragment deliberately excludes arithmetic, null semantics,
field-to-field comparison, arbitrary functions, nested boolean trees, and
collation-dependent string ordering. Unsupported syntax fails structurally;
TrustAero does not claim to validate semantics it has not defined.

The repository has not published IR v1 as a stable external release. Replacing
the earlier local free-form draft is therefore an intentional schema-breaking
prototype change, recorded before any compatibility promise is made.

Every obligation rewrite is checked again by the same graph invariants and
schema-transfer rules. Generated operator IDs deterministically avoid all
untrusted candidate IDs. TrustAero does not trust an operator merely because
its own rewriter created it.

Obligations without a defined IR v1 enforcement are rejected. This prevents a
validator response from claiming that an ignored obligation was satisfied.

## Obligation rewrite postconditions

Insertion and satisfaction are separate decisions. After rewriting, an
independent verifier walks backward from the validated output to the untrusted
candidate's original output. Only a unary enforcement suffix on that path may
satisfy an obligation; an unused operator on another branch does not count.

| Obligation | Verified postcondition |
|---|---|
| `VERSION_PIN` | every scanned dataset has a non-empty resolved snapshot binding |
| `MASK` | a suffix `Mask` covers all required fields with the required method |
| `GENERALIZE_LOCATION` | a suffix generalizer covers the fields, preserves selection, uses the required method, and has at least the required fixed-grid cell size |
| `MIN_GROUP_SIZE` | a suffix guard has `minimum_count` greater than or equal to the requirement |
| `LINEAGE_CAPTURE` | suffix lineage strength is at least `source < record` |

Malformed parameters, a broken output chain, a weaker enforcer, or an absent
enforcer returns `OBLIGATION_NOT_ENFORCED`. Only obligations proven by this
postcondition pass are copied into `satisfied_obligations`. This is an
executable safety check for the bounded IR fragment, not yet a general formal
proof or evidence that a physical DBMS executed the operator correctly.

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
