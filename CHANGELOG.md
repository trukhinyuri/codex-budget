# Changelog

## 0.2.0

- Add observed rolling 7/30-day shared-usage forecasts with coverage and explicit
  unknown future capacity. Reset transitions never create spendable capacity.
- Add quality-first model/effort/team-width advice, preserving explicit choices
  and checking supplied `model/list` capabilities without applying settings.
- Add audited estimate amendments for unknown-to-positive or increasing estimates;
  reservations and unreconciled status are retained. No charge reconciliation.
- Preserve the weekly 20 pp reserve, selected 3 pp buffer, permission checks,
  ordinary unknown-state deferral, shared-client uncertainty and existing storage.
- Add horizon/advice/amendment regressions and installed command readback.

## 0.1.1

- Document the host-qualified skill invocation `$codex-budget:codex-budget`.
- Verify enabled skill discovery through stock App Server `plugin/read` and
  `skills/list` during native plugin installation CI, in addition to registry and
  installed-file checks. No model turn or account secret is required.

## 0.1.0

- Public portable Codex plugin and repository marketplace.
- Weekly admission planning with a 20 pp reserve, selected 3 pp margin and durable
  transactional reservations; unknown ordinary stages defer.
- Identity/window isolation, crash recovery, strict quota validation and bounded
  read-only App Server transport.
- Standalone Python package, installation checks, tests and CI.
- No unlimited claims or enforced interception of all Codex clients.
