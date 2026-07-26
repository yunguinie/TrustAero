"""Build the content-addressed cross-workload candidate evidence matrix."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.benchmark_evidence import (
    build_benchmark_evidence_matrix,
    load_benchmark_evidence_config,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("experiments/configs/benchmark_evidence_matrix_v1.json"),
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config = load_benchmark_evidence_config(root / args.config)
    print("[1/2] verifying frozen evidence bindings", flush=True)
    result = build_benchmark_evidence_matrix(config, project_root=root)
    print("[2/2] evidence matrix complete", flush=True)
    print(json.dumps(result, indent=2, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
