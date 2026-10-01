#!/usr/bin/env python3
"""Run the bundled package directly, including from an installed plugin cache."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_budget.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
