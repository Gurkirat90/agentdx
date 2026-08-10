# scripts/

Repository-integrity gates that are not tests: they check the ledger, published numbers and
determinism hygiene rather than product behaviour. Each is wrapped by a `just` recipe and run both
in CI and by the local pre-commit hook, so a violation is caught before it is committed.
