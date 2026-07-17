# Pipeline-aware Mask optimizer development

This is grouped development cross-validation, not Phase 2G.

| Scheme | Top-1 | Within 3% | Mean regret | P95 | Max | Direct coverage |
|---|---:|---:|---:|---:|---:|---:|
| fixed_early | 22.5% | 42.5% | 167.88% | 724.00% | 959.53% | 100.0% |
| fixed_late | 77.5% | 77.5% | 1.54% | 9.56% | 11.46% | 100.0% |
| oracle_experimental_upper_bound | 100.0% | 100.0% | 0.00% | 0.00% | 0.00% | 100.0% |
| pipeline_cost_direct_leave_one_family_out | 77.5% | 77.5% | 1.54% | 9.56% | 11.46% | 100.0% |
| pipeline_cost_guarded_leave_one_family_out | 75.0% | 77.5% | 1.55% | 9.56% | 11.46% | 40.0% |
| v1_frozen_baseline | 75.0% | 77.5% | 1.55% | 9.56% | 11.46% | 100.0% |

Development gate passed: False

> The Phase 2I/J families informed this model design. The result is 
> development evidence only; Phase 2G remains untouched and unauthorized.
