# TrustAero paper result ledger

This page is the human-readable index of publication-facing evidence. The
machine-readable source of truth is
`experiments/frozen/paper_results_registry_v1_20260724.json`. Run
`python scripts/verify_paper_results.py` before producing paper tables or
figures. A failed digest check means the referenced result must not be cited
until the discrepancy is explained.

## Current evidence

| Evidence | Outcome | Result that can be stated | Important boundary |
|---|---|---|---|
| Phase 0 semantic faults | PASS | 26/26 status and reason-code decisions correct; all injected violations detected; no legal case rejected | Latency values are provisional because background load was not controlled |
| TPC-H Q1/Q3/Q6/Q10 semantics | PASS | Four official queries have exact trusted-IR execution; every Q3/Q10 candidate matched the official ordered answer and retained a distinct physical plan | Semantic coverage is 4/22 |
| TPC-H SF10 Q1/Q6 performance | PASS | Both frozen paired protocols produced accepted standard-benchmark evidence | Two timed queries do not represent all 22 TPC-H queries |
| TPC-H SF10 Q3/Q10 multicandidate boundary | RETAINED NEGATIVE | Six exact and distinct candidates executed stably, but neither query produced a singleton winner under the frozen 3% materiality rule | The optimizer-expansion branch is stopped; this is not a speedup result |
| Governed pipeline admission | PASS (development) | Join-first and policy-first each win in controlled legal scenarios; outputs and record lineage agree | Motivation evidence, not unseen-data generalization |
| Cost-model grouped development | PASS (development) | 95.83% 3%-Oracle-set hit; 0.339% mean regret | Used to admit the frozen model, not a final claim alone |
| Controlled independent holdout | PASS | 100% 3%-Oracle-set hit; 0.088% mean regret; 2.120% maximum regret | Controlled synthetic distribution |
| Permissive real transfer | RETAINED NEGATIVE | Join-first won all 96 decisions, so adaptive superiority was not established | Must not be hidden or relabeled |
| Policy holdout V1 | RETAINED NEGATIVE | Exact support boundary caused 22 false fallbacks and failed tail-regret gates | Remains negative after the fix |
| Policy holdout V2 | PASS | On unseen BTS/NYC months, no-raw-join optimizer hit the 3%-Oracle set in 96/96 decisions; mean/P95/max regret were 0.220%/1.442%/2.084% | Applies only to the frozen candidate fragment, policies, scale and DuckDB backend |
| System overhead 100K/500K pilots | PASS (integrity pilots) | Four equivalent layers agreed on BTS/NYC results and source-lineage coverage at both scales; all certificate observations were PARTIAL without diagnostics; no spill | Three timing blocks admit the formal stage but do not authorize a publication performance or record-lineage claim |
| Formal system overhead 100K/500K | PASS | Median complete-path overhead was 4.115%/3.560% on BTS and 14.627%/9.304% on NYC at 100K/500K; all four units had zero spill | BTS-100K comes from the corrected frozen confirmation; source-snapshot lineage and PARTIAL certificates only |
| Formal record lineage 100K/500K | PASS | Compact record evidence was generated and independently verified; storage remained 64.004/64.001 B per edge; record/direct latency ratios were 3.85x/5.18x | Single source and one unique unmasked STRING key; the high cost is a measured trade-off, not a low-overhead claim |
| Ordinal record lineage V4 | PASS | Binding output identity to result digest plus row ordinal reduced storage to 32.005/32.001 B per edge and reduced latency by 49.5%/62.8% versus V3 at 100K/500K | The optimized fragment still costs 2.12x/1.93x direct querying and does not cover Join, Aggregate or composite keys |
| Formal full-month overhead | PASS | At 547K BTS and 2.965M NYC rows, complete/direct paired 95% intervals included parity; control components totaled about 1.47/1.48 ms; peak memory was 11.9/53.4 MiB with zero spill | January 2024, source-snapshot lineage and PARTIAL certificates; the -3.101% BTS point estimate is not claimed as negative overhead |
| Four-source end-to-end V2 | PASS | A safe Agent request was rewritten and an illegal request rejected; four Pp candidates were generated, three policy-incompatible candidates pruned, the selected Pp returned 9,128 governed rows, four sources were covered, and six faults were rejected | Semantic integration only; selection is governance-determined, source lineage is not Lineage V4, and PARTIAL does not attest internal DuckDB operator outputs |
| Real Agent plan coverage | PASS | Across 3 current models, 20 frozen tasks and paired thinking/non-thinking modes, all 120 calls completed; strict JSON parse and expected-family rates were 99.2%, validator totality and deterministic revalidation were 100%, and unauthorized unsafe outcomes were 0/24 | Bounded four-source workload, not general LLM-quality evidence; one fenced GLM response is retained as a parse failure |
| Lineage checkpoint holdout | PASS | On 18 untouched decisions, the frozen model achieved 100% 3%-Oracle-set hit and 0 diagnostic regret; the frozen threshold achieved 83.3% hit and 2.126% mean regret | Frozen DuckDB record-lineage checkpoint family only |

## Main optimizer comparison

For the final no-raw-join holdout, the strongest legal fixed baseline was
policy-first:

| Metric | TrustAero | Best fixed | Relative improvement |
|---|---:|---:|---:|
| 3%-Oracle-set hit | 100.0% | 75.0% | +25.0 percentage points |
| Mean regret | 0.220% | 5.193% | 95.77% reduction |
| P95 regret | 1.442% | 20.443% | 92.95% reduction |
| Maximum regret | 2.084% | 20.788% | 89.98% reduction |

The optimizer selected policy-first 72 times and query-first 24 times. It made
zero illegal selections and used zero out-of-support fallbacks. The strict
policy had only one legal route and therefore demonstrates fail-closed pruning,
not cost-model benefit. Under the permissive policy, join-first won every case,
so no adaptive advantage is claimed there.

## What is still missing

The current evidence supports semantic correctness, legal candidate
construction, two independent optimizer holdouts over different governance
query shapes, standard-benchmark method checks, formal source-lineage
control-path overhead, formal bounded record-lineage cost, a clean-tree
four-source complete loop, and frozen real-Agent fail-closed coverage.
TPC-H Q3/Q10 multicandidate timing is complete as a retained negative and must
not be reopened.

Remaining work is:

- registry-driven final tables/figures with confidence intervals, memory, and
  spill reporting;
- consolidated optimizer component ablations and fixed/threshold/Oracle
  baselines in final tables;
- integrate the frozen external capability matrix into Related Work;
- clean-environment reproducibility and paper writing.

See `docs/paper-evidence-gap-audit-20260726.md` for the claim-to-evidence
matrix and stop rules.
