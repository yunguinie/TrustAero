"""Generate and verify TPC-H SF1 entirely below the E-drive project root."""

from __future__ import annotations

import time
from pathlib import Path

from trustaero.data.tpch import prepare_tpch_sf1


def _progress(current: int, total: int, label: str, elapsed: float) -> None:
    fraction = current / total
    width = 24
    filled = round(width * fraction)
    bar = "#" * filled + "-" * (width - filled)
    print(
        f"\r[{bar}] {current}/{total} ({fraction:6.1%}) elapsed={elapsed:7.1f}s {label:<44}",
        end="\n" if current == total else "",
        flush=True,
    )


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    started = time.monotonic()
    artifact = prepare_tpch_sf1(root, progress=_progress)
    print(f"TPC-H SF1 ready: {root / artifact.database_path}")
    print(f"Rows: {sum(artifact.table_rows.values()):,} across 8 tables")
    print(f"Size: {artifact.byte_size / 1024**2:.1f} MiB")
    print(f"SHA-256: {artifact.sha256}")
    print(f"Total elapsed: {time.monotonic() - started:.1f}s")


if __name__ == "__main__":
    main()
