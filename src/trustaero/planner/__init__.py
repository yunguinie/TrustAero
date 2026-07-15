"""Physical planning entry points for TrustAero."""

from trustaero.planner.candidates import generate_duckdb_candidates
from trustaero.planner.physical import plan_physical_execution

__all__ = ["generate_duckdb_candidates", "plan_physical_execution"]
