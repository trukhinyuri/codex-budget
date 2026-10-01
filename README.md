# Codex Budget

Weekly quota planning for **Codex Desktop and Codex CLI**, delivered as an installable
skill plugin and a dependency-free Python CLI. Keep a planning reserve of **20
percentage points** of the full weekly limit, plus a deliberately selected **3 pp
buffer**, and track stage estimates in a durable local SQLite ledger.

**This is best effort, not unlimited usage or a server-enforced spending cap.** It
does not intercept all Desktop turns, stop work automatically, map tokens to quota,
or prove the upper cost of the next model turn. Unknown ordinary costs defer; required
and urgent work retain priority with an explicit warning decision.

## Install as a Codex plugin

Prerequisites: **Python 3.11+**, a compatible stock **Codex CLI 0.159.2+**, a signed-in
Codex account, and a host/workspace that allows marketplace plugins. macOS, Linux
and Windows are included in the release CI matrix. Later Codex versions must preserve
the documented endpoint contract; the version number alone is not proof.

```sh
codex plugin marketplace add trukhinyuri/codex-budget --ref v0.1.0
codex plugin add codex-budget@codex-budget-marketplace
codex plugin list --marketplace codex-budget-marketplace --json
```

In Codex Desktop, open the plugin directory and select **Codex Budget**, then start
a new chat and invoke **`$codex-budget`**. The plugin is skill driven: selecting it
does not silently create a universal gate for all chats. Restart the host if a newly
installed plugin is not visible. The same marketplace/skill works in CLI `/plugins`.
Use the returned `installedPath`, not a guessed cache/version directory.

The plugin bundles ordinary Python scripts, no MCP server, hooks, daemon, scheduler,
login flow or model call. Installing it does not install Python or change your model,
reasoning, speed, payment or authorization preferences. The marketplace's standard
authentication policy does not imply this skill has a separate external account.

Local authoring: `codex plugin marketplace add /absolute/path/to/codex-budget`, then
the same `plugin add` command. In the skill, derive the plugin root from the actual
absolute `SKILL.md` path and run `python3 "<plugin-root>/scripts/budget.py" ...`.
On Windows use `py -3` if that is how Python 3.11+ is installed.

## Standalone CLI

No pip install is needed when running from a checkout or the installed plugin:

```sh
python3 scripts/budget.py doctor
python3 scripts/budget.py status --fresh
python3 scripts/budget.py assess --task-id CHAT:PHASE --root-id CHAT --estimate-pp 1
python3 scripts/budget.py start --task-id CHAT:PHASE --root-id CHAT --estimate-pp 1
python3 scripts/budget.py finish --task-id CHAT:PHASE
```

`1` is an illustrative estimate, not a recommended cost or a token conversion. Use
a justified estimate for the complete stage and all helpers. Omit `--estimate-pp`
when unknown. `assess` does not reserve; `start` atomically checks and reserves.
`finish` is also used for cancellation and keeps the reservation **unreconciled**.
Do not reuse an ended ID or delete records to admit more work.

Optional virtual environment installation:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/codex-budget doctor
```

Windows equivalents are `.venv\Scripts\python.exe` and `.venv\Scripts\codex-budget.exe`.
You can install directly from a pinned Git URL in a virtual environment. This project
is distributed on GitHub; it does not assume a package with the same name on PyPI.

## Quota reads and admission

`status --fresh` initializes a stock App Server, sends `initialized`, then calls only
`account/rateLimits/read` with Luna Reserve disabled. No model turn is started.
`ingest` accepts the original `get_usage_limits` JSON via stdin (also one MCP text
envelope or an App Server result). If delayed, set `--observed-at` to its actual
observation time; reading an old file does not make it fresh. The utility does not
open credential files; the unmodified CLI uses the current session. Errors are safe
and bounded. Sandboxed environments may need their ordinary permission to launch
the stock CLI; never alter security or auth to force a read.

Ordinary admission requires `U + P + M + estimate <= 80`, where U is observed weekly
used %, P is outstanding estimates and M is the selected 3 pp buffer. Unknown quota,
identity, estimates or permissions defer ordinary work. Significant stages require
an observation at most **60 seconds** old; `--short` permits a current-window snapshot
up to **12 hours** for short steps. `--background` defers optional background work at
remaining <=35% or a pace projecting reserve exhaustion. Remaining <=20% allows
only required/urgent warning decisions. `--priority required|urgent` is not a cost
guarantee and must not be used to bypass an ordinary deferral.

One ordinary root may be active. Include its agents in the estimate; other clients
spend the shared quota and remain uncertainty. An additional nonweekly window is
reported but ordinary work defers without an estimate for it. Only an explicit
10080-minute weekly window is supported, in either primary or secondary.

The report gives `available=max(0,80-U-P-3)` and a conservative daily guide
`available/max(1,ceil(secondsUntilReset/86400))`, including weekends. It is planning,
not launch permission. Pace requires same-account/window observations spanning at
least one hour and is also an estimate.

Finished/cancelled estimates are never automatically freed: shared percentage
changes cannot attribute charges to a stage. A cost may therefore count in both U
and P, conservatively reducing availability. A new confirmed period is separate;
early reset anomalies retain old reserves. History is preserved.

## Data and configuration

By default data lives under `~/.local/share/codex-budget/` (or `XDG_DATA_HOME`):
`ledger.sqlite3` is canonical, `budget-state.json` an atomic compatible mirror.
Plugin updates replace code, not this data. Credentials and credit balances are not
stored. Account IDs become SHA-256 fingerprints; these are pseudonymous, not anonymous.

Set `CODEX_BUDGET_DATA_DIR` for an independent installation, or pass global `--state`
and `--db` **before** the command. `CODEX_BUDGET_STATE`/`CODEX_BUDGET_DB` override each
path. Optional `~/.config/codex-budget/settings.json` (or XDG config location) can
pin an existing shared ledger with absolute `state` and `database` paths. All clients
must use the same database to share reservations. An explicit data directory bypasses
settings; `CODEX_BUDGET_SETTINGS` overrides the settings file location.

Keep runtime data private and out of Git. Snapshot custom metadata is preserved
locally. SQLite transactions serialize reservations and JSON publication; a cache
failure keeps committed canonical data for repair. Do not edit the database directly.
Use `doctor` for local versions/storage checks; it does not check login or the server.

Exit codes: **0** report/estimated permission/mandatory warning, **3** ordinary
deferral, **2** safe failure, **130** interruption. Code 0 does not imply safe spending.

## Optional global instruction integration

Native plugin installation does not rewrite `AGENTS.md` or model defaults. A separate
opt-in installer can append a managed budget block and change only the known Sol /
Standard baseline from Ultra to Medium:

```sh
python3 -m codex_budget.integration install --apply-sol-medium-defaults
python3 -m codex_budget.integration rollback
```

Use this only after choosing those changes explicitly. It refuses unexpected model,
tier or effort changes, preserves every other setting/instruction, backs up before
writing, applies idempotently and refuses detected later edits on rollback. It never
restores an old quota cache. Do not edit these files concurrently; filesystem replace
cannot supply a cross-editor CAS guarantee.
Run it from a persistent source checkout or a wheel installed in a persistent Python
environment. A disposable plugin cache is refused before global files are touched.
On Windows the generated instruction/rollback command uses PowerShell quoting and
its call operator; run that displayed command in PowerShell.

## Development and quality

```sh
python3 -m unittest discover -s tests -v
ruff check .
ruff format --check .
python3 scripts/verify_release.py --zip dist/codex-budget-plugin-0.1.0.zip
```

CI runs independent-platform/version tests, packaging and plugin-install checks with
no account secrets or model calls. Critical admission/transport changes get regression
tests and independent review. See [quality contract](docs/QUALITY.md),
[contribution guide](CONTRIBUTING.md), [security and privacy](SECURITY.md),
[changelog](CHANGELOG.md), and [русская инструкция](docs/README.ru.md).

Distribution follows the [official plugin format](https://developers.openai.com/plugins/build/plugins)
and [Codex plugin commands](https://learn.chatgpt.com/docs/developer-commands).
Quota semantics come from [App Server](https://learn.chatgpt.com/docs/app-server)
and [subscription rules](https://learn.chatgpt.com/docs/pricing). None of these APIs
provide a proven upper bound on the next turn or an atomic global quota reservation.
