"""Build publication-facing catalogs from the frozen result registry.

The catalog deliberately reports only claims already authorized by the
registry.  It does not search arbitrary result folders or infer a stronger
claim from a convenient metric, which keeps paper preparation fail-closed.
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from trustaero.reproducibility.paper_results import verify_paper_results_registry
from trustaero.reproducibility.source_freeze import sha256_file


@dataclass(frozen=True, slots=True)
class PaperEvidenceRow:
    """One publication-authorized evidence item."""

    entry_id: str
    evidence_role: str
    outcome: str
    authorized_claim: str
    boundary: str
    frozen_record_path: str
    primary_artifact_count: int
    primary_artifact_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PaperEvidenceCatalog:
    """Deterministic catalog plus integrity and coverage metadata."""

    schema_version: int
    registry_path: str
    registry_sha256: str
    registry_id: str
    registry_updated_at: str
    entry_count: int
    verified_artifact_count: int
    outcome_counts: dict[str, int]
    role_counts: dict[str, int]
    rows: tuple[PaperEvidenceRow, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation with stable field names."""

        return asdict(self)


def _resolve_registry(project_root: Path, registry_path: Path) -> Path:
    """Resolve a registry path while keeping it inside the repository."""

    root = project_root.resolve()
    resolved = (
        registry_path.resolve() if registry_path.is_absolute() else (root / registry_path).resolve()
    )
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ValueError("Paper registry must be inside the project root") from error
    return resolved


def build_paper_evidence_catalog(
    project_root: Path,
    registry_path: Path,
) -> PaperEvidenceCatalog:
    """Verify the registry and convert its authorized entries into table rows."""

    root = project_root.resolve()
    resolved_registry = _resolve_registry(root, registry_path)
    verification = verify_paper_results_registry(root, resolved_registry)
    if verification.status != "PASS":
        raise ValueError("Paper result registry failed integrity verification")

    payload = json.loads(resolved_registry.read_text(encoding="utf-8"))
    entries = payload["entries"]
    rows: list[PaperEvidenceRow] = []
    for entry in entries:
        artifacts = tuple(str(artifact["path"]) for artifact in entry["primary_artifacts"])
        rows.append(
            PaperEvidenceRow(
                entry_id=str(entry["entry_id"]),
                evidence_role=str(entry.get("evidence_role", "unspecified")),
                outcome=str(entry.get("outcome", "unspecified")),
                authorized_claim=str(entry.get("authorized_claim", "")),
                boundary=str(entry.get("boundary", "")),
                frozen_record_path=str(entry["frozen_record"]["path"]),
                primary_artifact_count=len(artifacts),
                primary_artifact_paths=artifacts,
            )
        )

    outcome_counts = Counter(row.outcome for row in rows)
    role_counts = Counter(row.evidence_role for row in rows)
    relative_registry = resolved_registry.relative_to(root).as_posix()
    return PaperEvidenceCatalog(
        schema_version=1,
        registry_path=relative_registry,
        registry_sha256=sha256_file(resolved_registry),
        registry_id=str(payload.get("registry_id", "")),
        registry_updated_at=str(payload.get("updated_at", "")),
        entry_count=len(rows),
        verified_artifact_count=verification.artifact_count,
        outcome_counts=dict(sorted(outcome_counts.items())),
        role_counts=dict(sorted(role_counts.items())),
        rows=tuple(rows),
    )


def write_paper_evidence_catalog(
    catalog: PaperEvidenceCatalog,
    output_dir: Path,
) -> tuple[Path, Path, Path]:
    """Write JSON, CSV, and Markdown views of one verified catalog."""

    output_dir.mkdir(parents=True, exist_ok=False)
    json_path = output_dir / "evidence_catalog.json"
    csv_path = output_dir / "evidence_catalog.csv"
    markdown_path = output_dir / "evidence_catalog.md"

    json_path.write_text(
        json.dumps(catalog.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        fieldnames = [
            "entry_id",
            "evidence_role",
            "outcome",
            "authorized_claim",
            "boundary",
            "frozen_record_path",
            "primary_artifact_count",
            "primary_artifact_paths",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in catalog.rows:
            payload = asdict(row)
            payload["primary_artifact_paths"] = "|".join(row.primary_artifact_paths)
            writer.writerow(payload)

    lines = [
        "# TrustAero verified paper evidence catalog",
        "",
        f"- Registry: `{catalog.registry_path}`",
        f"- Registry SHA-256: `{catalog.registry_sha256}`",
        f"- Entries: {catalog.entry_count}",
        f"- Verified artifacts: {catalog.verified_artifact_count}",
        "",
        "| Evidence | Role | Outcome | Authorized claim | Boundary |",
        "|---|---|---|---|---|",
    ]
    for row in catalog.rows:
        # Registry text is controlled project metadata. Escaping pipes keeps the
        # generated Markdown table structurally valid if prose later contains one.
        claim = row.authorized_claim.replace("|", "\\|").replace("\n", " ")
        boundary = row.boundary.replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| `{row.entry_id}` | {row.evidence_role} | {row.outcome} | {claim} | {boundary} |"
        )
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, csv_path, markdown_path
