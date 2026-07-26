"""Validate the frozen formal real-data development-partition protocol."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trustaero.experiments.real_data_formal_protocol import (
    validate_formal_real_data_protocol,
)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--protocol",
        default="experiments/configs/real_data_formal_protocol_v1.json",
    )
    parser.add_argument(
        "--output",
        default="results/real_data_formal_protocol_v1/validation.json",
    )
    args = parser.parse_args()
    check = validate_formal_real_data_protocol(
        root / args.protocol,
        project_root=root,
    )
    output = root / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(check.model_dump(mode="json"), indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"PASS {check.protocol_id}: {len(check.eligible_template_ids)} eligible, "
        f"{len(check.deferred_template_ids)} explicitly deferred"
    )
    print(f"Protocol SHA-256: {check.protocol_sha256}")
    print(f"Validation record: {output}")


if __name__ == "__main__":
    main()
