# Mechanism-based Mask cost model

This development model asks a narrower question than the earlier winner-based
models: can separately measured database operations explain when a required
`hash` Mask should be placed before or after a Join?

## Frozen formula

The formula was fixed before inspecting its end-to-end development score. It
contains three non-negative linear components, all measured in milliseconds:

- SHA-256: input rows and input bytes;
- payload materialization/movement: rows and payload bytes;
- DuckDB `HASH_JOIN`: input rows and matched output rows.

Identifier width is deliberately absent from the `HASH_JOIN` component. The
repeated operator calibration found that width had a weak, non-monotone effect
inside DuckDB's `HASH_JOIN`; forcing a width coefficient there would attribute
cost to the wrong mechanism. Width remains present in hashing and payload
movement, where it has a direct physical meaning.

For input cardinality `N`, match rate `m`, raw width `w`, and 64-byte SHA-256
output width `h`, candidate estimates are:

```text
early = Hash(N, w) + Move(N, h)   + Join(N, N*m)
late  = Hash(N*m, w) + Move(N*m,w) + Join(N, N*m)
```

`Move` is an explicit proxy for the materialization/intermediate payload
boundary, not a claim that every DuckDB operator copies every string byte.
The operation-level cross-validation files therefore remain diagnostics; the
decisive gate is the complete end-to-end development comparison.

## Leakage and governance boundaries

Microbenchmark targets fit the coefficients. End-to-end early/late winners do
not fit or tune the formula; they evaluate it once. Seed replicates are first
aggregated into complete `(rows, width)` or `(input rows, output rows)` groups.
Component cross-validation leaves one complete group out.

Candidate legality and maximum raw-value exposure are evaluated before cost.
If late Mask violates an exposure limit, it is excluded even when its estimated
runtime is lower. If neither placement is legal, selection fails closed.

## Predeclared development gate

Compared with frozen Optimizer V1, the mechanism formula must simultaneously:

1. strictly improve the fraction of workloads within the frozen 3% tie band;
2. not worsen mean regret;
3. not worsen P95 regret;
4. not worsen maximum regret;
5. preserve the expected match-rate monotonic direction;
6. pass all injected legality and exposure cases.

A failure is retained as a negative development result. A pass makes the model
eligible for a separate version-controlled freeze; it still does not authorize
Phase 2G by itself. Phase 2G must use untouched widths, match rates, scales, and
seeds fixed after the model artifact is frozen.

## Reproducing the one-shot development evaluation

Run from the repository root in `TrustAero_env`:

```powershell
python -u scripts/develop_mechanism_optimizer.py `
  --pilot-run-dir results/phase2h_mechanism_pilot/20260717T082251698399Z `
  --join-run-dir results/phase2h_join_operator_calibration/20260717T084603958765Z `
  --workload-run-dirs `
    results/phase2e_confirmation/20260715T140108314442Z `
    results/phase2f_optimizer_holdout/20260715T152953290778Z `
    results/phase2v2_boundary_calibration/20260716T012853745457Z `
  --frozen-predictions `
    results/optimizer_v2_development/phase2ef_boundary_local_guard/cross_validation_predictions.csv `
  --output-dir results/mechanism_optimizer_development/fixed_formula_v1
```

The command prints three progress checkpoints and writes the component model,
grouped component diagnostics, end-to-end predictions, gate summary, and a
short Markdown report.
