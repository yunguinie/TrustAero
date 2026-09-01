# Versioned JSON Schemas

The JSON files under `v1/` are public, language-neutral artifacts generated
from TrustAero's strict Pydantic models. They are committed so reviewers and
non-Python clients can inspect the exact contract used by an experiment.

Run `python scripts/export_json_schemas.py` after an intentional model change,
then review the diff. CI runs `python scripts/check_schema_sync.py` to prevent
unreviewed drift between models and committed schemas.
