---
name: codex-budget
description: Plan Codex weekly quota before substantial work, preserve a 20 percentage point reserve, and record full-stage estimates in a durable local ledger. Use when asked to assess quota, budget a task, or manage weekly Codex usage. Best effort; cannot guarantee uninterrupted or unlimited work.
---

# Codex Budget

Use the Python CLI bundled with this skill. Resolve the plugin root from the actual
absolute path of this `SKILL.md`: it is two parent directories above this skill's
directory. Run `python3 "<plugin-root>/scripts/budget.py" ...`. Do not assume a
`CODEX_PLUGIN_ROOT` environment variable. Python 3.11+ and a signed-in stock Codex
CLI are required. The live transport supports macOS, Linux and Windows. On Windows,
use `py -3` if that is the installed Python launcher.

1. Before substantial work, read `get_usage_limits` if available and feed its
   original JSON to `ingest`, keeping the real observation time with `--observed-at`
   if ingestion is delayed. Otherwise run `status --fresh`. The latter initializes
   a stock App Server and reads `account/rateLimits/read`; it starts no model turn.
   Never read credentials or alter auth, payments, security, or quota resets.
2. Evaluate the complete next stage, including helper agents. Run `assess`, then
   `start --task-id <unique-stage-id> --root-id <stable-chat-id>
   --priority ordinary|required|urgent --estimate-pp <justified-estimate>` before
   launching work. Omit the estimate when unknown; never invent a safe upper bound.
   Use `--short` only for a short step (current-window snapshot up to 12 hours),
   and `--background` for optional background work. Significant work requires a
   snapshot at most 60 seconds old. `status` is a report, not launch permission.
3. `defer` or error postpones ordinary work. `proceed_with_warning` is a decision
   for the assistant: explain the risk, prioritize necessary work and preserve its
   required quality checks. Do not mark optional work required to bypass a decision.
   Model and effort choices explicitly made by the user remain authoritative.
4. The policy is `used + pending estimates + selected 3 pp buffer + new estimate
   <= 80`. It preserves a planning reserve of 20 percentage points of the full
   weekly limit. Costs and trends are estimates. At remaining <=35% or dangerous
   pace, defer optional background work; at <=20%, prioritize required/urgent work.
   Keep one ordinary root active, count all its helpers, and account for external
   clients sharing the allowance as uncertainty.
5. After completion or cancellation, run `finish --task-id <unique-stage-id>`.
   The reservation becomes `unreconciled`; it stays in that period. Percent deltas
   do not prove per-task charges. Never delete reserves to admit more work or reuse
   an ended ID. A freshly confirmed new weekly period has a separate ledger.
6. For rolling weekly/month planning, use `horizon`. It reports only confirmed
   same-account, same-period intervals and observed coverage; extrapolated demand
   is shared spending, not future capacity or guaranteed cost. Never convert API
   token prices into included quota. Do not rely on credits or a future reset to
   complete today's task; plan all seven days, including weekends.
7. Before each task and substantial new stage, choose quality/verification first.
   `advise` provides deterministic model/effort/width suggestions. Supply a fresh,
   complete `model/list` response via `--catalog` to check support; otherwise choices
   remain unverified. Preserve an explicit user model/effort (including Ultra) and
   reduce optional width/scope under pressure. Never claim a midturn switch or a
   setting change: use only a supported interface before the next turn and confirm
   effective settings there. Prefer the minimum useful team with independent scopes;
   a reviewer complements objective checks. A decision error or unresolved
   contradiction warrants reassessment; unavailable data does not justify escalation.
8. `amend` may fill an unknown full-stage estimate only after its evidence is
   actually verified, or increase a known estimate. Record `--reason` and `--evidence`;
   the utility does not verify their truth. It never clears/decreases reservations
   or reconciles charges. A shared percent delta is insufficient attribution. If
   the evidence remains unknown, keep the reservation unknown. Reassess admission
   after amendment. Installation never amends existing records automatically.

Run `doctor` to check local prerequisites without a model call. If it fails, give
the exact safe diagnostic and preserve unknown state. Consult the bundled README
and `docs/QUALITY.md` for installation, data paths and limits. No hooks, background
service, automatic task interruption or unlimited entitlement are installed.
