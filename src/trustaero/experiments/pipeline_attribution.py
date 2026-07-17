"""Paired operator attribution for complete early/late Mask fragments.

DuckDB operator timings are profiling observations, not an additive causal
decomposition of wall-clock latency.  This module therefore compares paired
operator-role differences and reports association, dominance, and direction
reversal without claiming that one role independently caused the runtime gap.
"""

from __future__ import annotations

import csv
import json
import math
import statistics
from pathlib import Path
from typing import Any, cast

OPERATOR_ROLES = (
    "hash_projection",
    "support_projection",
    "hash_join",
    "order_by",
    "materialization",
    "event_scan",
    "dimension_scan",
    "output_sink",
)
_IGNORED_OPERATORS = {"EXPLAIN_ANALYZE"}
_SUPPORTED_OPERATORS = {
    "EXPLAIN_ANALYZE",
    "BATCH_CREATE_TABLE_AS",
    "CTE",
    "CTE_SCAN",
    "PROJECTION",
    "HASH_JOIN",
    "ORDER_BY",
    "SEQ_SCAN",
}


def _read_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return cast(dict[str, Any], payload)


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise ValueError(f"Missing Phase 2L artifact: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for field in row:
            if field not in seen:
                seen.add(field)
                fields.append(field)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _classify(log_early_late_ratio: float, tie_threshold_fraction: float) -> str:
    lower = math.log1p(-tie_threshold_fraction)
    # Match the frozen rule ``late < early * (1 - threshold)`` exactly.
    upper = -lower
    if log_early_late_ratio < lower:
        return "early"
    if log_early_late_ratio > upper:
        return "late"
    return "tie"


def _operator_role_totals(rows: list[dict[str, str]]) -> dict[str, float]:
    """Map one candidate's stable physical shape to semantic timing roles."""

    names = [row["operator_name"] for row in rows]
    unknown = set(names) - _SUPPORTED_OPERATORS
    if unknown:
        raise ValueError(f"Unknown physical operators in attribution input: {unknown}")
    if names.count("HASH_JOIN") != 1 or names.count("ORDER_BY") != 1:
        raise ValueError("Attribution candidate must contain one Join and one sort")
    if names.count("BATCH_CREATE_TABLE_AS") != 1:
        raise ValueError("Attribution candidate must contain one output sink")
    projections = [row for row in rows if row["operator_name"] == "PROJECTION"]
    scans = [row for row in rows if row["operator_name"] == "SEQ_SCAN"]
    if not projections or len(scans) != 2:
        raise ValueError("Attribution candidate has an unexpected projection/scan shape")

    # SHA-256 dominates the projection timings in this frozen fragment. The
    # role is selected by timing, not by a brittle operator index.
    hash_projection = max(
        projections, key=lambda row: float(row["median_operator_timing_ms"])
    )
    event_scan = max(scans, key=lambda row: int(row["rows_scanned"]))

    def timing(row: dict[str, str]) -> float:
        value = float(row["median_operator_timing_ms"])
        if value < 0.0:
            raise ValueError("Operator timing cannot be negative")
        return value

    roles = {role: 0.0 for role in OPERATOR_ROLES}
    roles["hash_projection"] = timing(hash_projection)
    roles["support_projection"] = sum(
        timing(row) for row in projections if row is not hash_projection
    )
    roles["hash_join"] = sum(
        timing(row) for row in rows if row["operator_name"] == "HASH_JOIN"
    )
    roles["order_by"] = sum(
        timing(row) for row in rows if row["operator_name"] == "ORDER_BY"
    )
    roles["materialization"] = sum(
        timing(row) for row in rows if row["operator_name"] in {"CTE", "CTE_SCAN"}
    )
    roles["event_scan"] = timing(event_scan)
    roles["dimension_scan"] = sum(timing(row) for row in scans if row is not event_scan)
    roles["output_sink"] = sum(
        timing(row)
        for row in rows
        if row["operator_name"] == "BATCH_CREATE_TABLE_AS"
    )
    accounted = sum(roles.values())
    expected = sum(
        timing(row) for row in rows if row["operator_name"] not in _IGNORED_OPERATORS
    )
    if not math.isclose(accounted, expected, rel_tol=1e-9, abs_tol=1e-9):
        raise ValueError("Operator-role mapping does not account for every profiled operator")
    return roles


def _average_ranks(values: list[float]) -> list[float]:
    """Return one-based average ranks with deterministic tie handling."""

    indexed = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    start = 0
    while start < len(indexed):
        end = start + 1
        while end < len(indexed) and indexed[end][1] == indexed[start][1]:
            end += 1
        rank = (start + 1 + end) / 2.0
        for position in range(start, end):
            ranks[indexed[position][0]] = rank
        start = end
    return ranks


def _pearson(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or len(left) < 2:
        raise ValueError("Correlation requires equal vectors with at least two values")
    left_mean = statistics.mean(left)
    right_mean = statistics.mean(right)
    numerator = sum(
        (a - left_mean) * (b - right_mean)
        for a, b in zip(left, right, strict=True)
    )
    left_scale = math.sqrt(sum((value - left_mean) ** 2 for value in left))
    right_scale = math.sqrt(sum((value - right_mean) ** 2 for value in right))
    if left_scale <= 1e-15 or right_scale <= 1e-15:
        return 0.0
    return numerator / (left_scale * right_scale)


def _spearman(left: list[float], right: list[float]) -> float:
    """Compute Spearman association without adding a scientific dependency."""

    return _pearson(_average_ranks(left), _average_ranks(right))


def _load_units(
    run_dirs: list[str | Path],
    tie_threshold_fraction: float,
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    component_units: dict[str, dict[str, dict[str, str]]] = {}
    operator_units: dict[str, dict[str, list[dict[str, str]]]] = {}
    source_run_ids: set[str] = set()
    source_commits: set[str] = set()
    for run_value in run_dirs:
        run_dir = Path(run_value).resolve()
        summary = _read_object(run_dir / "summary.json")
        unit_count = int(summary.get("unit_count", -1))
        if (
            summary.get("status") != "complete"
            or summary.get("all_validations_passed") is not True
            or int(summary.get("result_equivalent_fragment_count", -2)) != unit_count
            or int(summary.get("distinct_physical_plan_fragment_count", -3))
            != unit_count
        ):
            raise ValueError(f"Invalid Phase 2L source run: {run_dir}")
        run_id = str(summary["run_id"])
        source_run_ids.add(run_id)
        source_commits.add(
            str(_read_object(run_dir / "environment.json").get("commit_hash", "unknown"))
        )
        for row in _read_csv(run_dir / "component_summary.csv"):
            if row["benchmark"] != "mask_fragment":
                continue
            replicate_id = f"{run_id}/{row['unit_id']}"
            candidates = component_units.setdefault(replicate_id, {})
            if row["component"] in candidates:
                raise ValueError(f"Duplicate component summary: {replicate_id}")
            candidates[row["component"]] = row
        for row in _read_csv(run_dir / "operator_summary.csv"):
            if row["benchmark"] != "mask_fragment":
                continue
            replicate_id = f"{run_id}/{row['unit_id']}"
            operator_units.setdefault(replicate_id, {}).setdefault(
                row["component"], []
            ).append(row)

    expected = {"early_mask_fragment", "late_mask_fragment"}
    if set(component_units) != set(operator_units):
        raise ValueError("Component and operator summaries cover different units")
    output: list[dict[str, Any]] = []
    for replicate_id, candidates in sorted(component_units.items()):
        if set(candidates) != expected or set(operator_units[replicate_id]) != expected:
            raise ValueError(f"Incomplete paired attribution unit: {replicate_id}")
        early = candidates["early_mask_fragment"]
        late = candidates["late_mask_fragment"]
        early_ms = float(early["median_latency_ms"])
        late_ms = float(late["median_latency_ms"])
        log_ratio = math.log(early_ms / late_ms)
        early_roles = _operator_role_totals(
            operator_units[replicate_id]["early_mask_fragment"]
        )
        late_roles = _operator_role_totals(
            operator_units[replicate_id]["late_mask_fragment"]
        )
        relative_deltas = {
            role: (early_roles[role] - late_roles[role])
            / max(early_roles[role], late_roles[role], 1e-12)
            for role in OPERATOR_ROLES
        }
        # Dominance uses absolute profiled milliseconds. Relative differences
        # are reserved for cross-scale association; otherwise a tiny role that
        # is absent from one candidate would always appear dominant.
        dominant_role = max(
            OPERATOR_ROLES,
            key=lambda role: abs(early_roles[role] - late_roles[role]),
        )
        unit_result: dict[str, Any] = {
            "replicate_id": replicate_id,
            "family_id": (
                f"n{int(early['row_count'])}-w{int(early['identifier_width'])}-"
                f"m{round(float(early['match_rate']) * 1000):04d}"
            ),
            "row_count": int(early["row_count"]),
            "identifier_width": int(early["identifier_width"]),
            "match_rate": float(early["match_rate"]),
            "seed": int(early["seed"]),
            "early_latency_ms": early_ms,
            "late_latency_ms": late_ms,
            "observed_log_early_late_ratio": log_ratio,
            "classification": _classify(log_ratio, tie_threshold_fraction),
            "dominant_relative_difference_role": dominant_role,
        }
        for role in OPERATOR_ROLES:
            unit_result[f"early_{role}_ms"] = early_roles[role]
            unit_result[f"late_{role}_ms"] = late_roles[role]
            unit_result[f"{role}_delta_ms"] = early_roles[role] - late_roles[role]
            unit_result[f"{role}_relative_delta"] = relative_deltas[role]
        output.append(unit_result)
    if not output:
        raise ValueError("No paired Phase 2L units were loaded")
    return output, sorted(source_run_ids), sorted(source_commits)


def _family_rows(
    units: list[dict[str, Any]], required_agreement_fraction: float
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in units:
        grouped.setdefault(str(row["family_id"]), []).append(row)
    output: list[dict[str, Any]] = []
    for family_id, rows in sorted(grouped.items()):
        classes = [str(row["classification"]) for row in rows]
        required = math.ceil(len(rows) * required_agreement_fraction)
        if classes.count("early") >= required:
            family_class = "stable_early"
        elif classes.count("late") >= required:
            family_class = "stable_late"
        elif classes.count("tie") >= required:
            family_class = "stable_tie"
        else:
            family_class = "mixed"
        result: dict[str, Any] = {
            "family_id": family_id,
            "row_count": rows[0]["row_count"],
            "identifier_width": rows[0]["identifier_width"],
            "match_rate": rows[0]["match_rate"],
            "replicate_count": len(rows),
            "required_agreement_count": required,
            "early_count": classes.count("early"),
            "tie_count": classes.count("tie"),
            "late_count": classes.count("late"),
            "family_classification": family_class,
            "median_log_early_late_ratio": statistics.median(
                float(row["observed_log_early_late_ratio"]) for row in rows
            ),
        }
        for role in OPERATOR_ROLES:
            result[f"median_{role}_relative_delta"] = statistics.median(
                float(row[f"{role}_relative_delta"]) for row in rows
            )
            result[f"{role}_dominant_count"] = sum(
                row["dominant_relative_difference_role"] == role for row in rows
            )
        output.append(result)
    return output


def _role_rows(
    units: list[dict[str, Any]],
    families: list[dict[str, Any]],
    *,
    minimum_sign_agreement: float,
    minimum_absolute_spearman: float,
    minimum_dominant_family_fraction: float,
) -> list[dict[str, Any]]:
    decisive = [row for row in units if row["classification"] != "tie"]
    early_families = [
        row for row in families if row["family_classification"] == "stable_early"
    ]
    late_families = [
        row for row in families if row["family_classification"] == "stable_late"
    ]
    output: list[dict[str, Any]] = []
    for role in OPERATOR_ROLES:
        relative = [float(row[f"{role}_relative_delta"]) for row in units]
        log_ratios = [float(row["observed_log_early_late_ratio"]) for row in units]
        agreement = sum(
            math.copysign(1.0, float(row[f"{role}_relative_delta"]))
            == math.copysign(1.0, float(row["observed_log_early_late_ratio"]))
            for row in decisive
            if abs(float(row[f"{role}_relative_delta"])) > 1e-12
        )
        nonzero_decisive = sum(
            abs(float(row[f"{role}_relative_delta"])) > 1e-12 for row in decisive
        )
        sign_rate = agreement / nonzero_decisive if nonzero_decisive else 0.0
        early_median = (
            statistics.median(
                float(row[f"median_{role}_relative_delta"])
                for row in early_families
            )
            if early_families
            else float("nan")
        )
        late_median = (
            statistics.median(
                float(row[f"median_{role}_relative_delta"])
                for row in late_families
            )
            if late_families
            else float("nan")
        )
        dominant_family_count = sum(
            int(row[f"{role}_dominant_count"]) > 0 for row in families
        )
        dominant_fraction = dominant_family_count / len(families)
        direction_reverses = early_median < 0.0 < late_median
        checks = {
            "sign_agreement": sign_rate >= minimum_sign_agreement,
            "absolute_spearman": abs(_spearman(relative, log_ratios))
            >= minimum_absolute_spearman,
            "dominant_family_fraction": dominant_fraction
            >= minimum_dominant_family_fraction,
            "stable_region_direction_reversal": direction_reverses,
        }
        output.append(
            {
                "role": role,
                "decisive_unit_count": len(decisive),
                "sign_agreement_rate": sign_rate,
                "spearman_rho": _spearman(relative, log_ratios),
                "dominant_family_count": dominant_family_count,
                "dominant_family_fraction": dominant_fraction,
                "stable_early_median_relative_delta": early_median,
                "stable_late_median_relative_delta": late_median,
                "stable_region_direction_reversal": direction_reverses,
                **{f"check_{name}": value for name, value in checks.items()},
                "eligible_pipeline_interaction_role": all(checks.values()),
            }
        )
    return output


def _report(summary: dict[str, Any], roles: list[dict[str, Any]]) -> str:
    lines = [
        "# Phase 2L paired operator attribution",
        "",
        "This is development profiling association, not causal decomposition or Phase 2G.",
        "",
        f"- Paired replicates: {summary['replicate_count']}",
        f"- Physical families: {summary['family_count']}",
        f"- Stable early/late/tie/mixed: {summary['family_classification_counts']}",
        f"- Interaction-hypothesis eligible: {summary['interaction_hypothesis_eligible']}",
        "",
        "| Role | Sign agreement | Spearman | Dominant families | "
        "Early-region delta | Late-region delta | Eligible |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for row in roles:
        lines.append(
            "| {role} | {sign:.1%} | {rho:.3f} | {dominant:.1%} | "
            "{early:.3f} | {late:.3f} | {eligible} |".format(
                role=row["role"],
                sign=row["sign_agreement_rate"],
                rho=row["spearman_rho"],
                dominant=row["dominant_family_fraction"],
                early=row["stable_early_median_relative_delta"],
                late=row["stable_late_median_relative_delta"],
                eligible=row["eligible_pipeline_interaction_role"],
            )
        )
    lines.extend(
        [
            "",
            "> Operator timings come from separate EXPLAIN ANALYZE profiles and may ",
            "> include parallel CPU effects. Differences support hypotheses; they do ",
            "> not prove that one operator independently caused wall-clock speedup.",
        ]
    )
    return "\n".join(lines) + "\n"


def analyze_pipeline_operator_attribution(
    run_dirs: list[str | Path],
    output_dir: str | Path,
    *,
    tie_threshold_fraction: float = 0.03,
    required_family_agreement_fraction: float = 0.8,
    minimum_sign_agreement: float = 0.65,
    minimum_absolute_spearman: float = 0.4,
    minimum_dominant_family_fraction: float = 0.2,
) -> Path:
    """Execute the frozen Phase 2L descriptive attribution protocol."""

    fractions = (
        tie_threshold_fraction,
        required_family_agreement_fraction,
        minimum_sign_agreement,
        minimum_absolute_spearman,
        minimum_dominant_family_fraction,
    )
    if not all(0.0 <= value <= 1.0 for value in fractions):
        raise ValueError("Phase 2L thresholds must be fractions in [0, 1]")
    if required_family_agreement_fraction <= 0.5:
        raise ValueError("Family agreement must be a strict majority")
    units, run_ids, commits = _load_units(run_dirs, tie_threshold_fraction)
    families = _family_rows(units, required_family_agreement_fraction)
    roles = _role_rows(
        units,
        families,
        minimum_sign_agreement=minimum_sign_agreement,
        minimum_absolute_spearman=minimum_absolute_spearman,
        minimum_dominant_family_fraction=minimum_dominant_family_fraction,
    )
    family_counts = {
        name: sum(row["family_classification"] == name for row in families)
        for name in ("stable_early", "stable_late", "stable_tie", "mixed")
    }
    eligible_roles = [
        str(row["role"]) for row in roles if row["eligible_pipeline_interaction_role"]
    ]
    summary: dict[str, Any] = {
        "evaluation_label": "phase2l_paired_operator_attribution_development",
        "source_run_ids": run_ids,
        "source_commit_hashes": commits,
        "replicate_count": len(units),
        "family_count": len(families),
        "family_classification_counts": family_counts,
        "tie_threshold_fraction": tie_threshold_fraction,
        "required_family_agreement_fraction": required_family_agreement_fraction,
        "attribution_thresholds": {
            "minimum_sign_agreement": minimum_sign_agreement,
            "minimum_absolute_spearman": minimum_absolute_spearman,
            "minimum_dominant_family_fraction": minimum_dominant_family_fraction,
            "require_stable_region_direction_reversal": True,
        },
        "eligible_pipeline_interaction_roles": eligible_roles,
        "interaction_hypothesis_eligible": bool(eligible_roles),
        "phase2g_authorized": False,
        "scientific_boundary": (
            "Paired EXPLAIN ANALYZE operator differences are descriptive association. "
            "They may motivate a new versioned interaction hypothesis but are not "
            "an additive causal decomposition of timed wall-clock latency."
        ),
    }
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    _write_csv(output / "unit_operator_attribution.csv", units)
    _write_csv(output / "family_operator_attribution.csv", families)
    _write_csv(output / "role_summary.csv", roles)
    _write_json(output / "summary.json", summary)
    (output / "report.md").write_text(_report(summary, roles), encoding="utf-8")
    return output
