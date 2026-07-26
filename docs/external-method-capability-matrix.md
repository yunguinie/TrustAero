# External method and capability comparison

This matrix fixes the comparison scope used by the TrustAero paper. It is
based on primary papers and project artifacts, not secondary summaries. A
check mark means that the cited system explicitly implements the capability;
`partial` means that it addresses a narrower form; `—` means that the paper
does not claim the capability. Absence is not a criticism because the systems
solve different problems.

## Capability matrix

| System | Untrusted Agent/LLM query input | Typed fail-closed plan validation | Context-aware policy enforcement | Governance-aware physical planning | Lineage capture/planning | Execution evidence or certificate | Spatial/temporal IR |
|---|---:|---:|---:|---:|---:|---:|---:|
| TrustAero | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Qapla | — | — | ✓ | — | — | — | — |
| Sieve | — | — | ✓ | partial | — | — | partial |
| Alchemy | — | — | partial | ✓ | — | — | — |
| Smoke | — | — | — | partial | ✓ | — | — |
| VeriTuneSQL | ✓ | partial | — | — | — | — | — |

## What each comparison establishes

- **Qapla** intercepts application SQL and rewrites it using row/column
  policies that may depend on identity, time, values, and query operators. It
  is the closest reference for enforcement outside an untrusted application,
  but it does not validate Agent-generated typed plans or optimize a space of
  obligation-preserving physical candidates.
- **Sieve** rewrites queries with guarded expressions and uses a cost model to
  choose access-control enforcement strategies. It is the closest policy-aware
  optimization reference. Its target is scaling fine-grained access control
  over large policy sets rather than jointly planning Mask, materialization,
  lineage, and execution evidence.
- **Alchemy** performs rewrite, cardinality bounding, bushy-plan generation,
  and physical cost-based optimization for oblivious SQL under secure
  multiparty computation. It demonstrates that security semantics can require
  a specialized optimizer, but its circuit/MPC cost domain is not numerically
  comparable with TrustAero's DuckDB governance pipelines.
- **Smoke** instruments physical operators and optimizes lineage capture and
  future lineage-query processing. It is the closest lineage mechanism
  reference, but it does not infer policy obligations or validate untrusted
  Agent plans.
- **VeriTuneSQL** checks semantic equivalence of LLM-produced SQL rewrites using
  SQL Server optimizer capabilities. It directly supports the premise that LLM
  rewrites require independent verification, while addressing equivalence
  rather than governance authorization and obligation preservation.

## Frozen quantitative-comparison decision

The capability matrix does **not** replace quantitative baselines. TrustAero's
paper reports the following executable, common-semantics comparisons:

1. governed optimizer versus every legal fixed plan, a frozen threshold
   baseline, and Legal Oracle;
2. complete governed execution versus direct DuckDB execution;
3. Lineage V4 versus direct execution and the preceding V3 representation;
4. Agent outputs by model and paired thinking/non-thinking modes;
5. fault-injected plans and certificates versus unchanged legal inputs.

Running Qapla, Sieve, Alchemy, or Smoke latency numbers beside TrustAero is not
authorized as a headline comparison because their policy languages, security
guarantees, engines, candidate spaces, and output evidence differ. Such a table
would measure implementation and threat-model differences rather than the
claimed planning method. If a reviewer-facing artifact later exposes a truly
common policy/query fragment, it must be preregistered as a separate experiment
and may not replace the frozen baselines above.

## Primary sources

- Qapla: [USENIX Security 2017 paper and artifact page](https://www.usenix.org/conference/usenixsecurity17/technical-sessions/presentation/mehta)
- Sieve: [PVLDB 13 paper](https://www.vldb.org/pvldb/vol13/p2424-pappachan.pdf)
- Alchemy: [PVLDB 18 paper](https://www.vldb.org/pvldb/vol18/p3021-sohn.pdf) and [official artifact](https://github.com/vaultdb/alchemy)
- Smoke: [PVLDB 11 paper](https://www.vldb.org/pvldb/vol11/p719-psallidas.pdf)
- VeriTuneSQL: [CIDR 2026 paper page](https://www.vldb.org/cidrdb/2026/leveraging-query-optimizers-to-verify-the-soundness-of-llm-based-query-rewrites-for-real-world-workloads.html)

