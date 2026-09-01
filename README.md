# TrustAero

[![CI](https://github.com/yunguinie/TrustAero/actions/workflows/ci.yml/badge.svg)](https://github.com/yunguinie/TrustAero/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

TrustAero is a database research prototype for governed, agent-generated query
plans. It treats an Agent plan as an untrusted proposal, derives an approved
logical plan through deterministic validation and safe rewriting, searches only
the governance-legal physical-plan space, and binds execution evidence to the
same approval chain for independent checking.

The repository contains the implementation and reproducibility package for the
TrustAero paper.

## Paper resources

- **[Supplementary Material (PDF)](paper/TrustAero_Supplementary_Material.pdf)**
- **[Artifact reproduction guide](artifact/README.md)**
- **[Experimental result overview](artifact/RESULTS.md)**

## System pipeline

1. **Logical approval.** A typed plan IR is checked against the catalog,
   request context, policy snapshot, and normalized governance obligations.
2. **Legality-first physical planning.** Candidate generation is separated from
   governance feasibility; cost ranking is applied only to legal candidates.
3. **Evidence-bound execution.** Results, events, Lineage, versions, and the
   Planner decision are assembled into a Certificate.
4. **Independent checking.** The Checker recomputes the observable bindings and
   reports any cross-stage mismatch.

## Quick start

TrustAero supports Python 3.11--3.13.

```bash
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev,duckdb]"
python artifact/verify.py
python -m pytest -q
```

Validate a sample Agent plan:

```bash
trustaero validate examples/plans/rewrite_precision.json \
  --policy examples/policies/research_policy.json \
  --catalog examples/catalogs/minimal_catalog.json
```

## Reproducing the paper

Start with [artifact/README.md](artifact/README.md). It provides:

- a table/figure-to-artifact map;
- a fast verification path that uses the committed measurements;
- full commands for rerunning each experiment family;
- dataset sources, hardware guidance, and expected runtimes;
- the expected metrics for every reported result.

The compact result overview is in [artifact/RESULTS.md](artifact/RESULTS.md),
and machine-readable bindings are in
[artifact/manifest.json](artifact/manifest.json).

Large external datasets and generated DuckDB databases are not committed.
Their URLs, sizes, hashes, row counts, and preparation metadata are tracked in
[`data/manifests`](data/manifests). See [data/README.md](data/README.md) for the
download and preparation commands.

## Repository layout

| Path | Contents |
|---|---|
| `src/trustaero/` | Validator, policy engine, Planner, execution, Certificate, and Checker |
| `artifact/` | Paper results, manifest, verification script, and reproduction guide |
| `experiments/` | Final configurations, frozen protocols, and registered models |
| `scripts/` | Data preparation and experiment entry points |
| `examples/` | Candidate plans, policies, catalogs, and the four-source case |
| `schemas/` | Versioned JSON Schemas for public TrustAero objects |
| `tests/` | Core and publication-facing regression tests |

## Supported scope

The prototype implements the bounded relational and spatio-temporal fragment
used in the paper and the registered physical-candidate families included in
the artifact. DuckDB is the evaluated execution backend. The independent
Checker verifies observable cross-stage consistency under the system model
described in [docs/threat-model.md](docs/threat-model.md).

## Citation and license

Citation metadata is provided in [CITATION.cff](CITATION.cff). The software is
released under the Apache License 2.0; external datasets remain subject to
their original providers' terms.
