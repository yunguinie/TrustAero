"""Run and analyze the preregistered BTS 2025 temporal candidate-family test."""

from __future__ import annotations

import argparse
from pathlib import Path

from trustaero.experiments.bts_2025_temporal_holdout import (
    analyze_temporal_holdout,
    run_temporal_holdout,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--protocol",
        type=Path,
        default=Path(
            "experiments/frozen/bts_2025_multijoin_temporal_holdout_protocol_v1_20260813.json"
        ),
    )
    args = parser.parse_args()
    protocol = args.protocol if args.protocol.is_absolute() else PROJECT_ROOT / args.protocol
    run_dir = run_temporal_holdout(protocol, project_root=PROJECT_ROOT)
    summary = analyze_temporal_holdout(run_dir)
    print(summary["status"])
    print(summary["scientific_conclusion"])
    print(run_dir)
    return 0 if str(summary["status"]).startswith("PASS") else 2


if __name__ == "__main__":
    raise SystemExit(main())
