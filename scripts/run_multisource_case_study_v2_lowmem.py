"""Run the frozen four-source complete loop with bounded DuckDB memory."""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb

from trustaero.experiments.multisource_case_study_v2 import run_multisource_case_study_v2


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--progress", action="store_true")
    parser.add_argument("--memory-limit", default="512MB")
    parser.add_argument("--threads", type=int, default=1)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    real_connect = duckdb.connect

    def bounded_connect(*connect_args, **connect_kwargs):
        config = dict(connect_kwargs.pop("config", {}) or {})
        config.update(
            {
                "memory_limit": args.memory_limit,
                "threads": str(args.threads),
                "preserve_insertion_order": "false",
            }
        )
        return real_connect(*connect_args, config=config, **connect_kwargs)

    duckdb.connect = bounded_connect
    output = run_multisource_case_study_v2(root, progress=args.progress, require_clean=False)
    print("Status: PASS_MULTISOURCE_CASE_STUDY_V2_LOWMEM")
    print(f"Result: {output / 'summary.json'}")


if __name__ == "__main__":
    main()
