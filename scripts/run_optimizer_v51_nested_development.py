"""Run or resume nested Optimizer V5.1 development."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from trustaero.experiments.optimizer_v51_nested_development import (
    load_v51_nested_config,
    run_v51_nested_development,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--progress", action="store_true")
    parser.add_argument("--resume", nargs="?", const="latest", metavar="RUN_ID")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config_path = root / "experiments/configs/optimizer_v51_nested_development_v1.json"
    config = load_v51_nested_config(config_path)
    resume = args.resume
    if resume == "latest":
        latest = json.loads(
            (root / config.results_dir / "latest_run.json").read_text(encoding="utf-8")
        )
        resume = str(latest["run_id"])

    def progress(done: int, total: int, label: str, elapsed: float) -> None:
        if args.progress:
            per_fold = elapsed / done if done else 0.0
            eta = per_fold * (total - done)
            print(
                f"[{done:02d}/{total:02d}] {label} | elapsed={elapsed:.1f}s eta={eta:.1f}s",
                flush=True,
            )

    started = time.perf_counter()
    output = run_v51_nested_development(
        config,
        project_root=root,
        resume_run_id=resume,
        progress_callback=progress,
    )
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    print(f"V5.1 nested development completed in {time.perf_counter() - started:.1f}s")
    print(f"Output: {output}")
    print(f"Status: {summary['status']}")


if __name__ == "__main__":
    main()
