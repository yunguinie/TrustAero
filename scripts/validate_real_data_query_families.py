"""Validate the frozen real-data query-family design without running timings."""

from __future__ import annotations

import argparse
from pathlib import Path

from trustaero.experiments.real_data_governed import _atomic_json
from trustaero.experiments.real_data_query_families import validate_query_family_protocol


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--protocol",
        default="experiments/configs/real_data_query_families_v1.json",
        help="Protocol path relative to the project root.",
    )
    parser.add_argument(
        "--output",
        default="results/query_family_protocol/validation.json",
        help="Validation output path relative to the project root.",
    )
    args = parser.parse_args()
    check = validate_query_family_protocol(root / args.protocol, project_root=root)
    output = root / args.output
    _atomic_json(output, check.model_dump(mode="json"))
    print(
        f"PASS {check.protocol_id}: semantic-ready={check.semantic_ready_count}, "
        f"design-only={check.design_only_count}, performance-ready={check.performance_ready}"
    )
    print(f"Protocol SHA-256: {check.protocol_sha256}")
    print(f"Validation record: {output}")


if __name__ == "__main__":
    main()
