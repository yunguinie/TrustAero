# Threat model

The Agent and its candidate plans are untrusted. Unknown authorization and
unresolved references fail closed. Versioned policy snapshots and the catalog
are trusted inputs. The Validator, Candidate Generator, Planner, Compiler, and
Certificate Builder form the execution-side trusted computing base. The
Independent Checker uses a separate verification entry point and independent
or recomputable evidence. The current model assumes that the underlying DBMS
executes the approved physical plan; it does not claim cryptographic query
correctness against a malicious DBMS.
