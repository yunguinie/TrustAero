# Mask Optimizer V2 development report

This is grouped development cross-validation, not an independent held-out result.

| Scheme | Exact top-1 | Within 3% | Mean regret | P95 regret | Max regret | vs late | vs early |
|---|---:|---:|---:|---:|---:|---:|---:|
| decomposed_cost_leave_one_scenario_out | 80.0% | 80.0% | 4.03% | 37.64% | 39.20% | 1.051x | 1.218x |
| decomposed_cost_leave_one_workload_out | 80.0% | 80.0% | 4.03% | 37.64% | 39.20% | 1.051x | 1.218x |
| guarded_residual_nested_leave_one_scenario_out | 70.0% | 70.0% | 4.65% | 28.43% | 37.64% | 1.044x | 1.209x |
| residual_ranking_leave_one_scenario_out | 80.0% | 80.0% | 3.25% | 28.43% | 37.64% | 1.058x | 1.225x |
| residual_ranking_leave_one_workload_out | 83.3% | 83.3% | 3.02% | 28.43% | 37.64% | 1.060x | 1.228x |
| v1_frozen_baseline | 70.0% | 70.0% | 3.35% | 18.03% | 21.33% | 1.055x | 1.222x |
| v2_leave_one_scenario_out | 70.0% | 70.0% | 4.41% | 28.43% | 37.64% | 1.046x | 1.212x |
| v2_leave_one_workload_out | 73.3% | 73.3% | 4.29% | 28.43% | 37.64% | 1.047x | 1.213x |

> The feature basis and ridge constant were developed after inspecting Phase 2E/F. 
> Only a newly frozen workload can measure V2 generalization.
