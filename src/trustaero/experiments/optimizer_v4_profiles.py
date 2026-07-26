"""Development-only DuckDB operator profiles for Optimizer V4 design.

Profiles are descriptive calibration evidence.  Operator timings and actual
cardinalities written here are forbidden as inference-time V4 features.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from trustaero.data import verify_bts_mask_join_full_month_artifacts
from trustaero.execution import compile_approved_physical_plan, observe_duckdb_plan
from trustaero.experiments.optimizer_v4_statistics import (
    extract_january_pipeline_statistics,
)
from trustaero.experiments.real_data_governed import (
    GovernedRealDataSmokeError,
    _atomic_json,
    _load_json,
    _sql_literal,
)
from trustaero.experiments.real_data_pilot import _git_state, _Progress
from trustaero.experiments.real_optimizer_transfer import (
    EARLY_CANDIDATE,
    LATE_CANDIDATE,
    _candidate_id,
    _controlled_views,
    build_real_transfer_candidates,
)
from trustaero.reproducibility.source_freeze import sha256_file


@dataclass(frozen=True, slots=True)
class OptimizerV4ProfileConfig:
    """Frozen controls for the January development profile collection."""

    protocol_name: str
    results_dir: str
    identifier_widths: tuple[int, ...]
    target_match_rates: tuple[float, ...]
    profile_runs: int
    duckdb_threads: int
    duckdb_memory_limit_mb: int
    require_clean_git: bool
    statistics_path: str
    statistics_sha256: str
    scientific_boundary: str

    def __post_init__(self) -> None:
        if not self.protocol_name or not self.results_dir or not self.scientific_boundary:
            raise ValueError("V4 profile protocol identity and boundary are required")
        if not self.identifier_widths or any(width < 64 for width in self.identifier_widths):
            raise ValueError("V4 profile widths must be at least the digest width")
        if not self.target_match_rates or any(
            not 0.0 < rate <= 1.0 for rate in self.target_match_rates
        ):
            raise ValueError("V4 profile match rates must be in (0, 1]")
        if self.profile_runs < 1:
            raise ValueError("V4 profile_runs must be positive")
        if self.duckdb_threads < 1 or self.duckdb_memory_limit_mb < 512:
            raise ValueError("V4 DuckDB profile controls are invalid")
        if len(self.statistics_sha256) != 64:
            raise ValueError("V4 statistics binding must be a SHA-256 digest")


def load_optimizer_v4_profile_config(path: Path | str) -> OptimizerV4ProfileConfig:
    """Load the immutable profile protocol."""

    payload = cast(dict[str, Any], _load_json(Path(path)))
    return OptimizerV4ProfileConfig(
        protocol_name=str(payload["protocol_name"]),
        results_dir=str(payload["results_dir"]),
        identifier_widths=tuple(int(value) for value in payload["identifier_widths"]),
        target_match_rates=tuple(float(value) for value in payload["target_match_rates"]),
        profile_runs=int(payload["profile_runs"]),
        duckdb_threads=int(payload["duckdb_threads"]),
        duckdb_memory_limit_mb=int(payload["duckdb_memory_limit_mb"]),
        require_clean_git=bool(payload["require_clean_git"]),
        statistics_path=str(payload["statistics_path"]),
        statistics_sha256=str(payload["statistics_sha256"]),
        scientific_boundary=str(payload["scientific_boundary"]),
    )


def _checksum_existing_output(connection: Any) -> tuple[Any, ...]:
    row = connection.execute(
        "SELECT count(*)::BIGINT, "
        "coalesce(sum(length(Tail_Number)), 0)::HUGEINT, "
        "coalesce(bit_xor(hash(Tail_Number, airport_code, city_name, state_code, Distance)), 0) "
        "FROM transfer_output"
    ).fetchone()
    if row is None:
        raise GovernedRealDataSmokeError("V4 profile output checksum is missing")
    return tuple(row)


def _profile_family(
    connection: Any,
    root: Path,
    config: OptimizerV4ProfileConfig,
    *,
    width: int,
    target_match_rate: float,
    plan_dir: Path,
    progress: _Progress,
) -> dict[str, object]:
    logical, catalog, candidates = build_real_transfer_candidates(root)
    bindings, _, _, _ = _controlled_views(
        connection, root, width=width, target_match_rate=target_match_rate
    )
    stats, metadata = extract_january_pipeline_statistics(
        connection, root, width=width, target_match_rate=target_match_rate
    )
    compiled = {
        _candidate_id(candidate): compile_approved_physical_plan(
            logical, candidate, catalog, bindings
        )
        for candidate in candidates
    }
    candidate_ids = (EARLY_CANDIDATE, LATE_CANDIDATE)
    observations: dict[str, list[Any]] = {item: [] for item in candidate_ids}
    checksums: dict[str, tuple[Any, ...]] = {}
    plan_dir.mkdir(parents=True, exist_ok=True)
    for profile_index in range(config.profile_runs):
        order = candidate_ids if profile_index % 2 == 0 else tuple(reversed(candidate_ids))
        for candidate_id in order:
            query = compiled[candidate_id]
            connection.execute("DROP TABLE IF EXISTS transfer_output")
            observation = observe_duckdb_plan(
                connection,
                "CREATE TEMP TABLE transfer_output AS " + query.sql,
                query.parameters,
                analyze=True,
            )
            checksum = _checksum_existing_output(connection)
            if candidate_id in checksums and checksums[candidate_id] != checksum:
                raise GovernedRealDataSmokeError("V4 profile checksum changed across runs")
            checksums[candidate_id] = checksum
            observations[candidate_id].append(observation)
            _atomic_json(
                plan_dir / f"{candidate_id}-analyze-r{profile_index}.json",
                json.loads(observation.plan_json),
            )
            progress.advance(f"profile w{width} r{target_match_rate:g} {candidate_id}")
    connection.execute("DROP TABLE IF EXISTS transfer_output")

    profiles: dict[str, object] = {}
    for candidate_id, samples in observations.items():
        reference = samples[0]
        shape_stable = (
            len({item.fingerprint for item in samples}) == 1
            and len({item.operator_names for item in samples}) == 1
            and len({item.actual_cardinalities for item in samples}) == 1
        )
        if not shape_stable:
            raise GovernedRealDataSmokeError("V4 physical profile shape changed")
        profiles[candidate_id] = {
            "fingerprint": reference.fingerprint,
            "operator_names": list(reference.operator_names),
            "operator_cardinalities": list(reference.actual_cardinalities),
            "rows_scanned": list(reference.rows_scanned),
            "median_operator_timings_ms": [
                statistics.median(item.operator_timings_ms[index] for item in samples)
                for index in range(len(reference.operator_names))
            ],
            "profile_latency_samples_ms": [item.profile_latency_ms for item in samples],
            "median_profile_latency_ms": statistics.median(
                item.profile_latency_ms for item in samples
            ),
            "peak_buffer_memory_bytes": max(item.peak_buffer_memory_bytes for item in samples),
            "peak_temp_directory_bytes": max(item.peak_temp_directory_bytes for item in samples),
            "total_memory_allocated_bytes": max(
                item.total_memory_allocated_bytes for item in samples
            ),
            "profile_runs": len(samples),
            "shape_stable": shape_stable,
            "timings_are_inference_features": False,
        }
    fingerprints = {
        str(cast(dict[str, object], profile)["fingerprint"]) for profile in profiles.values()
    }
    return {
        "family_id": f"bts-jan-w{width}-target{target_match_rate:.2f}",
        "status": "PASS"
        if len(set(checksums.values())) == 1 and len(fingerprints) == 2
        else "FAIL",
        "statistics": stats.to_dict(),
        "statistics_metadata": metadata,
        "result_equivalent": len(set(checksums.values())) == 1,
        "physical_plans_distinct": len(fingerprints) == 2,
        "profiles": profiles,
        "semantic_checksum": [str(item) for item in next(iter(checksums.values()))],
    }


def _summarize(families: list[dict[str, object]]) -> dict[str, object]:
    """Apply structural profile gates without making performance claims."""

    all_profiles: list[dict[str, Any]] = [
        cast(dict[str, Any], profile)
        for family in families
        for profile in cast(dict[str, object], family["profiles"]).values()
    ]
    passed = all(family["status"] == "PASS" for family in families) and all(
        profile["shape_stable"]
        and int(profile["peak_temp_directory_bytes"]) == 0
        and profile["timings_are_inference_features"] is False
        for profile in all_profiles
    )
    return {
        "schema_version": 1,
        "status": "PASS" if passed else "FAIL",
        "scientific_label": "january_v4_operator_profiles_development_only",
        "family_count": len(families),
        "profile_count": len(all_profiles),
        "all_results_equivalent": all(family["result_equivalent"] for family in families),
        "all_physical_plans_distinct": all(
            family["physical_plans_distinct"] for family in families
        ),
        "all_shapes_stable": all(profile["shape_stable"] for profile in all_profiles),
        "spilled_profile_count": sum(
            int(profile["peak_temp_directory_bytes"]) > 0 for profile in all_profiles
        ),
        "operator_timings_are_inference_features": False,
        "model_fitted": False,
        "interpretation": (
            "Operator profiles are descriptive January development evidence for "
            "choosing a compact model structure. They are not additive causal costs, "
            "inference features, a V4 accuracy result, or external holdout evidence."
        ),
    }


def run_optimizer_v4_profiles(
    config: OptimizerV4ProfileConfig,
    *,
    project_root: Path,
    config_path: Path,
    resume_run_id: str | None = None,
    show_progress: bool = False,
) -> Path:
    """Run or resume January profiles with an atomic checkpoint per family."""

    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise GovernedRealDataSmokeError("DuckDB is required for V4 profiles") from exc
    root = project_root.resolve()
    verify_bts_mask_join_full_month_artifacts(root / "data")
    statistics_path = root / config.statistics_path
    if sha256_file(statistics_path) != config.statistics_sha256:
        raise GovernedRealDataSmokeError("Frozen V4 statistics binding changed")
    commit, dirty = _git_state(root)
    if config.require_clean_git and dirty:
        raise GovernedRealDataSmokeError("V4 profiles require a clean committed worktree")
    results_root = root / config.results_dir
    if resume_run_id is not None:
        if resume_run_id == "latest":
            resume_run_id = str(
                cast(dict[str, Any], _load_json(results_root / "latest_run.json"))["run_id"]
            )
        run_dir = results_root / resume_run_id
        environment = cast(dict[str, Any], _load_json(run_dir / "environment.json"))
        if environment["commit_hash"] != commit:
            raise GovernedRealDataSmokeError("V4 profile resume commit changed")
        if environment["config_sha256"] != sha256_file(config_path):
            raise GovernedRealDataSmokeError("V4 profile resume config changed")
    else:
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        run_dir = results_root / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        _atomic_json(run_dir / "config.json", asdict(config))
        _atomic_json(
            run_dir / "environment.json",
            {
                "commit_hash": commit,
                "git_dirty": dirty,
                "config_sha256": sha256_file(config_path),
                "duckdb_threads": config.duckdb_threads,
                "duckdb_memory_limit_mb": config.duckdb_memory_limit_mb,
                "gpu_acceleration": False,
                "profile_timings_are_descriptive_only": True,
            },
        )
        _atomic_json(results_root / "latest_run.json", {"run_id": run_id})
    family_dir = run_dir / "families"
    plan_root = run_dir / "plans"
    family_dir.mkdir(parents=True, exist_ok=True)
    total = len(config.identifier_widths) * len(config.target_match_rates) * config.profile_runs * 2
    progress = _Progress(total, show_progress)
    connection = duckdb.connect()
    try:
        connection.execute(f"SET threads = {config.duckdb_threads}")
        connection.execute(f"SET memory_limit = '{config.duckdb_memory_limit_mb}MB'")
        temp_dir = root / "data/tmp/duckdb-optimizer-v4-profiles"
        temp_dir.mkdir(parents=True, exist_ok=True)
        connection.execute(f"SET temp_directory = {_sql_literal(temp_dir)}")
        for width in config.identifier_widths:
            for rate in config.target_match_rates:
                family_id = f"bts-jan-w{width}-target{rate:.2f}"
                target = family_dir / f"{family_id}.json"
                if target.is_file():
                    for _ in range(config.profile_runs * 2):
                        progress.advance(f"resume skip {family_id}")
                    continue
                family = _profile_family(
                    connection,
                    root,
                    config,
                    width=width,
                    target_match_rate=rate,
                    plan_dir=plan_root / family_id,
                    progress=progress,
                )
                _atomic_json(target, family)
                _atomic_json(
                    run_dir / "checkpoint.json",
                    {"last_completed_family": family_id},
                )
    finally:
        connection.close()
    families = [
        cast(dict[str, object], _load_json(path)) for path in sorted(family_dir.glob("*.json"))
    ]
    expected = len(config.identifier_widths) * len(config.target_match_rates)
    if len(families) != expected:
        raise GovernedRealDataSmokeError("V4 profiles are incomplete; resume the run")
    _atomic_json(run_dir / "summary.json", _summarize(families))
    return run_dir
