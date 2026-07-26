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

That operator calibration completed all 36 units with exact cardinalities and
no spill. Median cross-seed relative difference for `HASH_JOIN` timing is about
9% (maximum about 45%). The evidence supports a row/cardinality Join term; it
does not support forcing identifier width into the hash-table cost. Width must
enter the next formula through measured hashing and payload/materialization.

The resulting mechanism formula was committed before evaluation and fitted
only from those independent component measurements. It preserved all injected
governance constraints and match-rate monotonicity, but reached only 66.7%
within the 3% band, 7.13% mean regret, 39.20% P95 regret, and 49.32% maximum
regret. It is a frozen negative result; Phase 2G remains unauthorized. See
`docs/mechanism-mask-cost-results.md` and
`experiments/frozen/phase2_mechanism_formula_negative.json`.

## Phase 2I complete physical fragments

Because isolated operator times did not add up to accurate end-to-end choices,
Phase 2I measures the complete early- and late-Mask DuckDB fragments, including
`HASH_JOIN`, `ORDER_BY`, projection, and the early `MATERIALIZED` CTE boundary.
The existing checkpoint runner now accepts `mask_fragment` and additionally
requires equal content digests, different actual-plan fingerprints, exact Join
cardinality, and stable repeated profile shapes.

The smoke and 72-unit pilot protocols are frozen in
`phase2i_fragment_smoke.json` and `phase2i_fragment_pilot.json`. Both retain
terminal progress, ETA, atomic per-unit checkpoints, and resume. Phase 2I is
development measurement only; it does not authorize Phase 2G or train a new
optimizer. See `docs/phase2i-fragment-protocol.md`.

## Phase 2J high-match boundary confirmation

Phase 2J freezes 90 complete-fragment units around the single stable-early
Phase 2I point. It uses three scales, three narrow-to-medium widths, 90%/100%
match rates, and five new seeds. A family needs at least four of five seeds
outside the existing 3% tie band in the same direction.

The predeclared optimizer-design gate additionally requires at least two
adjacent stable-early families, one stable-late family, and no spill. Passing
only permits model design; it does not authorize Phase 2G. See
`docs/phase2j-boundary-protocol.md` and
`experiments/configs/phase2j_fragment_boundary_confirmation.json`.

The completed confirmation has 90/90 equivalent result pairs, 90/90 distinct
actual-plan pairs, exact Join cardinalities, and no spill. The frozen 4/5 rule
finds five stable-early, three stable-late, and ten mixed families. All four
optimizer-design gate checks pass, including adjacency of the stable-early
region. This permits pipeline-aware model design only; Phase 2G remains
unauthorized. See `docs/phase2j-boundary-results.md` and
`experiments/frozen/phase2j_boundary_confirmation_record.json`.

## Phase 2K pipeline-aware optimizer development

Phase 2K fits one complete-fragment cost formula on the combined Phase 2I/J
development families. It uses whole-family cross-validation, non-negative
physical-work coefficients, an uncertainty fallback to frozen V1, and
predeclared regret and coverage gates. Passing Phase 2K still does not authorize
Phase 2G until the resulting model artifact is separately frozen. See
`docs/pipeline-aware-mask-optimizer.md` and
`experiments/configs/phase2k_pipeline_optimizer_development.json`.

The one-shot Phase 2K evaluation merges 162 paired replicates into 40 complete
families. Its direct formula collapses to fixed late Mask; the guarded version
matches frozen V1 at 77.5% within the 3% band and therefore fails the required
strict-improvement check. This is frozen as a negative result without post-hoc
parameter tuning, and Phase 2G remains unauthorized. See
`docs/phase2k-pipeline-optimizer-results.md` and
`experiments/frozen/phase2k_pipeline_optimizer_negative.json`.

## Phase 2L paired operator attribution

Phase 2L uses the already frozen Phase 2I/J `EXPLAIN ANALYZE` artifacts to
compare early/late operator roles within the same data seed. It predeclares
sign, rank-association, dominance, and stable-region reversal thresholds before
the combined analysis. Operator timings are treated as descriptive association,
not an additive causal decomposition. See
`docs/phase2l-operator-attribution-protocol.md` and
`experiments/configs/phase2l_pipeline_attribution.json`.

The completed analysis covers 162 paired replicates and 40 physical families.
Hash projection is the strongest association but does not reverse direction in
stable-early families; no single role passes all four frozen checks. The result
is retained as negative evidence and points to a cross-operator ablation rather
than post-hoc threshold changes. See
`docs/phase2l-operator-attribution-results.md` and
`experiments/frozen/phase2l_operator_attribution_negative.json`.

## Phase 2M complete-pipeline ablation

Phase 2M freezes four result-equivalent SQL fragments that place explicit
boundaries after Join, after Hash, or before Join. The smoke uses one existing
stable-early, one stable-late, and one mixed region with a new development seed.
It parses actual DuckDB JSON plans, rejects collapsed fingerprints or misplaced
boundaries, verifies complete output digests and exact Join cardinality, and
retains progress/resume support. Smoke timing is not performance evidence. See
`docs/phase2m-pipeline-ablation-protocol.md` and
`experiments/configs/phase2m_pipeline_ablation_smoke.json`.

The completed smoke validates all three scenarios, four distinct plans per
scenario, every requested boundary, exact Join cardinality, equal output, and
zero spill. Join-materialized is the diagnostic fastest in all three instances,
but it writes raw matched values to an intermediate and is therefore
policy-conditional. A compact development matrix is authorized only with
explicit raw-exposure annotations; Phase 2G remains unauthorized. See
`docs/phase2m-pipeline-ablation-smoke-results.md` and
`experiments/frozen/phase2m_pipeline_ablation_smoke_record.json`.

The compact Phase 2M protocol reuses only the same three development families
with five new seeds. It annotates raw Join, raw materialization, and masked
materialization exposure before comparing cost under three fixed policy
profiles. Complete-family analysis reports policy-dependent legal optima and
governance overhead. See `docs/phase2m-compact-ablation-protocol.md` and
`experiments/configs/phase2m_pipeline_ablation_compact.json`.

The compact run completed 15/15 units, 900 timed measurements, and 180 physical
profiles with equal output, validated boundaries, exact Join cardinality, and
zero spill. Policy changed the legal optimum in all three families, but the
`no_raw_materialization` profile produced one stable tie and two mixed families,
so the frozen V2.1 gate failed. The tie band and seed-agreement threshold were
not changed after observing the result; Phase 2G remains unauthorized. See
`docs/phase2m-compact-ablation-results.md` and
`experiments/frozen/phase2m_compact_policy_ablation_negative.json`.

## Formal real-data development-partition suite

The first formal measurement protocol freezes three semantic-ready query
families before their clean-source run: BTS governed masked read, NYC zone
aggregate, and BTS early/late Mask placement. The BTS natural multi-Join remains
explicitly deferred because its current timing adapter covers only the 100K
semantic slice.

```powershell
python scripts/validate_real_data_formal_protocol.py
python -u scripts/run_real_data_formal_v1.py --progress
```

The runner refuses a dirty worktree, rechecks all protocol and semantic-smoke
SHA-256 bindings, rotates candidate order, displays progress, records CPU,
memory, spill, lineage, certificate, and exposure evidence, and applies the
predeclared stability gates. January 2024 is a development partition already
used by integration pilots: it can support method-level paper analysis after
all gates pass, but is not independent Optimizer V1/V2 holdout evidence.

The first clean-source execution completed with all integrity and stability
gates passing. BTS selected post-Mask materialization (1.147x opportunity over
fused), NYC selected fused execution, and BTS early/late Mask placement was a
paired 3% tie while strict policy permitted only early Mask. The exact result
boundary and non-claims are recorded in
`docs/formal-real-data-development-results.md` and the raw artifacts are bound
by `experiments/frozen/real_data_formal_development_results_20260719.json`.

The previously deferred BTS natural multi-Join now has a separate full-month
protocol. Four candidates imply 24 possible execution orders, so the protocol
uses 48 paired blocks—two complete permutation cycles and 192 timed candidate
executions:

```powershell
python -u scripts/run_bts_multijoin_formal.py --progress
```

This remains January development evidence and computes only a diagnostic
Oracle. It does not claim that an online optimizer selected the fastest route.

## Real-data infrastructure pilot

The BTS and NYC TLC integration pilot exercises the canonical validated plan at
100K and 500K rows. It records real selectivities, intermediate cardinalities,
DuckDB's observed physical plan, memory/spill metrics, client-materialization
latency, Mask exposure, source lineage, and certificate status. Atomic units can
be resumed safely and the terminal displays elapsed time plus ETA.

```powershell
python -u scripts/run_real_data_pilot.py --progress
python scripts/summarize_real_data_pilot.py
```

The configuration permanently labels this run as infrastructure development,
not optimizer comparison and not paper performance evidence. It contains one
canonical candidate per unit; multi-candidate claims remain prohibited until
each alternative is represented by an approved physical plan and its observed
DuckDB structure is confirmed.

The subsequent semantic gate now constructs three `ApprovedPhysicalPlan`
objects for each BTS/NYC workload: fused execution plus two reviewed storage
boundaries. The generic execution compiler independently rechecks logical-plan
digest, snapshots, pending obligations, backend support, and the physical DAG
before realizing an explicit DuckDB `MATERIALIZED` CTE. All six candidates have
equal results, distinct observed DuckDB fingerprints, source lineage, and
candidate-specific `PARTIAL` certificates.

Under the permissive output-Mask profile all BTS candidates are legal. Under
the no-raw-sensitive-materialization profile, the candidate that stores 19,447
raw filtered tail numbers is rejected before timing, while fused execution and
post-Mask materialization remain legal. This is semantic evidence that policy
changes the feasible physical-plan space; it is still not a speedup result.

The balanced multi-candidate performance pilot then runs 84 visible steps over
100K/500K BTS and NYC slices. Each candidate is preflighted with result,
lineage, certificate, `EXPLAIN ANALYZE`, memory, and spill checks before timed
rounds. Candidate order rotates across repetitions. The second, resource-
complete run passes every integrity gate with zero spill and authorizes a
full-month pre-experiment. With the frozen 3% tie band it does **not** show a
stable policy-dependent Oracle or scale reversal, so the noisier first pilot is
not promoted as evidence. See the latest report under
`results/real_data_candidate_pilot/<run_id>/report.md`.

The first full-month pre-experiment binds all 547,271 BTS rows and all
2,964,624 official NYC TLC January rows, uses two warmups and ten measured
rounds, and completes 78/78 steps without spill or result disagreement. Its
semantic/resource integrity gates pass, but a post-pilot timing diagnostic finds
46% and 90% maximum first-half/second-half median drift for BTS and NYC. The run
is therefore retained as useful diagnostic evidence while formal performance
reporting remains unauthorized. The next measurement protocol must balance all
six candidate permutations and use more paired repetitions; no latency winner
from this run may be promoted to a paper table.

The corrected paired protocol covers all six candidate permutations five times
(30 paired blocks and 90 timed candidate executions per workload) under one hot
DuckDB connection. It records UTC start time, process CPU time, block ID,
permutation, and position. The full-month validation passes absolute drift,
paired-ratio drift, and paired outlier gates. This authorizes the protocol for
future frozen governance-driven query families, but the validation run itself
remains non-paper evidence. See
`experiments/frozen/real_data_paired_timing_protocol_20260719.json`.

### Frozen governance-driven real-data query families

The paired timing protocol is only a measurement method; it does not authorize
choosing queries after observing which candidate wins.  The versioned study
design is therefore stored in
`experiments/configs/real_data_query_families_v1.json` and checked with:

```powershell
python scripts/validate_real_data_query_families.py
```

The check is deliberately data-free and performs no timing.  It confirms that
four executable templates still validate to the expected logical mechanisms:
the BTS masked read, NYC zone aggregate, BTS natural multi-Join, and BTS
Mask/Join placement. The two BTS placement-related semantic smokes are:

```powershell
python scripts/run_bts_multijoin_smoke.py
python scripts/run_bts_mask_join_smoke.py
```

The multi-Join smoke checks four result-equivalent candidates and four distinct
observed DuckDB plans. The Mask/Join smoke checks early versus late SHA-256
placement over the native `OriginAirportID` Join. Its strict profile rejects
the late route before cost comparison because raw `Tail_Number` values would
enter the Join; the permissive profile keeps both routes. Both smokes verify
source lineage and candidate-specific certificates without recording latency.
The historical V1 query-family file still records TPC-H and the multisource
certificate case as `design_only`.  Do not edit that hash-bound snapshot.
`experiments/frozen/query_family_execution_status_v2_20260726.json` upgrades
`QF-MULTISOURCE-CERTIFICATE` to `executed_verified` using the complete V2
four-source evidence.  It remains semantic rather than performance evidence.
TPC-H support and performance boundaries are tracked by their later frozen
coverage and retained-negative records.

The BTS Mask/Join query also has a non-paper paired timing-protocol validator:

```powershell
python -u scripts/run_bts_mask_join_pilot.py --progress
```

It runs both orders (`late -> early` and `early -> late`) equally often in 30
hot-cache paired blocks, records process CPU time and DuckDB memory/spill, and
gates absolute drift, paired-ratio drift, and ratio outliers. The 100K protocol
validation run `20260719T040615396550Z` passed. Its early-Mask median was about
241 ms versus 254 ms for late Mask, with a median paired ratio of 0.958 and no
spill. These numbers are integration diagnostics only: the worktree was dirty,
the query used the 100K development slice, and no optimizer was evaluated.

The same runner accepts the frozen full-January configuration:

```powershell
python -u scripts/run_bts_mask_join_pilot.py `
  --config experiments/configs/bts_mask_join_full_month_pilot.json `
  --progress
```

The non-paper full-month confirmation `20260719T042845123244Z` processed
547,271 flights, of which 106,251 reached the Join. It completed 60 timed
candidate executions in about 154 seconds with no spill and passed all paired
stability gates. Separate medians were 875 ms (early) and 907 ms (late), but
the primary paired median ratio was 0.987. Therefore the predeclared 3% paired
rule classifies the candidates as tied; reporting the separate-median gap as a
win would be methodologically incorrect. The strict policy still admits only
early Mask because late Mask sends 106,251 raw sensitive rows into the Join.

## TPC-H support audit and governed Q6

The TPC-H stage retains all 22 official queries in its support denominator.
`scripts/audit_tpch_sf1.py` executes every query and records explicit IR
blockers. `scripts/run_tpch_q6_smoke.py` runs the first supported official query
through validation, three physical candidates, exact result comparison, source
lineage and certificate verification. Neither command records performance
timing; see `docs/tpch-support-audit.md` for the scientific boundary.

### TPC-H SF10 formal timing boundary

TPC-H SF10 generation, Q1/Q6 semantic gates, and content-addressed timing
configs are available through the commands documented in the project README.
Every complete measured block is persisted atomically. `--resume-run-id` can
finish an interrupted diagnostic run without repeating complete blocks, but a
multi-process result fails the final `single_execution_process` integrity gate
because it no longer satisfies the frozen single-connection cache protocol.

The first complete SF10 Q1 run (`20260720T043902336493Z`) is retained as
negative diagnostic evidence. All 450 measurements, result-equivalence checks,
candidate-space checks, and permutation-balance checks completed. Fused and
post-aggregate materialization were inside the 3% tie band, while post-filter
materialization was about 6.64x slower because 98.6% of 59,986,052 input rows
crossed the materialization boundary. The run is not authorized for a final
paper table: the paired-ratio outlier gate failed and the run crossed two
process sessions. The immutable boundary is recorded in
`experiments/frozen/tpch_sf10_q1_resumed_diagnostic_negative_20260720.json`.

The next uninterrupted Q1 run (`20260720T065522179359Z`) also remains frozen
as diagnostic evidence. It passed all integrity gates, but the post-filter
materialization route had 39.9% paired half drift and a 13.3% outlier fraction,
so the V1 all-candidates stability gate rejected it. More importantly, mirrored
middle-position orders showed that running this heavy route first made the
following post-aggregate route about 16.6% faster. This establishes a real
carryover hazard rather than justifying a relaxed threshold.

The predeclared V2 configs therefore add two protections. First, mirrored
orders test each possible heavy route for carryover into its immediate
successor. Second, candidate claims use only pollution-safe blocks and require
a deterministic 95% permutation-stratified paired-bootstrap interval. The
interval must lie wholly below the 3% material-speedup boundary, wholly above
the 3% material-slowdown boundary, or wholly inside the 3% equivalence band;
otherwise the conclusion is `INCONCLUSIVE`. The old V1 artifacts are never
reanalyzed as accepted V2 evidence.

The first uninterrupted V2 Q1 run (`20260720T094049562476Z`) passed all
integrity gates and completed 450 measurements. Its pollution-safe 95% paired
interval authorizes one claim: post-filter materialization is materially slower
than fused (median ratio 9.577, interval `[7.371, 10.306]`). Post-aggregate
materialization versus fused remains inconclusive (`[0.877, 1.053]`), as do
both carryover checks. The latter findings must not be described as ties or as
proof that carryover is absent. The immutable accepted boundary is recorded in
`experiments/frozen/tpch_sf10_q1_paired_ci_accepted_20260720.json`.

The first uninterrupted V2 Q6 run (`20260721T012711938759Z`) also passed all
integrity gates and completed 900 measurements. All four predeclared carryover
intervals stayed inside the 10% materiality band. Its pollution-safe 95% paired
interval authorizes one claim: materializing after the temporal predicate is
materially slower than fused (median ratio 5.358, interval `[4.934, 5.741]`).
Materializing after all Q6 predicates remains inconclusive (`[0.992, 1.165]`).
The immutable boundary is recorded in
`experiments/frozen/tpch_sf10_q6_paired_ci_accepted_20260721.json`.
