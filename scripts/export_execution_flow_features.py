"""Export label-free Execution-Aware features from a completed EA-0 run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.execution_flow_features import (
    export_execution_flow_features,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", nargs="?")
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
    output = export_execution_flow_features(run_dir)
    print(f"Execution-Aware features: {output}", flush=True)


if __name__ == "__main__":
    main()
