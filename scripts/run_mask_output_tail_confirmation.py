"""Run the frozen, isolated mask-output tail confirmation experiment."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.mask_output_tail_confirmation import (
    load_mask_output_tail_config,
    run_mask_output_tail_confirmation,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="experiments/configs/mask_output_tail_confirmation_v1.json",
    )
    parser.add_argument("--resume", nargs="?", const="latest", metavar="RUN_ID")
    parser.add_argument("--progress", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = root / config_path
    config = load_mask_output_tail_config(config_path)
    resume = args.resume
    if resume == "latest":
        latest = root / config.results_dir / "latest_run.json"
        resume = str(json.loads(latest.read_text(encoding="utf-8"))["run_id"])

    def progress(done: int, total: int, label: str, elapsed: float) -> None:
        if args.progress:
            eta = elapsed / done * (total - done) if done else 0.0
            print(
                f"[Tail {done:03d}/{total:03d}] {label} elapsed={elapsed:.1f}s eta={eta:.1f}s",
                flush=True,
            )

    try:
        output = run_mask_output_tail_confirmation(
            config,
            project_root=root,
            resume_run_id=resume,
            progress_callback=progress,
        )
    except KeyboardInterrupt:
        print("\nStopped safely. Resume with --resume latest.", flush=True)
        raise SystemExit(130) from None
    result = json.loads((output / "tail_confirmation.json").read_text(encoding="utf-8"))
    print(f"Status: {result['status']}", flush=True)
    print(f"Conclusion: {result['scientific_conclusion']}", flush=True)
    print(f"Result: {output / 'tail_confirmation.json'}", flush=True)


if __name__ == "__main__":
    main()
