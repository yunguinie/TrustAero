"""Run and analyze the frozen full-month BTS natural multi-Join protocol."""

from __future__ import annotations

import argparse
from pathlib import Path

from trustaero.experiments.bts_multijoin_formal import (
    analyze_bts_multijoin_formal,
    load_bts_multijoin_formal_config,
    run_bts_multijoin_formal,
)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="experiments/configs/bts_multijoin_formal_v1.json",
    )
    parser.add_argument("--progress", action="store_true")
    args = parser.parse_args()
    config = load_bts_multijoin_formal_config(root / args.config)
    try:
        run_dir = run_bts_multijoin_formal(
            config,
            project_root=root,
            show_progress=args.progress,
        )
    except KeyboardInterrupt:
        print("\nBTS multi-Join stopped; completed block metadata remains on E drive.")
        raise SystemExit(130) from None
    acceptance = analyze_bts_multijoin_formal(run_dir)
    print(f"BTS multi-Join {acceptance['status']}: {run_dir / 'report.md'}")
    if acceptance["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
