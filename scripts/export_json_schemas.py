"""Export versioned JSON Schemas from the strict Pydantic model source."""

from __future__ import annotations

import json
from pathlib import Path

from trustaero.ir.models import (
    CandidatePlan,
    GovernedExecutionCertificate,
    PolicySet,
    ValidatedLogicalPlan,
    ValidatorResponse,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "schemas" / "v1"
MODELS = {
    "candidate_plan.schema.json": CandidatePlan,
    "validated_logical_plan.schema.json": ValidatedLogicalPlan,
    "validator_response.schema.json": ValidatorResponse,
    "policy.schema.json": PolicySet,
    "governed_execution_certificate.schema.json": GovernedExecutionCertificate,
}


def schemas() -> dict[str, dict[str, object]]:
    """Return canonical model schemas with stable public identifiers."""

    result: dict[str, dict[str, object]] = {}
    for filename, model in MODELS.items():
        schema = model.model_json_schema()
        schema["$id"] = f"https://trustaero.org/schemas/v1/{filename}"
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        result[filename] = schema
    return result


def main() -> None:
    SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
    for filename, schema in schemas().items():
        (SCHEMA_DIR / filename).write_text(
            json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
