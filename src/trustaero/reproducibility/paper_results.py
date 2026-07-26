"""Verification helpers for the publication-facing result registry.

The registry is intentionally small: it points to the frozen records and
primary result files that may be cited in the paper.  Historical development
runs remain on disk, but they cannot silently become publication evidence.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from trustaero.reproducibility.source_freeze import sha256_file

VerificationStatus = Literal["PASS", "MISSING", "MISMATCH", "INVALID"]


@dataclass(frozen=True, slots=True)
class PaperArtifactCheck:
    """Integrity result for one registry-bound artifact."""

    entry_id: str
    artifact_kind: str
    path: str
    expected_sha256: str
    actual_sha256: str | None
    status: VerificationStatus


@dataclass(frozen=True, slots=True)
class PaperRegistryVerification:
    """Complete read-only verification result for a paper registry."""

    status: Literal["PASS", "FAIL"]
    registry_path: str
    entry_count: int
    artifact_count: int
    checks: tuple[PaperArtifactCheck, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a deterministic JSON-safe representation."""

        return asdict(self)


def _resolve_inside(project_root: Path, relative_path: str) -> Path | None:
    """Resolve a repository-relative path and reject traversal or absolutes."""

    candidate = Path(relative_path)
    if candidate.is_absolute():
        return None
    root = project_root.resolve()
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    return resolved


def _check_artifact(
    project_root: Path,
    *,
    entry_id: str,
    artifact_kind: str,
    artifact: object,
) -> PaperArtifactCheck:
    """Validate one ``{"path", "sha256"}`` registry binding."""

    if not isinstance(artifact, dict):
        return PaperArtifactCheck(entry_id, artifact_kind, "", "", None, "INVALID")
    relative_path = str(artifact.get("path", ""))
    expected = str(artifact.get("sha256", "")).lower()
    resolved = _resolve_inside(project_root, relative_path)
    if (
        resolved is None
        or not relative_path
        or len(expected) != 64
        or any(character not in "0123456789abcdef" for character in expected)
    ):
        return PaperArtifactCheck(entry_id, artifact_kind, relative_path, expected, None, "INVALID")
    if not resolved.is_file():
        return PaperArtifactCheck(entry_id, artifact_kind, relative_path, expected, None, "MISSING")
    actual = sha256_file(resolved)
    return PaperArtifactCheck(
        entry_id,
        artifact_kind,
        relative_path,
        expected,
        actual,
        "PASS" if actual == expected else "MISMATCH",
    )


def verify_paper_results_registry(
    project_root: Path,
    registry_path: Path,
) -> PaperRegistryVerification:
    """Verify all frozen records and primary artifacts named by the registry.

    This function never rewrites a digest.  A changed or missing result fails
    closed so a later paper draft cannot accidentally cite mutated evidence.
    """

    root = project_root.resolve()
    resolved_registry = registry_path
    if not registry_path.is_absolute():
        resolved_registry = root / registry_path
    payload = json.loads(resolved_registry.read_text(encoding="utf-8"))
    entries = payload.get("entries")
    if payload.get("schema_version") != 1 or not isinstance(entries, list):
        raise ValueError("Paper result registry must use schema_version=1 and an entries list")

    checks: list[PaperArtifactCheck] = []
    seen_ids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Every paper result entry must be an object")
        entry_id = str(entry.get("entry_id", ""))
        if not entry_id or entry_id in seen_ids:
            raise ValueError(f"Paper result entry ID is empty or duplicated: {entry_id!r}")
        seen_ids.add(entry_id)
        checks.append(
            _check_artifact(
                root,
                entry_id=entry_id,
                artifact_kind="frozen_record",
                artifact=entry.get("frozen_record"),
            )
        )
        primary_artifacts = entry.get("primary_artifacts", [])
        if not isinstance(primary_artifacts, list) or not primary_artifacts:
            raise ValueError(f"Paper result entry has no primary artifacts: {entry_id}")
        for index, artifact in enumerate(primary_artifacts, start=1):
            checks.append(
                _check_artifact(
                    root,
                    entry_id=entry_id,
                    artifact_kind=f"primary_artifact_{index}",
                    artifact=artifact,
                )
            )

    relative_registry = resolved_registry.resolve().relative_to(root).as_posix()
    # Annotate the literal union explicitly so static type checking preserves
    # the two allowed publication-registry outcomes instead of widening to str.
    status: Literal["PASS", "FAIL"] = (
        "PASS" if all(check.status == "PASS" for check in checks) else "FAIL"
    )
    return PaperRegistryVerification(
        status=status,
        registry_path=relative_registry,
        entry_count=len(entries),
        artifact_count=len(checks),
        checks=tuple(checks),
    )
