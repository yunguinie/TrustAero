# Cross-stage contract ablation

This frozen analysis tests whether approval, legality-first planning, and evidence-bound checking are substitutable.

| Profile | UA | FR | Illegal physical selections | Registered faults detected | Complete contract |
|---|---:|---:|---:|---:|:---:|
| policy_output_plus_blind_planner_plus_event_log | 0 | 1 | 192 | 6/19 | no |
| approval_plus_blind_planner_plus_event_log | 0 | 0 | 192 | 6/19 | no |
| approval_plus_legality_first_plus_event_log | 0 | 0 | 0 | 6/19 | no |
| full_trustaero | 0 | 0 | 0 | 19/19 | yes |

## Boundary

- This is an offline composition of previously frozen observations, not a new independent holdout.
- No DuckDB query is rerun, no optimizer is refit, and no three-percent threshold is changed.
- The experiment establishes non-substitutability of stage guarantees over registered frozen cases, not universal necessity for every possible system.
- The 11-case validation, 96-decision-per-policy planning, and 19-fault Certificate denominators remain unchanged.
