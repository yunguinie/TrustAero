"""Run the frozen governed Join-Aggregate winner-diversity admission."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from trustaero.experiments.governed_aggregate_admission import (
    load_governed_aggregate_admission_config,
    run_governed_aggregate_admission,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="experiments/configs/governed_aggregate_admission_v1.json",
    )
    parser.add_argument("--progress", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = root / config_path
    config = load_governed_aggregate_admission_config(config_path)

    def report(done: int, total: int, label: str, elapsed: float) -> None:
        if not args.progress or (done != total and done % 10):
            return
        eta = elapsed / done * (total - done) if done else 0.0
        print(
            f"[Aggregate-Admission {done:04d}/{total:04d}] {label} "
            f"elapsed={elapsed:.1f}s eta={eta:.1f}s",
            flush=True,
        )

    started = time.perf_counter()
    output = run_governed_aggregate_admission(
        config,
        project_root=root,
        config_path=config_path,
        progress=report,
    )
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    print(f"Completed in {time.perf_counter() - started:.1f}s")
    print(f"Status: {summary['status']}")
    print(f"Winners: {summary['singleton_winner_counts']}")
    print(f"Conclusive rate: {summary['conclusive_scenario_rate']:.3f}")
    print(f"Result: {(output / 'summary.json').resolve()}")


if __name__ == "__main__":
    main()
