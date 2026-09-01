"""Deterministic synthetic DuckDB workloads with controlled statistics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, Self


class SyntheticConnection(Protocol):
    """DuckDB methods required by the synthetic workload generator."""

    def execute(self, query: str, parameters: tuple[Any, ...] = ()) -> Self: ...

    def fetchone(self) -> tuple[Any, ...] | None: ...


@dataclass(frozen=True)
class SyntheticDataConfig:
    """Control six statistics relevant to governed query planning.

    Selectivities are fractions of the fact-table row count. ``hot_key_fraction``
    controls join skew and cannot exceed ``join_match_rate`` because hot rows
    must still match the dimension table.
    """

    workload_id: str
    row_count: int
    temporal_selectivity: float
    spatial_selectivity: float
    policy_selectivity: float
    join_match_rate: float
    hot_key_fraction: float
    identifier_width: int = 18
    seed: int = 0

    def __post_init__(self) -> None:
        if not self.workload_id:
            raise ValueError("workload_id cannot be empty")
        if self.row_count < 1:
            raise ValueError("row_count must be positive")
        for name in (
            "temporal_selectivity",
            "spatial_selectivity",
            "policy_selectivity",
            "join_match_rate",
            "hot_key_fraction",
        ):
            value = float(getattr(self, name))
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]")
        if self.hot_key_fraction > self.join_match_rate:
            raise ValueError("hot_key_fraction cannot exceed join_match_rate")
        if not 18 <= self.identifier_width <= 4096:
            raise ValueError("identifier_width must be between 18 and 4096 characters")
        if self.seed < 0:
            raise ValueError("seed cannot be negative")


@dataclass(frozen=True)
class SyntheticWorkloadStats:
    """Observed counts and selectivities measured back from DuckDB."""

    workload_id: str
    row_count: int
    dimension_row_count: int
    temporal_rows: int
    spatial_rows: int
    policy_rows: int
    join_matched_rows: int
    hot_key_rows: int
    temporal_selectivity: float
    spatial_selectivity: float
    policy_selectivity: float
    join_match_rate: float
    hot_key_fraction: float
    identifier_width: int


def _count(row_count: int, fraction: float) -> int:
    """Convert a target fraction to a deterministic, bounded row count."""

    return max(0, min(row_count, round(row_count * fraction)))


def generate_synthetic_workload(
    connection: SyntheticConnection,
    config: SyntheticDataConfig,
) -> SyntheticWorkloadStats:
    """Create fact/dimension tables and measure their realized statistics.

    Rotated row indices make temporal, spatial, and policy membership exact but
    not identical. The SQL uses DuckDB ``range`` so future large workloads do
    not materialize millions of Python tuples first.
    """

    row_count = config.row_count
    temporal_count = _count(row_count, config.temporal_selectivity)
    spatial_count = _count(row_count, config.spatial_selectivity)
    policy_count = _count(row_count, config.policy_selectivity)
    match_count = _count(row_count, config.join_match_rate)
    hot_count = _count(row_count, config.hot_key_fraction)
    temporal_offset = config.seed % row_count
    # Nearby rotations preserve exact marginal selectivities while keeping a
    # useful non-zero overlap for selective multi-predicate pilot workloads.
    # Correlation becomes a separate future control rather than an accidental
    # consequence of offsets that can make all predicates disjoint.
    spatial_offset = (config.seed + row_count // 20) % row_count
    policy_offset = (config.seed + row_count // 10) % row_count

    connection.execute(
        """
        CREATE OR REPLACE TABLE synthetic_events AS
        WITH base AS (
          SELECT CAST(i AS BIGINT) AS i FROM range(?) AS generated(i)
        )
        SELECT
          CASE WHEN ? = 18
            THEN printf('event-%012d', i)
            ELSE rpad(printf('event-%012d', i), ?, 'x')
          END AS event_id,
          CASE WHEN ((i + ?) % ?) < ?
            THEN TIMESTAMPTZ '2026-06-01 00:00:00+00:00'
                 + CAST((i % 86400) AS BIGINT) * INTERVAL '1 second'
            ELSE TIMESTAMPTZ '2026-07-01 00:00:00+00:00'
                 + CAST((i % 86400) AS BIGINT) * INTERVAL '1 second'
          END AS event_time,
          CASE WHEN ((i + ?) % ?) < ?
            THEN 40.0 + CAST((i % 7) - 3 AS DOUBLE) * 0.001
            ELSE 10.0 + CAST(i % 7 AS DOUBLE) * 0.001
          END AS latitude,
          CASE WHEN ((i + ?) % ?) < ?
            THEN 116.3 + CAST((i % 5) - 2 AS DOUBLE) * 0.001
            ELSE 10.0 + CAST(i % 5 AS DOUBLE) * 0.001
          END AS longitude,
          ((i + ?) % ?) < ? AS policy_allowed,
          CASE
            WHEN i < ? THEN 'hot-key'
            WHEN i < ? THEN printf('key-%012d', i)
            ELSE printf('missing-%012d', i)
          END AS join_key,
          3.0 + CAST(i % 50 AS DOUBLE) / 10.0 AS magnitude
        FROM base
        """,
        (
            row_count,
            config.identifier_width,
            config.identifier_width,
            temporal_offset,
            row_count,
            temporal_count,
            spatial_offset,
            row_count,
            spatial_count,
            spatial_offset,
            row_count,
            spatial_count,
            policy_offset,
            row_count,
            policy_count,
            hot_count,
            match_count,
        ),
    )

    # The dimension key deliberately has a different field name from the fact
    # key.  This keeps the trusted IR join output unambiguous: both fields may
    # coexist without silently shadowing one another.
    hot_sql = "SELECT 'hot-key' AS dimension_key, 'hot' AS severity_label" if hot_count else None
    unique_sql = (
        "SELECT printf('key-%012d', i) AS dimension_key, "
        "printf('severity-%d', i % 5) AS severity_label "
        "FROM range(?, ?) AS generated(i)"
        if match_count > hot_count
        else None
    )
    if hot_sql and unique_sql:
        dimension_sql = f"CREATE OR REPLACE TABLE severity_dim AS {hot_sql} UNION ALL {unique_sql}"
        connection.execute(dimension_sql, (hot_count, match_count))
    elif hot_sql:
        connection.execute(f"CREATE OR REPLACE TABLE severity_dim AS {hot_sql}")
    elif unique_sql:
        connection.execute(
            f"CREATE OR REPLACE TABLE severity_dim AS {unique_sql}",
            (hot_count, match_count),
        )
    else:
        connection.execute(
            "CREATE OR REPLACE TABLE severity_dim(dimension_key VARCHAR, severity_label VARCHAR)"
        )

    row = connection.execute(
        """
        SELECT
          COUNT(*) AS row_count,
          COUNT(*) FILTER (
            WHERE event_time >= TIMESTAMPTZ '2026-06-01 00:00:00+00:00'
              AND event_time < TIMESTAMPTZ '2026-06-02 00:00:00+00:00'
          ) AS temporal_rows,
          COUNT(*) FILTER (
            WHERE 111.045 * sqrt(
              power(latitude - 40.0, 2)
              + power((longitude - 116.3) * cos(radians(40.0)), 2)
            ) <= 20.0
          ) AS spatial_rows,
          COUNT(*) FILTER (WHERE policy_allowed) AS policy_rows,
          COUNT(*) FILTER (WHERE join_key = 'hot-key') AS hot_key_rows
        FROM synthetic_events
        """
    ).fetchone()
    dimension_row = connection.execute("SELECT COUNT(*) FROM severity_dim").fetchone()
    join_row = connection.execute(
        """
        SELECT COUNT(*)
        FROM synthetic_events AS events
        INNER JOIN severity_dim AS dimension
          ON events.join_key = dimension.dimension_key
        """
    ).fetchone()
    if row is None or dimension_row is None or join_row is None:
        raise RuntimeError("DuckDB did not return synthetic workload statistics.")

    observed_count = int(row[0])

    def ratio(value: Any) -> float:
        # DuckDB values are dynamically typed at the DB-API boundary; the SQL
        # query guarantees these particular columns are integer counts.
        return float(value) / observed_count

    return SyntheticWorkloadStats(
        workload_id=config.workload_id,
        row_count=observed_count,
        dimension_row_count=int(dimension_row[0]),
        temporal_rows=int(row[1]),
        spatial_rows=int(row[2]),
        policy_rows=int(row[3]),
        join_matched_rows=int(join_row[0]),
        hot_key_rows=int(row[4]),
        temporal_selectivity=ratio(row[1]),
        spatial_selectivity=ratio(row[2]),
        policy_selectivity=ratio(row[3]),
        join_match_rate=ratio(join_row[0]),
        hot_key_fraction=ratio(row[4]),
        identifier_width=config.identifier_width,
    )
