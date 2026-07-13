# TrustAero

TrustAero is a research prototype for validating **untrusted agent-generated
query plans** over governed spatio-temporal data. An agent may propose a plan,
but it cannot execute arbitrary SQL or bypass deterministic validation.

## Current scope (0.1.0)

- strict typed Candidate Plan and Validated Logical Plan models;
- versioned JSON Schemas exported from the typed models;
- deterministic L1 structural, L2 plan-graph, and L3 governance validation;
- stable reason codes and fail-closed outcomes;
- minimal policy decisions and obligation-driven safe rewrites;
- a local CLI with no online API dependency.

Not implemented yet: database execution, physical optimization, LLM access,
or cryptographic proof generation. The Governed Execution Certificate model is
a structured record under a trusted-executor assumption, not a proof against a
malicious DBMS.

## Development setup

Open this repository as the VS Code workspace, select the Conda interpreter
named `TrustAero_env`, and run all commands from the repository root:

```powershell
conda activate TrustAero_env
python -m pip install -e ".[dev]"
python -m pytest
python scripts/export_json_schemas.py
python scripts/check_schema_sync.py
```

The core dependency list intentionally stays small. Optional libraries are
added only when production code uses them; a package already present in a
developer's Conda environment is not automatically a TrustAero dependency.

Validate an example:

```powershell
trustaero validate examples/plans/rewrite_precision.json `
  --policy examples/policies/research_policy.json `
  --catalog examples/catalogs/minimal_catalog.json
```

## Security model

- The LLM/agent and its output are untrusted.
- Unknown or indeterminate authorization is never treated as permission.
- Only a validated logical plan may proceed to later optimization.
- The current prototype assumes the validator, catalog, policy store, executor,
  and event log are trusted.

## Repository status

Research prototype. Public API stability is not guaranteed before package
version 1.0.0. The IR specification version and package version are separate.
