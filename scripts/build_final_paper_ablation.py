"""Build the final paper ablation bundle from frozen evidence."""

from __future__ import annotations

from pathlib import Path

from trustaero.experiments.final_paper_ablation import (
    build_final_paper_ablation,
)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    output = build_final_paper_ablation(
        root,
        root / "experiments/configs/final_paper_ablation_v1.json",
    )
    print("Status: PASS_FINAL_PAPER_ABLATION_BUNDLE")
    print(f"Result: {output / 'ablation.json'}")
    print(f"Report: {output / 'report.md'}")


if __name__ == "__main__":
    main()
