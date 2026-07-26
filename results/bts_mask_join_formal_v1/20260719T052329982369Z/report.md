# BTS Mask/Join paired timing-protocol validation

Status: **PASS**

This is a formal development-partition full_month run over 547271 rows, not held-out optimizer evidence.

| Candidate | Median (ms) | P95 (ms) | Peak memory (MiB) | Spill (MiB) |
|---|---:|---:|---:|---:|
| early_mask_before_join | 761.672 | 1161.011 | 27.48 | 0.00 |
| late_mask_fused | 792.310 | 1082.635 | 27.48 | 0.00 |

## Stability and governance boundary

- Paired protocol stable: `True`
- Median early/late ratio: `1.0067`
- Paired 3% tie-band set: `['early_mask_before_join', 'late_mask_fused']`
- Strict policy feasible set: `['early_mask_before_join']`
- Source worktree dirty: `False`
- Formal paper experiment authorized: `True`.
- No optimizer selection was evaluated.
