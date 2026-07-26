"""Fail-closed source-freeze checks for publication-facing experiments.

The checker is intentionally read-only: it reports why a repository is not
ready, but never stages files, creates commits, or changes frozen artifacts.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

Severity = Literal["error", "warning"]

_PUBLICATION_TEXT_SUFFIXES = {
    ".cff",
    ".csv",
    ".json",
    ".md",
    ".py",
    ".toml",
    ".yaml",
    ".yml",
}
_PROHIBITED_TRACKED_PREFIXES = ("data/raw/", "data/processed/", "data/tmp/")
_CONFLICT_MARKER = re.compile(r"^(<<<<<<< |=======\s*$|>>>>>>> )", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class FreezeDiagnostic:
    """One stable, machine-readable reason emitted by the freeze gate."""

    code: str
    severity: Severity
    message: str
    paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ImmutableFileCheck:
    """Integrity result for one file referenced by a frozen record."""

    record_path: str
    file_path: str
    expected_sha256: str
    actual_sha256: str | None
    status: Literal["PASS", "MISSING", "MISMATCH", "INVALID_PATH"]


@dataclass(frozen=True, slots=True)
class SourceFreezeAudit:
    """Complete source-freeze decision and its supporting evidence."""

    schema_version: int
    status: Literal["READY", "NOT_READY"]
    project_root: str
    source_commit: str | None
    python_executable: str
    python_environment: str
    expected_python_environment: str
    modified_paths: tuple[str, ...]
    staged_paths: tuple[str, ...]
    untracked_paths: tuple[str, ...]
    tracked_large_artifacts: tuple[str, ...]
    frozen_record_count: int
    immutable_file_checks: tuple[ImmutableFileCheck, ...]
    diagnostics: tuple[FreezeDiagnostic, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation with deterministic field order."""

        return asdict(self)


def sha256_file(path: Path) -> str:
    """Hash a file without loading a possibly large artifact into memory."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git_lines(project_root: Path, *args: str) -> tuple[str, ...]:
    result = subprocess.run(
        ["git", *args],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return tuple(sorted(line.strip().replace("\\", "/") for line in result.stdout.splitlines()))


def _git_text(project_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def _inside_root(project_root: Path, relative_path: str) -> Path | None:
    """Resolve a frozen path while rejecting absolute paths and traversal."""

    candidate = Path(relative_path)
    if candidate.is_absolute():
        return None
    resolved_root = project_root.resolve()
    resolved = (resolved_root / candidate).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError:
        return None
    return resolved


def verify_frozen_records(project_root: Path) -> tuple[int, tuple[ImmutableFileCheck, ...]]:
    """Verify SHA-256 bindings declared by every compatible frozen record."""

    records = sorted((project_root / "experiments/frozen").glob("*.json"))
    checks: list[ImmutableFileCheck] = []
    compatible_record_count = 0
    for record in records:
        record_path = record.relative_to(project_root).as_posix()
        try:
            payload = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            checks.append(ImmutableFileCheck(record_path, "<record>", "", None, "INVALID_PATH"))
            continue
        immutable_files = payload.get("immutable_files")
        if "immutable_files" in payload and not isinstance(immutable_files, list):
            checks.append(
                ImmutableFileCheck(record_path, "<immutable_files>", "", None, "INVALID_PATH")
            )
            continue
        if not isinstance(immutable_files, list):
            continue
        compatible_record_count += 1
        for entry in immutable_files:
            if not isinstance(entry, dict):
                checks.append(
                    ImmutableFileCheck(record_path, "<malformed>", "", None, "INVALID_PATH")
                )
                continue
            relative_path = str(entry.get("path", ""))
            expected = str(entry.get("sha256", ""))
            resolved = _inside_root(project_root, relative_path)
            if resolved is None or not relative_path or len(expected) != 64:
                checks.append(
                    ImmutableFileCheck(record_path, relative_path, expected, None, "INVALID_PATH")
                )
            elif not resolved.is_file():
                checks.append(
                    ImmutableFileCheck(record_path, relative_path, expected, None, "MISSING")
                )
            else:
                actual = sha256_file(resolved)
                checks.append(
                    ImmutableFileCheck(
                        record_path,
                        relative_path,
                        expected,
                        actual,
                        "PASS" if actual == expected else "MISMATCH",
                    )
                )
    return compatible_record_count, tuple(checks)


def _conflict_marker_paths(project_root: Path, paths: tuple[str, ...]) -> tuple[str, ...]:
    conflicts: list[str] = []
    for relative_path in paths:
        path = project_root / relative_path
        if path.suffix.lower() not in _PUBLICATION_TEXT_SUFFIXES or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _CONFLICT_MARKER.search(text):
            conflicts.append(relative_path)
    return tuple(sorted(conflicts))


def audit_source_freeze(
    project_root: Path,
    *,
    expected_environment: str = "TrustAero_env",
    large_artifact_bytes: int = 10 * 1024 * 1024,
) -> SourceFreezeAudit:
    """Evaluate whether the current checkout may produce formal experiment data.

    A clean Git snapshot is a hard requirement. This prevents measurements from
    being attributed to a commit that does not actually contain the measured code.
    """

    root = project_root.resolve()
    diagnostics: list[FreezeDiagnostic] = []
    try:
        source_commit = _git_text(root, "rev-parse", "HEAD")
        modified = _git_lines(root, "diff", "--name-only")
        staged = _git_lines(root, "diff", "--cached", "--name-only")
        untracked = _git_lines(root, "ls-files", "--others", "--exclude-standard")
        tracked = _git_lines(root, "ls-files")
        whitespace_errors = "\n".join(
            filter(
                None,
                (
                    _git_text(root, "diff", "--check"),
                    _git_text(root, "diff", "--cached", "--check"),
                ),
            )
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        message = f"source-freeze audit requires a readable Git repository: {root}"
        raise RuntimeError(message) from exc

    if modified or staged or untracked:
        diagnostics.append(
            FreezeDiagnostic(
                "SOURCE_WORKTREE_DIRTY",
                "error",
                "Formal experiment runs require a committed, clean source snapshot.",
                tuple(sorted(set(modified + staged + untracked))),
            )
        )
    if untracked:
        diagnostics.append(
            FreezeDiagnostic(
                "SOURCE_UNTRACKED_FILES",
                "error",
                "Untracked, non-ignored files are not bound to the reported source commit.",
                untracked,
            )
        )
    if whitespace_errors:
        diagnostics.append(
            FreezeDiagnostic("SOURCE_DIFF_CHECK_FAILED", "error", whitespace_errors.splitlines()[0])
        )

    tracked_large: list[str] = []
    prohibited_tracked: list[str] = []
    for relative_path in tracked:
        normalized = relative_path.casefold()
        if normalized.startswith(_PROHIBITED_TRACKED_PREFIXES):
            prohibited_tracked.append(relative_path)
        path = root / relative_path
        if path.is_file() and path.stat().st_size > large_artifact_bytes:
            tracked_large.append(relative_path)
    if prohibited_tracked:
        diagnostics.append(
            FreezeDiagnostic(
                "SOURCE_RAW_DATA_TRACKED",
                "error",
                "Raw, processed, and temporary data must remain outside Git.",
                tuple(sorted(prohibited_tracked)),
            )
        )
    if tracked_large:
        diagnostics.append(
            FreezeDiagnostic(
                "SOURCE_LARGE_ARTIFACT_TRACKED",
                "error",
                (
                    f"Files larger than {large_artifact_bytes} bytes require "
                    "an explicit artifact store."
                ),
                tuple(sorted(tracked_large)),
            )
        )

    changed_text_paths = tuple(sorted(set(modified + staged + untracked)))
    conflict_paths = _conflict_marker_paths(root, changed_text_paths)
    if conflict_paths:
        diagnostics.append(
            FreezeDiagnostic(
                "SOURCE_CONFLICT_MARKER",
                "error",
                "Unresolved merge-conflict markers were found.",
                conflict_paths,
            )
        )

    frozen_record_count, immutable_checks = verify_frozen_records(root)
    failed_checks = tuple(check.file_path for check in immutable_checks if check.status != "PASS")
    if failed_checks:
        diagnostics.append(
            FreezeDiagnostic(
                "SOURCE_FROZEN_HASH_MISMATCH",
                "error",
                "At least one frozen result binding is missing, invalid, or modified.",
                failed_checks,
            )
        )
    if frozen_record_count == 0:
        diagnostics.append(
            FreezeDiagnostic(
                "SOURCE_NO_COMPATIBLE_FROZEN_RECORD",
                "warning",
                "No SHA-256 freeze record was found.",
            )
        )

    environment = Path(sys.prefix).name
    if environment.casefold() != expected_environment.casefold():
        diagnostics.append(
            FreezeDiagnostic(
                "SOURCE_WRONG_PYTHON_ENV",
                "error",
                f"Expected Python environment {expected_environment!r}, found {environment!r}.",
            )
        )

    ready = not any(item.severity == "error" for item in diagnostics)
    return SourceFreezeAudit(
        schema_version=1,
        status="READY" if ready else "NOT_READY",
        project_root=str(root),
        source_commit=source_commit or None,
        python_executable=sys.executable,
        python_environment=environment,
        expected_python_environment=expected_environment,
        modified_paths=modified,
        staged_paths=staged,
        untracked_paths=untracked,
        tracked_large_artifacts=tuple(sorted(tracked_large)),
        frozen_record_count=frozen_record_count,
        immutable_file_checks=immutable_checks,
        diagnostics=tuple(diagnostics),
    )
