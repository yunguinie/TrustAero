"""Run or resume the DuckDB EA-0 execution-flow mechanism audit."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from trustaero.experiments.execution_flow_audit import (
    load_execution_flow_audit_config,
    run_execution_flow_audit,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="experiments/configs/execution_flow_audit_pilot_v1.json",
        help="Versioned EA-0 JSON configuration.",
    )
    parser.add_argument(
        "--resume",
        nargs="?",
        const="latest",
        metavar="RUN_ID",
        help="Resume RUN_ID, or the latest run when no ID is supplied.",
    )
    parser.add_argument(
        "--progress",
        action="store_true",
        help="Print completed units, elapsed seconds, and estimated time remaining.",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = root / config_path
    config = load_execution_flow_audit_config(config_path)
    resume_run_id = args.resume
    if resume_run_id == "latest":
        latest_path = root / config.results_dir / "latest_run.json"
        if not latest_path.is_file():
            raise SystemExit(f"No resumable EA-0 run found at {latest_path}")
        resume_run_id = str(json.loads(latest_path.read_text(encoding="utf-8"))["run_id"])

    def progress(done: int, total: int, label: str, elapsed: float) -> None:
        if not args.progress:
            return
        eta = elapsed / done * (total - done) if done else 0.0
        print(
            f"[EA-0 {done:02d}/{total:02d}] {label} elapsed={elapsed:.1f}s eta={eta:.1f}s",
            flush=True,
        )

    started = time.perf_counter()
    try:
        output = run_execution_flow_audit(
            config,
            project_root=root,
            resume_run_id=resume_run_id,
            progress_callback=progress,
        )
    except KeyboardInterrupt:
        print("\nEA-0 stopped safely; resume with --resume latest.", flush=True)
        raise SystemExit(130) from None
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    print(f"EA-0 completed in {time.perf_counter() - started:.1f}s", flush=True)
    print(f"Output: {output}", flush=True)
    print(f"Status: {summary['status']}", flush=True)


if __name__ == "__main__":
    main()
