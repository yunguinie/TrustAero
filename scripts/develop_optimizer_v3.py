"""Run the frozen Optimizer V3-v2 nested development evaluation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.interaction_optimizer import develop_interaction_optimizer


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Develop Optimizer V3 under the frozen complete-family protocol."
    )
    parser.add_argument(
        "--config",
        default="experiments/configs/optimizer_v3_development_v3.json",
    )
    parser.add_argument("--progress", action="store_true")
    args = parser.parse_args()

    def progress(completed: int, total: int, family_id: str, elapsed: float) -> None:
        if not args.progress:
            return
        rate = completed / elapsed if elapsed > 0.0 else 0.0
        remaining = (total - completed) / rate if rate > 0.0 else 0.0
        print(
            f"[V3] {completed}/{total} ({completed / total:.0%}) "
            f"holdout={family_id} elapsed={elapsed:.1f}s eta={remaining:.1f}s",
            flush=True,
        )

    output_dir = develop_interaction_optimizer(
        root,
        root / args.config,
        progress_callback=progress,
    )
    summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    primary = summary["schemes"]["v3_nested_complete_family"]
    print(f"Output: {output_dir}")
    print(f"Status: {summary['status']}")
    print(f"Development gate passed: {summary['development_gate']['passes']}")
    print(
        "V3 metrics: within3={within:.1%}, mean={mean:.3f}%, "
        "p95={p95:.3f}%, max={maximum:.3f}%, coverage={coverage:.1%}".format(
            within=primary["within_tie_rate"],
            mean=primary["mean_regret_percent"],
            p95=primary["p95_regret_percent"],
            maximum=primary["max_regret_percent"],
            coverage=primary["direct_model_coverage"],
        )
    )
    if not summary["development_gate"]["passes"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
