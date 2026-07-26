"""Run or resume the frozen V3.1 real-distribution transfer measurement."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from trustaero.experiments.real_governed_checkpoint_transfer import (
    load_real_checkpoint_transfer_config,
    run_real_governed_checkpoint_transfer,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        default="experiments/configs/real_governed_checkpoint_transfer_v1.json",
    )
    parser.add_argument(
        "--resume",
        nargs="?",
        const="latest",
        help="Resume a run ID, or the latest run when no ID is supplied.",
    )
    parser.add_argument("--progress", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config = load_real_checkpoint_transfer_config(root / args.config)
    resume = args.resume
    if resume == "latest":
        latest = (root / config.results_dir / "latest_run.json").read_text(encoding="utf-8")
        import json

        resume = str(json.loads(latest)["run_id"])

    def progress(done: int, total: int, label: str, elapsed: float) -> None:
        if not args.progress:
            return
        eta = elapsed / done * (total - done) if done else 0.0
        print(
            f"[Real-EA1 {done:04d}/{total:04d}] {label} elapsed={elapsed:.1f}s eta={eta:.1f}s",
            flush=True,
        )

    started = time.perf_counter()
    output = run_real_governed_checkpoint_transfer(
        config,
        project_root=root,
        resume_run_id=resume,
        progress_callback=progress,
    )
    print(f"Completed in {time.perf_counter() - started:.1f}s", flush=True)
    print(f"Result: {output / 'summary.json'}", flush=True)


if __name__ == "__main__":
    main()
