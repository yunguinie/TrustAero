"""Committed schemas must match the Pydantic single source of truth."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from export_json_schemas import SCHEMA_DIR, schemas  # noqa: E402


def test_committed_schemas_are_current() -> None:
    for filename, expected in schemas().items():
        path = SCHEMA_DIR / filename
        assert path.exists(), filename
        assert json.loads(path.read_text(encoding="utf-8")) == expected
