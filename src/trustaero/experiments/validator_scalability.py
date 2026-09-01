"""Deterministic control-plane scalability benchmark for logical approval.

The benchmark varies one input dimension at a time while exercising the full
``validate`` entry point.  Fixture construction is outside the timed region.
"""

from __future__ import annotations

import hashlib
import json
import platform
import statistics
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.catalog.models import CatalogDocument
from trustaero.ir.enums import ValidationStatus
from trustaero.ir.models import PolicySet
from trustaero.validator.service import validate


class ValidatorScalabilityConfig(BaseModel):
    """Frozen benchmark parameters."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    results_dir: str
    dimensions: dict[str, tuple[int, ...]]
    warmup_rounds: int = Field(ge=1)
    measured_blocks: int = Field(ge=10)
    target_block_ms: float = Field(gt=0)
    maximum_inner_loops: int = Field(ge=1)
    bootstrap_draws: int = Field(ge=1000)
    bootstrap_seed: int
    require_tracked_tree_clean: bool = True


@dataclass(frozen=True)
class _Case:
    case_id: str
    dimension: str
    size: int
    raw_plan: dict[str, Any]
    policy_set: PolicySet
    catalog: InMemoryCatalog
    expected_status: ValidationStatus


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _git(project_root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=project_root, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _tracked_tree_clean(project_root: Path) -> bool:
    return not _git(project_root, "status", "--short", "--untracked-files=no")


def _catalog(field_count: int) -> InMemoryCatalog:
    payload = {
        "schema_version": "1.0",
        "datasets": [
            {
                "dataset_id": "bench",
                "versions": ["v1"],
                "default_version": "v1",
                "fields": [
                    {"name": f"f{i}", "data_type": "string", "roles": []}
                    for i in range(field_count)
                ],
                "spatial": None,
                "temporal_field": None,
            }
        ],
    }
    return InMemoryCatalog(CatalogDocument.model_validate(payload))


def _plan(node_count: int, field_count: int, plan_id: str) -> dict[str, Any]:
    fields = [f"f{i}" for i in range(field_count)]
    operators: list[dict[str, Any]] = [
        {
            "operator_type": "ScanSource",
            "operator_id": "op0000",
            "inputs": [],
            "dataset": "bench",
            "snapshot": None,
        }
    ]
    previous = "op0000"
    for index in range(1, node_count):
        current = f"op{index:04d}"
        operators.append(
            {
                "operator_type": "Project",
                "operator_id": current,
                "inputs": [previous],
                "fields": fields,
            }
        )
        previous = current
    return {
        "schema_version": "1.0",
        "plan_id": plan_id,
        "request_context": {
            "subject": {"subject_id": "bench-user", "role": "researcher", "attributes": {}},
            "purpose": "research",
            "action": "read",
            "query_time_window": None,
        },
        "requested_output": {
            "fields": fields,
            "export": {"requested": False, "destination": None, "format": None},
            "lineage_level": "none",
        },
        "operators": operators,
        "output_operator": previous,
    }


def _policies(rule_count: int, obligation_count: int) -> PolicySet:
    rules: list[dict[str, Any]] = []
    if obligation_count:
        rules.append(
            {
                "policy_id": "P-OBLIGATIONS",
                "policy_version": "1",
                "subject_roles": ["researcher"],
                "purposes": ["research"],
                "actions": ["read"],
                "resources": ["bench"],
                "decision": "PERMIT",
                "obligations": [
                    {
                        "obligation_type": "MASK",
                        "parameters": {"fields": ["f0"], "method": "hash"},
                    }
                    for _ in range(obligation_count)
                ],
                "reason": "Repeated compatible obligations test normalization.",
            }
        )
    for index in range(rule_count):
        rules.append(
            {
                "policy_id": f"P-PERMIT-{index:05d}",
                "policy_version": "1",
                "subject_roles": ["researcher"],
                "purposes": ["research"],
                "actions": ["read"],
                "resources": ["bench"],
                "decision": "PERMIT",
                "obligations": [],
                "reason": "Applicable permit rule.",
            }
        )
    return PolicySet.model_validate(
        {
            "schema_version": "1.0",
            "policy_set_id": "validator-scale-policy",
            "policy_snapshot": "policy-scale-v1",
            "rules": rules,
        }
    )


def build_cases(config: ValidatorScalabilityConfig) -> tuple[_Case, ...]:
    """Build one-factor-at-a-time cases from the frozen matrix."""

    cases: list[_Case] = []
    for size in config.dimensions["plan_nodes"]:
        cases.append(
            _Case(
                f"plan_nodes-{size}",
                "plan_nodes",
                size,
                _plan(size, 8, f"pc-plan-nodes-{size}"),
                _policies(1, 0),
                _catalog(8),
                ValidationStatus.ACCEPT,
            )
        )
    for size in config.dimensions["output_fields"]:
        cases.append(
            _Case(
                f"output_fields-{size}",
                "output_fields",
                size,
                _plan(2, size, f"pc-output-fields-{size}"),
                _policies(1, 0),
                _catalog(size),
                ValidationStatus.ACCEPT,
            )
        )
    for size in config.dimensions["applicable_policies"]:
        cases.append(
            _Case(
                f"applicable_policies-{size}",
                "applicable_policies",
                size,
                _plan(2, 8, f"pc-policy-count-{size}"),
                _policies(size, 0),
                _catalog(8),
                ValidationStatus.ACCEPT,
            )
        )
    for size in config.dimensions["raw_obligations"]:
        cases.append(
            _Case(
                f"raw_obligations-{size}",
                "raw_obligations",
                size,
                _plan(2, 8, f"pc-obligation-count-{size}"),
                _policies(0, size),
                _catalog(8),
                ValidationStatus.REWRITE,
            )
        )
    return tuple(cases)


def _median_ci(values: list[float], draws: int, seed: int) -> tuple[float, float]:
    import random

    rng = random.Random(seed)
    medians = []
    for _ in range(draws):
        medians.append(statistics.median(rng.choice(values) for _ in values))
    medians.sort()
    lower = medians[int(0.025 * (draws - 1))]
    upper = medians[int(0.975 * (draws - 1))]
    return lower, upper


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, int(0.95 * len(ordered) + 0.999999) - 1)]


def _time_case(case: _Case, loops: int) -> tuple[float, str]:
    started = time.perf_counter_ns()
    digest = ""
    for _ in range(loops):
        response = validate(case.raw_plan, case.policy_set, case.catalog)
        if response.status != case.expected_status or response.validated_plan is None:
            raise RuntimeError(f"Unexpected validation result for {case.case_id}: {response}")
        digest = response.validated_plan.validation.canonical_digest
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    return elapsed_ms / loops, digest


def run_validator_scalability(
    config: ValidatorScalabilityConfig, project_root: Path, config_path: Path
) -> Path:
    """Execute the frozen benchmark and write a self-describing result bundle."""

    tracked_clean = _tracked_tree_clean(project_root)
    if config.require_tracked_tree_clean and not tracked_clean:
        raise RuntimeError("Formal validator scalability run requires a clean tracked tree")
    cases = build_cases(config)
    case_by_id = {case.case_id: case for case in cases}
    loops: dict[str, int] = {}
    expected_digests: dict[str, str] = {}
    for case in cases:
        for _ in range(config.warmup_rounds):
            _, digest = _time_case(case, 1)
        probe_ms, digest = _time_case(case, 1)
        loops[case.case_id] = min(
            config.maximum_inner_loops,
            max(1, int(config.target_block_ms / max(probe_ms, 0.001) + 0.999999)),
        )
        expected_digests[case.case_id] = digest

    observations: dict[str, list[float]] = {case.case_id: [] for case in cases}
    for block in range(config.measured_blocks):
        rotation = block % len(cases)
        ordered = cases[rotation:] + cases[:rotation]
        for case in ordered:
            latency_ms, digest = _time_case(case, loops[case.case_id])
            if digest != expected_digests[case.case_id]:
                raise RuntimeError(f"Nondeterministic digest for {case.case_id}")
            observations[case.case_id].append(latency_ms)

    rows = []
    for index, case_id in enumerate(sorted(case_by_id)):
        case = case_by_id[case_id]
        values = observations[case_id]
        ci = _median_ci(values, config.bootstrap_draws, config.bootstrap_seed + index)
        rows.append(
            {
                "case_id": case_id,
                "dimension": case.dimension,
                "size": case.size,
                "expected_status": case.expected_status.value,
                "inner_loops": loops[case_id],
                "observation_count": len(values),
                "median_latency_ms": statistics.median(values),
                "p95_latency_ms": _p95(values),
                "median_ci95_ms": list(ci),
                "minimum_latency_ms": min(values),
                "maximum_latency_ms": max(values),
                "canonical_digest": expected_digests[case_id],
                "exception_count": 0,
            }
        )

    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    output = project_root / config.results_dir / timestamp
    output.mkdir(parents=True, exist_ok=False)
    config_bytes = config_path.read_bytes()
    summary = {
        "schema_version": 1,
        "status": "PASS_VALIDATOR_CONTROL_PLANE_SCALABILITY",
        "run_id": timestamp,
        "git_commit": _git(project_root, "rev-parse", "HEAD"),
        "tracked_tree_clean": tracked_clean,
        "config_path": str(config_path.relative_to(project_root)).replace("\\", "/"),
        "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
        "case_count": len(cases),
        "measured_blocks": config.measured_blocks,
        "all_statuses_correct": True,
        "all_digests_deterministic": True,
        "exception_count": 0,
        "results": rows,
        "claim_boundary": (
            "This measures in-process logical approval over synthetic metadata fixtures. "
            "It excludes DBMS execution, network I/O, policy retrieval, and unsupported IR forms."
        ),
    }
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    environment = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "processor": platform.processor(),
        "config_digest": _sha256_json(config.model_dump(mode="json")),
    }
    (output / "environment.json").write_text(
        json.dumps(environment, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return output


def load_validator_scalability_config(path: Path) -> ValidatorScalabilityConfig:
    return ValidatorScalabilityConfig.model_validate_json(path.read_text(encoding="utf-8"))
