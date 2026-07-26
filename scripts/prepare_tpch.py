"""Generate a verified TPC-H SF1 or SF10 database inside the project.

Examples from the activated ``TrustAero_env`` terminal::

    python -u scripts/prepare_tpch.py --scale-factor 10 --progress

The command is safe to rerun. A complete database is verified and reused; an
interrupted build resumes after its last completed partition, while the
``.building`` database is never published as a dataset.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from trustaero.data.tpch import TpchPreparationError, prepare_tpch_scale


def _progress(current: int, total: int, label: str, elapsed: float) -> None:
    """Show durable newline progress so VS Code always displays liveness."""

    fraction = current / total
    width = 24
    filled = round(width * fraction)
    bar = "#" * filled + "-" * (width - filled)
    eta = elapsed * (total - current) / current if current else 0.0
    print(
        f"[{bar}] {current}/{total} ({fraction:6.1%}) "
        f"elapsed={elapsed:8.1f}s ETA~{eta:8.1f}s {label}",
        flush=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scale-factor",
        type=int,
        choices=(1, 10),
        required=True,
        help="Reviewed scale factor. SF10 is the paper-scale artifact.",
    )
    parser.add_argument(
        "--progress",
        action="store_true",
        help="Print each generation partition, elapsed time, and rough ETA.",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    started = time.monotonic()
    try:
        artifact = prepare_tpch_scale(
            root,
            scale_factor=args.scale_factor,
            progress=_progress if args.progress else None,
        )
    except TpchPreparationError as exc:
        print(f"ERROR: {exc}", flush=True)
        return 2

    print(f"TPC-H SF{artifact.scale_factor} ready: {root / artifact.database_path}")
    print(f"Rows: {sum(artifact.table_rows.values()):,} across 8 tables")
    print(f"Size: {artifact.byte_size / 1024**3:.2f} GiB")
    print(f"SHA-256: {artifact.sha256}")
    print(f"Total elapsed: {time.monotonic() - started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
