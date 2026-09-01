"""Run a deterministic pairwise plan-mutation robustness analysis.

This supplemental experiment composes two distinct unsafe mutations on the same
candidate plan.  It reuses the frozen validator layers and does not call a
model API, execute DuckDB, refit an optimizer, or modify the paper's frozen
results.  The output is intended to test totality and fail-closed behavior
under compounded corruption, not to estimate open-world attack probability.
"""

from __future__ import annotations

import argparse
import csv
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


def _compose_pair(base: JsonObject, pair: tuple[str, str], rng: random.Random) -> JsonObject:
    """Apply two distinct unsafe mutations in a fixed, deterministic order."""

    plan = _mutate_fuzz_plan(base, pair[0], rng)
    return _mutate_fuzz_plan(plan, pair[1], rng)


def _write_csv(path: Path, rows: list[JsonObject]) -> None:
    fieldnames = [
        "case_id",
        "mutation_pair",
        "unsafe_mutation",
        "layer",
        "expected_executable",
        "actual_executable",
        "outcome",
        "reason_codes",
        "exception",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    **row,
                    "reason_codes": "|".join(row["reason_codes"]),
                }
            )


def run(root: Path, *, seed: int, case_count: int, output_root: Path) -> Path:
    config = _object(root / "experiments/configs/paper_gap_closure_v1.json")
    policy = PolicySet.model_validate(_object(root / config["fuzz_policy_path"]))
    catalog = InMemoryCatalog(
        CatalogDocument.model_validate(_object(root / config["fuzz_catalog_path"]))
    )
    bases = [_object(root / path) for path in config["fuzz_plan_paths"]]
    pairs = [
        (left, right)
        for index, left in enumerate(UNSAFE_MUTATIONS)
        for right in UNSAFE_MUTATIONS[index + 1 :]
    ]
    rng = random.Random(seed)
    unsafe_target = round(case_count * 0.8)
    rows: list[JsonObject] = []

    for index in range(case_count):
        unsafe = index < unsafe_target
        if unsafe:
            pair = pairs[index % len(pairs)]
            plan = _compose_pair(rng.choice(bases), pair, rng)
            label = "+".join(pair)
        else:
            mutation = VALID_MUTATIONS[index % len(VALID_MUTATIONS)]
            plan = _mutate_fuzz_plan(rng.choice(bases), mutation, rng)
            label = mutation
        expected = not unsafe
        for layer in VALIDATOR_LAYERS:
            try:
                decision = _layer_decision(layer, deepcopy(plan), policy, catalog)
                exception = None
            except Exception as error:  # pragma: no cover - measured in the run
                decision = type(
                    "Decision",
                    (),
                    {
                        "executable": False,
                        "outcome": "EXCEPTION",
                        "reason_codes": (type(error).__name__,),
                    },
                )()
                exception = f"{type(error).__name__}: {error}"
            rows.append(
                {
                    "case_id": f"PW-{index + 1:05d}",
                    "mutation_pair": label,
                    "unsafe_mutation": unsafe,
                    "layer": layer,
                    "expected_executable": expected,
                    "actual_executable": decision.executable,
                    "outcome": decision.outcome,
                    "reason_codes": list(decision.reason_codes),
                    "exception": exception,
                }
            )

    summaries = _summarize_layer_rows(rows)
    for layer in VALIDATOR_LAYERS:
        layer_rows = [row for row in rows if row["layer"] == layer]
        summaries[layer]["exception_count"] = sum(
            row["outcome"] == "EXCEPTION" for row in layer_rows
        )
        summaries[layer]["unsafe_mutation_pair_count"] = len(
            {row["mutation_pair"] for row in layer_rows if row["unsafe_mutation"]}
        )
        summaries[layer]["valid_control_type_count"] = len(
            {row["mutation_pair"] for row in layer_rows if not row["unsafe_mutation"]}
        )

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output = root / output_root / timestamp
    output.mkdir(parents=True, exist_ok=False)
    _write_csv(output / "pairwise_fuzz_cases.csv", rows)
    summary = {
        "status": "PASS_PAIRWISE_FAIL_CLOSED"
        if all(
            summaries[layer]["unsafe_acceptance_count"] == 0
            and summaries[layer]["exception_count"] == 0
            for layer in ("trustaero_full",)
        )
        else "FAIL_PAIRWISE_FAIL_CLOSED",
        "analysis_type": "deterministic_pairwise_plan_mutation",
        "seed": seed,
        "case_count": case_count,
        "unsafe_case_count": unsafe_target,
        "valid_control_count": case_count - unsafe_target,
        "pair_count": len(pairs),
        "summaries": summaries,
    }
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    report_lines = [
        "# Pairwise plan-mutation robustness",
        "",
        "This frozen supplemental run composes two distinct unsafe mutations.",
        "It does not call an Agent API, execute DuckDB, or refit any model.",
        "",
        "The unsafe matrix covers 78 distinct mutation pairs; valid controls use "
        "2 separate semantics-preserving mutation types.",
        "",
        "| Layer | Unsafe accepts | False rejects | Exceptions | Unsafe pairs | Control types |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for layer in VALIDATOR_LAYERS:
        item = summaries[layer]
        report_lines.append(
            f"| {layer} | {item['unsafe_acceptance_count']} | "
            f"{item['false_reject_count']} | {item['exception_count']} | "
            f"{item['unsafe_mutation_pair_count']} | "
            f"{item['valid_control_type_count']} |"
        )
    (output / "report.md").write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    return output


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=20260807)
    parser.add_argument("--case-count", type=int, default=5000)
    parser.add_argument(
        "--output-root",
        default="results/paper_gap_closure_pairwise_v1",
    )
    args = parser.parse_args()
    output = run(
        root,
        seed=args.seed,
        case_count=args.case_count,
        output_root=Path(args.output_root),
    )
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    print(f"Status: {summary['status']}")
    print(f"Result: {output / 'summary.json'}")


if __name__ == "__main__":
    main()
