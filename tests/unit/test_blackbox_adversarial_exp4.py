import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_generator_has_only_standard_library_imports() -> None:
    path = ROOT / "experiments/blackbox_exp4/generate_cases.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = {
        (node.module or "").split(".", 1)[0]
        if isinstance(node, ast.ImportFrom)
        else alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
        if alias.name != "annotations"
    }
    assert imported <= {
        "__future__",
        "argparse",
        "copy",
        "hashlib",
        "json",
        "random",
        "pathlib",
        "typing",
    }


def test_frozen_case_corpus_shape() -> None:
    payload = json.loads(
        (ROOT / "artifact/results/rq1-blackbox/cases.json").read_text(encoding="utf-8")
    )
    assert (payload["case_count"], payload["unsafe_count"], payload["valid_count"]) == (
        1000,
        800,
        200,
    )
    assert len({row["case_id"] for row in payload["cases"]}) == 1000
