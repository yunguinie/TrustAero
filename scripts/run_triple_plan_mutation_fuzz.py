"""Deterministic triple-composition plan-mutation robustness analysis."""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import random
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.catalog.models import CatalogDocument
from trustaero.experiments.paper_gap_closure import (
    VALIDATOR_LAYERS,
    _layer_decision,
    _mutate_fuzz_plan,
    _object,
    _summarize_layer_rows,
)
from trustaero.ir.models import PolicySet

JsonObject = dict[str, Any]

UNSAFE_MUTATIONS = (
    "unknown_dataset",
    "unknown_field",
    "invalid_snapshot",
    "unbound_input",
    "cyclic_self_reference",
    "unknown_output",
    "duplicate_operator_id",
    "missing_purpose",
    "unauthorized_role",
    "unknown_operator",
    "invalid_arity",
    "masked_semantic_use",
    "expression_type_mismatch",
)
VALID_MUTATIONS = ("plan_id_only", "operator_order_only")


def _compose(base: JsonObject, mutations: tuple[str, ...], rng: random.Random) -> JsonObject:
    plan = deepcopy(base)
    for mutation in mutations:
        plan = _mutate_fuzz_plan(plan, mutation, rng)
    return plan


def _decision(
    plan: JsonObject, layer: str, policy: PolicySet, catalog: InMemoryCatalog
) -> JsonObject:
    try:
        result = _layer_decision(layer, deepcopy(plan), policy, catalog)
        return {
            "actual_executable": result.executable,
            "outcome": result.outcome,
            "reason_codes": list(result.reason_codes),
            "exception": None,
        }
    except Exception as error:  # measured as a totality failure
        return {
            "actual_executable": False,
            "outcome": "EXCEPTION",
            "reason_codes": [type(error).__name__],
            "exception": f"{type(error).__name__}: {error}",
        }


def run(root: Path, *, seed: int, control_count_per_base: int, output_root: Path) -> Path:
    config = _object(root / "experiments/configs/paper_gap_closure_v1.json")
    policy = PolicySet.model_validate(_object(root / config["fuzz_policy_path"]))
    catalog = InMemoryCatalog(
        CatalogDocument.model_validate(_object(root / config["fuzz_catalog_path"]))
    )
    base_paths = list(config["fuzz_plan_paths"])
    bases = [_object(root / path) for path in base_paths]
    triples = list(itertools.combinations(UNSAFE_MUTATIONS, 3))
    rng = random.Random(seed)
    rows: list[JsonObject] = []
    case_index = 0
    for base_path, base in zip(base_paths, bases, strict=False):
        for triple in triples:
            case_index += 1
            plan = _compose(base, triple, rng)
            label = "+".join(triple)
            for layer in VALIDATOR_LAYERS:
                d = _decision(plan, layer, policy, catalog)
                rows.append(
                    {
                        "case_id": f"TR-{case_index:05d}",
                        "base_plan": str(base_path),
                        "mutation_set": label,
                        "unsafe_mutation": True,
                        "layer": layer,
                        "expected_executable": False,
                        **d,
                    }
                )
        for control_index in range(control_count_per_base):
            case_index += 1
            mutation = VALID_MUTATIONS[control_index % len(VALID_MUTATIONS)]
            plan = _mutate_fuzz_plan(base, mutation, rng)
            for layer in VALIDATOR_LAYERS:
                d = _decision(plan, layer, policy, catalog)
                rows.append(
                    {
                        "case_id": f"TR-{case_index:05d}",
                        "base_plan": str(base_path),
                        "mutation_set": mutation,
                        "unsafe_mutation": False,
                        "layer": layer,
                        "expected_executable": True,
                        **d,
                    }
                )

    summaries = _summarize_layer_rows(rows)
    for layer in VALIDATOR_LAYERS:
        layer_rows = [r for r in rows if r["layer"] == layer]
        summaries[layer]["exception_count"] = sum(r["outcome"] == "EXCEPTION" for r in layer_rows)
        summaries[layer]["unsafe_case_count"] = sum(bool(r["unsafe_mutation"]) for r in layer_rows)
        summaries[layer]["control_count"] = sum(not r["unsafe_mutation"] for r in layer_rows)

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output = root / output_root / timestamp
    output.mkdir(parents=True, exist_ok=False)
    with (output / "triple_fuzz_cases.csv").open("w", newline="", encoding="utf-8") as handle:
        fields = [
            "case_id",
            "base_plan",
            "mutation_set",
            "unsafe_mutation",
            "layer",
            "expected_executable",
            "actual_executable",
            "outcome",
            "reason_codes",
            "exception",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "reason_codes": "|".join(row["reason_codes"])})

    full = summaries["trustaero_full"]
    passed = (
        full["unsafe_acceptance_count"] == 0
        and full["false_reject_count"] == 0
        and full["exception_count"] == 0
    )
    summary = {
        "status": "PASS_TRIPLE_FAIL_CLOSED" if passed else "FAIL_TRIPLE_FAIL_CLOSED",
        "analysis_type": "deterministic_triple_plan_mutation",
        "seed": seed,
        "base_plan_count": len(bases),
        "unsafe_mutation_count": len(UNSAFE_MUTATIONS),
        "triple_combination_count": len(triples),
        "unsafe_case_count": len(triples) * len(bases),
        "valid_control_count": control_count_per_base * len(bases),
        "total_case_count": (len(triples) + control_count_per_base) * len(bases),
        "summaries": summaries,
        "frozen_scope": {
            "main_rq1_denominators_changed": False,
            "agent_api_called": False,
            "duckdb_executed": False,
            "optimizer_refit": False,
        },
    }
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    lines = [
        "# Triple-composition plan-mutation robustness",
        "",
        "Exhaustive three-way combinations of the 13 registered unsafe mutations were "
        "run on each of the two frozen base plans, with pre-registered valid controls.",
        "This supplemental run is separate from the main and pairwise denominators.",
        "",
        "| Layer | Unsafe accepts | False rejects | Exceptions |",
        "|---|---:|---:|---:|",
    ]
    for layer in VALIDATOR_LAYERS:
        item = summaries[layer]
        lines.append(
            f"| {layer} | {item['unsafe_acceptance_count']} | "
            f"{item['false_reject_count']} | {item['exception_count']} |"
        )
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=20260807)
    parser.add_argument("--control-count-per-base", type=int, default=100)
    parser.add_argument("--output-root", default="results/paper_gap_closure_triple_v1")
    args = parser.parse_args()
    output = run(
        root,
        seed=args.seed,
        control_count_per_base=args.control_count_per_base,
        output_root=Path(args.output_root),
    )
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    print(f"Status: {summary['status']}")
    print(
        f"Cases: {summary['total_case_count']} ({summary['unsafe_case_count']} unsafe, "
        f"{summary['valid_control_count']} controls)"
    )
    print(f"Result: {output / 'summary.json'}")


if __name__ == "__main__":
    main()
