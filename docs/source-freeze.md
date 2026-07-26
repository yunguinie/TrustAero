# Source-freeze gate

Formal performance measurements must be attributable to the exact source that
produced them. TrustAero therefore uses a fail-closed source-freeze gate before
publication-facing experiments.

Run it from the repository root in `TrustAero_env`:

```powershell
python -u scripts/check_source_freeze.py
```

The command exits with status 1 until all hard requirements pass. During normal
development, use `--report-only` to inspect and save the same decision without
treating `NOT_READY` as a shell failure:

```powershell
python -u scripts/check_source_freeze.py --report-only
```

The machine-readable `audit.json` and human-readable `report.md` are written
below `results/source_freeze_audit/`, which is intentionally ignored by Git.

## Hard requirements

- the source checkout is a readable Git repository with no modified, staged, or
  untracked non-ignored files;
- Python comes from the `TrustAero_env` environment;
- raw, processed, and temporary datasets are not tracked by Git;
- no tracked file exceeds the conservative 10 MiB repository-artifact limit;
- changed text has no unresolved merge-conflict markers or `git diff --check`
  errors;
- every file bound by a compatible record in `experiments/frozen/` still has
  its recorded SHA-256 digest.

The checker never stages or commits files. A human must review the source
boundary before creating the frozen commit. Historical pilot results with a
dirty-worktree marker remain non-paper evidence even after a later source freeze.

The first frozen real-data development suite uses the same gate automatically:

```powershell
python -u scripts/run_real_data_formal_v1.py --progress
```

January 2024 is a previously inspected development partition. Passing this
suite permits method-level paper analysis, but never re-labels it as independent
Optimizer V1/V2 holdout evidence.
