# Mechanism Mask formula: frozen negative result

The mechanism formula was committed as `6d987f1` before its one-shot
end-to-end development evaluation. The evaluation used 30 seed-aggregated
workloads and the frozen comparison predictions from V1, linear V2, the
regret-aware residual, and the local guard. This is a development result, not
an independent Phase 2G result.

## Result

| Model | Within 3% | Mean regret | P95 regret | Maximum regret |
|---|---:|---:|---:|---:|
| Frozen V1 | 70.0% | 3.35% | 18.03% | 21.33% |
| Mechanism formula V1 | 66.7% | 7.13% | 39.20% | 49.32% |

The mechanism formula failed all four performance gates. It is therefore
stored as `development_only_rejected_by_predeclared_gate`, and Phase 2G remains
unauthorized. No coefficient, formula term, or development workload was
changed after seeing this result.

The safety side behaved correctly: all four injected legality/exposure cases
passed, and all 270 match-rate monotonicity comparisons passed. This matters,
but safety correctness cannot compensate for an inaccurate cost ranking.

## What the result teaches us

The operation-level leave-one-group-out median relative errors were about:

- SHA-256: 9.46%;
- DuckDB `HASH_JOIN`: 10.41%;
- materialization round trip: 33.35%.

The weakest component is materialization. More importantly, the complete
query is not simply the sum of isolated operator times: DuckDB pipelines,
`ORDER BY`, CTE boundaries, vector reuse, and overlapping scan/projection work
change together when Mask placement changes. A formula can therefore fit hash
and Join microbenchmarks reasonably while still make damaging plan choices.

This negative result rules out another threshold tweak over the same 30
workloads. The next development step should measure complete physical
fragments and pipeline boundaries while preserving scenario-family isolation.
Any replacement structure must be committed before evaluation and must face a
newly declared gate; the current result remains permanently available as an
ablation.

Exact files and SHA-256 digests are recorded in
`experiments/frozen/phase2_mechanism_formula_negative.json`.
