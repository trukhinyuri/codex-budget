# Quality contract and evidence

There is no single published set of "all frontier-company quality criteria".
This project uses explicit, reviewable acceptance gates. It is not certified by
OpenAI, another model company, or an independent security auditor.

| Contract | Evidence required for a release |
| --- | --- |
| Portable plugin distribution | Root Agent Plugins 1.0.0 manifest, compatibility manifest, marketplace parser and installed skill readback |
| Honest admission decisions | Decimal boundary tests, unknown/freshness tests, 35/20 thresholds, additional-window deferral |
| Durable estimates | Concurrent SQLite reservations, crash/cache recovery, idempotence and finished-reservation retention |
| Account and window isolation | Account switches, reset anomalies, out-of-order data and strict snapshot validation tests |
| Read-only quota transport | Fixed RPC-envelope tests, live initialized quota read, no model/auth/payment/reset requests |
| Controlled failures | Bounded time/bytes/messages, fake-process error and cleanup tests, safe error output |
| Portable storage | No account data inside plugin caches; explicit data/settings paths and isolated installed-package test |
| Repeatable delivery | macOS/Linux/Windows Python 3.11–3.14 CI, lint/format checks, deterministic plugin ZIP and wheel installation |
| Public data hygiene | Release allowlist, tracked-file scan, no caches/ledgers/backups/credentials or private absolute paths |
| Maintainability | License, support matrix, changelog, contribution and security reporting guidance; independent critical review |
| Observed horizons | Same-account intervals within confirmed periods only; 7/30-day coverage, reset/decrease rejection, unknown future capacity |
| Quality advice | Risk before cost, supported supplied capabilities, explicit Ultra preservation, unknown/pressure width reduction, no applied settings |
| Audited amendments | Unknown-to-positive/increase only, mandatory evidence/reason, unchanged finished status, retained reservation and original estimate |

Support is conditional on Python 3.11+, a compatible stock Codex CLI (baseline
0.159.2), macOS, Linux or Windows, writable stable local storage, and an account for which
the documented quota endpoint returns identity plus exactly one weekly window.
Newer CLI versions must preserve that endpoint contract; version ordering alone
does not prove compatibility. `doctor` checks local prerequisites; a successful
`status --fresh` checks the actual endpoint/session at that time. Plugin availability
also depends on the host/workspace allowing user-installed marketplace plugins.

Installing a plugin does not provision Python or sign in to Codex. A new chat or host restart may
be needed to discover newly installed skills. CI tests are offline; they cannot prove
future network availability or the server's subscription accounting.

On POSIX the JSON mirror fsyncs its file and directory. Windows fsyncs the file and
atomically replaces it; it cannot promise the same directory durability on a sudden
power loss. The committed SQLite ledger remains canonical and repairs the mirror.

The planning invariant is **U + P + 3 + estimate <= 80** for an ordinary new stage,
where U is the last observed weekly used percentage and P is the sum of outstanding
estimates. This is not a financial/server-enforced bound. Unknown costs, stale quota
and other clients prevent an absolute reserve guarantee. One long model turn may
overshoot an estimate. Helper agents belong in the stage estimate. Required/urgent
work receives a warning decision rather than an automatic block or safe-cost promise.

Finished estimates remain unreconciled because a shared percentage delta cannot
attribute a charge to a stage. This may double-count conservatively and defer work
early. No silent release is performed. A new period needs a fresh confirmed identity
and reset transition; history stays intact.

Audited amendments record a caller-verified justified estimate when previously
unknown, or increase an existing estimate. They never decrease or clear one, and
the utility cannot establish the authenticity of evidence text. No automatic
reconciliation is attempted. Horizon forecasts extrapolate observed shared demand;
unobserved future capacity is unknown. Model/team advice is a heuristic with no
quality/latency superiority claim and no runtime or global setting mutation.

The optional global integration installer is separate from plugin installation.
It requires explicit opt-in, preserves backups and refuses detected edits. It is
not a concurrent filesystem transaction or CAS with an unrelated editor. Do not edit
those files in parallel. Rollback does not replace the current quota with an old one.
Run integration from a persistent source checkout or an installed wheel; disposable
plugin cache paths are refused before writing global instructions. Its bootstrap is
a packaged direct-file entry, so a wheel does not require a repository scripts folder.

Windows uses a bounded pipe reader and a Job Object to terminate assigned processes.
Job assignment precedes RPCs but follows process creation; descendants created before
assignment are outside this guarantee. System taskkill is a bounded fallback. Batch
launcher paths/arguments containing percent expansion, quotes or CR/LF are rejected.

Sources: [OpenAI plugin package format](https://developers.openai.com/plugins/build/plugins),
[Codex CLI commands](https://learn.chatgpt.com/docs/developer-commands),
[App Server](https://learn.chatgpt.com/docs/app-server),
[subscription quota](https://learn.chatgpt.com/docs/pricing),
[PyPA packaging guidance](https://packaging.python.org/en/latest/tutorials/packaging-projects/),
[GitHub Actions secure use](https://docs.github.com/en/actions/reference/security/secure-use).
The exact plugin/runtime implementation examined is OpenAI Codex
[0.159.2](https://github.com/openai/codex/tree/ff6aec96948b70d94983af2641a6b67c94faeff5).
