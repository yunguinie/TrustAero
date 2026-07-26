"""Run the bounded record-lineage integrity and overhead pilot."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from trustaero.experiments.record_lineage_pilot import (
    load_record_lineage_pilot_config,
    run_record_lineage_pilot,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="experiments/configs/record_lineage_pilot_v1.json",
    )
    parser.add_argument("--progress", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config_path = root / args.config
    config = load_record_lineage_pilot_config(config_path)

    def report(done: int, total: int, label: str, elapsed: float) -> None:
        if not args.progress:
            return
        eta = elapsed / done * (total - done) if done else 0.0
        print(
            f"[Record-Lineage {done:03d}/{total:03d}] "
            f"{label} elapsed={elapsed:.1f}s eta={eta:.1f}s",
            flush=True,
        )

    started = time.perf_counter()
    output = run_record_lineage_pilot(
        config,
        project_root=root,
        config_path=config_path,
        progress=report,
    )
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    print(f"Completed in {time.perf_counter() - started:.1f}s")
    print(f"Status: {summary['status']}")
    for unit in summary["unit_summaries"]:
        print(
            f"n={unit['row_count']}: direct={unit['direct_median_ms']:.3f}ms "
            f"record={unit['record_median_ms']:.3f}ms "
            f"capture={unit['capture_median_ms']:.3f}ms "
            f"verify={unit['verification_median_ms']:.3f}ms"
        )
    print(f"Result: {(output / 'summary.json').resolve()}")


if __name__ == "__main__":
    main()
