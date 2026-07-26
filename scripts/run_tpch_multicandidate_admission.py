"""Run the balanced Q3/Q10 SF1 multi-candidate admission."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.tpch_multicandidate_admission import (
    load_tpch_multicandidate_admission_config,
    run_tpch_multicandidate_admission,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        default="experiments/configs/tpch_multicandidate_admission_v1.json",
    )
    parser.add_argument("--progress", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config = load_tpch_multicandidate_admission_config(root / args.config)

    def report(done: int, total: int, label: str, elapsed: float, eta: float) -> None:
        if args.progress:
            print(
                f"[TPC-H Admission {done:03d}/{total:03d}] {label} "
                f"elapsed={elapsed:.1f}s eta={eta:.1f}s",
                flush=True,
            )

    result_path = run_tpch_multicandidate_admission(
        root, config, progress=report if args.progress else None
    )
    result = json.loads(result_path.read_text(encoding="utf-8"))
    print(f"Status: {result['status']}", flush=True)
    print(f"Winners: {result['singleton_winners']}", flush=True)
    print(f"Result: {result_path}", flush=True)


if __name__ == "__main__":
    main()
