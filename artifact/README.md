# TrustAero reproducibility package

This directory connects the paper's evaluation to frozen protocols, executable
entry points, and committed measurements. The package offers two workflows:

- **Verification** checks the committed artifact and reproduces reported
  aggregates without rerunning long database benchmarks.
- **Full reproduction** downloads the public datasets and reruns the selected
  experiment family from its frozen configuration.

## Requirements

| Workflow | Software | Suggested resources | Typical time |
|---|---|---|---|
| Verification | Python 3.11--3.13 | 2 cores, 4 GB RAM, 1 GB free disk | 5--10 min |
| BTS/NYC experiments | Python, DuckDB | 8 cores, 16 GB RAM, 20 GB free disk | several hours |
| TPC-H SF10 | Python, DuckDB TPCH extension | 8 cores, 16 GB RAM, 20 GB free disk | several hours |
| One-million-row Lineage | Python, DuckDB | 4 cores, 8 GB RAM | under 1 hour |

Runtimes depend on storage and CPU. Performance comparisons should be rerun on
one machine with the frozen candidate order and repetition counts.

## 1. Install

```bash
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev,duckdb]"
```

Docker may be used for the verification workflow:

```bash
docker build -t trustaero-artifact .
docker run --rm trustaero-artifact
```

## 2. Fast verification

```bash
python artifact/verify.py
python scripts/check_schema_sync.py
python -m pytest -q
```

`artifact/verify.py` checks the machine-readable artifact map, required files,
result structure, and headline metrics. It does not retime the DBMS.

## 3. Paper map

| Evaluation component | Protocol/configuration | Committed result | Full-run entry point |
|---|---|---|---|
| Deterministic approval | `experiments/configs/phase0.json` | `artifact/results/rq1-phase0/` | `scripts/run_phase0.py` |
| LLM-as-a-Judge approval baseline | `experiments/frozen/llm_judge_approval_protocol_v1_20260901.json` | `artifact/results/rq1-llm-judge/` | `scripts/verify_llm_judge_approval_result.py` |
| Independent black-box plans | `experiments/frozen/blackbox_adversarial_exp4_v1.json` | `artifact/results/rq1-blackbox/` | `experiments/blackbox_exp4/generate_cases.py`, then `scripts/run_blackbox_adversarial_exp4.py` |
| Validator scalability | `experiments/configs/validator_control_plane_scalability_v3.json` | `artifact/results/rq1-validator-scalability/` | `scripts/run_validator_scalability.py` |
| Governed-pipeline holdout | `experiments/frozen/policy_stratified_pipeline_holdout_v2_protocol_20260724.json` | `artifact/results/rq2-governed-pipeline/` | `scripts/evaluate_policy_stratified_pipeline_holdout.py` |
| Lineage-checkpoint holdout | `experiments/frozen/lineage_checkpoint_holdout_protocol_v1_20260726.json` | `artifact/results/rq2-lineage-checkpoint/` | `scripts/evaluate_lineage_checkpoint_holdout.py` |
| Third candidate family | `experiments/frozen/tpch_multijoin_aggregate_exp1_sf10_holdout_v1.json` | `artifact/results/rq2-third-family/` | `scripts/run_tpch_multijoin_aggregate_exp1_holdout.py` |
| Hard legality vs. soft penalty | `experiments/frozen/hard_vs_soft_legality_exp2_v1.json` | `artifact/results/rq2-hard-vs-soft/` | `scripts/run_hard_vs_soft_legality_exp2.py` |
| Candidate-space scalability | `experiments/frozen/candidate_space_scalability_exp3_v1.json` | `artifact/results/rq2-candidate-scalability/` | `scripts/run_candidate_space_scalability_exp3.py` |
| Full-month source Lineage | `experiments/frozen/system_scalability_full_month_formal_protocol_v1_20260724.json` | `artifact/results/rq3-full-month/` | `scripts/run_system_scalability.py` |
| One-million-row record Lineage | `experiments/frozen/record_lineage_ordinal_scaleup_protocol_v1_20260810.json` | `artifact/results/rq3-lineage-1m/` | `scripts/run_record_lineage_pilot.py` |
| Certificate fault matrix | `experiments/frozen/paper_gap_closure_protocol_v1_20260801.json` | `artifact/results/rq3-certificate/` | `scripts/run_cross_stage_contract_ablation.py` |
| Four-source complete loop | `experiments/frozen/multisource_case_study_v2_protocol_20260726.json` | `artifact/results/rq3-four-source/` | `scripts/run_multisource_case_study_v2_lowmem.py` |
| Independent Checker process | `experiments/frozen/independent_checker_manifest_protocol_v1_20260812.json` | `artifact/results/rq3-independent-checker/` | `scripts/run_independent_checker_manifest_experiment.py` |
| Cross-stage ablation | `experiments/frozen/cross_stage_contract_ablation_protocol_v1_20260812.json` | `artifact/results/rq4-cross-stage/` | `scripts/run_cross_stage_contract_ablation.py` |
| PostgreSQL RLS and masking baseline | `experiments/frozen/postgres_conventional_baseline_v1_20260901.json` | `artifact/results/postgresql-baseline/` | `scripts/run_postgres_conventional_baseline.py` |

The TPC-H Q1/Q6 method checks are under `artifact/results/tpch/`. BTS 2025 is
kept under `artifact/results/supplementary/` as a temporal robustness result and
is not merged into the paper's original holdout denominator.

## 4. Data preparation

List all registered inputs:

```bash
python scripts/download_datasets.py --list
```

Prepare the 2024 BTS and NYC TLC workloads:

```bash
python scripts/download_datasets.py --stage main_2024
python scripts/prepare_real_data_2024.py --months 1-12
```

Prepare TPC-H:

```bash
python scripts/prepare_tpch.py --scale-factor 1 --progress
python scripts/prepare_tpch.py --scale-factor 10 --progress
```

Prepare BTS 2025 when reproducing the temporal robustness experiment:

```bash
python scripts/download_datasets.py --stage bts_2025_temporal_holdout_v1
python scripts/prepare_bts_2025_temporal_holdout.py --months 1-12
```

Every prepared input is checked against the tracked manifests before timing.

## 5. Representative full runs

```bash
# Logical approval and scalability
python scripts/run_phase0.py --progress
python scripts/run_validator_scalability.py

# Frozen TPC-H third-family holdout (requires SF10)
python scripts/run_tpch_multijoin_aggregate_exp1_holdout.py

# Planner mechanisms that replay committed measurements
python scripts/run_hard_vs_soft_legality_exp2.py
python scripts/run_candidate_space_scalability_exp3.py

# Execution and evidence
python scripts/run_system_scalability.py \
  --config experiments/configs/system_scalability_full_month_formal_v1.json --progress
python scripts/run_record_lineage_pilot.py \
  --config experiments/configs/record_lineage_ordinal_scaleup_v1.json --progress
python scripts/run_multisource_case_study_v2_lowmem.py --progress \
  --memory-limit 512MB --threads 1
python scripts/run_cross_stage_contract_ablation.py

# Conventional PostgreSQL enforcement baseline (requires Docker)
python scripts/run_postgres_conventional_baseline.py --docker docker --root . --output results/postgresql-baseline/run
```

Each runner writes a new timestamped directory below `results/`, which is
ignored by Git. The submitted measurements remain unchanged in
`artifact/results/`.

## 6. Expected results

The expected aggregates and their interpretation are listed in
[RESULTS.md](RESULTS.md). Machine-readable values and paths are in
[manifest.json](manifest.json).

## 7. Cleanup

Preview ignored generated data and new runs without touching the committed
artifact:

```bash
git clean -ndX data/raw data/processed data/tmp results
```

After reviewing the listed paths, replace `-n` with `-f` to remove them.
