# Final experiment readiness audit (2026-07-26)

## Decision

TrustAero can begin formal paper writing now. No additional long-running
database or Agent experiment is required before drafting the system, design,
methodology, and most of the evaluation sections.

The remaining closure task is offline only: generate the final ablation bundle
from measurements and models that were frozen before their independent
holdouts. This task does not rerun DuckDB, refit either optimizer, or change a
threshold after observing holdout results.

## What is already complete

- Validator handling outcomes: `ACCEPT`, `REWRITE`, `CLARIFY`, and `REJECT`.
- Validator and planner fault injection: 100% status accuracy, reason-code
  accuracy, and injected-violation detection, with zero false rejection.
- Four cumulative system layers: direct DuckDB, TrustAero without lineage or
  certificate, TrustAero with source lineage, and complete TrustAero with a
  certificate.
- Certificate checks: plan/snapshot/planner binding, event/DAG/result binding,
  and lineage obligation/evidence.
- Certificate tampering: Phase 0 structural faults plus end-to-end result,
  policy, data, planner, lineage, and dependency tampering.
- Governed-pipeline optimizer: controlled and real-data independent holdouts.
- Lineage-checkpoint optimizer: three-way winner reversal and an independent
  holdout.
- Record-lineage scalability, full-month BTS/NYC scalability, TPC-H semantic
  coverage, four-source end-to-end case study, and real Agent plan coverage.

## Terminology correction

The four Validator states are four **handling outcomes**, not four safety layers
that should be disabled one by one. Disabling schema, semantic, or policy
validation would create invalid executions and is not a scientifically useful
ablation.

The system-level cumulative four-layer experiment is the correct overhead
ablation. Its last three TrustAero variants also provide the requested
certificate progression: no lineage/certificate, lineage only, and lineage plus
certificate.

## External SOTA comparison

The external capability matrix covers Qapla, Sieve, Alchemy, Smoke, and
VeriTuneSQL. A direct latency race against all five is not scientifically fair:
they use different policy languages, threat models, engines, candidate spaces,
and evidence semantics.

The quantitative baselines that share TrustAero's exact semantics are already
the stronger comparison: every fixed legal candidate, a frozen threshold
baseline, a legal Oracle, direct DuckDB, older lineage representations, and
Agent thinking/non-thinking modes. External systems should therefore appear in
the capability/design comparison, while the common-semantics baselines remain
the quantitative comparison.

## Writing boundary

Writing may start immediately, but the Evaluation section must preserve these
boundaries:

- `PARTIAL` certificate verification is not cryptographic attestation of every
  internal DuckDB action.
- Unsupported TPC-H queries are reported as unsupported rather than silently
  omitted.
- Failed optimizer branches remain negative development evidence and are not
  presented as successful variants.
- Independent holdout measurements are never used to refit the reported model.
