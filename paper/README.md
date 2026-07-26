# TrustAero paper workspace

This directory contains the paper-facing outline and, later, the official
PVLDB LaTeX source. It deliberately does not duplicate raw experiment output.
Every quantitative claim must point to an entry in
`experiments/frozen/paper_results_registry_v1_20260724.json` or to a newer
source-controlled frozen record.

## Writing rules

1. Distinguish development, validation, and untouched holdout evidence.
2. Report retained negative experiments when they constrain a claim.
3. Never describe `PARTIAL` certificate verification as cryptographic
   attestation of DuckDB internals.
4. Never compare runtime numbers across systems with different semantics.
5. Report every fixed legal strategy, the strongest simple baseline, and the
   Legal Oracle beside an optimizer result.
6. Mark generated policies and sensitive payloads separately from fields
   originating in public datasets.

The final manuscript must use the official template for the target PVLDB
volume. Do not copy an old template into this directory.
