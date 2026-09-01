# Governed TPC-H SF10 Q6 paired measurement

Status: **PASS**

| Candidate | Median (ms) | P95 (ms) | Peak memory (MiB) |
| --- | ---: | ---: | ---: |
| fused | 644.876 | 926.227 | 491.49 |
| materialize-after-q06-predicate | 734.160 | 943.689 | 1059.43 |
| materialize-after-q06-time | 3501.582 | 4149.601 | 1059.43 |

- Six-permutation balance: `True`
- Single execution process: `True`
- Stability gates: `{'absolute_half_drift': True, 'paired_ratio_half_drift': True, 'paired_ratio_outlier_fraction': False}`
- Paired 3% diagnostic Oracle set: `['fused']`
- Optimizer selection evaluated: `False`.

## Carryover checks

| Possible polluter | Target | Exposed/control | 95% CI | Assessment |
| --- | --- | ---: | --- | --- |
| materialize-after-q06-time | fused | 0.9853 | [0.9106, 1.0563] | NO_MATERIAL_CARRYOVER |
| materialize-after-q06-time | materialize-after-q06-predicate | 0.9960 | [0.9125, 1.0197] | NO_MATERIAL_CARRYOVER |
| materialize-after-q06-predicate | fused | 1.0149 | [0.9467, 1.0987] | NO_MATERIAL_CARRYOVER |
| materialize-after-q06-predicate | materialize-after-q06-time | 1.0327 | [1.0055, 1.0759] | NO_MATERIAL_CARRYOVER |

## Pollution-safe paired claims

| Candidate | Baseline | Paired blocks | Ratio | 95% CI | Conclusion | Authorized |
| --- | --- | ---: | ---: | --- | --- | --- |
| materialize-after-q06-predicate | fused | 10 | 1.1017 | [0.9919, 1.1653] | INCONCLUSIVE | False |
| materialize-after-q06-time | fused | 10 | 5.3579 | [4.9340, 5.7410] | MATERIALLY_SLOWER | True |
