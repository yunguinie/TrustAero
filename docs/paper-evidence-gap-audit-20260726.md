# TrustAero paper evidence gap audit

Date: 2026-07-26

This audit maps frozen evidence to bounded paper claims. The machine-readable
source of truth is `experiments/frozen/paper_results_registry_v1_20260724.json`;
development runs not named there cannot silently become publication evidence.

## Executive decision

TrustAero is past the feasibility stage. The validator, legality-first planner,
DuckDB execution boundary, lineage/certificate path, real-data optimizer
holdout, formal overhead studies, and four-source complete loop all have frozen
positive evidence.

The project is not yet submission-ready. The remaining scientific risk is
concentration: the strongest optimizer result covers one bounded governed
pipeline family on BTS and NYC parameter/month strata. Adding datasets or
rerunning TPC-H Q3/Q10 will not fix that risk. A second preregistered,
governance-motivated query shape and a paper-ready ablation/baseline analysis
are the only potentially necessary new experiments.

## Proposed research questions

| RQ | Question | Primary frozen evidence | Status |
|---|---|---|---|
| RQ1 | Does TrustAero safely validate, rewrite, reject, and independently detect tampering in untrusted Agent plans? | Phase 0; four-source V2 complete loop | Ready |
| RQ2 | Do governance constraints change the legal/optimal physical plan, and can the bounded optimizer choose with low regret? | governed-pipeline admission; controlled holdout; policy-stratified BTS/NYC holdout; retained permissive and TPC-H negatives | Ready, but narrow |
| RQ3 | What execution, lineage, certificate, memory, and storage overhead does governance introduce as data scales? | 100K/500K and full-month system overhead; record Lineage V3/V4 | Ready |
| RQ4 | Does the implementation work across standard, real-domain, and heterogeneous spatial workloads? | TPC-H Q1/Q3/Q6/Q10 semantics and Q1/Q6 timing; BTS; NYC; four-source case | Partially ready |

## Claim-to-evidence matrix

| Candidate paper claim | Authorizing evidence | Exact boundary |
|---|---|---|
| Fail-closed semantic handling is deterministic for the frozen IR fragment. | Phase 0: 26/26 correct status and reason codes, 100% injected-fault detection, 0% false rejection. | Does not prove correctness outside the supported IR/policy fragment. |
| Governance legality is enforced before cost comparison. | Four-source V2: four candidates generated, three policy-incompatible candidates rejected, sole legal candidate selected and planner-bound. | This V2 selection is governance-determined, not a cost-model speed win. |
| A fixed legal order is not universally optimal. | Governed-pipeline semantic admission: policy-first and join-first each win in controlled strata. | Development motivation, not unseen-data generalization. |
| The frozen cost model selects near-Oracle plans on controlled unseen data. | Controlled independent holdout: 100% 3%-Oracle-set hit, 0.088% mean regret, 2.120% maximum regret. | Controlled synthetic workload. |
| The optimizer transfers to unseen real BTS/NYC months under the frozen no-raw-join policy. | Policy-stratified V2: 96/96 Oracle-set hits; 0.220% mean, 1.442% P95, 2.084% maximum regret; zero illegal selections. | One candidate fragment, DuckDB, frozen scales, policies, and query shape. |
| The optimizer beats the strongest legal fixed plan in that fragment. | Same holdout: best fixed hit 75%, mean regret 5.193%, P95 20.443%; optimizer reduced mean regret by 95.77%. | Do not compare this number to unrelated earlier Mask models. |
| Full TrustAero control-path overhead is modest for source-lineage execution. | Full-month BTS/NYC: paired intervals include parity; about 1.47-1.48 ms median control components; zero spill. | Source lineage and PARTIAL certificate only. |
| Record lineage has a measurable but reducible cost. | V3 and V4 formal: V4 uses about 32 B/edge and reduces end-to-end latency 49.5%/62.8% versus V3. | Single source, one unique unmasked STRING key; V4 remains 2.12x/1.93x direct. |
| TrustAero closes the complete Agent-to-certificate loop over heterogeneous sources. | Four-source V2: safe REWRITE, illegal REJECT, Pl, four Pp candidates, policy pruning, selected Pp, 9,128 rows, four-source lineage, six rejected faults. | Semantic evidence only; no cryptographic attestation of DuckDB internals. |
| TPC-H supplies standard semantic and method evidence. | Exact Q1/Q3/Q6/Q10 support; accepted SF10 Q1/Q6 timing. | Coverage is 4/22, not full TPC-H support or a cross-query optimizer result. |
| Standard TPC-H Q3/Q10 do not expose a material candidate-selection boundary here. | Frozen SF10 negative: no singleton winner at the preregistered 3% threshold. | Retained negative; this branch is permanently stopped. |

## Evidence strengths

1. The main optimizer result is a frozen independent real-data holdout, not
   training accuracy.
2. Both positive and negative results are retained, including failed transfer,
   support-boundary failure, permissive-policy collapse, and the stopped TPC-H
   Q3/Q10 branch.
3. Results bind commits, configs, protocols, data snapshots, plans, events, and
   artifact hashes.
4. The evaluation already spans controlled data, BTS, NYC TLC, TPC-H, and a
   four-source spatial case; another dataset alone has low marginal value.
5. System overhead, record-lineage overhead, peak memory, and spill are
   measured separately rather than hidden inside optimizer speedup.

## Remaining gaps

### G1: second optimizer query shape — high scientific priority

The real holdout varies policy selectivity, query selectivity, join match rate,
width, month, seed, and dataset, but it still uses one governed pipeline shape.
For a conservative PVLDB submission, freeze one additional query shape whose
governance semantics naturally create at least two legal, non-dominated plans.
Examples include a governed Join-Aggregate result or a governed sorted-output
query, provided Mask, checkpoint, and lineage semantics are independently
validated before timing.

Admission must occur before a model is evaluated:

- every candidate returns the same result and lineage;
- every candidate is legal under the stated policy;
- at least two candidates win under confidence-authorized development strata;
- no candidate is mechanically dominated everywhere;
- the frozen existing model is used unchanged or a new model is clearly scoped;
- untouched months/templates are opened only once after freezing.

If the admission gate fails, retain the negative and do not manufacture a new
optimizer version.

### G2: optimizer ablation and strong baselines — required for paper tables

Use existing development/holdout artifacts where possible. Report:

- each legal fixed candidate;
- Legal Oracle;
- frozen optimizer;
- legality plus conservative fallback without the cost model;
- cost-model component ablations on grouped development data;
- policy regimes separately, including the permissive negative.

Do not tune a new threshold on the exposed final holdout. A post-hoc baseline
may be descriptive only unless it was frozen independently.

### G3: paper-ready aggregation and figures — required

Generate tables and plots directly from the verified registry:

- semantic/fault-detection table;
- optimizer hit/regret/baseline table with mean, P95, maximum, and selection
  counts;
- policy-dependent legal-candidate diagram;
- scalability/overhead plot with paired 95% intervals, memory, and spill;
- Lineage V3/V4 latency and bytes-per-edge plot;
- workload/coverage table including all 22 TPC-H queries and the supported
  denominator;
- complete-loop diagram keyed to four-source V2 artifacts.

Every plotted number must link to a registered artifact and be regenerated by
one script.

### G4: reproducibility release check — required engineering, not a new result

- update stale architecture and status documentation;
- run the full test/schema/registry checks in `TrustAero_env`;
- verify a clean editable install from the committed repository;
- ensure data downloads, licenses, manifests, and checksums are documented;
- provide one command for each paper table/figure;
- keep raw public data and generated large results out of Git while preserving
  deterministic preparation scripts and hashes.

## Explicitly unnecessary work

- Do not rerun or retune TPC-H Q3/Q10 multicandidate optimization.
- Do not add more datasets merely to increase the dataset count.
- Do not force ordinal Lineage V4 onto the unsupported four-source spatial
  many-to-many Join.
- Do not reopen rejected V1-V5 Mask models.
- Do not weaken the 3% materiality or paired-confidence rules.

## Recommended execution order

1. Update the human evidence ledger and architecture boundary.
2. Build the registry-driven paper table/figure generator.
3. Produce the optimizer ablation from already measured development artifacts.
4. Design and preregister exactly one second governance-driven query-shape
   admission experiment.
5. Run that admission; proceed to an untouched holdout only if it passes.
6. Freeze the final evidence set and start the paper draft in parallel with
   figure polishing.

The current evidence is sufficient to begin writing the system design,
semantics, implementation, datasets, and existing evaluation sections now.
Only the breadth-sensitive optimizer claims should wait for G1/G2.
