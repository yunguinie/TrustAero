"""Prepare the frozen four-source end-to-end case-study tables.

Run from the activated ``TrustAero_env``:

    python -u scripts/prepare_multisource_case.py

This is deterministic data preparation, not a timed paper experiment.  The
script prints each stage and writes only below ``data/processed/multisource``.
"""

from __future__ import annotations

from pathlib import Path

from trustaero.data.multisource import (
    MultisourcePreparationError,
    prepare_multisource_case,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = PROJECT_ROOT / "data"


def main() -> int:
    try:
        manifest = prepare_multisource_case(
            DATA_ROOT,
            stage=lambda message: print(f"[stage] {message}", flush=True),
        )
    except MultisourcePreparationError as exc:
        print(f"ERROR: {exc}")
        return 2

    print("\nPrepared case-study tables:")
    for artifact in manifest["outputs"]:
        print(
            f"- {artifact['artifact_id']}: {artifact['row_count']:,}/"
            f"{artifact['source_rows']:,} rows, "
            f"{artifact['byte_size'] / 1024:.1f} KiB, "
            f"sha256={artifact['sha256']}"
        )
    print("Manifest: data/manifests/processed/multisource-case-v1.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
