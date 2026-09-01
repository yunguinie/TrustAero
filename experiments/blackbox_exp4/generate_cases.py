"""Standalone black-box case generator for Experiment 4.

Only the Python standard library is imported. The generator consumes the
public CandidatePlan JSON Schema, public example plans, and frozen objectives.
It must not inspect TrustAero code, diagnostics, tests, or prior mutators.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import random
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _rename_and_shuffle(plan: dict[str, Any], index: int, rng: random.Random) -> dict[str, Any]:
    result = copy.deepcopy(plan)
    mapping = {
        str(operator["operator_id"]): f"bb{index:04d}-n{position:02d}"
        for position, operator in enumerate(result["operators"])
    }
    for operator in result["operators"]:
        operator["operator_id"] = mapping[str(operator["operator_id"])]
        operator["inputs"] = [mapping[str(item)] for item in operator.get("inputs", [])]
    result["output_operator"] = mapping[str(result["output_operator"])]
    rng.shuffle(result["operators"])
    result["plan_id"] = f"bb-valid-{index:04d}"
    return result


def _unsafe(plan: dict[str, Any], objective: str, index: int) -> dict[str, Any]:
    result = copy.deepcopy(plan)
    result["plan_id"] = f"bb-unsafe-{objective}-{index:04d}"
    operators = result.get("operators", [])
    if objective == "missing_operator_collection":
        result.pop("operators", None)
    elif objective == "unexpected_plan_property":
        result["agent_override"] = {"approved": True}
    elif objective == "unsupported_schema_version":
        result["schema_version"] = "99.0"
    elif objective == "unknown_operator_kind":
        operators[-1]["operator_type"] = "RemotePythonEval"
    elif objective == "duplicate_operator_identity":
        operators[-1]["operator_id"] = operators[0]["operator_id"]
        result["output_operator"] = operators[0]["operator_id"]
    elif objective == "dangling_input_reference":
        operators[-1]["inputs"] = [f"absent-{index}"]
    elif objective == "cyclic_dataflow":
        operators[0]["inputs"] = [operators[-1]["operator_id"]]
    elif objective == "unknown_declared_output":
        result["output_operator"] = f"absent-output-{index}"
    elif objective == "unreachable_subgraph":
        operators.append(
            {
                "operator_type": "ScanSource",
                "operator_id": f"orphan-{index}",
                "inputs": [],
                "dataset": "earthquakes",
                "snapshot": None,
            }
        )
    elif objective == "invalid_operator_arity":
        operators[-1]["inputs"] = []
    elif objective == "unknown_dataset_reference":
        operators[0]["dataset"] = f"shadow_dataset_{index}"
    elif objective == "unknown_field_reference":
        target = operators[-1]
        if target.get("operator_type") == "Project":
            target["fields"] = [f"secret_field_{index}"]
        else:
            result["requested_output"]["fields"] = [f"secret_field_{index}"]
    elif objective == "unavailable_snapshot":
        operators[0]["snapshot"] = f"future-{index}"
    elif objective == "missing_declared_purpose":
        result["request_context"]["purpose"] = None
    elif objective == "unauthorized_subject_role":
        result["request_context"]["subject"]["role"] = f"external-{index}"
    elif objective == "unknown_requested_output":
        result["requested_output"]["fields"] = [f"non_output_{index}"]
    else:
        raise ValueError(f"unsupported objective: {objective}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--accept-seed", type=Path, required=True)
    parser.add_argument("--rewrite-seed", type=Path, required=True)
    parser.add_argument("--objectives", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    schema = _load(args.schema)
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise ValueError("unexpected public CandidatePlan schema")
    accept_seed = _load(args.accept_seed)
    rewrite_seed = _load(args.rewrite_seed)
    protocol = _load(args.objectives)
    rng = random.Random(int(protocol["seed"]))
    cases: list[dict[str, Any]] = []
    per_objective = int(protocol["unsafe_cases_per_objective"])
    seeds = (accept_seed, rewrite_seed)
    for objective in protocol["unsafe_objectives"]:
        for local_index in range(per_objective):
            seed_name = "accept" if local_index % 2 == 0 else "rewrite"
            cases.append(
                {
                    "case_id": f"unsafe-{objective}-{local_index:03d}",
                    "expected_class": "unsafe",
                    "expected_seed_status": ("ACCEPT" if seed_name == "accept" else "REWRITE"),
                    "objective": objective,
                    "plan": _unsafe(seeds[local_index % 2], str(objective), local_index),
                }
            )
    controls_per_seed = int(protocol["valid_controls_per_seed"])
    controls = (
        ("accept", accept_seed, "ACCEPT"),
        ("rewrite", rewrite_seed, "REWRITE"),
    )
    for seed_name, seed, expected in controls:
        for local_index in range(controls_per_seed):
            index = local_index + (0 if seed_name == "accept" else controls_per_seed)
            cases.append(
                {
                    "case_id": f"valid-{seed_name}-{local_index:03d}",
                    "expected_class": "valid",
                    "expected_seed_status": expected,
                    "objective": ("graph_preserving_identifier_renaming_and_list_permutation"),
                    "plan": _rename_and_shuffle(seed, index, rng),
                }
            )
    rng.shuffle(cases)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "experiment_id": protocol["experiment_id"],
        "seed": protocol["seed"],
        "case_count": len(cases),
        "unsafe_count": sum(row["expected_class"] == "unsafe" for row in cases),
        "valid_count": sum(row["expected_class"] == "valid" for row in cases),
        "input_hashes": {
            "candidate_plan_schema": _digest(args.schema),
            "accept_seed": _digest(args.accept_seed),
            "rewrite_seed": _digest(args.rewrite_seed),
            "attack_objectives": _digest(args.objectives),
        },
        "cases": cases,
    }
    args.output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: manifest[key] for key in ("case_count", "unsafe_count", "valid_count")}))


if __name__ == "__main__":
    main()
