# Result overview

The values below summarize the committed measurements. Exact per-case records
are stored under `artifact/results/`.

## Logical approval

- The deterministic semantic suite produced the expected decision and reason
  code for all 26 registered cases.
- The independent black-box corpus contained 800 adversarial plans and 200
  valid controls. TrustAero produced 0 unsafe acceptances, 0 false rejections,
  and 0 unexpected exceptions.
- Validator scalability covers 24 configurations. The largest plan, output,
  policy, and obligation settings completed with median latencies of 59.3,
  231.6, 17.5, and 201.3 ms, respectively.

## Legality-first physical planning

- Across 96 governed-pipeline and 18 Lineage-checkpoint held-out decisions,
  TrustAero selected a plan within 3% of the Legal Oracle in 114/114 decisions
  and made no illegal selection. Governed-pipeline mean, P95, and maximum
  regret were 0.220%, 1.442%, and 2.084%.
- In the independently registered third candidate family, all three physical
  realizations won at least one SF1 development configuration. The selector
  frozen on SF1 reached the 3% Legal-Oracle set in 11/12 primary SF10 decisions
  with 0.614% mean regret and no illegal selection. An unchanged replication
  reached 12/12.
- Under no-raw-join and strict policies, soft penalties up to 1 ms selected an
  illegal candidate in 96/96 decisions; a 10 ms penalty selected an illegal
  candidate in 45/96. Hard feasibility filtering selected 0 illegal candidates
  and required no penalty calibration.
- Candidate-space scalability covers 3, 6, 12, 24, and 48 candidates across
  72,000 planning trials. Median total planning latency increased from 26.1 to
  379.0 microseconds, with exact legal cost-oracle agreement and no illegal
  selections.

## Execution evidence and Lineage

- The full Certificate detected 19/19 registered fault and tampering cases.
- Row-ordinal record Lineage stored approximately 32 bytes per edge. At one
  million rows it required 32.000462 bytes per edge and ran at 2.045x Direct
  SQL (95% CI [2.015, 2.145]).
- The four-source workflow bound four fixed snapshots and returned 9,128
  governed rows; the complete plan, execution, Lineage, and Certificate chain
  passed independent checking.
- In the cross-stage ablation, full logical approval removed the sole false
  rejection, legality-first planning reduced illegal selections from 192/288
  to 0/288, and the full Certificate increased registered-fault detection from
  6/19 to 19/19.

## Standard benchmark checks

TPC-H Q1, Q3, Q6, and Q10 have exact trusted-IR result checks. The committed
SF10 timing artifacts for Q1 and Q6 record equivalent results and distinct
physical candidates. They are method checks rather than the primary adaptive
Planner result.
