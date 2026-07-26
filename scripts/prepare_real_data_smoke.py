"""Prepare deterministic BTS and NYC integration slices on the E drive.

This is an engineering and semantic smoke stage, not a paper performance run.
It converts the January BTS CSV inside its official ZIP to selected-column
Parquet, creates 100K/500K evenly spaced source-order samples spanning each
fixed file, and records exact checksums, row counts, schemas, and derivations.
"""

from __future__ import annotations

from pathlib import Path

from trustaero.data.prepare import PreparationError, prepare_real_data_smoke

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = PROJECT_ROOT / "data"


def main() -> int:
    try:
        manifest = prepare_real_data_smoke(
            DATA_ROOT,
            stage=lambda message: print(f"[stage] {message}"),
        )
    except PreparationError as exc:
        print(f"ERROR: {exc}")
        return 2

    print("\nPrepared artifacts:")
    for artifact in manifest["outputs"]:
        size_mib = artifact["byte_size"] / (1024 * 1024)
        print(
            f"- {artifact['artifact_id']}: {artifact['row_count']:,} rows, "
            f"{size_mib:.2f} MiB, {artifact['relative_path']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
