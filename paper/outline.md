# TrustAero: evidence-bound PVLDB paper outline

## Working thesis

Agent-generated data requests should be treated as untrusted candidate plans.
TrustAero turns a candidate into a governed execution by validating a typed
intermediate representation, inferring and safely rewriting obligations,
enumerating only policy-feasible physical candidates, optimizing governance
checkpoint placement using execution-aware work estimates, and independently
checking execution evidence.

This is a database paper because its main technical object is a constrained
query-planning and execution problem, not an Agent safety wrapper.

## Intended contributions

1. **Governed plan semantics.** A typed IR and fail-closed validation/rewrite
   fragment that distinguishes accepted, safely rewritten, underspecified, and
   forbidden requests.
2. **Governance-aware physical optimization.** Legal candidate enumeration plus
   execution-aware ranking for governed pipeline and record-lineage checkpoint
   placement.
3. **Evidence-bound execution.** A certificate checker that binds logical and
   physical plans, policy/data snapshots, result digests, event-DAG order, and
   lineage evidence without claiming DBMS-internal cryptographic attestation.
4. **Reproducible evaluation.** Controlled reversals, untouched optimizer
   holdouts, real BTS/NYC distributions, TPC-H semantic coverage, real Agent
   plans, scalability, lineage storage/cost, and a four-source end-to-end case.

## Research questions

### RQ1 — Can TrustAero safely handle untrusted Agent plans?

Primary evidence:

- Phase 0: 26 frozen semantic/planner cases.
- Four outcomes: ACCEPT, REWRITE, CLARIFY, REJECT.
- Real Agent coverage: 120 frozen model/task/mode cells.
- Fault detection and false-reject rate.

Candidate table:

- outcome/fault category;
- expected versus observed status;
- reason-code accuracy;
- median validator and certificate-check latency.

### RQ2 — Do governance obligations create a real optimization problem?

Primary evidence:

- governed-pipeline winner reversals;
- three-way lineage-checkpoint winner reversals;
- physical-plan distinctness and result/lineage equivalence;
- rejected aggregate and TPC-H multicandidate admissions as negative scope
  evidence.

Candidate figure:

- policy/query/join selectivity versus winning governed-pipeline checkpoint;
- query batch/reuse level versus winning lineage checkpoint.

### RQ3 — Can the optimizer generalize beyond fixed legal strategies?

Primary evidence:

- governed-pipeline independent controlled holdout;
- policy-stratified real-data holdout;
- lineage-checkpoint independent holdout;
- fixed candidates, frozen threshold, and Legal Oracle;
- frozen component-deletion ablation.

Headline values must be copied from frozen records, not recomputed manually.

Candidate tables:

- Oracle-set hit, mean/P95/max regret, and illegal selections;
- full model versus component deletion;
- strongest fixed and threshold baselines.

### RQ4 — What does governance cost?

Primary evidence:

- full-month BTS and NYC four-layer system ablation;
- record-lineage V4 at 100K and 500K;
- storage bytes per edge;
- planner, validator, lineage, and certificate component medians;
- zero-spill checks where authorized.

Candidate figure:

- cumulative end-to-end layer overhead;
- record-lineage capture/verification time and storage scaling.

### RQ5 — Does the complete system work end to end?

Primary evidence:

- four-source USGS/NYSDEC/FAA/Census case study V2;
- safe rewrite, illegal-plan rejection, candidate pruning, execution, lineage,
  certificate verification, and six end-to-end tamper injections;
- TPC-H exact semantic support for Q1, Q3, Q6, and Q10.

## Section plan for a 12-page research paper

1. Introduction — 1.0 page
2. Motivation and problem formulation — 1.0 page
3. Governed IR and validation semantics — 1.5 pages
4. Legal physical candidate space — 1.0 page
5. Execution-aware governance optimizer — 2.0 pages
6. Execution evidence and certificate checking — 1.0 page
7. Implementation — 0.5 page
8. Evaluation — 3.0 pages
9. Related work — 0.6 page
10. Limitations and conclusion — 0.4 page

References are excluded from the 12-page limit under the current PVLDB
research-paper policy, but the final template and submission month must be
checked again before submission.

## Claim boundaries that must survive editing

- TrustAero validates the supported IR and policy/operator fragment; it does not
  validate arbitrary SQL or arbitrary Agent prose.
- A certificate cannot prove an uninstrumented DBMS's internal execution.
- TPC-H support is exact for four queries, not all 22.
- Agent plan parse coverage is 99.2%, not 100%.
- External systems are compared by capability unless a genuinely common
  semantic workload is implemented.
- Failed development optimizers are useful diagnosis, not successful baselines.
- A passed holdout supports the frozen model only; it does not authorize later
  retuning on the same holdout.

## Immediate writing order

1. Freeze title, one-sentence problem, and the four contributions.
2. Draw the architecture and plan-lifecycle figure.
3. Write problem formulation and supported fragment.
4. Write optimizer method and legality constraints.
5. Populate evaluation tables directly from the evidence bundle.
6. Write the introduction last, once claims and tables are stable.
