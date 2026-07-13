# Threat model

The agent and all candidate plans are untrusted. Unknown authorization and
unresolved references fail closed. The current implementation assumes trusted
catalog, policy store, validator, future executor, and event log. It does not
claim cryptographic query correctness against a malicious DBMS.

