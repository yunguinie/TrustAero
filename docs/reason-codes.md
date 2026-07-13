# Stable reason codes

Reason codes are defined in `src/trustaero/ir/enums.py`. They are stable within
IR version 1.0 and are the grouping key for experimental error statistics.
Human-readable messages may improve without changing the code.

The following L2 codes distinguish root plan-semantic failures:

| Code | Meaning |
|---|---|
| `UNKNOWN_FIELD` | requested field never occurs in a scanned dataset |
| `FIELD_NOT_AVAILABLE` | a field is absent at the operator where it is used, often after projection |
| `DUPLICATE_OUTPUT_FIELD` | one operator declares the same output name more than once |
| `EXPRESSION_TYPE_MISMATCH` | a structured comparison uses incompatible logical types or undefined ordering semantics |
| `AGGREGATE_TYPE_NOT_SUPPORTED` | an aggregate function is undefined for its input field's logical type |
| `MASKED_FIELD_USED_SEMANTICALLY` | a masked presentation field is used for an operation that requires raw field semantics |
| `LINEAGE_REQUIREMENT_UNSATISFIED` | a lineage requirement has not been met by validated instrumentation or evidence |
| `LINEAGE_INSTRUMENTATION_MISSING` | lineage is required but no validated instrumentation covers the target |
| `LINEAGE_LEVEL_INSUFFICIENT` | implemented or observed lineage level is weaker than the requirement |
| `LINEAGE_EVIDENCE_MISSING` | execution-time lineage evidence is required but absent |
| `LINEAGE_EVIDENCE_INCONSISTENT` | lineage evidence is malformed or internally inconsistent |
| `LINEAGE_TARGET_NOT_COVERED` | lineage evidence does not cover a required output target |
| `OBLIGATION_NOT_ENFORCED` | the final output path or snapshot bindings do not prove a policy obligation's postcondition |
| `OBLIGATION_PARAMETER_INVALID` | a supported obligation has missing, malformed, unknown, or not-yet-defined parameters |
| `OBLIGATION_NORMALIZATION_FAILED` | evaluated obligations and their internal policy provenance are inconsistent |
| `MASK_METHOD_CONFLICT` | one field is required to use two masking methods without a defined strength order |
| `JOIN_KEY_TYPE_MISMATCH` | left and right join keys have different logical types |
| `AMBIGUOUS_OUTPUT_FIELD` | two join inputs expose the same unaliased field name |
| `SPATIAL_FIELD_REQUIRED` | an operator lacks a complete Catalog-declared coordinate pair |
| `SPATIAL_CRS_MISMATCH` | spatial inputs do not share the required CRS |
| `TEMPORAL_FIELD_TYPE_INVALID` | a temporal predicate targets a non-temporal/non-datetime field |
| `INVALID_TIME_RANGE` | the interval does not satisfy `start < end` |
| `GENERALIZATION_TARGET_NOT_SPATIAL` | a governance rewrite targets fields without spatial capability |
| `OPERATOR_SEMANTICS_UNSUPPORTED` | the current IR representation is too free-form for sound inference |
