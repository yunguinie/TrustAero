# Governed TPC-H SF10 Q1 paired measurement

Status: **PASS**

| Candidate | Median (ms) | P95 (ms) | Peak memory (MiB) |
| --- | ---: | ---: | ---: |
| fused | 3648.815 | 5187.077 | 1052.77 |
| materialize-after-q01-aggregate | 3667.110 | 5499.772 | 7813.47 |
| materialize-after-q01-filter | 33890.084 | 45012.470 | 7813.47 |

- Six-permutation balance: `True`
- Single execution process: `True`
- Stability gates: `{'absolute_half_drift': True, 'paired_ratio_half_drift': False, 'paired_ratio_outlier_fraction': True}`
- Paired 3% diagnostic Oracle set: `['fused', 'materialize-after-q01-aggregate']`
- Optimizer selection evaluated: `False`.

## Carryover checks

| Possible polluter | Target | Exposed/control | 95% CI | Assessment |
| --- | --- | ---: | --- | --- |
| materialize-after-q01-filter | fused | 0.8600 | [0.8390, 1.0313] | INCONCLUSIVE |
| materialize-after-q01-filter | materialize-after-q01-aggregate | 0.8383 | [0.7665, 1.2363] | INCONCLUSIVE |

## Pollution-safe paired claims

| Candidate | Baseline | Paired blocks | Ratio | 95% CI | Conclusion | Authorized |
| --- | --- | ---: | ---: | --- | --- | --- |
| materialize-after-q01-aggregate | fused | 10 | 1.0066 | [0.8774, 1.0525] | INCONCLUSIVE | False |
| materialize-after-q01-filter | fused | 15 | 9.5773 | [7.3712, 10.3059] | MATERIALLY_SLOWER | True |
