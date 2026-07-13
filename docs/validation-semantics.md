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
| `Mask` | every target field exists | masked fields remain projectable but lose raw semantic capability | limits disclosed values |
| `MinGroupSize` | existing aggregate output path | unchanged logical schema | guards grouped output |
| `LineageCapture` | one valid relation | unchanged | plans logical lineage instrumentation |

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

## Obligation normalization and conflicts

After deny-overrides evaluation, at least one matching `PERMIT` rule is
required; obligations from `NOT_APPLICABLE` rules never grant permission or
enter normalization. Applicable permit rules may produce duplicate or
differently strong requirements. TrustAero normalizes them before rewriting,
using a separate partial order for each supported obligation rather than
treating every parameter as one numeric scale.

| Obligation | IR v1 normalization rule |
|---|---|
| `VERSION_PIN` | parameter-free duplicates collapse to one requirement |
| `GENERALIZE_LOCATION` | equal field set and method use the largest fixed-grid cell size |
| `MASK` | equal methods union target fields; incomparable methods on the same field conflict |
| `MIN_GROUP_SIZE` | use the largest `minimum_count` |
| `LINEAGE_CAPTURE` | use `none < source < record` |

Unknown parameters are rejected instead of silently ignored. Fixed version
values/ranges, `field` lineage, execution jurisdictions, and an ExportControl
strength lattice are not defined in IR v1; normalization does not invent those
semantics. Unsupported obligation types remain visible and fail closed in the
rewriter.

The normalized tuple is deterministic, permutation-invariant, and idempotent.
Its logical suffix order is `VERSION_PIN`, generalization, masking, minimum
group size, and lineage capture; disjoint masks use method and field names only
as a deterministic tie-break, not as a strength ranking. Merge provenance
records source policy IDs and the applied algebra rule internally. It is not
yet part of the public execution certificate.

This order is a canonical normalization and deterministic rewrite schedule for
the current logical suffix fragment. It is not a claim that every future
physical optimizer must execute governance in that order. A physical plan will
need operator-specific preconditions, dependency checks, and cost reasoning
before moving any governance operation earlier or later.

## Rewrite rule contracts

IR v1 treats supported rewrites as governance contractions, not ordinary
relational equivalences. Some operators preserve the visible relation
(`VERSION_PIN` through resolved bindings and logical `LineageCapture`), while
others intentionally change what may be observed (`Mask`,
`GeneralizeLocation`, and `MinGroupSize`). The validator therefore checks that
the rewritten plan still represents the user's permitted task while satisfying
the policy obligation; it does not claim `Result(rewritten) = Result(original)`
for every governance operator.

| Rewrite | Precondition | Allowed effect | Failure mode |
|---|---|---|---|
| `VERSION_PIN` | each scanned dataset can resolve a snapshot | binds data versions without changing relation schema | `VERSION_UNRESOLVED` or postcondition failure |
| `GENERALIZE_LOCATION` | target fields form a complete Catalog-declared spatial pair | coarsens disclosed spatial precision while preserving prior selection | `GENERALIZATION_TARGET_NOT_SPATIAL` or postcondition failure |
| `MASK` | target fields exist and method is defined | hides target values and removes raw semantic capability from those fields | `FIELD_NOT_AVAILABLE`, `MASK_METHOD_CONFLICT`, or postcondition failure |
| `MIN_GROUP_SIZE` | the candidate output already depends on an `Aggregate` | guards grouped output; it does not invent a group-by for detail queries | `OBLIGATION_CONFLICT` or postcondition failure |
| `LINEAGE_CAPTURE` | one valid output relation exists | records a logical lineage requirement and instrumentation spec | `LINEAGE_INSTRUMENTATION_MISSING` or evidence failure |

## Mask value states and downstream capability

Masking changes more than a Python or SQL scalar type. It also changes whether
later operators may rely on the field's original meaning. IR v1 therefore gives
each field a value state. Catalog fields start as `raw`; a `Mask` rewrites only
the named fields to one of `redacted`, `hashed`, or `nullified`.

| Method | Output type in IR v1 | Field remains projectable? | Raw semantic capability after mask |
|---|---|---|---|
| `redact` | `string` | yes | none |
| `hash` | `string` | yes | none |
| `null` | original logical type, nullable | yes | none |

The current fragment intentionally does not rank these methods by strength.
It also does not claim that hashed values are valid join keys: salt choice,
domain separation, collision handling, and cross-dataset determinism are not
defined in IR v1. In the current IR fragment, masked fields are not
semantically reusable unless an operation-specific compatibility rule is
explicitly defined. Consequently, a masked field may be projected or requested
as output, but it may not later be used by `Filter`, `Join`, `Aggregate`,
`SpatialFilter`, `SpatialJoin`, `TemporalFilter`, or
`GeneralizeLocation`. Unsupported downstream use fails closed instead of
guessing whether the masked value still has the original field semantics.

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
| `LINEAGE_CAPTURE` | suffix lineage instrumentation strength is at least `source < record` |

Malformed parameters, a broken output chain, a weaker enforcer, or an absent
enforcer returns `OBLIGATION_NOT_ENFORCED`. Only obligations proven by this
postcondition pass are copied into `satisfied_obligations`. Lineage is the
exception: a validated logical plan may contain `lineage_requirements` and
`lineage_instrumentation`, but `LINEAGE_CAPTURE` remains in
`pending_obligations` until execution evidence is checked. This prevents a
logical `LineageCapture` node from being mistaken for proof that a physical
DBMS actually emitted lineage records.

## Lineage requirements, instrumentation, and evidence

TrustAero separates lineage into three layers:

- `LineageRequirement` belongs to the validated logical plan and records what
  the policy requires, such as record-level lineage for the candidate output.
- `LineageInstrumentationSpec` records the validated logical instrumentation
  that a later physical plan must realize.
- `LineageEvidenceSummary` belongs to a future execution certificate and
  records what was actually observed after execution.

The evidence checker validates that the observed evidence covers each required
target and is at least as strong as the policy requirement. A source-level
requirement may be satisfied by record-level evidence, but a record-level
requirement is not satisfied by source-level evidence. Without execution
evidence, lineage stays pending rather than satisfied.

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
