"""Run exhaustive one-, two-, and three-way mutations across registered plan shapes."""

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

PROTOCOL = Path("experiments/frozen/multifixture_plan_mutation_protocol_v1_20260810.json")
RESULTS = Path("results/multifixture_plan_mutation_v1")
JsonObject = dict[str, Any]


def decide(plan: JsonObject, layer: str, policy, catalog) -> JsonObject:
    try:
        result = _layer_decision(layer, deepcopy(plan), policy, catalog)
        return {
            "actual_executable": result.executable,
            "outcome": result.outcome,
            "reason_codes": list(result.reason_codes),
            "exception": None,
        }
    except Exception as error:
        return {
            "actual_executable": False,
            "outcome": "EXCEPTION",
            "reason_codes": [type(error).__name__],
            "exception": f"{type(error).__name__}: {error}",
        }


def compose(base: JsonObject, mutations: tuple[str, ...], rng) -> JsonObject:
    plan = deepcopy(base)
    for mutation in mutations:
        plan = _mutate_fuzz_plan(plan, mutation, rng)
    return plan


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--progress", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    protocol = _object(root / PROTOCOL)
    rng = random.Random(int(protocol["seed"]))
    mutations = tuple(protocol["generic_unsafe_mutations"])
    combinations = tuple(
        combo
        for order in protocol["composition_orders"]
        for combo in itertools.combinations(mutations, int(order))
    )
    rows: list[JsonObject] = []
    preflight: list[JsonObject] = []
    case_index = 0
    for fixture_index, fixture in enumerate(protocol["fixtures"], start=1):
        base = _object(root / fixture["plan"])
        policy = PolicySet.model_validate(_object(root / fixture["policy"]))
        catalog = InMemoryCatalog(
            CatalogDocument.model_validate(_object(root / fixture["catalog"]))
        )
        base_decision = decide(base, "trustaero_full", policy, catalog)
        preflight.append({"fixture_id": fixture["id"], **base_decision})
        if not base_decision["actual_executable"] or base_decision["exception"]:
            raise SystemExit(
                f"Fixture preflight failed: {fixture['id']} "
                f"{base_decision['outcome']} {base_decision['reason_codes']}"
            )
        for mutation_set in combinations:
            case_index += 1
            plan = compose(base, mutation_set, rng)
            label = "+".join(mutation_set)
            for layer in VALIDATOR_LAYERS:
                rows.append(
                    {
                        "case_id": f"MF-{case_index:05d}",
                        "fixture_id": fixture["id"],
                        "plan_path": fixture["plan"],
                        "mutation_set": label,
                        "mutation_order": len(mutation_set),
                        "unsafe_mutation": True,
                        "layer": layer,
                        "expected_executable": False,
                        **decide(plan, layer, policy, catalog),
                    }
                )
        controls = int(protocol["valid_control_count_per_fixture"])
        valid_mutations = tuple(protocol["valid_controls"])
        for control_index in range(controls):
            case_index += 1
            mutation = valid_mutations[control_index % len(valid_mutations)]
            plan = _mutate_fuzz_plan(base, mutation, rng)
            for layer in VALIDATOR_LAYERS:
                rows.append(
                    {
                        "case_id": f"MF-{case_index:05d}",
                        "fixture_id": fixture["id"],
                        "plan_path": fixture["plan"],
                        "mutation_set": mutation,
                        "mutation_order": 0,
                        "unsafe_mutation": False,
                        "layer": layer,
                        "expected_executable": True,
                        **decide(plan, layer, policy, catalog),
                    }
                )
        if args.progress:
            print(
                f"[{fixture_index:02d}/{len(protocol['fixtures']):02d}] {fixture['id']} complete",
                flush=True,
            )
    summaries = _summarize_layer_rows(rows)
    fixture_findings = []
    for fixture in protocol["fixtures"]:
        fixture_rows = [
            row
            for row in rows
            if row["fixture_id"] == fixture["id"] and row["layer"] == "trustaero_full"
        ]
        fixture_findings.append(
            {
                "fixture_id": fixture["id"],
                "unsafe_acceptance_count": sum(
                    row["unsafe_mutation"] and row["actual_executable"] for row in fixture_rows
                ),
                "false_reject_count": sum(
                    (not row["unsafe_mutation"]) and (not row["actual_executable"])
                    for row in fixture_rows
                ),
                "exception_count": sum(row["outcome"] == "EXCEPTION" for row in fixture_rows),
            }
        )
    for layer in VALIDATOR_LAYERS:
        layer_rows = [row for row in rows if row["layer"] == layer]
        summaries[layer]["exception_count"] = sum(
            row["outcome"] == "EXCEPTION" for row in layer_rows
        )
    full = summaries["trustaero_full"]
    passed = (
        full["unsafe_acceptance_count"] == 0
        and full["false_reject_count"] == 0
        and full["exception_count"] == 0
    )
    unsafe_count = len(combinations) * len(protocol["fixtures"])
    control_count = int(protocol["valid_control_count_per_fixture"]) * len(protocol["fixtures"])
    if (
        unsafe_count != int(protocol["expected_total_unsafe_cases"])
        or control_count != int(protocol["expected_total_controls"])
        or unsafe_count + control_count != int(protocol["expected_total_cases"])
    ):
        raise SystemExit("Frozen multifixture denominator changed")
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output = root / RESULTS / run_id
    output.mkdir(parents=True, exist_ok=False)
    fields = [
        "case_id",
        "fixture_id",
        "plan_path",
        "mutation_set",
        "mutation_order",
        "unsafe_mutation",
        "layer",
        "expected_executable",
        "actual_executable",
        "outcome",
        "reason_codes",
        "exception",
    ]
    with (output / "cases.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "reason_codes": "|".join(row["reason_codes"])})
    summary = {
        "schema_version": 1,
        "status": "PASS_MULTIFIXTURE_FAIL_CLOSED" if passed else "FAIL_MULTIFIXTURE_FAIL_CLOSED",
        "protocol_path": PROTOCOL.as_posix(),
        "fixture_count": len(protocol["fixtures"]),
        "preflight": preflight,
        "unsafe_mutation_count": len(mutations),
        "composition_orders": protocol["composition_orders"],
        "combination_count_per_fixture": len(combinations),
        "unsafe_case_count": unsafe_count,
        "valid_control_count": control_count,
        "total_case_count": unsafe_count + control_count,
        "validator_decision_count": len(rows),
        "summaries": summaries,
        "fixture_findings": fixture_findings,
        "claim_boundary": protocol["claim_boundary"],
    }
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"Status: {summary['status']}")
    print(
        f"Cases: {summary['total_case_count']} across "
        f"{summary['fixture_count']} fixtures; decisions={len(rows)}"
    )
    print(f"Result: {output / 'summary.json'}")


if __name__ == "__main__":
    main()
