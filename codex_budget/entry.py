"""Stable direct-file entry for installed wheels and persistent source checkouts."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if __name__ == "__main__":
    if sys.argv[1:2] == ["--integration"]:
        sys.argv.pop(1)
        from codex_budget.integration import main

        main()
    else:
        from codex_budget.cli import main

        raise SystemExit(main())
