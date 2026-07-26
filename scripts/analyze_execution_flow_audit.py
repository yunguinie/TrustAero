"""Apply the frozen paired inference protocol to a completed EA-0 run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.execution_flow_inference import (
    analyze_execution_flow_inference,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", nargs="?", help="Completed EA-0 formal run directory.")
    parser.add_argument(
        "--protocol",
        default="experiments/frozen/execution_flow_audit_formal_protocol_20260722.json",
        help="Protocol frozen before the formal run.",
    )
    parser.add_argument("--progress", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.run_dir:
        run_dir = Path(args.run_dir)
        if not run_dir.is_absolute():
            run_dir = root / run_dir
    else:
        latest = json.loads(
            (root / "results/execution_flow_audit_formal/latest_run.json").read_text(
                encoding="utf-8"
            )
        )
        run_dir = root / "results/execution_flow_audit_formal" / str(latest["run_id"])
    protocol = Path(args.protocol)
    if not protocol.is_absolute():
        protocol = root / protocol

    def progress(done: int, total: int, label: str) -> None:
        if args.progress and (done == 1 or done == total or done % 10 == 0):
            print(f"[EA-0 inference {done:03d}/{total:03d}] {label}", flush=True)

    result = analyze_execution_flow_inference(
        run_dir,
        protocol,
        progress_callback=progress,
    )
    print(f"Status: {result['status']}", flush=True)
    print(f"Families: {result['family_count']}", flush=True)
    print(f"Pairwise comparisons: {result['pairwise_comparison_count']}", flush=True)
    print(f"Result: {run_dir / 'paired_inference.json'}", flush=True)


if __name__ == "__main__":
    main()
