"""Unit tests for frozen real-Agent generation without making network calls."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from trustaero.experiments.real_agent_plan_coverage import (
    _strict_fragment,
    _summarize,
    _trusted_plan,
)


def test_strict_fragment_rejects_markdown_and_extra_keys() -> None:
    valid = {"operators": [], "output_operator": "result"}
    assert _strict_fragment(json.dumps(valid)) == valid
    for invalid in (
        "```json\n" + json.dumps(valid) + "\n```",
        json.dumps({**valid, "purpose": "self_granted"}),
    ):
        try:
            _strict_fragment(invalid)
        except (ValueError, json.JSONDecodeError):
            pass
        else:
            raise AssertionError("Non-contract Agent output was accepted")


def test_trusted_envelope_cannot_be_supplied_by_agent() -> None:
    project_root = Path(__file__).resolve().parents[2]
    base = json.loads((project_root / "examples/multisource/plan.json").read_text(encoding="utf-8"))
    fragment = {"operators": base["operators"], "output_operator": base["output_operator"]}
    task: dict[str, Any] = {
        "task_id": "RA-X",
        "trusted_envelope_mutation": {
            "set": {"request_context.subject.role": "unauthorized_agent"}
        },
    }
    plan = _trusted_plan(base, fragment, task, "model", "thinking_high")
    assert plan["request_context"]["subject"]["role"] == "unauthorized_agent"
    assert set(fragment) == {"operators", "output_operator"}


def test_summary_requires_complete_safe_deterministic_cells() -> None:
    record = {
        "api_status": "SUCCESS",
        "stratum": "unauthorized",
        "mode_id": "thinking_high",
        "latency_ms": 10.0,
        "evaluation": {
            "strict_json_parsed": True,
            "outcome": "REJECT",
            "expected_outcome_family": True,
            "deterministic_revalidation": True,
        },
    }
    summary = _summarize([record], 1)
    assert summary["status"] == "PASS_REAL_AGENT_PLAN_COVERAGE_SAFETY"
    unsafe = json.loads(json.dumps(record))
    unsafe["evaluation"]["outcome"] = "ACCEPT"
    assert _summarize([unsafe], 1)["status"].startswith("FAIL_")
