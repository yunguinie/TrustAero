"""Run the fail-closed gate before any Optimizer V3 implementation work."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.optimizer_v3_readiness import (
    OptimizerV3ReadinessAudit,
    audit_optimizer_v3_readiness,
)


def _write_report(path: Path, audit: OptimizerV3ReadinessAudit) -> None:
    lines = [
        "# Optimizer V3 readiness audit",
        "",
        f"- Status: **{audit.status}**",
        f"- Source commit: `{audit.source_commit}`",
        (f"- V3 protocol design authorized: **{audit.optimizer_v3_protocol_design_authorized}**"),
        f"- V3 training authorized: **{audit.optimizer_v3_training_authorized}**",
        f"- Phase 2G authorized: **{audit.phase2g_authorized}**",
        "",
        "## Hard gates",
        "",
    ]
    lines.extend(
        f"- {'PASS' if check.passed else 'FAIL'} `{check.code}` — {check.message}"
        for check in audit.checks
    )
    lines.extend(["", "## Position-effect confidence intervals", ""])
    for effect in audit.position_effects:
        lower, upper = effect.confidence_interval_95
        lines.append(
            f"- `{effect.run_id}` / `{effect.component}`: median "
            f"{effect.median_position_1_over_0:.4f}, 95% CI "
            f"[{lower:.4f}, {upper:.4f}], {'PASS' if effect.passed else 'FAIL'}"
        )
    lines.extend(["", "## Scientific boundary", "", audit.scientific_boundary, ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Audit whether Optimizer V3 protocol design may begin."
    )
    parser.add_argument(
        "--config",
        default="experiments/configs/optimizer_v3_readiness_v1.json",
    )
    parser.add_argument(
        "--output-dir",
        default="results/optimizer_v3_readiness/v1",
    )
    args = parser.parse_args()
    audit = audit_optimizer_v3_readiness(root, root / args.config)
    output_dir = root / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "audit.json").write_text(
        json.dumps(audit.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    _write_report(output_dir / "report.md", audit)

    print(f"Optimizer V3 readiness: {audit.status}")
    for check in audit.checks:
        print(f"[{'PASS' if check.passed else 'FAIL'}] {check.code}")
    for effect in audit.position_effects:
        lower, upper = effect.confidence_interval_95
        print(
            f"Position effect {effect.run_id}/{effect.component}: "
            f"median={effect.median_position_1_over_0:.4f}, "
            f"CI95=[{lower:.4f}, {upper:.4f}]"
        )
    print(f"Report: {output_dir / 'report.md'}")
    if audit.status != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
