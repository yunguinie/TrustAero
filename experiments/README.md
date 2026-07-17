# Phase 0: repeatable semantic evaluation

Phase 0 is the first experiment layer for TrustAero. It evaluates the validator,
rewrite logic, fail-closed behavior, and certificate/physical-plan structural
checks. It is not a database-system performance experiment.

Current Phase 0 questions:

- Does each case return the expected validation status?
- Do diagnostics include the expected stable reason codes?
- Do governed execution certificates detect injected event, lineage, and
  physical-DAG violations?
- How much core validation latency does each case add after inputs are loaded?
- Which commit and environment produced the result files?

Run from the repository root:

```powershell
python scripts/run_phase0.py
python scripts/run_phase0.py --config experiments/configs/phase0.json
python scripts/summarize_phase0.py
```

The runner writes:

```text
results/phase0/<run_id>/
  cases.csv
  summary.json
  environment.json
  config.json
  failures/
```

`cases.csv` contains both expected and actual outcomes. Reason-code columns are
pipe-separated because a case may produce multiple diagnostics. Latency columns
separate one cold end-to-end measurement from repeated preloaded core
validation measurements.

The input matrix uses two important columns:

- `case_kind=validation` runs the candidate plan through the validator.
- `case_kind=certificate` first obtains a validated/rewrite plan, derives an
  approved physical plan, then verifies a deterministic certificate scenario.

`scenario` names a deterministic mutation such as `unknown_dataset`,
`masked_filter`, `expression_type_mismatch`, `weak_lineage`,
`missing_lineage_event`, `snapshot_mismatch`, `event_order_invalid`, or
`dependency_violation`. These are fixed fault injections, not random fuzzing.

This phase can support paper results about semantic correctness, failure
classification, and validator overhead. It cannot support claims about DuckDB
execution latency, physical optimization quality, or real lineage-capture
runtime overhead.

`summarize_phase0.py` reads one or more run folders and writes:

```text
results/phase0_summary/
  phase0_summary.csv
  phase0_summary.json
  phase0_category_summary.csv
  phase0_category_summary.json
  phase0_reason_code_summary.csv
  phase0_reason_code_summary.json
```

The summary includes status accuracy, reason-code accuracy, detection rate over
negative/injected cases, false reject rate over legal cases, and latency
aggregates across each run. The category summary groups cases by experiment
dimension, while the reason-code summary counts expected, observed, and matched
diagnostics without collapsing multi-code cases into a single label.

When a case does not match its expected status or expected reason codes, the
runner writes `failures/<case_id>.json` with the case metadata, flattened result
row, input paths, and serialized diagnostics. Passing cases do not create
failure artifacts, which keeps successful result directories compact.

## Phase 1: minimal DuckDB execution smoke

Phase 1 is the first real execution layer. It runs a small fixed matrix of
validated plans through the trusted SQL compiler, executes them in an in-memory
DuckDB database, computes result digests, and verifies those digests against
governed execution certificates.

Run from the repository root after installing the optional DuckDB extra:

```powershell
python scripts/run_phase1.py
python scripts/run_phase1.py --config experiments/configs/phase1.json
python scripts/summarize_phase1.py
```

The runner writes:

```text
results/phase1/<run_id>/
  cases.csv
  summary.json
  environment.json
  config.json
  failures/
```

`summarize_phase1.py` reads one or more Phase 1 run folders and writes:

```text
results/phase1_summary/
  phase1_summary.csv
  phase1_summary.json
  phase1_category_summary.csv
  phase1_category_summary.json
```

This phase can support claims that the minimal validated-plan-to-certificate
execution path is wired end to end for projection, filters, Join, Aggregate,
the three Mask methods, fixed-grid GeneralizeLocation, and source lineage. The
exact contract is frozen in `docs/execution-semantics-v1.md`.

It still cannot support claims about cost-based optimization, large-scale DBMS
performance, record-level lineage, or malicious database protection. In the
current certificate result, `physical_plan_execution` remains unverified
because the physical trace is trusted-executor evidence, not a cryptographic
proof.

## Phase 2A: controlled statistics and actual physical plans

Phase 2A is a pre-optimizer experiment. It controls fact-table volume, temporal
selectivity, spatial selectivity, policy selectivity, join match rate, and join
skew. The generator uses DuckDB `range` rather than Python row construction, so
larger pilot workloads remain practical.

Run:

```powershell
python scripts/run_phase2a.py --config experiments/configs/phase2a.json
```

For each workload the runner first validates one governed logical query and
generates two `ApprovedPhysicalPlan` candidates: fused execution and one
explicit materialization boundary. It then verifies identical result rows and
saves DuckDB `EXPLAIN (ANALYZE, FORMAT JSON)` output under `plans/`. Each CSV
row records both the shared logical-plan ID and its distinct approved physical-
plan ID. A stable fingerprint is computed from the physical operator tree after
volatile timings and estimates are removed. SQL text differences alone do not
count as plan differences.

This phase still does not claim an optimizer contribution. It establishes a
small legal candidate space and a trustworthy measurement boundary; candidate
enumeration across more operator choices, a cost model, and measured plan
selection remain future work.

## Phase 2B: multiple legal boundaries and source-lineage cost

Phase 2B expands the same validated logical query to five approved candidates:
fused execution plus materialization after temporal, spatial, policy, or final
fact-side projection. Every boundary has an explicit fail-closed SQL mapping.
The runner rejects result differences, groups candidates by observed DuckDB
physical fingerprint, and marks only one representative per duplicate group.

Run:

```powershell
python scripts/run_phase2b.py --config experiments/configs/phase2b.json
```

The Phase 2B policy also requires source lineage. Query latency, actual source-
evidence capture latency, and their combined governed latency are reported
separately. Record-level lineage remains unsupported and is never estimated.
This is still a pilot candidate-space experiment, not a trained or cost-based
optimizer.

## Phase 2C: stability, scale, and resumable measurements

Phase 2C varies scenario, row count, and data seed independently. Candidate
orders use deterministic rotations, not one fixed order or irreproducible
random shuffling. Each completed scenario/scale/seed unit is stored atomically;
`--resume` reruns only an interrupted unit and never admits partial samples.

Calibration run with a visible progress bar:

```powershell
python -u scripts/run_phase2c.py `
  --config experiments/configs/phase2c_calibration.json `
  --progress
```

Resume the latest interrupted calibration:

```powershell
python -u scripts/run_phase2c.py `
  --config experiments/configs/phase2c_calibration.json `
  --progress --resume
```

The runner writes raw timings, per-strategy and per-unit summaries, seed-level
bootstrap intervals, actual DuckDB profiles, checkpoint state, failures, a log,
and both run-local and stable `latest_progress.json` progress files. The bundled
configuration is calibration-only and must not be presented as final paper
data.

The paper-candidate configuration additionally requires a clean Git worktree,
and a resumed run must use the same Git commit recorded at creation. This
prevents one result directory from silently mixing measurements from two code
versions. `Ctrl+C` stops safely; the same command with `--resume` reruns only
the interrupted unit.

Analyze a completed run with paired data-seed comparisons:

```powershell
python scripts/summarize_phase2c.py `
  results/phase2c_paper_candidate/<run_id>
```

The analyzer writes only derived files below `<run_id>/analysis/`. It compares
each candidate with `fused` inside the same generated seed before bootstrapping
seed summaries, so repeated timings from one database are not incorrectly
treated as independent samples. A stable reversal must exceed the frozen tie
threshold, have a positive paired interval, and win at least 80% of seeds.
The default stable label additionally requires at least five independent data
seeds; smaller diagnostic runs can reveal candidates but cannot satisfy it.

## Phase 2D: bounded filter-order diagnostic

Phase 2D does not permit arbitrary operator movement. It accepts only a
complete, unbranched chain made of IR v1 `TemporalFilter`, `SpatialFilter`, and
pure comparison `Filter` operators. Every ordered stage is materialized, the
approved physical DAG is rewired to the same order, and all six permutations
must return identical rows. Masks, generalization, joins, aggregates, partial
filter chains, and branched graphs remain non-reorderable.

Run the short development calibration with:

```powershell
python -u scripts/run_phase2c.py `
  --config experiments/configs/phase2d_calibration.json `
  --progress
```

After committing a clean revision, run the diagnostic protocol with:

```powershell
python -u scripts/run_phase2c.py `
  --config experiments/configs/phase2d_diagnostic.json `
  --progress
```

The diagnostic uses 100K and 500K rows, three independent seeds, and ten
measured repetitions. It is a reversal-discovery experiment, not final paper
evidence. A later confirmation run must freeze scenarios before increasing to
five or more seeds and 30 repetitions.

## Phase 2E: governed Mask placement

Phase 2D confirmed that DuckDB already handles ordinary filter ordering better
than forced materialization. Phase 2E therefore evaluates a decision the
database does not understand: whether a required `hash(event_id)` runs before
or after a Join. The early candidate prevents raw identifiers from entering the
Join; the late candidate hashes only matched rows. The approved placement may
cross only projections retaining `event_id` and joins on different keys.

The runner records both latency and two governance-aware work metrics:
`raw_sensitive_rows_exposed_to_join` and `mask_rows_processed`. Identifier
width and Join match rate are controlled independently to look for the expected
privacy/cost tradeoff without moving Mask across a semantic use.

Run the short calibration with:

```powershell
python -u scripts/run_phase2c.py `
  --config experiments/configs/phase2e_calibration.json `
  --progress
```

The later three-seed diagnostic uses `phase2e_diagnostic.json`; it must run only
after a clean commit and remains screening evidence rather than a paper claim.

The discovery run froze the following confirmation hypotheses before the
larger run: wide identifiers with a high Join match rate may favor early Mask
at 300K rows; a low Join match rate should favor late Mask because fewer rows
need hashing; narrow identifiers are a near-tie control. Confirm them without
changing the scenarios using:

```powershell
python -u scripts/run_phase2c.py `
  --config experiments/configs/phase2e_confirmation.json `
  --progress
```

This confirmation protocol uses the same two scales and three scenarios, but
raises the independent seed count to five, warmups to five, and measured runs
to 30. It is the first Phase 2E result eligible for the analyzer's stable label.
If interrupted, add `--resume`; completed atomic units are not repeated.

Evaluate the frozen V1 selector on this run as calibration data:

```powershell
python scripts/evaluate_optimizer_v1.py `
  results/phase2e_confirmation/<run_id> `
  --evaluation-label calibration
```

The selector first excludes semantically illegal or exposure-infeasible
candidates. It then compares an explicit byte-work proxy for hashing,
identifier width, Join matches, and early materialization. The proxy is
auditable and deterministic; it is not presented as a learned latency model.

## Phase 2F: held-out Optimizer V1 screening

Phase 2F freezes unseen seeds, scales, identifier widths, and Join match rates
to test whether the Phase 2E-calibrated rule generalizes. Do not change the V1
constants after viewing these results and still call the same run held out.

Run with visible progress and ETA:

```powershell
python -u scripts/run_phase2c.py `
  --config experiments/configs/phase2f_optimizer_holdout.json `
  --progress
```

Then compute top-1 selection, 3%-tie accuracy, regret, and speedup versus fixed
late Mask:

```powershell
python scripts/evaluate_optimizer_v1.py `
  results/phase2f_optimizer_holdout/<run_id> `
  --evaluation-label held_out
```

This is an optimizer screening experiment, not yet the final paper benchmark.
A positive result still needs a larger frozen protocol and a real public
dataset. A negative result should be used to revise the model and then create a
new, untouched holdout rather than silently retuning against Phase 2F.

## Optimizer V2 development

After freezing the V1 held-out result, Phase 2E and Phase 2F may be used as V2
development data. V2 predicts the log latency ratio between early and late
Mask from a small, inspectable basis: input rows, identifier width, Join match
rate, raw input work, and matched work.

Run grouped development cross-validation with:

```powershell
python scripts/develop_optimizer_v2.py `
  results/phase2e_confirmation/20260715T140108314442Z `
  results/phase2f_optimizer_holdout/20260715T152953290778Z `
  --output-dir results/optimizer_v2_development/phase2ef
```

The script aggregates repetitions within each seed and then aggregates paired
seed ratios per workload. It reports both leave-one-workload-out and the more
conservative leave-one-scenario-family-out cross-validation. Its fitted model
is marked `development_only_not_held_out_validated`.

These cross-validation numbers are diagnostic. The feature basis was designed
after inspecting Phase 2E/F, and controlled realized cardinalities currently
stand in for estimated cardinalities. V2 requires a newly frozen Phase 2G run
before making any generalization claim.

## V2 targeted boundary calibration

The first V2 regression did not improve the stricter scenario-family split, so
the next run fills specific missing boundaries instead of launching a new
holdout. Its scenarios declare their own scale subsets to avoid an expensive
full Cartesian matrix.

Run from the activated `TrustAero_env` environment with a visible ETA:

```powershell
python -u scripts/run_phase2c.py `
  --config experiments/configs/phase2v2_boundary_calibration.json `
  --progress
```

Resume safely after an interruption:

```powershell
python -u scripts/run_phase2c.py `
  --config experiments/configs/phase2v2_boundary_calibration.json `
  --progress --resume
```

This development protocol contains 32 atomic units, 448 measured executions,
and 128 warmup executions. It uses only two seeds and must not be reported as
stable paper evidence. The maximum 2048-byte workload is capped at 450K rows
because Phase 2F already approached the 4 GB DuckDB memory setting at 500K.

After completion, rerun V2 development with the new run directory added:

```powershell
python scripts/develop_optimizer_v2.py `
  results/phase2e_confirmation/20260715T140108314442Z `
  results/phase2f_optimizer_holdout/20260715T152953290778Z `
  results/phase2v2_boundary_calibration/<run_id> `
  --output-dir results/optimizer_v2_development/phase2ef_boundary
```

The strict acceptance gate is recorded in
`docs/phase2v2-boundary-protocol.md`. Do not freeze Phase 2G merely because one
cross-validation number improves; tail regret and match-rate monotonicity must
also pass.

The V2 development command now evaluates an additional decomposed candidate-
cost model. It writes `decomposed_cost_model.json` and adds both workload- and
scenario-family-held-out rows to `cross_validation_predictions.csv`. The model
estimates early and late latency separately from input, hash, Join-payload, and
materialization work; it never replaces the feasibility checks.

On the current 30 development observations this model improves exact selection
to 80%, but its P95 and maximum regret worsen to 37.64% and 39.20%. It is
therefore an explanatory baseline, not the model to take into Phase 2G. See
`docs/decomposed-mask-cost-model.md` for the formula and frozen interpretation.

The same command also evaluates a regret-aware residual ranking model. It keeps
the decomposed early/late candidate costs as its base score and fits only the
remaining paired log-ratio error. Severe wrong choices receive continuously
higher (capped) training weight, complete scenario families remain isolated in
the primary cross-validation split, and match-rate interaction coefficients
are sign-constrained. An uncertain sign flip or extrapolation outside the
training feature support falls back to the auditable base score. The command
writes `residual_ranking_model.json`; its embedded gate status determines
whether a new Phase 2G holdout may be frozen.

On the current 30-observation development matrix, strict scenario-family CV
reaches 80% within the 3% tie band and 3.25% mean regret, but P95 and maximum
regret are still 28.43% and 37.64%. The artifact is rejected by the predeclared
tail-risk gate, so Phase 2G remains unfrozen. See
`docs/regret-aware-mask-residual-model.md` for the failure analysis.

The next development candidate adds a nested local-regret guard. For every
outer scenario-family holdout, its calibration records are generated by inner
scenario-family holdouts over only the outer training data. It compares the
residual selector and frozen V1 over three nearest distinct scenario families;
the outer winner is never used to choose the selector. The output artifact is
`local_regret_guard_model.json`. See `docs/local-regret-guard.md`.

The frozen three-neighbor guard fails the development gate: it reaches 70%
within the 3% band, 4.65% mean regret, 28.43% P95 regret, and 37.64% maximum
regret. Do not tune the neighbor count on these outer-fold answers and do not
freeze Phase 2G. Add `--progress` to the development command to print each
completed outer guard fold in the VS Code terminal.

## Phase 2H mechanism microbenchmarks

The next stage measures SHA-256 work, Join payload consumption, and explicit
materialization separately rather than fitting another boundary over the same
end-to-end observations. The checkpointed runner is
`scripts/run_mechanism_microbench.py`; the smoke and pilot protocols are in
`experiments/configs/mechanism_microbench_smoke.json` and
`experiments/configs/phase2h_mechanism_pilot.json`.

The pilot is development calibration, not Phase 2G and not paper evidence. It
has 80 atomic units and prints elapsed time plus ETA with `--progress`. See
`docs/mechanism-microbenchmarks.md` for the isolation assumptions, output
artifacts, resume command, and scientific boundary.

The first pilot found identifiable hash and materialization scaling but an
unstable scalar Join diagnostic. The refined Join protocol forces equal-schema
payload materialization on both sides of the paired subtraction and is frozen
in `experiments/configs/phase2h_join_payload_refinement.json`. Do not build the
cost formula until that refinement passes validation and seed-stability checks.

The refinement confirms that whole-query subtraction is not an additive Join
cost: 17/36 median differences are negative. The runner now supports repeated
physical profiles and writes `operator_summary.csv`. The frozen follow-up
`phase2h_join_operator_calibration.json` uses five `EXPLAIN ANALYZE` repetitions
and new seeds to measure `HASH_JOIN` timing/cardinality directly.
