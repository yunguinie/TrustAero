"""Reproducible dataset acquisition utilities for TrustAero experiments."""

from trustaero.data.artifacts import (
    PreparedArtifactVerificationError,
    VerifiedPreparedArtifact,
    verify_bts_mask_join_full_month_artifacts,
    verify_bts_mask_join_slice_artifacts,
    verify_bts_multijoin_full_month_artifacts,
    verify_bts_multijoin_slice_artifacts,
    verify_real_data_full_month_artifacts,
    verify_real_data_slice_artifacts,
)
from trustaero.data.download import (
    ArtifactSpec,
    DownloadError,
    DownloadResult,
    download_artifact,
    load_artifact_registry,
)
from trustaero.data.multisource import (
    MultisourcePreparationError,
    MultisourcePreparedArtifact,
    prepare_multisource_case,
)
from trustaero.data.prepare_year import normalize_2024_months, prepare_real_data_2024
from trustaero.data.tpch import (
    TPCH_EXPECTED_ROWS_BY_SCALE,
    TPCH_SF1_EXPECTED_ROWS,
    TPCH_SF10_EXPECTED_ROWS,
    TpchPreparationError,
    TpchPreparedArtifact,
    prepare_tpch_scale,
    prepare_tpch_sf1,
)

__all__ = [
    "ArtifactSpec",
    "DownloadError",
    "DownloadResult",
    "MultisourcePreparationError",
    "MultisourcePreparedArtifact",
    "PreparedArtifactVerificationError",
    "TPCH_EXPECTED_ROWS_BY_SCALE",
    "TPCH_SF1_EXPECTED_ROWS",
    "TPCH_SF10_EXPECTED_ROWS",
    "TpchPreparationError",
    "TpchPreparedArtifact",
    "VerifiedPreparedArtifact",
    "download_artifact",
    "load_artifact_registry",
    "normalize_2024_months",
    "prepare_multisource_case",
    "prepare_real_data_2024",
    "prepare_tpch_scale",
    "prepare_tpch_sf1",
    "verify_bts_mask_join_full_month_artifacts",
    "verify_bts_mask_join_slice_artifacts",
    "verify_bts_multijoin_full_month_artifacts",
    "verify_bts_multijoin_slice_artifacts",
    "verify_real_data_full_month_artifacts",
    "verify_real_data_slice_artifacts",
]
