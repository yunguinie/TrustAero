"""Check whether this checkout is safe for publication-facing experiments."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from trustaero.reproducibility.source_freeze import SourceFreezeAudit, audit_source_freeze


def _write_json_atomically(path: Path, payload: dict[str, object]) -> None:
    """Publish a complete report without leaving a partially written JSON file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        Path(temporary_name).replace(path)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _print_summary(audit: SourceFreezeAudit, output: Path) -> None:
    passed_hashes = sum(item.status == "PASS" for item in audit.immutable_file_checks)
    print(f"Source-freeze status: {audit.status}")
    print(f"Commit: {audit.source_commit}")
    print(
        "Working tree: "
        f"{len(audit.modified_paths)} modified, {len(audit.staged_paths)} staged, "
        f"{len(audit.untracked_paths)} untracked"
    )
    print(
        f"Frozen integrity: {passed_hashes}/{len(audit.immutable_file_checks)} files pass "
        f"across {audit.frozen_record_count} compatible records"
    )
    for diagnostic in audit.diagnostics:
        print(f"[{diagnostic.severity.upper()}] {diagnostic.code}: {diagnostic.message}")
    print(f"Audit report: {output}")


def _write_markdown_report(path: Path, audit: SourceFreezeAudit) -> None:
    """Write a compact human-readable companion to the JSON evidence."""

    passed_hashes = sum(item.status == "PASS" for item in audit.immutable_file_checks)
    lines = [
        "# Source-freeze audit",
        "",
        f"- Status: **{audit.status}**",
        f"- Source commit: `{audit.source_commit}`",
        f"- Python environment: `{audit.python_environment}`",
        f"- Modified paths: {len(audit.modified_paths)}",
        f"- Staged paths: {len(audit.staged_paths)}",
        f"- Untracked paths: {len(audit.untracked_paths)}",
        f"- Tracked large artifacts: {len(audit.tracked_large_artifacts)}",
        (
            f"- Frozen integrity: {passed_hashes}/{len(audit.immutable_file_checks)} "
            f"files pass across {audit.frozen_record_count} compatible records"
        ),
        "",
        "## Diagnostics",
        "",
    ]
    if audit.diagnostics:
        lines.extend(
            f"- `{item.code}` ({item.severity}): {item.message}" for item in audit.diagnostics
        )
    else:
        lines.append("- No diagnostics.")
    lines.extend(["", "## Source boundary", ""])
    for label, paths in (
        ("Modified", audit.modified_paths),
        ("Staged", audit.staged_paths),
        ("Untracked", audit.untracked_paths),
    ):
        lines.append(f"### {label} ({len(paths)})")
        lines.append("")
        lines.extend(f"- `{item}`" for item in paths)
        if not paths:
            lines.append("- None")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Fail closed unless source, environment, and frozen hashes are reproducible."
    )
    parser.add_argument(
        "--output",
        default="results/source_freeze_audit/latest/audit.json",
        help="JSON report path relative to the project root.",
    )
    parser.add_argument("--expected-environment", default="TrustAero_env")
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Return exit code 0 even when not ready; useful while preparing a freeze.",
    )
    args = parser.parse_args()
    output = root / args.output
    audit = audit_source_freeze(root, expected_environment=args.expected_environment)
    _write_json_atomically(output, audit.to_dict())
    _write_markdown_report(output.with_name("report.md"), audit)
    _print_summary(audit, output)
    if audit.status != "READY" and not args.report_only:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
