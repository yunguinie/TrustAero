# Stable reason codes

Reason codes are defined in `src/trustaero/ir/enums.py`. They are stable within
IR version 1.0 and are the grouping key for experimental error statistics.
Human-readable messages may improve without changing the code.

TrustAero uses reason codes as a failure taxonomy: a diagnostic code should
identify the root class of a rejection, while the diagnostic details carry the
operator ID, field name, attempted operation, or snapshot value needed for
debugging.

## L1 structural and graph codes

These codes reject malformed or ill-connected candidate plans before policy
authorization is considered.

| Code | Meaning |
|---|---|
| `PLAN_PARSE_ERROR` | the candidate JSON cannot be parsed as the strict IR model |
| `MISSING_REQUIRED_FIELD` | a required model field is absent |
| `UNKNOWN_OPERATOR` | an operator tag is not part of the supported IR fragment |
| `INVALID_OPERATOR_ARGUMENT` | an operator argument is malformed or outside its allowed range |
| `DUPLICATE_OPERATOR_ID` | two logical operators share the same ID |
| `UNBOUND_REFERENCE` | an operator input points to a missing logical operator |
| `CYCLIC_PLAN` | the candidate logical operator graph contains a cycle |
| `UNREACHABLE_OPERATOR` | a logical operator does not contribute to the declared output |
| `OUTPUT_OPERATOR_UNKNOWN` | the declared logical output operator does not exist |

## L2 relational and expression semantics

These codes distinguish root plan-semantic failures over catalogs, schemas,
fields, expressions, and relation capabilities.

| Code | Meaning |
|---|---|
| `UNKNOWN_DATASET` | a scanned dataset is absent from the catalog |
| `UNKNOWN_FIELD` | requested field never occurs in a scanned dataset |
| `FIELD_NOT_AVAILABLE` | a field is absent at the operator where it is used, often after projection |
| `DUPLICATE_OUTPUT_FIELD` | one operator declares the same output name more than once |
| `JOIN_KEY_TYPE_MISMATCH` | left and right join keys have different logical types |
| `AMBIGUOUS_OUTPUT_FIELD` | two join inputs expose the same unaliased field name |
| `SPATIAL_FIELD_REQUIRED` | an operator lacks a complete Catalog-declared coordinate pair |
| `SPATIAL_CRS_MISMATCH` | spatial inputs do not share the required CRS |
| `TEMPORAL_FIELD_TYPE_INVALID` | a temporal predicate targets a non-temporal/non-datetime field |
| `INVALID_TIME_RANGE` | the interval does not satisfy `start < end` |
| `EXPRESSION_TYPE_MISMATCH` | a structured comparison uses incompatible logical types or undefined ordering semantics |
| `AGGREGATE_TYPE_NOT_SUPPORTED` | an aggregate function is undefined for its input field's logical type |
| `MASKED_FIELD_USED_SEMANTICALLY` | a masked presentation field is used for an operation that requires raw field semantics |
| `GENERALIZATION_TARGET_NOT_SPATIAL` | a governance rewrite targets fields without spatial capability |
| `OPERATOR_SEMANTICS_UNSUPPORTED` | the current IR representation is too free-form for sound inference |

## Policy and obligation codes

These codes record governance failures: missing purpose, denied policy,
unsupported obligation parameters, or failed rewrite postconditions.

| Code | Meaning |
|---|---|
| `PURPOSE_MISSING` | the request lacks a purpose required for policy evaluation |
| `POLICY_DENIED` | at least one applicable policy rule denies the request |
| `POLICY_INDETERMINATE` | policy evaluation could not determine a safe permit/deny result |
| `POLICY_NOT_APPLICABLE` | no applicable policy rule permits the request |
| `POLICY_CONFLICT` | applicable policy decisions or obligations are inconsistent |
| `OBLIGATION_CONFLICT` | obligations cannot be normalized into a single supported requirement |
| `OBLIGATION_PARAMETER_INVALID` | a supported obligation has missing, malformed, unknown, or not-yet-defined parameters |
| `OBLIGATION_NORMALIZATION_FAILED` | evaluated obligations and their internal policy provenance are inconsistent |
| `OBLIGATION_NOT_ENFORCED` | the final output path or snapshot bindings do not prove a policy obligation's postcondition |
| `MASK_METHOD_CONFLICT` | one field is required to use two masking methods without a defined strength order |
| `VERSION_UNRESOLVED` | a required data version or snapshot cannot be resolved |
| `SPATIAL_PRECISION_EXCEEDED` | disclosed spatial precision is finer than allowed |
| `MASK_REQUIRED` | policy requires masking before the request can proceed |
| `MIN_GROUP_SIZE_REQUIRED` | policy requires a minimum group-size guard |
| `EXPORT_FORBIDDEN` | export-control policy forbids the requested access |
| `LINEAGE_REQUIRED` | policy requires lineage capture or evidence |
| `REWRITE_CYCLE_DETECTED` | rewrite insertion would create a graph cycle |
| `REWRITE_DID_NOT_CONVERGE` | deterministic rewrite failed to reach a stable validated plan |

## Lineage evidence codes

Lineage is split into logical requirements, physical instrumentation, and
execution evidence. These codes avoid treating a planned lineage node as proof
that execution evidence exists.

| Code | Meaning |
|---|---|
| `LINEAGE_REQUIREMENT_UNSATISFIED` | a lineage requirement has not been met by validated instrumentation or evidence |
| `LINEAGE_INSTRUMENTATION_MISSING` | lineage is required but no validated instrumentation covers the target |
| `LINEAGE_LEVEL_INSUFFICIENT` | implemented or observed lineage level is weaker than the requirement |
| `LINEAGE_EVIDENCE_MISSING` | execution-time lineage evidence is required but absent |
| `LINEAGE_EVIDENCE_INCONSISTENT` | lineage evidence is malformed or internally inconsistent |
| `LINEAGE_TARGET_NOT_COVERED` | lineage evidence does not cover a required output target |

## Certificate and physical-plan codes

These codes are used by `verify_execution_certificate(...)`. They validate the
structure of the approved physical plan and governed execution certificate, but
they do not prove that a real DBMS computed the result bytes correctly.

| Code | Meaning |
|---|---|
| `CERTIFICATE_BINDING_MISMATCH` | an execution certificate does not bind to the validated logical plan ID or digest |
| `PHYSICAL_PLAN_BINDING_MISMATCH` | an approved physical plan or certificate does not bind to the expected logical/physical plan |
| `CERTIFICATE_SNAPSHOT_MISMATCH` | certificate policy or data snapshots differ from the validated plan bindings |
| `CERTIFICATE_DIGEST_MISSING` | a required certificate digest field is absent or empty |
| `CERTIFICATE_EVENT_MISSING` | a required execution event such as `PlanApproved`, `ResultMaterialized`, `LineageRecorded`, or `CertificateEmitted` is absent |
| `CERTIFICATE_EVENT_ORDER_INVALID` | event sequence numbers or phase ordering are invalid |
| `CERTIFICATE_OPERATOR_EVENT_MISSING` | a physical operator is missing a required `OperatorStarted` or `OperatorCompleted` event |
| `CERTIFICATE_OPERATOR_DEPENDENCY_VIOLATION` | a physical operator starts before one of its direct input operators completes |
| `CERTIFICATE_PHYSICAL_PLAN_CYCLIC` | the approved physical operator graph contains a dependency cycle |
| `CERTIFICATE_PHYSICAL_OPERATOR_UNKNOWN` | a physical input or output operator ID does not exist, or a physical operator does not contribute to the output |
| `CERTIFICATE_PHYSICAL_OPERATOR_DUPLICATE` | two physical operators share the same physical operator ID |

## Experimental use

For paper experiments, prefer reporting both coarse categories and exact reason
codes. For example, a failure table can group
`CERTIFICATE_OPERATOR_DEPENDENCY_VIOLATION` under "certificate/physical
execution structure" while still preserving the exact code for reproducibility.
