"""Download BTS ZIP files with verified byte-range requests.

The BTS IIS endpoint occasionally closes long TLS transfers and can ignore an
automatic resume request.  This helper requests fixed byte ranges, verifies
the returned Content-Range for every block, and publishes a ZIP only after its
official length and CRC have both been checked.  Completed blocks are retained
so an interrupted acquisition can resume without trusting a partial stream.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from zipfile import ZipFile

CONTENT_RANGE = re.compile(r"content-range:\s*bytes\s+(\d+)-(\d+)/(\d+)", re.I)


def _download_block(
    *, url: str, target: Path, header: Path, start: int, end: int, total: int
) -> None:
    expected = end - start + 1
    if target.exists() and header.exists() and target.stat().st_size == expected:
        match = CONTENT_RANGE.search(header.read_text(encoding="latin-1"))
        if match and tuple(map(int, match.groups())) == (start, end, total):
            return

    target.unlink(missing_ok=True)
    header.unlink(missing_ok=True)
    command = [
        "curl.exe",
        "--silent",
        "--show-error",
        "--fail",
        "--location",
        "--retry",
        "8",
        "--retry-all-errors",
        "--connect-timeout",
        "30",
        "--max-time",
        "420",
        "--range",
        f"{start}-{end}",
        "--dump-header",
        str(header),
        "--output",
        str(target),
        url,
    ]
    subprocess.run(command, check=True)
    actual = target.stat().st_size
    match = CONTENT_RANGE.search(header.read_text(encoding="latin-1"))
    returned = tuple(map(int, match.groups())) if match else None
    if actual != expected or returned != (start, end, total):
        target.unlink(missing_ok=True)
        raise RuntimeError(
            f"invalid range response for {start}-{end}: bytes={actual}, content_range={returned}"
        )


def _download_month(*, root: Path, year: int, month: int, total: int, chunk_size: int) -> Path:
    filename = f"On_Time_Reporting_Carrier_On_Time_Performance_1987_present_{year}_{month}.zip"
    url = f"https://transtats.bts.gov/PREZIP/{filename}"
    destination = root / filename
    chunk_root = root / ".range_chunks" / f"{year}_{month:02d}"
    chunk_root.mkdir(parents=True, exist_ok=True)
    chunks: list[Path] = []
    block_count = (total + chunk_size - 1) // chunk_size

    for index, start in enumerate(range(0, total, chunk_size), start=1):
        end = min(start + chunk_size - 1, total - 1)
        block = chunk_root / f"{start:012d}-{end:012d}.part"
        header = block.with_suffix(".headers")
        for attempt in range(1, 6):
            try:
                _download_block(
                    url=url,
                    target=block,
                    header=header,
                    start=start,
                    end=end,
                    total=total,
                )
                break
            except (OSError, RuntimeError, subprocess.CalledProcessError):
                if attempt == 5:
                    raise
                time.sleep(min(5 * attempt, 20))
        chunks.append(block)
        print(
            f"month={month:02d} block={index}/{block_count} range={start}-{end}",
            flush=True,
        )

    assembled = destination.with_suffix(".zip.assembled")
    with assembled.open("wb") as output:
        for block in chunks:
            with block.open("rb") as source:
                shutil.copyfileobj(source, output, length=1024 * 1024)
    if assembled.stat().st_size != total:
        raise RuntimeError(f"assembled length mismatch: {assembled.stat().st_size} != {total}")
    with ZipFile(assembled) as archive:
        bad_member = archive.testzip()
    if bad_member is not None:
        raise RuntimeError(f"ZIP CRC failure in {bad_member}")
    assembled.replace(destination)
    print(f"month={month:02d} COMPLETE bytes={total} crc=OK", flush=True)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument("--chunk-mib", type=int, default=1)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument(
        "--month-size",
        action="append",
        required=True,
        help="MONTH:OFFICIAL_BYTE_SIZE (repeat for each file)",
    )
    args = parser.parse_args()
    month_sizes: dict[int, int] = {}
    for item in args.month_size:
        month_text, size_text = item.split(":", maxsplit=1)
        month_sizes[int(month_text)] = int(size_text)
    args.root.mkdir(parents=True, exist_ok=True)

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                _download_month,
                root=args.root,
                year=args.year,
                month=month,
                total=total,
                chunk_size=args.chunk_mib * 1024 * 1024,
            ): month
            for month, total in sorted(month_sizes.items())
        }
        for future in as_completed(futures):
            month = futures[future]
            path = future.result()
            print(f"published month={month:02d} path={path}", flush=True)


if __name__ == "__main__":
    main()
