"""Fail when committed schemas drift from the Pydantic single source of truth."""

from __future__ import annotations

import json

from export_json_schemas import SCHEMA_DIR, schemas


def main() -> None:
    mismatches: list[str] = []
    for filename, expected in schemas().items():
        path = SCHEMA_DIR / filename
        if not path.exists() or json.loads(path.read_text(encoding="utf-8")) != expected:
            mismatches.append(filename)
    if mismatches:
        raise SystemExit("Schema files are out of sync: " + ", ".join(mismatches))


if __name__ == "__main__":
    main()
