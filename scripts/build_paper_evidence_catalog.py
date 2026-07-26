"""Generate paper-facing evidence tables from the verified frozen registry."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from trustaero.reproducibility.paper_catalog import (
    build_paper_evidence_catalog,
    write_paper_evidence_catalog,
)


def main() -> None:
    """Verify registered evidence, then emit deterministic table inputs."""

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--registry",
        default="experiments/frozen/paper_results_registry_v1_20260724.json",
        help="Repository-relative frozen result registry.",
    )
    parser.add_argument(
        "--output-dir",
        help="Optional output directory; defaults to a timestamped results run.",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_dir = (
        Path(args.output_dir)
        if args.output_dir
        else root / "results" / "paper_evidence_catalog" / run_id
    )
    if not output_dir.is_absolute():
        output_dir = root / output_dir

    catalog = build_paper_evidence_catalog(root, Path(args.registry))
    paths = write_paper_evidence_catalog(catalog, output_dir)
    print("Status: PASS_PAPER_EVIDENCE_CATALOG")
    print(f"Entries: {catalog.entry_count}; verified artifacts: {catalog.verified_artifact_count}")
    for path in paths:
        print(f"Output: {path}")


if __name__ == "__main__":
    main()
