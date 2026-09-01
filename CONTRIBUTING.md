# Contributing

1. Open an issue describing the change and the affected component or public
   contract.
2. Add or update tests with behavioral changes.
3. Keep reason-code values backward compatible within an IR version.
4. Run `ruff check .`, `ruff format --check .`, `mypy src/trustaero`,
   `python scripts/check_schema_sync.py`, and `python -m pytest -q`.
5. Do not commit credentials, private data, generated databases, benchmark
   scratch files, or machine-specific paths.

Changes to validation, planning, or checking should state the invariant being
preserved and update the corresponding example, schema, or artifact mapping.
