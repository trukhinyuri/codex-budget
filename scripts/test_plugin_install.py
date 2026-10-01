"""Install the plugin with stock Codex in an isolated home, without auth/models."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def verify_host_skill(binary: str, environment: dict, root: Path, installed_root: Path) -> None:
    from codex_budget.transport import _command, _Responses, _terminate, _WindowsJob

    command, options = _command(binary)
    job = _WindowsJob() if sys.platform == "win32" else None
    try:
        process = subprocess.Popen(  # noqa: S603 - stock CLI and fixed read-only RPCs
            command,
            **options,
            env=environment,
            cwd=root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
    except BaseException:
        if job is not None:
            job.close()
        raise
    responses = None
    try:
        if job is not None:
            job.assign(process.pid)
        responses = _Responses(process, time.monotonic() + 30)

        def send(message: dict) -> None:
            assert process.stdin is not None
            process.stdin.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
            process.stdin.flush()

        send(
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "codex_budget_plugin_test", "version": "1.0"},
                    "capabilities": None,
                },
            }
        )
        responses.response(1)
        send({"method": "initialized", "params": {}})
        send(
            {
                "id": 2,
                "method": "plugin/list",
                "params": {
                    "cwds": [str(root)],
                    "marketplaceKinds": ["local"],
                    "forceRefetch": False,
                },
            }
        )
        catalog = responses.response(2)
        marketplace = next(
            item for item in catalog["marketplaces"] if item["name"] == "codex-budget-marketplace"
        )
        plugin = next(item for item in marketplace["plugins"] if item["name"] == "codex-budget")
        assert plugin["installed"] and plugin["enabled"]
        send(
            {
                "id": 3,
                "method": "plugin/read",
                "params": {
                    "marketplacePath": marketplace["path"],
                    "remoteMarketplaceName": None,
                    "pluginName": "codex-budget",
                },
            }
        )
        detail = responses.response(3)["plugin"]
        assert any(
            item["name"] == "codex-budget:codex-budget" and item["enabled"]
            for item in detail["skills"]
        )
        assert detail["mcpServers"] == [] and detail["hooks"] == []
        send(
            {
                "id": 4,
                "method": "skills/list",
                "params": {
                    "cwds": [str(root)],
                    "forceReload": True,
                },
            }
        )
        inventory = responses.response(4)
        loaded = [
            skill
            for entry in inventory["data"]
            for skill in entry["skills"]
            if skill["name"] == "codex-budget:codex-budget"
        ]
        assert len(loaded) == 1 and loaded[0]["enabled"]
        assert Path(loaded[0]["path"]).samefile(installed_root / "skills/codex-budget/SKILL.md")
    finally:
        try:
            if responses is not None:
                responses.close()
        finally:
            try:
                _terminate(process, job)
            finally:
                if responses is not None:
                    responses.join()


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
        assert installed["version"] == "0.1.1"
        assert (installed_root / "skills/codex-budget/SKILL.md").is_file()
        result = subprocess.run(  # noqa: S603 - current Python and installed entry point
            [sys.executable, str(installed_root / "scripts/budget.py"), "--version"],  # noqa: S603 - current Python and verified installed script
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        assert result.stdout.strip() == "codex-budget 0.1.1"
        listed = run("plugin", "list", "--marketplace", "codex-budget-marketplace", "--json")
        entry = next(item for item in listed["installed"] if item["name"] == "codex-budget")
        assert entry["installed"] and entry["enabled"]
        verify_host_skill(binary, environment, root, installed_root)
        removed = run("plugin", "remove", "codex-budget@codex-budget-marketplace", "--json")
        assert removed["name"] == "codex-budget"
        print(
            json.dumps(
                {
                    "nativePluginInstall": True,
                    "installedEntryPoint": True,
                    "enabledReadback": True,
                    "enabledSkillReadback": True,
                    "loadedSkillReadback": True,
                    "nativeRemoval": True,
                    "isolatedHome": True,
                    "modelTurnsStarted": 0,
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
