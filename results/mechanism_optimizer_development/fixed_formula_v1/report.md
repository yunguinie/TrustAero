# Mechanism Mask formula development evaluation

The operation formula was frozen before this end-to-end development evaluation. 
It is not an independent Phase 2G result.

| Scheme | Within 3% | Mean regret | P95 regret | Max regret |
|---|---:|---:|---:|---:|
| guarded_residual_nested_leave_one_scenario_out | 70.0% | 4.65% | 28.43% | 37.64% |
| mechanism_formula_fixed_development | 66.7% | 7.13% | 39.20% | 49.32% |
| residual_ranking_leave_one_scenario_out | 80.0% | 3.25% | 28.43% | 37.64% |
| v1_frozen_baseline | 70.0% | 3.35% | 18.03% | 21.33% |
| v2_leave_one_scenario_out | 70.0% | 4.41% | 28.43% | 37.64% |

Development gate passed: **False**.

Phase 2G remains unauthorized until a passing artifact and its protocol are 
separately frozen in version control.
