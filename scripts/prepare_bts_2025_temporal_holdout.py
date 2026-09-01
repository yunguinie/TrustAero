"""Prepare verified 2025 BTS months for the preregistered temporal holdout."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from trustaero.data.prepare import PreparationError
from trustaero.data.prepare_bts_year import normalize_calendar_months, prepare_bts_calendar_year

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _parse_months(value: str) -> tuple[int, ...]:
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
        return normalize_calendar_months(months)
    except PreparationError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--months", type=_parse_months, default=tuple(range(1, 13)))
    args = parser.parse_args()
    started = time.perf_counter()

    def progress(message: str) -> None:
        print(f"[prepare elapsed={time.perf_counter() - started:,.1f}s] {message}", flush=True)

    try:
        summary = prepare_bts_calendar_year(
            PROJECT_ROOT / "data",
            year=2025,
            months=args.months,
            stage=progress,
        )
    except PreparationError as exc:
        print(f"ERROR: {exc}", flush=True)
        return 2
    print(
        f"Prepared {summary['month_count']} BTS 2025 months: {summary['bts_total_rows']:,} rows",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
