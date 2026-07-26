# Architecture

TrustAero is a mediation layer between an untrusted agent and governed
spatio-temporal data. The agent may propose what it wants, but TrustAero decides
whether that request is well-formed, policy-compliant, safely rewritten, and
structurally ready for controlled execution.

## End-to-end pipeline

```text
User request
  -> agent proposes CandidatePlan JSON
  -> L1 strict model/schema validation
  -> L2 graph, expression, catalog, and schema validation
  -> L3 policy decision and obligation inference
  -> obligation normalization and conflict detection
  -> deterministic safe rewrite
  -> ValidatedLogicalPlan
  -> bounded physical-candidate generation
  -> governance feasibility and dominance pruning
  -> authorized cost ranking or conservative fallback
  -> ApprovedPhysicalPlan
  -> controlled DuckDB execution and lineage capture
  -> GovernedExecutionCertificate
  -> CertificateVerification
```

An everyday analogy: the agent writes a request form, TrustAero checks the form,
adds mandatory safety conditions, removes forbidden routes, selects an approved
work order, executes the supported fragment through DuckDB, and checks whether
the execution log matches that work order.

## Stage responsibilities

| Stage | Main object | What TrustAero checks | What it does not claim |
|---|---|---|---|
| Agent proposal | `CandidatePlan` | JSON can be parsed as the bounded IR fragment | Agent output is trustworthy |
| Logical validation | `ValidatedLogicalPlan` | operator graph, catalog references, typed expressions, policy decisions, inferred obligations, safe rewrites | physical execution is already implemented |
| Physical planning | `ApprovedPhysicalPlan` | candidate legality before cost, bounded materialization/placement strategies, planner-decision digest, logical-plan and snapshot binding | arbitrary SQL or unrestricted Join enumeration |
| Controlled execution | DuckDB compiled query and lineage artifact | supported operators lower to SQL, results are materialized deterministically, selected candidates preserve result semantics | every DuckDB-internal operator output is independently recomputed |
| Execution record | `GovernedExecutionCertificate` | certificate binds to logical/physical plans and planner choice; required digests/events/evidence are present | certificate can prove database bytes by itself |
| Certificate verification | `CertificateVerification` | structural event coverage, physical DAG validity, dependency-respecting event order, lineage evidence consistency | a malicious or buggy DBMS could not lie |

## Trust boundary

"Untrusted" does not mean that every agent plan is malicious or wrong. It means
the agent cannot grant itself database authority. A candidate plan earns
permission only after deterministic validation and, when needed, deterministic
rewrite.

The current trusted computing base includes:

- the validator;
- the catalog and policy store;
- the physical-plan approval logic;
- the future controlled executor;
- the execution-event log used to build certificates.

The LLM/agent, its JSON plan, and any free-form explanation it gives are outside
the trusted computing base.

## Governance flow

Policy rules produce decisions and obligations. TrustAero then normalizes
obligations before rewriting the plan. For example, if policy requires location
generalization and lineage capture, the validated logical plan records both the
rewritten governance operators and the obligations that still need execution
evidence.

Lineage is intentionally split across three layers:

```text
LineageRequirement          policy-level requirement
LineageInstrumentationSpec  approved plan instrumentation requirement
LineageEvidenceSummary      execution-time evidence summary
```

This prevents a logical `LineageCapture` node from being mistaken for proof
that a database execution actually emitted lineage evidence.

## Physical-plan and certificate checks

`ApprovedPhysicalPlan` is a pre-execution specification, not executable SQL. It
freezes the physical operator skeleton that a future backend must implement.

`verify_execution_certificate(...)` checks that:

- the approved physical plan binds to the validated logical plan;
- the certificate binds to the approved physical plan;
- policy and data snapshots match;
- result and lineage digests are present where required;
- every physical operator has start and completion events;
- event order respects plan approval, result materialization, lineage recording,
  and certificate emission;
- the physical operator graph is a DAG whose operators contribute to the output;
- an operator starts only after all direct input operators complete.

Independent branches may overlap or interleave. TrustAero rejects dependency
violations, not legitimate parallelism.

## Current implementation boundary

The current prototype executes a bounded DuckDB fragment and is not a general
SQL optimizer. It does not:

- support arbitrary SQL or all 22 TPC-H queries;
- optimize arbitrary Join orders or every governance operator placement;
- provide record-level lineage for Join, SpatialJoin, Aggregate, composite
  keys, or multiple contributing sources;
- recompute every physical operator output independently;
- cryptographically prove result bytes;
- defend against a malicious database engine.

When those components are not independently recomputed, the verifier reports
them as unverified rather than silently accepting them. This is why a structurally
consistent certificate currently returns `PARTIAL`, not `VERIFIED`.
