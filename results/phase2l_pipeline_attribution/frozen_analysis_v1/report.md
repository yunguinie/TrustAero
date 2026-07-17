# Phase 2L paired operator attribution

This is development profiling association, not causal decomposition or Phase 2G.

- Paired replicates: 162
- Physical families: 40
- Stable early/late/tie/mixed: {'stable_early': 5, 'stable_late': 20, 'stable_tie': 1, 'mixed': 14}
- Interaction-hypothesis eligible: False

| Role | Sign agreement | Spearman | Dominant families | Early-region delta | Late-region delta | Eligible |
|---|---:|---:|---:|---:|---:|---|
| hash_projection | 71.9% | 0.685 | 100.0% | 0.016 | 0.570 | False |
| support_projection | 33.6% | -0.374 | 0.0% | -0.734 | -0.778 | False |
| hash_join | 66.4% | 0.212 | 0.0% | 0.759 | 0.783 | False |
| order_by | 66.4% | 0.353 | 12.5% | 0.012 | 0.158 | False |
| materialization | 66.4% | 0.000 | 17.5% | 1.000 | 1.000 | False |
| event_scan | 64.8% | 0.586 | 0.0% | -0.002 | 0.430 | False |
| dimension_scan | 47.7% | -0.003 | 0.0% | 0.027 | -0.048 | False |
| output_sink | 53.9% | -0.060 | 0.0% | 0.079 | 0.082 | False |

> Operator timings come from separate EXPLAIN ANALYZE profiles and may 
> include parallel CPU effects. Differences support hypotheses; they do 
> not prove that one operator independently caused wall-clock speedup.
