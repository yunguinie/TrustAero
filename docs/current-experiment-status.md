# Current experiment status

Last updated: 2026-07-26

This ledger separates implemented system functionality, development evidence,
publication-ready evidence, and retained negative results. It exists to prevent
development experiments from being accidentally presented as final paper
results.

## One-sentence status

TrustAero has a working governed-query system, a successful frozen
policy-stratified optimizer holdout on unseen BTS/NYC months, formal
source/record-lineage evidence, a complete four-source Agent-to-certificate
case, and exact trusted-IR support for TPC-H Q1/Q3/Q6/Q10. Paper assembly,
optimizer ablation, and a second governance-driven query shape remain.

## What is implemented

- A bounded IR with schema and type validation.
- Policy-to-obligation inference, normalization, conflict detection, and
  deterministic safe rewriting.
- Conservative Mask and Generalize semantics, including semantic capability
  tracking after masking.
- Logical lineage requirements, physical instrumentation specifications,
  execution evidence, and independent certificate verification.
- Governance feasibility filtering before cost ranking. Illegal candidates are
  never selected because they appear faster.
- A hierarchical planner that records policy rejection, safe component-wise
  dominance pruning, conservative fallback, and explicit deferral when no
  performance ranker is authorized.
- Planner-decision digests bound to approved physical plans and execution
  certificates, with independent recomputation and fail-closed mismatch checks.
- DuckDB execution for the supported fragment, physical-plan inspection,
  result-equivalence checks, and repeatable experiment runners.
- Paired timing, balanced candidate order, progress reporting, resumable runs,
  frozen protocols, stable reason codes, and result provenance.

These are reusable system components rather than one-off paper scripts.

## Evidence already established

### Semantic and governance evidence

The Phase 0 framework exercises ACCEPT, REWRITE, CLARIFY, REJECT,
fail-closed behavior, idempotent rewriting, Mask capability restrictions,
lineage evidence, certificate consistency, and physical-DAG event ordering.
The frozen 26-case planner-certificate fault run achieved 100% status and
reason-code accuracy, 100% injected-violation detection, and 0% false rejects.
Its sub-millisecond overhead observations remain provisional because unrelated
workloads were active during timing.

### Database execution evidence

Supported logical plans execute in DuckDB, result-equivalent candidates return
the same answers, and materially different SQL candidates have distinct
observed DuckDB physical plans.

### Optimizer motivation

Both policy-first and query-first checkpoint plans win in different controlled
scenarios. Therefore a single fixed physical order is not always optimal. The
final policy-stratified real holdout also used both legal plans.

Within the frozen no-raw-join fragment, the optimizer hit the 3%-Oracle set in
96/96 unseen real-data decisions with 0.220% mean regret, versus 75% hit and
5.193% mean regret for the strongest legal fixed baseline. This supports a
bounded optimizer claim, not universal generalization.

### Data and workload preparation

BTS airline and NYC TLC workloads have been executed on real monthly data.
TPC-H exact semantics cover Q1, Q3, Q6, and Q10; Q1/Q6 have accepted SF10
timing evidence. The separately frozen Q3/Q10 SF1 and SF10 multicandidate
experiments were stable and exact but produced no confidence-authorized
singleton winners, so that optimizer-expansion branch is permanently stopped.
Controlled synthetic workloads remain useful for isolating selectivity, width,
match rate, and governance costs. The four-source V2 case now covers a safe and
an illegal Agent request, Pl validation, four Pp candidates, policy-first
pruning, planner-bound selection, deterministic DuckDB execution, source
lineage, independent certificate checking, and six tamper classes.

## Optimizer result ledger

| Stage | Result | Interpretation |
|---|---|---|
| Synthetic V3.1 frozen holdout | PASS | The optimizer learned the controlled synthetic boundary. |
| V3.1 transfer to real data | FAIL | The synthetic cost boundary did not transfer to real distributions. |
| Real-data V4 development | PASS | The execution-aware model fit its development months. |
| V4 independent validation | FAIL | P95 regret exceeded the frozen gate. |
| V4.1 untouched-month holdout | FAIL | It passed absolute regret gates but lost to the frozen 35% threshold. |
| 10% adaptive runtime pilot | FAIL | Only 8.85% of pilots were conclusive; both overrides of the threshold were harmful, and pilot cost did not amortize after 20 reuses. |
| Optional-checkpoint three-candidate pilot | FAIL | Fused execution won all 12 frozen scenarios; the two materialized checkpoint plans were dominated when a durable checkpoint was not required. |
| Required-checkpoint boundary admission | FAIL | Both checkpoint orders won, but the best threshold sat just above the largest tested q and therefore reduced to fixed query-first, which classified 34/37 conclusive scenarios (91.9%). Label diversity was insufficient to authorize a more complex model. |
| Governed pipeline semantic admission | PASS | After fixing candidate semantics, both legal pipeline orders won across the frozen matrix. |
| Pipeline cost grouped development | PASS | 95.8% 3%-Oracle-set hit and 0.339% mean regret admitted independent testing. |
| Pipeline cost controlled holdout | PASS | 100% 3%-Oracle-set hit, 0.088% mean regret, and 2.120% maximum regret. |
| Permissive real transfer | FAIL (retained) | Join-first won every decision, so adaptive superiority is not claimed under this policy. |
| Policy-stratified real holdout V1 | FAIL (retained) | An exact support-boundary bug caused false fallbacks. |
| Policy-stratified real holdout V2 | PASS | On unseen BTS/NYC months the corrected frozen optimizer achieved 100% 3%-Oracle-set hit and 0.220% mean regret. |

The strongest prior simple baseline for the earlier two-candidate checkpoint
problem remains the frozen 35% query-selectivity threshold:

- confidence-family hit rate: 96.875%;
- mean regret: 0.608%;
- P95 regret: 5.068%;
- maximum regret: 10.455%.

This baseline must remain visible in every later comparison.

## What can and cannot currently be claimed

Supported claims:

- TrustAero constructs and verifies a bounded governed-query fragment.
- Governance legality is enforced before physical cost comparison.
- Governance obligations can change legal plan space and execution cost.
- Different data/selectivity regimes produce bidirectional plan reversals.
- The experiment artifact detects and retains optimizer failures rather than
  silently relabeling them as successes.
- Under the frozen no-raw-join policy and candidate fragment, the independent
  real-data holdout outperformed the strongest legal fixed plan.
- One four-source spatial query closes the Agent, policy, rewrite, candidate
  generation, legality pruning, selected Pp, physical execution, source-lineage,
  certificate, and fault-injection loop.

Not yet supported:

- TrustAero's optimizer is universally better across arbitrary policies,
  backends, query shapes, and datasets.
- The optimizer generalizes across all selected datasets and query families.
- The complete paper performance evaluation is finished.

## Remaining publication milestones

### M1: Broaden governance-conditioned query coverage

Define governance-driven query families with at least three non-dominated legal
physical candidates. Each candidate must have explicit result-equivalence,
exposure, lineage, and certificate semantics. Remove candidates that are always
dominated before timing. The first optional-checkpoint family did not pass this
gate: fused execution dominated both checkpoint candidates. Under a
checkpoint-required policy, fused is illegal and both remaining orders can
win, but the isolated family does not justify a complex learned model.

### M2: Preserve and report the validated hierarchical legal planner

First filter candidates by governance feasibility. Then remove mechanically
dominated candidates. Retain fixed query-first as a visible development
baseline for the current required-checkpoint matrix; the degenerate 0.16
threshold is not a validated optimizer. A more complex cost model is permitted
only if a broader, governance-motivated query family passes a new
pre-registered admission test. Compare against fixed plans, simple thresholds,
and a Legal Oracle.

The minimum planner code path and unit-level invariants are implemented, and
the policy-stratified BTS/NYC holdout passed. Further work should broaden query
shapes without retuning or overwriting this result.

### M3: Complete the narrow remaining publication work

The project has completed the untouched BTS/NYC optimizer holdout, TPC-H
Q1/Q3/Q6/Q10 semantics, Q1/Q6 timing, the retained Q3/Q10 multicandidate
negative, semantic fault injection, source-lineage overhead, record-lineage
scaling, and the four-source V2 complete loop. Remaining work is a
registry-driven paper table/figure generator, optimizer component ablation, and
one preregistered second governance-driven query shape if its admission gate
passes.

## Immediate decision

Do not retune the successful policy-stratified holdout, reopen TPC-H Q3/Q10
optimization, or overwrite retained negatives. The four-source V2 complete
loop has passed on a clean tree and is registered. Build paper-ready
tables/ablations now, while preregistering at most one additional
governance-driven query-shape admission experiment.
