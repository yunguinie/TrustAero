# External experiment data

Large raw and processed datasets are intentionally excluded from Git. This
directory retains only small, reviewable manifests and documentation needed to
reproduce downloads and deterministic transformations.

- `raw/`: immutable files fetched from authoritative sources;
- `processed/`: deterministic slices and derived experiment tables;
- `manifests/`: tracked URLs, hashes, schemas, row counts, and transform rules.

Do not place data on the C drive. The proposed first source and approval gate
are documented in `docs/real-data-selection.md`.

