"""Reanalyze a BTS Mask/Join paired pilot without rerunning queries."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.bts_mask_join_analysis import analyze_bts_mask_join_pilot


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", nargs="?")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.run_dir:
        run_dir = Path(args.run_dir).resolve()
    else:
        results = root / "results/bts_mask_join_paired_pilot"
        latest = json.loads((results / "latest_run.json").read_text(encoding="utf-8"))
        run_dir = results / str(latest["run_id"])
    result = analyze_bts_mask_join_pilot(run_dir)
    print(f"{result['status']}: {run_dir / 'report.md'}")
    if result["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
