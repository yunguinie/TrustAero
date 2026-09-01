# TrustAero four-source end-to-end case study V2

- Status: `PASS_MULTISOURCE_CASE_STUDY_V2_COMPLETE_LOOP`
- Valid Agent request: `REWRITE`
- Illegal Agent request: `REJECT`
- Validated logical plan (Pl): `pl-7b7986dcdc0d8b0b`
- Generated physical candidates: `4`
- Policy-rejected candidates: `3`
- Optimizer-selected candidate: `materialize-after-gov-001-mask`
- Approved physical plan (Pp): `pp-a78057888206307e`
- Governed output rows: `9128`
- Source-lineage inputs: `4`
- Certificate status: `PARTIAL`
- Rejected injected faults: `6/6`

## Reproduce

```powershell
conda activate TrustAero_env
python -u scripts/download_datasets.py --stage multisource_case_v1
python -u scripts/prepare_multisource_case.py --progress
python -u scripts/run_multisource_case_study_v2.py --progress --require-clean
```

This is semantic system evidence, not an optimizer speed benchmark. Certificate `PARTIAL` is intentional because DuckDB-internal operator outputs are not cryptographically attested.
