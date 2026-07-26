"""Audit deployable development Oracle labels with paired confidence sets."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from trustaero.experiments.execution_aware_oracle_stability import (
    audit_execution_aware_oracle_stability,
    load_oracle_stability_config,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="experiments/configs/execution_aware_oracle_stability_v1.json",
    )
    parser.add_argument("--progress", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = root / config_path
    config = load_oracle_stability_config(config_path)
    started = time.perf_counter()

    def progress(done: int, total: int, label: str) -> None:
        if args.progress:
            elapsed = time.perf_counter() - started
            eta = elapsed / done * (total - done) if done else 0.0
            print(
                f"[Oracle audit {done:03d}/{total:03d}] {label} "
                f"elapsed={elapsed:.1f}s eta={eta:.1f}s",
                flush=True,
            )

    output = audit_execution_aware_oracle_stability(
        config, project_root=root, progress_callback=progress
    )
    result_path = output / "oracle_stability_audit.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    metrics = result["selection_metrics"]
    print(f"Status: {result['status']}", flush=True)
    print(
        "Families: "
        f"{result['family_count']} "
        f"seed-label-disagreement={result['families_with_seed_oracle_disagreement']} "
        f"singleton-confidence-winner={result['singleton_confidence_winner_count']}",
        flush=True,
    )
    print(
        "Confidence-set hit: "
        f"model={metrics['model_confidence_set_hit_rate']:.3f} "
        f"fixed={metrics['fixed_confidence_set_hit_rate']:.3f}",
        flush=True,
    )
    print(f"Result: {result_path}", flush=True)


if __name__ == "__main__":
    main()
