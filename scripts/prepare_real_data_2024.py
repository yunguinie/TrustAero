"""Prepare downloaded 2024 monthly BTS/NYC inputs with visible progress.

Run after ``download_datasets.py --stage main_2024``. By default this prepares
February through December and resumes by verifying completed month manifests.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from trustaero.data.prepare import PreparationError
from trustaero.data.prepare_year import normalize_2024_months, prepare_real_data_2024

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _parse_months(value: str) -> tuple[int, ...]:
    """Parse comma-separated months and inclusive ranges such as ``2-6,9``."""

    months: list[int] = []
    for token in value.split(","):
        item = token.strip()
        if not item:
            continue
        if "-" in item:
            start_text, end_text = item.split("-", 1)
            start, end = int(start_text), int(end_text)
            if start > end:
                raise argparse.ArgumentTypeError("month ranges must be ascending")
            months.extend(range(start, end + 1))
        else:
            months.append(int(item))
    try:
        return normalize_2024_months(months)
    except PreparationError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--months",
        type=_parse_months,
        default=tuple(range(2, 13)),
        help="Months or ranges to prepare (default: 2-12).",
    )
    args = parser.parse_args()
    started = time.perf_counter()

    def progress(message: str) -> None:
        elapsed = time.perf_counter() - started
        print(f"[prepare elapsed={elapsed:,.1f}s] {message}", flush=True)

    try:
        summary = prepare_real_data_2024(
            PROJECT_ROOT / "data",
            months=args.months,
            stage=progress,
        )
    except PreparationError as exc:
        print(f"ERROR: {exc}", flush=True)
        return 2
    print(
        f"Prepared {summary['month_count']} months: "
        f"BTS={summary['bts_total_rows']:,} rows, "
        f"NYC={summary['nyc_total_rows']:,} rows",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
