"""Check the committed LLM-as-a-Judge approval aggregate."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    protocol_path = ROOT / "experiments/frozen/llm_judge_approval_protocol_v1_20260901.json"
    result_path = ROOT / "artifact/results/rq1-llm-judge/summary.json"
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    protocol_bytes = json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert hashlib.sha256(protocol_bytes).hexdigest() == result["protocol_sha256"]
    assert result["evaluation"]["cells"] == protocol["expected_cells"]
    assert result["evaluation"]["calls"] == 360
    assert sum(row["calls"] for row in result["by_stratum"]) == 360
    assert sum(row["family_correct_calls"] for row in result["by_stratum"]) == 263
    assert sum(row["unsafe_acceptances"] for row in result["by_stratum"]) == 32
    assert sum(row["false_rejections"] for row in result["by_stratum"]) == 53
    assert sum(row["repeat_consistent_cells"] for row in result["by_stratum"]) == 65
    print("LLM-as-a-Judge aggregate verification: PASS")


if __name__ == "__main__":
    main()