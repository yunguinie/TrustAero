# Contributing

1. Open an issue describing the change and the affected IR/specification rule.
2. Add or update tests before changing validator behavior.
3. Keep stable reason-code values backward compatible within an IR version.
4. Run `ruff check .`, `ruff format --check .`, `mypy src/trustaero`, and
   `python -u scripts/run_tests.py -q`.
5. Never commit API keys, private data, machine-specific paths, or generated caches.

Security-sensitive changes must explain which invariant they preserve and why a
failure results in ACCEPT, REWRITE, CLARIFY, or REJECT.
