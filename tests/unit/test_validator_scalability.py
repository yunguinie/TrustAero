from pathlib import Path

from trustaero.catalog.models import FieldDescriptor
from trustaero.experiments.validator_scalability import (
    ValidatorScalabilityConfig,
    build_cases,
    load_validator_scalability_config,
)
from trustaero.ir.enums import DataType, ValidationStatus
from trustaero.validator.service import validate
from trustaero.validator.type_checker import RelationSchema

ROOT = Path(__file__).resolve().parents[2]


def test_relation_schema_lookup_index_is_stable() -> None:
    schema = RelationSchema(
        tuple(FieldDescriptor(name=f"f{index}", data_type=DataType.STRING) for index in range(128))
    )
    assert schema.get("f127") is schema.fields[-1]
    assert schema.get("missing") is None
    assert schema.fields_by_name is schema.fields_by_name


def test_frozen_validator_scalability_matrix_is_one_factor_at_a_time() -> None:
    config = load_validator_scalability_config(
        ROOT / "experiments/configs/validator_control_plane_scalability_v3.json"
    )
    cases = build_cases(config)
    assert len(cases) == 24
    assert {case.dimension for case in cases} == {
        "plan_nodes",
        "output_fields",
        "applicable_policies",
        "raw_obligations",
    }
    assert len({case.case_id for case in cases}) == len(cases)


def test_small_scalability_cases_reach_expected_deterministic_status() -> None:
    config = ValidatorScalabilityConfig(
        results_dir="results/test-validator-scale",
        dimensions={
            "plan_nodes": (2,),
            "output_fields": (4,),
            "applicable_policies": (3,),
            "raw_obligations": (3,),
        },
        warmup_rounds=1,
        measured_blocks=10,
        target_block_ms=1.0,
        maximum_inner_loops=2,
        bootstrap_draws=1000,
        bootstrap_seed=7,
        require_tracked_tree_clean=False,
    )
    for case in build_cases(config):
        first = validate(case.raw_plan, case.policy_set, case.catalog)
        second = validate(case.raw_plan, case.policy_set, case.catalog)
        assert first.status == case.expected_status
        assert first.validated_plan is not None
        assert second.validated_plan is not None
        assert (
            first.validated_plan.validation.canonical_digest
            == second.validated_plan.validation.canonical_digest
        )
        if case.dimension == "raw_obligations":
            assert first.status == ValidationStatus.REWRITE
