# Governance feasibility before physical-plan cost

TrustAero treats governance compatibility as a hard physical-plan constraint.
The optimizer must first construct a legal candidate set and only then compare
latency or estimated cost. A candidate cannot become legal because it is fast.

## Supported V1 fragment

The reusable checker currently covers two independently verifiable exposures:

- raw sensitive rows entering a Join;
- raw sensitive rows written to a materialized intermediate.

Each candidate also records masked materialization rows for later physical-cost
accounting, but V1 does not invent a security ordering between raw, hashed,
redacted, and nullified values. Method-specific semantic compatibility remains
the validator's responsibility.

Policies use row limits. `None` means that this small policy fragment does not
restrict the exposure; zero forbids it; a positive integer permits a bounded
number of rows. The stable rejection codes are:

- `CANDIDATE_RAW_JOIN_LIMIT_EXCEEDED`;
- `CANDIDATE_RAW_MATERIALIZATION_LIMIT_EXCEEDED`.

## Example

Suppose a candidate sends 100 raw rows through a Join and materializes 80 of
them. Under a policy with unrestricted raw Join but zero raw materialization,
the candidate is rejected with the materialization reason code before any cost
is inspected. A slower candidate that materializes no raw rows remains eligible.

If every candidate violates the policy, the batch result is `REJECT` with an
empty feasible set. This is an ordinary fail-closed result, not permission to
fall back to an illegal plan.

## Trust boundary

Exposure counts must come from trusted candidate construction or independent
physical-plan inspection. They are not accepted from an agent-generated plan
as self-attested evidence. The checker only establishes compatibility with this
bounded exposure policy; it does not verify SQL result equivalence, Mask method
semantics, lineage evidence, or execution certificates.

