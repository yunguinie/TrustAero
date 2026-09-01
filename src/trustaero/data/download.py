"""Safe, resumable downloads for reproducible experiment datasets.

The downloader deliberately has no third-party dependency. Large files are
written beside their final E-drive destination using a ``.part`` suffix. A
file is promoted atomically only after its byte count and optional checksum
have passed validation, so an interrupted download cannot masquerade as a
complete dataset.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ProgressCallback = Callable[[int, int | None], None]
_CHUNK_SIZE = 1024 * 1024


class DownloadError(RuntimeError):
    """Raised when an artifact cannot be acquired or verified safely."""


@dataclass(frozen=True, slots=True)
class ArtifactSpec:
    """One immutable external file declared in the dataset registry."""

    artifact_id: str
    dataset_id: str
    url: str
    relative_path: str
    stage: str
    expected_bytes: int | None = None
    expected_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """Verified properties of a local artifact."""

    artifact_id: str
    path: Path
    byte_size: int
    sha256: str
    reused_existing_file: bool


def _artifact_from_mapping(payload: dict[str, Any]) -> ArtifactSpec:
    """Validate a registry entry without silently accepting malformed values."""

    required = ("artifact_id", "dataset_id", "url", "relative_path", "stage")
    missing = [name for name in required if not payload.get(name)]
    if missing:
        raise DownloadError(f"Artifact entry is missing required values: {', '.join(missing)}")

    expected_bytes = payload.get("expected_bytes")
    if expected_bytes is not None and (not isinstance(expected_bytes, int) or expected_bytes <= 0):
        raise DownloadError("expected_bytes must be a positive integer when provided")

    expected_sha256 = payload.get("expected_sha256")
    if expected_sha256 is not None:
        expected_sha256 = str(expected_sha256).lower()
        if len(expected_sha256) != 64 or any(c not in "0123456789abcdef" for c in expected_sha256):
            raise DownloadError("expected_sha256 must contain exactly 64 hexadecimal characters")

    return ArtifactSpec(
        artifact_id=str(payload["artifact_id"]),
        dataset_id=str(payload["dataset_id"]),
        url=str(payload["url"]),
        relative_path=str(payload["relative_path"]),
        stage=str(payload["stage"]),
        expected_bytes=expected_bytes,
        expected_sha256=expected_sha256,
    )


def load_artifact_registry(path: Path) -> tuple[ArtifactSpec, ...]:
    """Load the tracked registry and reject duplicate artifact identifiers."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DownloadError(f"Cannot read dataset registry {path}: {exc}") from exc

    raw_artifacts = payload.get("artifacts")
    if not isinstance(raw_artifacts, list):
        raise DownloadError("Dataset registry must contain an 'artifacts' list")

    artifacts = tuple(_artifact_from_mapping(item) for item in raw_artifacts)
    ids = [item.artifact_id for item in artifacts]
    if len(ids) != len(set(ids)):
        raise DownloadError("Dataset registry contains duplicate artifact_id values")
    return artifacts


def _safe_destination(data_root: Path, relative_path: str) -> Path:
    """Resolve an artifact path and prevent writes outside the project data root."""

    root = data_root.resolve()
    destination = (root / relative_path).resolve()
    try:
        destination.relative_to(root)
    except ValueError as exc:
        raise DownloadError(f"Artifact path escapes the data root: {relative_path}") from exc
    if destination == root:
        raise DownloadError("Artifact destination must name a file below the data root")
    return destination


def sha256_file(path: Path) -> str:
    """Hash a file incrementally so large datasets do not need to fit in RAM."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _content_total(headers: Any, resume_offset: int) -> int | None:
    """Derive the full byte count from Content-Range or Content-Length."""

    content_range = headers.get("Content-Range")
    if content_range and "/" in content_range:
        total_text = content_range.rsplit("/", 1)[1]
        if total_text.isdigit():
            return int(total_text)
    content_length = headers.get("Content-Length")
    if content_length and str(content_length).isdigit():
        return resume_offset + int(content_length)
    return None


def _response_status(response: BinaryIO) -> int:
    """Read an HTTP status while remaining testable with file-like responses."""

    status = getattr(response, "status", None)
    if isinstance(status, int):
        return status
    getcode = getattr(response, "getcode", None)
    return int(getcode()) if callable(getcode) and getcode() is not None else 200


def _stream_chunks(response: BinaryIO) -> Iterator[bytes]:
    while True:
        chunk = response.read(_CHUNK_SIZE)
        if not chunk:
            return
        yield chunk


def _verify_part(part_path: Path, spec: ArtifactSpec) -> tuple[int, str]:
    byte_size = part_path.stat().st_size
    if spec.expected_bytes is not None and byte_size != spec.expected_bytes:
        raise DownloadError(
            f"Byte-size mismatch for {spec.artifact_id}: "
            f"expected {spec.expected_bytes}, received {byte_size}"
        )
    digest = sha256_file(part_path)
    if spec.expected_sha256 is not None and digest != spec.expected_sha256:
        raise DownloadError(
            f"SHA-256 mismatch for {spec.artifact_id}: "
            f"expected {spec.expected_sha256}, received {digest}"
        )
    return byte_size, digest


def _existing_result(destination: Path, spec: ArtifactSpec) -> DownloadResult | None:
    """Reuse only a complete local file that still satisfies declared invariants."""

    if not destination.is_file():
        return None
    try:
        byte_size, digest = _verify_part(destination, spec)
    except DownloadError:
        return None
    return DownloadResult(spec.artifact_id, destination, byte_size, digest, True)


def download_artifact(
    spec: ArtifactSpec,
    data_root: Path,
    *,
    progress: ProgressCallback | None = None,
    timeout_seconds: float = 60.0,
) -> DownloadResult:
    """Download, verify, and atomically publish one registered artifact.

    If a server accepts HTTP Range requests, an existing ``.part`` file is
    resumed. If it ignores the Range header, the partial file is safely
    overwritten from byte zero. The final path is never exposed before
    validation succeeds.
    """

    destination = _safe_destination(data_root, spec.relative_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    existing = _existing_result(destination, spec)
    if existing is not None:
        if progress is not None:
            progress(existing.byte_size, existing.byte_size)
        return existing

    part_path = destination.with_name(f"{destination.name}.part")
    resume_offset = part_path.stat().st_size if part_path.exists() else 0
    headers = {"User-Agent": "TrustAero-dataset-downloader/0.1"}
    if resume_offset:
        headers["Range"] = f"bytes={resume_offset}-"
    request = Request(spec.url, headers=headers)

    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
            status = _response_status(response)
            resumed = resume_offset > 0 and status == 206
            write_offset = resume_offset if resumed else 0
            mode = "ab" if resumed else "wb"
            total = _content_total(response.headers, write_offset)
            with part_path.open(mode) as output:
                current = write_offset
                if progress is not None:
                    progress(current, total)
                for chunk in _stream_chunks(response):
                    output.write(chunk)
                    current += len(chunk)
                    if progress is not None:
                        progress(current, total)
    except (HTTPError, URLError, OSError, TimeoutError) as exc:
        raise DownloadError(f"Download failed for {spec.artifact_id}: {exc}") from exc

    byte_size, digest = _verify_part(part_path, spec)
    os.replace(part_path, destination)
    return DownloadResult(spec.artifact_id, destination, byte_size, digest, False)


class TerminalProgress:
    """A compact progress bar suitable for the VS Code PowerShell terminal."""

    def __init__(self, label: str, *, min_interval_seconds: float = 0.2) -> None:
        self.label = label
        self.min_interval_seconds = min_interval_seconds
        self.started_at = time.monotonic()
        self.last_rendered_at = 0.0
        self.finished = False

    @staticmethod
    def _format_bytes(value: float) -> str:
        units = ("B", "KiB", "MiB", "GiB", "TiB")
        for unit in units:
            if value < 1024 or unit == units[-1]:
                return f"{value:.1f} {unit}"
            value /= 1024
        return f"{value:.1f} TiB"

    def __call__(self, current: int, total: int | None) -> None:
        now = time.monotonic()
        complete = total is not None and current >= total
        if not complete and now - self.last_rendered_at < self.min_interval_seconds:
            return
        self.last_rendered_at = now

        elapsed = max(now - self.started_at, 1e-9)
        speed = current / elapsed
        if total:
            fraction = min(current / total, 1.0)
            width = 24
            filled = int(width * fraction)
            bar = "#" * filled + "-" * (width - filled)
            remaining = max(total - current, 0)
            eta = remaining / speed if speed > 0 else 0.0
            message = (
                f"\r{self.label:<24} [{bar}] {fraction:6.1%} "
                f"{self._format_bytes(current)}/{self._format_bytes(total)} "
                f"{self._format_bytes(speed)}/s ETA {eta:6.1f}s"
            )
        else:
            message = (
                f"\r{self.label:<24} {self._format_bytes(current)} {self._format_bytes(speed)}/s"
            )
        print(message, end="\n" if complete else "", flush=True)
        self.finished = complete

    def finish_unknown_total(self) -> None:
        """Terminate the current terminal line when the server hid total size."""

        if not self.finished:
            print(flush=True)
