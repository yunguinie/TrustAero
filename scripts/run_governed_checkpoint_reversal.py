"""Run the frozen EA-1 governed-checkpoint reversal discovery pilot."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from trustaero.experiments.governed_checkpoint_reversal import (
    load_governed_checkpoint_config,
    run_governed_checkpoint_reversal,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="experiments/configs/governed_checkpoint_reversal_v1.json",
    )
    parser.add_argument("--resume", nargs="?", const="latest", metavar="RUN_ID")
    parser.add_argument("--progress", action="store_true")
    parser.add_argument(
        "--progress-every",
        type=int,
        default=10,
        help="Print one progress update per N complete paired blocks.",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = root / config_path
    config = load_governed_checkpoint_config(config_path)
    resume = args.resume
    if resume == "latest":
        latest = root / config.results_dir / "latest_run.json"
        resume = str(json.loads(latest.read_text(encoding="utf-8"))["run_id"])
    started = time.perf_counter()

    def progress(done: int, total: int, label: str, elapsed: float) -> None:
        if args.progress and (done == total or done % max(1, args.progress_every) == 0):
            eta = elapsed / done * (total - done) if done else 0.0
            print(
                f"[EA-1 {done:03d}/{total:03d}] {label} elapsed={elapsed:.1f}s eta={eta:.1f}s",
                flush=True,
            )

    try:
        output = run_governed_checkpoint_reversal(
            config,
            project_root=root,
            resume_run_id=resume,
            progress_callback=progress,
        )
    except KeyboardInterrupt:
        print("\nEA-1 stopped safely. Resume with --resume latest.", flush=True)
        raise SystemExit(130) from None
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    print(f"EA-1 completed in {time.perf_counter() - started:.1f}s", flush=True)
    print(f"Status: {summary['status']}", flush=True)
    print(f"Reversal: {summary['reversal_discovery']}", flush=True)
    print(
        "Winners: "
        f"policy-first={summary['policy_first_winner_count']} "
        f"query-first={summary['query_first_winner_count']} "
        f"inconclusive={summary['inconclusive_count']}",
        flush=True,
    )
    print(f"Result: {output / 'summary.json'}", flush=True)


if __name__ == "__main__":
    main()
