# Contributing

Use Python 3.11+ and a virtual environment. Runtime dependencies must remain standard
library only. Never add model calls, login flows, payment mutations, reset-credit
consumption, hooks or unattended monitoring to quota reads.

Run `python3 -m unittest discover -s tests -v`, `ruff check .`,
`ruff format --check .`, and `python3 scripts/verify_release.py` before a PR.
Tests use temporary databases and fake processes; do not run them on a real ledger.
Every correction to admission, identity, reset or retention needs a regression that
fails before the change. New releases must keep their schema/data migration explicit.
Do not mistake percentage estimates for server-enforced cost bounds.
