"""Run the frozen governed multi-operator winner-diversity admission."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from trustaero.experiments.governed_pipeline_admission import (
    load_governed_pipeline_admission_config,
    run_governed_pipeline_admission,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="experiments/configs/governed_pipeline_semantic_admission_v3.json",
    )
    parser.add_argument("--resume", nargs="?", const="latest")
    parser.add_argument("--progress", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    requested_config = Path(args.config)
    config_path = requested_config if requested_config.is_absolute() else root / requested_config
    config = load_governed_pipeline_admission_config(config_path)
    resume = args.resume
    if resume == "latest":
        latest = root / config.results_dir / "latest_run.json"
        resume = str(json.loads(latest.read_text(encoding="utf-8"))["run_id"])

    def report(done: int, total: int, label: str, elapsed: float) -> None:
        if not args.progress or (done != total and done % 10):
            return
        eta = elapsed / done * (total - done) if done else 0.0
        print(
            f"[Pipeline-Admission {done:04d}/{total:04d}] {label} "
            f"elapsed={elapsed:.1f}s eta={eta:.1f}s",
            flush=True,
        )

    started = time.perf_counter()
    try:
        output = run_governed_pipeline_admission(
            config,
            project_root=root,
            config_path=config_path,
            resume_run_id=resume,
            progress=report,
        )
    except KeyboardInterrupt:
        print("\nStopped safely. Resume with --resume latest.", flush=True)
        raise SystemExit(130) from None
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    print(f"Completed in {time.perf_counter() - started:.1f}s")
    print(f"Status: {summary['status']}")
    print(f"Winners: {summary['singleton_winner_counts']}")
    print(f"Conclusive rate: {summary['conclusive_scenario_rate']:.3f}")
    print(f"Result: {(output / 'summary.json').resolve()}")


if __name__ == "__main__":
    main()
