"""Install the plugin with stock Codex in an isolated home, without auth/models."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    binary = os.environ.get("CODEX_BUDGET_TEST_CLI") or shutil.which("codex")
    if not binary:
        raise RuntimeError("stock Codex CLI required for plugin installation test")
    with tempfile.TemporaryDirectory(prefix="codex-budget-plugin-test-") as directory:
        environment = dict(os.environ)
        environment["CODEX_HOME"] = str(Path(directory) / "codex")
        Path(environment["CODEX_HOME"]).mkdir()
        environment["CODEX_BUDGET_DATA_DIR"] = str(Path(directory) / "data")
        # The package is copied to a clean input so dev caches/build outputs cannot
        # affect the native host's installation behavior.
        root = Path(directory) / "marketplace&safe"
        root.mkdir()
        sys.path.insert(0, str(ROOT / "scripts"))
        from verify_release import release_files

        sys.path.insert(0, str(ROOT))
        from codex_budget.transport import platform_command

        for source in release_files():
            target = root / source.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)

        def run(*arguments: str) -> dict:
            command, options = platform_command(binary, *arguments)
            result = subprocess.run(  # noqa: S603 - fixed stock CLI commands, temporary paths
                command,
                **options,
                env=environment,
                cwd=root,
                capture_output=True,  # noqa: S603 - stock CLI, fixed commands and temporary paths
                text=True,
                encoding="utf-8",
                timeout=60,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"plugin command failed: {arguments[0:3]!r}; exit={result.returncode}"
                )
            return json.loads(result.stdout)

        added = run("plugin", "marketplace", "add", str(root), "--json")
        assert added["marketplaceName"] == "codex-budget-marketplace"
        installed = run("plugin", "add", "codex-budget@codex-budget-marketplace", "--json")
        installed_root = Path(installed["installedPath"])
        assert installed["version"] == "0.1.0"
        assert (installed_root / "skills/codex-budget/SKILL.md").is_file()
        result = subprocess.run(  # noqa: S603 - current Python and installed entry point
            [sys.executable, str(installed_root / "scripts/budget.py"), "--version"],  # noqa: S603 - current Python and verified installed script
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        assert result.stdout.strip() == "codex-budget 0.1.0"
        listed = run("plugin", "list", "--marketplace", "codex-budget-marketplace", "--json")
        entry = next(item for item in listed["installed"] if item["name"] == "codex-budget")
        assert entry["installed"] and entry["enabled"]
        removed = run("plugin", "remove", "codex-budget@codex-budget-marketplace", "--json")
        assert removed["name"] == "codex-budget"
        print(
            json.dumps(
                {
                    "nativePluginInstall": True,
                    "installedEntryPoint": True,
                    "enabledReadback": True,
                    "nativeRemoval": True,
                    "isolatedHome": True,
                    "modelTurnsStarted": 0,
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
