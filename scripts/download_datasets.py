"""Download registered experiment artifacts into this repository's E-drive data tree.

Examples from an activated ``TrustAero_env`` PowerShell terminal:

    python scripts/download_datasets.py --list
    python scripts/download_datasets.py --stage smoke

The second command displays a progress bar, preserves partial downloads for
safe resumption, verifies declared byte counts, and writes SHA-256 audit files.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from trustaero.data.download import (
    ArtifactSpec,
    DownloadError,
    DownloadResult,
    TerminalProgress,
    download_artifact,
    load_artifact_registry,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = PROJECT_ROOT / "data"
REGISTRY_PATH = DATA_ROOT / "manifests" / "dataset-registry.json"


def _select_artifacts(
    artifacts: tuple[ArtifactSpec, ...], artifact_ids: list[str], stage: str | None
) -> tuple[ArtifactSpec, ...]:
    requested = set(artifact_ids)
    if requested:
        known = {artifact.artifact_id for artifact in artifacts}
        unknown = sorted(requested - known)
        if unknown:
            raise DownloadError(f"Unknown artifact_id values: {', '.join(unknown)}")
        return tuple(artifact for artifact in artifacts if artifact.artifact_id in requested)
    if stage is not None:
        return tuple(artifact for artifact in artifacts if artifact.stage == stage)
    return ()


def _write_audit(spec: ArtifactSpec, result_path: Path, byte_size: int, sha256: str) -> Path:
    """Record exactly what was acquired without storing machine-specific paths."""

    audit_dir = DATA_ROOT / "manifests" / "downloads"
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_path = audit_dir / f"{spec.artifact_id}.json"
    payload = {
        "schema_version": 1,
        "artifact": asdict(spec),
        "retrieved_at_utc": datetime.now(UTC).isoformat(),
        "local_path": result_path.relative_to(PROJECT_ROOT).as_posix(),
        "byte_size": byte_size,
        "sha256": sha256,
    }
    audit_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return audit_path


def _download_with_retries(
    artifact: ArtifactSpec, *, retries: int, timeout_seconds: float
) -> DownloadResult:
    """Resume an interrupted artifact with bounded, visible retry attempts.

    Some government data servers are reliable but slow enough to leave an
    individual socket read idle. ``download_artifact`` deliberately preserves
    its verified prefix as ``.part``; this wrapper reopens that prefix with an
    HTTP Range request instead of discarding already received bytes.
    """

    for attempt in range(1, retries + 2):
        progress = TerminalProgress(artifact.artifact_id)
        try:
            result = download_artifact(
                artifact,
                DATA_ROOT,
                progress=progress,
                timeout_seconds=timeout_seconds,
            )
            progress.finish_unknown_total()
            return result
        except DownloadError:
            progress.finish_unknown_total()
            if attempt > retries:
                raise
            delay_seconds = min(2 ** (attempt - 1), 30)
            print(
                f"  network attempt {attempt} failed; preserving .part and "
                f"resuming in {delay_seconds}s ({retries - attempt + 1} retries left)"
            )
            time.sleep(delay_seconds)
    raise AssertionError("retry loop must return or raise")


def _print_registry(artifacts: tuple[ArtifactSpec, ...]) -> None:
    print("Registered downloadable artifacts:")
    for artifact in artifacts:
        expected = str(artifact.expected_bytes) if artifact.expected_bytes else "server-reported"
        print(
            f"- {artifact.artifact_id}: stage={artifact.stage}, bytes={expected}, "
            f"path=data/{artifact.relative_path}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="List registry artifacts and exit.")
    parser.add_argument(
        "--artifact",
        action="append",
        default=[],
        help="Download one artifact_id; repeat the flag to select several artifacts.",
    )
    parser.add_argument("--stage", help="Download every artifact in a registry stage, e.g. smoke.")
    parser.add_argument(
        "--retries",
        type=int,
        default=5,
        help="Number of resumable retries after a network failure (default: 5).",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=120.0,
        help="Socket timeout for each attempt (default: 120 seconds).",
    )
    args = parser.parse_args()

    if args.retries < 0:
        parser.error("--retries must be non-negative")
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")

    try:
        artifacts = load_artifact_registry(REGISTRY_PATH)
        if args.list:
            _print_registry(artifacts)
            return 0
        selected = _select_artifacts(artifacts, args.artifact, args.stage)
        if not selected:
            parser.error("choose --list, --artifact ARTIFACT_ID, or --stage STAGE")

        print(f"Data root: {DATA_ROOT}")
        for index, artifact in enumerate(selected, start=1):
            print(f"\n[{index}/{len(selected)}] {artifact.artifact_id}")
            result = _download_with_retries(
                artifact,
                retries=args.retries,
                timeout_seconds=args.timeout_seconds,
            )
            audit_path = _write_audit(artifact, result.path, result.byte_size, result.sha256)
            action = "verified existing" if result.reused_existing_file else "downloaded"
            print(f"  {action}: {result.path}")
            print(f"  SHA-256: {result.sha256}")
            print(f"  audit: {audit_path}")
        return 0
    except DownloadError as exc:
        print(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
