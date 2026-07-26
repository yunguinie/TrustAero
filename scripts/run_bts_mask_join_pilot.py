"""Run the non-paper BTS Mask/Join paired timing-protocol validation."""

from __future__ import annotations

import argparse
from pathlib import Path

from trustaero.experiments.bts_mask_join_analysis import analyze_bts_mask_join_pilot
from trustaero.experiments.bts_mask_join_pilot import (
    load_bts_mask_join_pilot_config,
    run_bts_mask_join_pilot,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="experiments/configs/bts_mask_join_paired_pilot.json",
    )
    parser.add_argument("--progress", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config = load_bts_mask_join_pilot_config(root / args.config)
    run_dir = run_bts_mask_join_pilot(
        config,
        project_root=root,
        show_progress=args.progress,
    )
    result = analyze_bts_mask_join_pilot(run_dir)
    print(f"{result['status']}: {run_dir / 'report.md'}")
    if result["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
