"""Codex quota planning CLI. Never dispatches a model turn."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

from . import __version__
from .core import BudgetError, Ledger, strict_json_loads
from .paths import database_path, state_path
from .planning import advise
from .transport import TransportError, _resolve_cli, platform_command, read_rate_limits


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--version", action="version", version=f"codex-budget {__version__}")
    p.add_argument("--state", type=Path, help="override public snapshot cache")
    p.add_argument("--db", type=Path, help="override shared transactional ledger")
    sub = p.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status", help="report quota and outstanding estimates")
    status.add_argument(
        "--fresh", action="store_true", help="read quota via stock App Server, no model turn"
    )
    status.add_argument("--cli", help="stock Codex executable")
    status.add_argument("--timeout", type=float, default=20)
    ingest = sub.add_parser("ingest", help="accept get_usage_limits JSON via stdin")
    ingest.add_argument("--observed-at", help="actual observation time, timezone-aware ISO8601")
    ingest.add_argument("--limit-id", default="codex")
    for name in ("assess", "start"):
        action = sub.add_parser(name)
        action.add_argument(
            "--task-id", required=True, help="globally unique stage ID; never reuse for new work"
        )
        action.add_argument("--root-id", help="stable ID shared by every stage in this root chat")
        action.add_argument(
            "--priority", choices=("ordinary", "required", "urgent"), default="ordinary"
        )
        action.add_argument(
            "--estimate-pp", help="estimated COMPLETE stage cost in weekly pp, not an upper bound"
        )
        action.add_argument(
            "--short", action="store_true", help="current-window snapshot up to 12 hours old"
        )
        action.add_argument(
            "--background", action="store_true", help="optional background/metawork"
        )
    finish = sub.add_parser("finish", help="mark ended; retain unreconciled reservation")
    finish.add_argument("--task-id", required=True)
    amend = sub.add_parser("amend", help="audit a justified estimate; never clear or decrease")
    amend.add_argument("--task-id", required=True)
    amend.add_argument("--estimate-pp", required=True)
    amend.add_argument("--reason", required=True)
    amend.add_argument(
        "--evidence", required=True, help="verified source for the complete estimate"
    )
    sub.add_parser("horizon", help="observed shared-usage forecasts for 7 and 30 days")
    advice = sub.add_parser("advise", help="quality/model/team advice; never applies settings")
    advice.add_argument(
        "--complexity", choices=("simple", "ordinary", "complex"), default="ordinary"
    )
    advice.add_argument("--error-cost", choices=("low", "moderate", "high"), default="moderate")
    advice.add_argument("--uncertainty", choices=("low", "moderate", "high"), default="moderate")
    advice.add_argument("--repeatable", action="store_true")
    advice.add_argument("--independent-parts", type=int, default=1)
    advice.add_argument("--no-objective-check", action="store_true")
    advice.add_argument("--explicit-model")
    advice.add_argument("--explicit-effort")
    advice.add_argument(
        "--catalog", type=Path, help="original model/list JSON; verify freshness separately"
    )
    doctor = sub.add_parser("doctor", help="check local prerequisites, no model or quota request")
    doctor.add_argument("--cli", help="stock Codex executable")
    return p


def doctor(binary: str | None, state: Path, database: Path) -> dict:
    executable = _resolve_cli(binary)
    command, options = platform_command(executable, "--version")
    result = subprocess.run(  # noqa: S603 - validated CLI; fixed argv, no shell
        command, **options, capture_output=True, text=True, timeout=5, check=False
    )  # noqa: S603 - validated CLI path and fixed argument; no shell
    version = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", result.stdout)
    cli_supported = bool(
        result.returncode == 0 and version and tuple(map(int, version.groups())) >= (0, 159, 2)
    )
    platform_supported = sys.platform in ("darwin", "linux", "win32")
    quick_check = None
    if database.exists():
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
            quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
    ancestor = database.parent
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    writable = os.access(ancestor, os.W_OK)
    healthy = cli_supported and platform_supported and writable and quick_check in (None, "ok")
    return {
        "healthy": healthy,
        "pythonVersion": sys.version.split()[0],
        "platform": sys.platform,
        "liveTransportSupported": platform_supported,
        "codexVersion": version.group(0) if version else None,
        "codexMinimumSupported": "0.159.2",
        "cliVersionCompatible": cli_supported,
        "databaseQuickCheck": quick_check,
        "storageParentWritable": writable,
        "statePath": str(state),
        "databasePath": str(database),
        "authChecked": False,
        "modelTurnsStarted": 0,
        "bestEffort": True,
    }


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        args = parser().parse_args(argv)
        state, database = args.state or state_path(), args.db or database_path()
        if args.command == "doctor":
            result = doctor(args.cli, state, database)
            code = 0 if result["healthy"] else 2
        else:
            ledger = Ledger(database, state)
            if args.command == "status":
                if args.fresh:
                    ledger.ingest(
                        read_rate_limits(cli_path=args.cli, timeout=args.timeout),
                        source="account/rateLimits/read",
                    )
                result = ledger.status()
            elif args.command == "ingest":
                raw = sys.stdin.buffer.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise BudgetError("usage JSON exceeds 1 MiB")
                ledger.ingest(
                    strict_json_loads(raw), observed_at=args.observed_at, limit_id=args.limit_id
                )
                result = ledger.status()
            elif args.command in ("assess", "start"):
                result = getattr(ledger, args.command)(
                    task_id=args.task_id,
                    root_id=args.root_id,
                    priority=args.priority,
                    estimate=args.estimate_pp,
                    short=args.short,
                    background=args.background,
                )
            elif args.command == "amend":
                result = ledger.amend(args.task_id, args.estimate_pp, args.reason, args.evidence)
            elif args.command == "horizon":
                result = ledger.horizon()
            elif args.command == "advise":
                try:
                    planning = ledger.status()["planning"]
                except BudgetError:
                    planning = {"remainingPercent": None}
                catalog = None
                if args.catalog:
                    if args.catalog.stat().st_size > 1024 * 1024:
                        raise BudgetError("model catalog exceeds 1 MiB")
                    catalog = strict_json_loads(args.catalog.read_bytes())
                result = advise(
                    complexity=args.complexity,
                    error_cost=args.error_cost,
                    uncertainty=args.uncertainty,
                    repeatable=args.repeatable,
                    independent_parts=args.independent_parts,
                    objective_check=not args.no_objective_check,
                    explicit_model=args.explicit_model,
                    explicit_effort=args.explicit_effort,
                    catalog=catalog,
                    planning=planning,
                )
            else:
                result = ledger.finish(args.task_id)
            code = 3 if result.get("decision") == "defer" else 0
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return code
    except (
        BudgetError,
        TransportError,
        OSError,
        ValueError,
        sqlite3.Error,
        subprocess.SubprocessError,
    ) as error:
        safe = (
            str(error) if isinstance(error, (BudgetError, TransportError)) else type(error).__name__
        )
        print(
            json.dumps(
                {
                    "error": safe,
                    "decision": "defer",
                    "bestEffort": True,
                    "requiredOrUrgent": "assistant must assess mandatory work explicitly; no automatic model dispatch",
                },
                ensure_ascii=False,
            )
        )
        return 2
    except KeyboardInterrupt:
        print(json.dumps({"error": "interrupted", "decision": "defer", "bestEffort": True}))
        return 130
