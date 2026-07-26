# Optimizer V3 readiness gate

This gate exists to prevent another optimizer version from being fitted before
the development evidence is scientifically usable. It is deliberately stricter
than checking that the Python code runs.

The gate verifies the following boundaries:

- Phase 2I and Phase 2J inputs still match their frozen SHA-256 digests.
- The repository is a committed, clean snapshot in `TrustAero_env`.
- Candidate positions are balanced and their aggregate 95% confidence
  intervals show no material systematic position effect (predeclared ±10%).
- Replicates remain grouped by complete rows-width-match workload families.
- Only pre-execution workload statistics and governance constraints may become
  model inputs; observed latency, winner, and regret are labels only.
- Candidate legality and raw-exposure limits are applied before cost ranking.
- Rejected Phase 2K, Phase 2L, and Phase 2M results remain rejected evidence.
- No Phase 2G holdout has been generated or inspected.

A `PASS` authorizes only the next activity: writing and freezing the Optimizer
V3 development protocol. It does not authorize V3 training, Phase 2G, or a
paper performance claim. Those require later, separate gates.

Run the short audit with:

```powershell
python -u scripts/audit_optimizer_v3_readiness.py
```

The machine-readable and human-readable reports are written below
`results/optimizer_v3_readiness/v1/`.
