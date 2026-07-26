"""Tests for policy-stratified real optimizer holdout."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from trustaero.experiments.policy_stratified_pipeline_holdout import (
    load_policy_stratified_holdout_config,
)
from trustaero.experiments.real_governed_pipeline_transfer import (
    load_real_governed_pipeline_config,
    real_governed_pipeline_units,
)
from trustaero.optimizer.candidate_feasibility import GovernanceFeasibilityPolicy
from trustaero.optimizer.governed_pipeline_space import (
    JOIN_FIRST_MASKED_CHECKPOINT,
    POLICY_FIRST_MASKED_CHECKPOINT,
    QUERY_FIRST_RAW_CHECKPOINT,
    GovernedPipelineStatistics,
    plan_governed_pipeline,
)

ROOT = Path(__file__).resolve().parents[2]


def _statistics() -> GovernedPipelineStatistics:
    return GovernedPipelineStatistics(
        input_rows=120_000,
        estimated_policy_rows=24_000,
        estimated_query_rows=78_000,
        estimated_governed_rows=15_600,
        estimated_query_join_rows=50_700,
        estimated_result_rows=10_140,
        sensitive_width_bytes=256.0,
    )


def test_holdout_months_are_disjoint_from_retained_development() -> None:
    measurement = load_real_governed_pipeline_config(
        ROOT / "experiments/configs/policy_stratified_pipeline_holdout_measurement_v1.json"
    )
    development = load_real_governed_pipeline_config(
        ROOT / "experiments/configs/real_governed_pipeline_transfer_v1.json"
    )

    assert {item.month for item in measurement.sources}.isdisjoint(
        {item.month for item in development.sources}
    )
    assert len(real_governed_pipeline_units(measurement)) == 96


@pytest.mark.local_artifact
def test_every_frozen_source_and_manifest_exists() -> None:
    """Fail during tests instead of after the user starts a formal run."""

    for version in ("v1", "v2"):
        measurement = load_real_governed_pipeline_config(
            ROOT
            / (f"experiments/configs/policy_stratified_pipeline_holdout_measurement_{version}.json")
        )
        for source in measurement.sources:
            assert (ROOT / source.event_path).is_file()
            assert (ROOT / source.dimension_path).is_file()
            manifest_path = ROOT / source.preparation_manifest_path
            assert manifest_path.is_file()
            assert json.loads(manifest_path.read_text(encoding="utf-8"))["status"] == "PASS"


@pytest.mark.local_artifact
def test_normalized_january_manifest_matches_immutable_files() -> None:
    """January normalization must not merely assert hashes without checking."""

    manifest = json.loads(
        (ROOT / "data/manifests/processed/real-data-2024-01.json").read_text(encoding="utf-8")
    )

    for artifact in (*manifest["inputs"], *manifest["outputs"]):
        path = ROOT / "data" / artifact["relative_path"]
        assert path.stat().st_size == artifact["byte_size"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact["sha256"]


def test_policy_regimes_create_three_two_and_one_candidate_spaces() -> None:
    config = load_policy_stratified_holdout_config(
        ROOT / "experiments/configs/policy_stratified_pipeline_holdout_evaluation_v1.json"
    )
    legal = {
        regime.policy_id: plan_governed_pipeline(
            _statistics(),
            regime.to_policy(),
        ).nondominated_candidate_ids
        for regime in config.policy_regimes
    }

    assert legal["permissive"] == (
        POLICY_FIRST_MASKED_CHECKPOINT,
        QUERY_FIRST_RAW_CHECKPOINT,
        JOIN_FIRST_MASKED_CHECKPOINT,
    )
    assert legal["no_raw_join"] == (
        POLICY_FIRST_MASKED_CHECKPOINT,
        QUERY_FIRST_RAW_CHECKPOINT,
    )
    assert legal["strict"] == (POLICY_FIRST_MASKED_CHECKPOINT,)


def test_primary_claim_and_stop_rule_are_frozen() -> None:
    protocol = json.loads(
        (
            ROOT / "experiments/frozen/policy_stratified_pipeline_holdout_protocol_v1_20260724.json"
        ).read_text(encoding="utf-8")
    )

    assert protocol["retained_development_result"]["posthoc_diagnostic_is_not_holdout_evidence"]
    assert "outperform the best legal fixed baseline" in protocol["primary_claim"]
    assert any("ends this optimizer claim" in item for item in protocol["one_shot_rule"])


def test_core_policy_constructor_matches_frozen_regime() -> None:
    """The frozen representation maps exactly to the production policy API."""

    policy = GovernanceFeasibilityPolicy("no_raw_join", 0, None, True)
    assert policy.max_raw_join_rows == 0
    assert policy.max_raw_materialized_rows is None
    assert policy.require_governance_checkpoint


def test_v2_final_months_and_support_are_frozen() -> None:
    """Final months are disjoint and the evaluator rejects any fallback."""

    final_measurement = load_real_governed_pipeline_config(
        ROOT / "experiments/configs/policy_stratified_pipeline_holdout_measurement_v2.json"
    )
    first_measurement = load_real_governed_pipeline_config(
        ROOT / "experiments/configs/policy_stratified_pipeline_holdout_measurement_v1.json"
    )
    development = load_real_governed_pipeline_config(
        ROOT / "experiments/configs/real_governed_pipeline_transfer_v1.json"
    )
    evaluation = load_policy_stratified_holdout_config(
        ROOT / "experiments/configs/policy_stratified_pipeline_holdout_evaluation_v2.json"
    )

    final_months = {item.month for item in final_measurement.sources}
    opened_months = {item.month for item in (*first_measurement.sources, *development.sources)}
    assert final_months == {"2024-03", "2024-06", "2024-09", "2024-12"}
    assert final_months.isdisjoint(opened_months)
    assert evaluation.maximum_out_of_support_fallback_rate == 0.0
    assert evaluation.support_path is not None
    assert evaluation.prior_holdout_negative_path is not None
