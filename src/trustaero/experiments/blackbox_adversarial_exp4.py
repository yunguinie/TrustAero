"""One-shot evaluator for the frozen Experiment 4 black-box case corpus."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from trustaero.catalog.in_memory import InMemoryCatalog
from trustaero.catalog.models import CatalogDocument
from trustaero.ir.models import PolicySet
from trustaero.validator.service import validate

EXPECTED_CASES_SHA256 = "fb28a8316dd68018ad3295a784889524d2c1b3670dbca9418a0e2cf963d4f79b"
EXECUTABLE_STATUSES = {"ACCEPT", "REWRITE"}


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    paths = sorted(
        (
            *((root / "src/trustaero/validator").glob("*.py")),
            root / "src/trustaero/policy/evaluator.py",
            root / "src/trustaero/ir/models.py",
            root / "src/trustaero/ir/enums.py",
            root / "src/trustaero/catalog/models.py",
            root / "src/trustaero/catalog/in_memory.py",
        ),
        key=lambda p: p.as_posix(),
    )
    for path in paths:
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def evaluate_frozen_cases(
    root: Path, *, cases_path: Path, policy_path: Path, catalog_path: Path
) -> dict[str, Any]:
    cases_hash = _sha(cases_path)
    if cases_hash != EXPECTED_CASES_SHA256:
        raise ValueError(f"frozen cases hash mismatch: {cases_hash}")
    corpus = _load(cases_path)
    policy = PolicySet.model_validate(_load(policy_path))
    catalog = InMemoryCatalog(CatalogDocument.model_validate(_load(catalog_path)))
    rows, exceptions = [], []
    for case in corpus["cases"]:
        try:
            response = validate(case["plan"], policy, catalog)
            status = response.status.value
            codes = sorted({item.code.value for item in response.diagnostics})
            rows.append(
                {
                    "case_id": case["case_id"],
                    "expected_class": case["expected_class"],
                    "expected_seed_status": case["expected_seed_status"],
                    "objective": case["objective"],
                    "status": status,
                    "diagnostic_codes": codes,
                    "unsafe_acceptance": case["expected_class"] == "unsafe"
                    and status in EXECUTABLE_STATUSES,
                    "false_rejection": case["expected_class"] == "valid"
                    and status not in EXECUTABLE_STATUSES,
                    "decision_match": case["expected_class"] == "valid"
                    and status == case["expected_seed_status"],
                }
            )
        except Exception as exc:
            exceptions.append(
                {"case_id": str(case["case_id"]), "type": type(exc).__name__, "message": str(exc)}
            )
    unsafe = [r for r in rows if r["expected_class"] == "unsafe"]
    valid = [r for r in rows if r["expected_class"] == "valid"]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in unsafe:
        grouped[str(row["objective"])].append(row)
    by_objective = {
        objective: {
            "cases": len(items),
            "unsafe_acceptance_count": sum(bool(r["unsafe_acceptance"]) for r in items),
            "status_counts": dict(sorted(Counter(str(r["status"]) for r in items).items())),
            "diagnostic_codes": sorted({code for r in items for code in r["diagnostic_codes"]}),
        }
        for objective, items in sorted(grouped.items())
    }
    codes = sorted({code for r in rows for code in r["diagnostic_codes"]})
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()
    return {
        "schema_version": 1,
        "experiment_id": corpus["experiment_id"],
        "evaluation_type": "one_shot_frozen_black_box",
        "frozen_artifacts": {
            "git_commit": commit,
            "cases_sha256": cases_hash,
            "production_validation_tree_sha256": _tree_hash(root),
            "candidate_schema_sha256": corpus["input_hashes"]["candidate_plan_schema"].removeprefix(
                "sha256:"
            ),
            "policy_sha256": _sha(policy_path),
            "catalog_sha256": _sha(catalog_path),
        },
        "metrics": {
            "total_cases": len(corpus["cases"]),
            "evaluated_cases": len(rows),
            "unsafe_cases": len(unsafe),
            "valid_controls": len(valid),
            "unsafe_acceptance_count": sum(bool(r["unsafe_acceptance"]) for r in unsafe),
            "false_rejection_count": sum(bool(r["false_rejection"]) for r in valid),
            "valid_decision_match_count": sum(bool(r["decision_match"]) for r in valid),
            "unexpected_exception_count": len(exceptions),
            "attack_objectives_covered": len(grouped),
            "diagnostic_code_coverage": len(codes),
            "diagnostic_codes": codes,
            "status_counts": dict(sorted(Counter(str(r["status"]) for r in rows).items())),
        },
        "by_objective": by_objective,
        "exceptions": exceptions,
        "rows": rows,
    }


def write_result(result: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
