"""Run the frozen three-candidate governed-checkpoint development pilot."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from trustaero.experiments.governed_checkpoint_multicandidate_pilot import (
    load_multicandidate_pilot_config,
    run_multicandidate_pilot,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="experiments/configs/governed_checkpoint_multicandidate_pilot_v1.json",
    )
    parser.add_argument("--resume", nargs="?", const="latest", metavar="RUN_ID")
    parser.add_argument("--progress", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = root / config_path
    config = load_multicandidate_pilot_config(config_path)
    resume = args.resume
    if resume == "latest":
        latest = root / config.results_dir / "latest_run.json"
        resume = str(json.loads(latest.read_text(encoding="utf-8"))["run_id"])
    started = time.perf_counter()

    def progress(done: int, total: int, label: str, elapsed: float) -> None:
        if not args.progress:
            return
        # Keep the terminal informative without printing all 1,080 blocks.
        if done != total and done % 10 != 0:
            return
        eta = elapsed / done * (total - done) if done else 0.0
        print(
            f"[MC-Admission {done:04d}/{total:04d}] {label} elapsed={elapsed:.1f}s eta={eta:.1f}s",
            flush=True,
        )

    try:
        output = run_multicandidate_pilot(
            config,
            project_root=root,
            resume_run_id=resume,
            progress_callback=progress,
        )
    except KeyboardInterrupt:
        print("\nStopped safely. Resume with --resume latest.", flush=True)
        raise SystemExit(130) from None

    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    print(f"Completed in {time.perf_counter() - started:.1f}s", flush=True)
    print(f"Status: {summary['status']}", flush=True)
    print(f"Winners: {summary['singleton_winner_counts']}", flush=True)
    print(
        f"Conclusive scenario rate={summary['conclusive_scenario_rate']:.3f}",
        flush=True,
    )
    print(f"Result: {output / 'summary.json'}", flush=True)


if __name__ == "__main__":
    main()
