"""Stable user data paths; plugin upgrades never replace the planning ledger."""

from __future__ import annotations

import os
from pathlib import Path


def data_directory() -> Path:
    override = os.environ.get("CODEX_BUDGET_DATA_DIR")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base).expanduser() if base else Path.home() / ".local" / "share") / "codex-budget"


def settings_path() -> Path:
    override = os.environ.get("CODEX_BUDGET_SETTINGS")
    base = os.environ.get("XDG_CONFIG_HOME")
    return (
        Path(override).expanduser()
        if override
        else (Path(base).expanduser() if base else Path.home() / ".config")
        / "codex-budget"
        / "settings.json"
    )


def settings() -> dict:
    # An explicit data directory selects an independent installation/test context.
    if os.environ.get("CODEX_BUDGET_DATA_DIR"):
        return {}
    path = settings_path()
    if not path.exists():
        return {}
    if not path.is_file() or path.stat().st_size > 16384:
        raise ValueError("invalid codex-budget settings file")
    from .core import BudgetError, strict_json_loads

    try:
        data = strict_json_loads(path.read_text(encoding="utf-8"))
    except BudgetError:
        raise ValueError("invalid codex-budget settings JSON") from None
    if not isinstance(data, dict) or set(data) - {"state", "database"}:
        raise ValueError("invalid codex-budget settings fields")
    if any(
        not isinstance(value, str) or not value or not Path(value).is_absolute()
        for value in data.values()
    ):
        raise ValueError("settings paths must be absolute strings")
    return data


def state_path() -> Path:
    value = os.environ.get("CODEX_BUDGET_STATE") or settings().get("state")
    return Path(value).expanduser() if value else data_directory() / "budget-state.json"


def database_path() -> Path:
    value = os.environ.get("CODEX_BUDGET_DB") or settings().get("database")
    return Path(value).expanduser() if value else data_directory() / "ledger.sqlite3"
