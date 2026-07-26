"""Evaluate one Phase 0 run against the frozen semantic gates."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.phase0_evaluation import evaluate_phase0_run


def _latest_run(results_dir: Path) -> Path:
    runs = sorted(
        path for path in results_dir.iterdir() if path.is_dir() and (path / "cases.csv").exists()
    )
    if not runs:
        raise ValueError(f"No completed Phase 0 run under {results_dir}")
    return runs[-1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", nargs="?", default="latest")
    parser.add_argument(
        "--results-dir",
        default="results/phase0",
    )
    parser.add_argument(
        "--protocol",
        default="experiments/frozen/phase0_planner_fault_protocol_v1_20260724.json",
    )
    args = parser.parse_args()

    results_dir = Path(args.results_dir)
    run_dir = _latest_run(results_dir) if args.run_id == "latest" else results_dir / args.run_id
    result = evaluate_phase0_run(run_dir, Path(args.protocol))
    output = run_dir / "phase0_evaluation.json"
    output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    metrics = result["metrics"]
    print(f"Status: {result['status']}")
    print(
        "Correctness: "
        f"status={metrics['status_accuracy']:.3f} "
        f"reason={metrics['reason_code_accuracy']:.3f} "
        f"detection={metrics['detection_rate']:.3f} "
        f"false-reject={metrics['false_reject_rate']:.3f}"
    )
    print(
        "Median overhead: "
        f"planner={metrics['median_planner_latency_ms']:.3f}ms "
        "certificate="
        f"{metrics['median_certificate_verification_latency_ms']:.3f}ms"
    )
    print(f"Result: {output.resolve()}")


if __name__ == "__main__":
    main()
