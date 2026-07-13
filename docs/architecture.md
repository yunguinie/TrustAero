# Architecture

```text
user request
  -> agent proposes a candidate JSON plan
  -> TrustAero treats that plan as untrusted until validated
  -> L1 strict model/schema validation
  -> L2 graph and catalog validation
  -> L3 policy decision and obligation inference
  -> deterministic safe rewrite
  -> validated logical plan
  -> future trusted optimizer/executor
  -> governed database access
```

"Untrusted" does not mean that every agent plan is malicious or wrong. It
means the agent cannot grant itself database authority: a candidate plan earns
permission only after deterministic validation. The validator, policy store,
catalog, and future controlled executor form the current trusted computing
base.

TrustAero is the mediation layer studied by the paper. The database executor
and physical optimizer are intentionally outside the 0.1.0 milestone, so the
current prototype produces no directly executable database command.
