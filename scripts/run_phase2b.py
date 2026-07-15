"""Run Phase 2B multi-candidate and source-lineage pilot experiments."""

from __future__ import annotations

import argparse

from run_phase2a import load_phase2_config

from trustaero.experiments.phase2a import run_phase2a


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="experiments/configs/phase2b.json",
        help="Phase 2B JSON configuration file.",
    )
    args = parser.parse_args()
    output_dir = run_phase2a(load_phase2_config(args.config))
    print(output_dir)


if __name__ == "__main__":
    main()
