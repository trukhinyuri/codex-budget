"""Verify release metadata and archive privacy, optionally build a plugin ZIP."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = [
    "plugin.json",
    ".codex-plugin/plugin.json",
    ".agents/plugins/marketplace.json",
    "pyproject.toml",
    "README.md",
    "LICENSE",
    "CHANGELOG.md",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "docs/QUALITY.md",
    "docs/implementation-contract.json",
    "docs/README.ru.md",
    "skills/codex-budget/SKILL.md",
    "scripts/budget.py",
    "scripts/verify_release.py",
]


def release_files() -> list[Path]:
    return [ROOT / name for name in FILES] + sorted((ROOT / "codex_budget").glob("*.py"))


def verify() -> dict:
    portable = json.loads((ROOT / "plugin.json").read_text())
    legacy = json.loads((ROOT / ".codex-plugin/plugin.json").read_text())
    marketplace = json.loads((ROOT / ".agents/plugins/marketplace.json").read_text())
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert portable["$schema"] == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
    assert (
        portable["name"] == legacy["name"] == marketplace["plugins"][0]["name"] == project["name"]
    )
    assert portable["version"] == legacy["version"] == project["version"]
    assert marketplace["plugins"][0]["source"] == {"source": "local", "path": "./"}
    assert marketplace["plugins"][0]["policy"] == {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL",
    }
    assert project["dependencies"] == []
    assert "hooks" not in portable["extensions"]["com.openai"]
    assert not (ROOT / "hooks").exists() and not (ROOT / "mcp.json").exists()
    for path in release_files():
        assert path.is_file() and not path.is_symlink(), (
            f"missing or aliased release file: {path.name}"
        )
        assert not re.search(
            r"/(?:Users|home)/[A-Za-z0-9_.-]+/", path.read_text(encoding="utf-8")
        ), "personal absolute path in release"
    skill = (ROOT / "skills/codex-budget/SKILL.md").read_text()
    assert skill.startswith("---\nname: codex-budget\ndescription:")
    assert re.search(
        r"__version__\s*=\s*[\"\x27]" + re.escape(portable["version"]),
        (ROOT / "codex_budget/__init__.py").read_text(),
    )
    return {
        "version": portable["version"],
        "releaseFiles": len(release_files()),
        "runtimeDependencies": 0,
        "privateRuntimeStateIncluded": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", type=Path, help="optional plugin ZIP path")
    parser.add_argument("--wheel", type=Path, help="optional built wheel to validate")
    args = parser.parse_args()
    result = verify()
    if args.wheel:
        with zipfile.ZipFile(args.wheel) as wheel:
            assert wheel.testzip() is None
            expected_modules = {
                "codex_budget/" + path.name for path in (ROOT / "codex_budget").glob("*.py")
            }
            actual_modules = {name for name in wheel.namelist() if name.startswith("codex_budget/")}
            assert actual_modules == expected_modules
            for name in wheel.namelist():
                assert name in expected_modules or re.fullmatch(
                    r"codex_budget-[^/]+\.dist-info/(?:METADATA|WHEEL|RECORD|entry_points\.txt|top_level\.txt|licenses/LICENSE)",
                    name,
                ), f"unexpected wheel file: {name}"
                contents = wheel.read(name).decode("utf-8")
                assert not re.search(r"/(?:Users|home)/[A-Za-z0-9_.-]+/", contents)
                if name in expected_modules:
                    assert wheel.read(name) == (ROOT / name).read_bytes()
        result["wheelReadbackVerified"] = True
    if args.zip:
        args.zip.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(args.zip, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in release_files():
                relative = path.relative_to(ROOT).as_posix()
                entry = zipfile.ZipInfo(relative, date_time=(2026, 1, 1, 0, 0, 0))
                entry.compress_type = zipfile.ZIP_DEFLATED
                entry.external_attr = 0o100644 << 16
                archive.writestr(entry, path.read_bytes())
        with zipfile.ZipFile(args.zip) as archive:
            assert archive.testzip() is None
            assert set(archive.namelist()) == {
                path.relative_to(ROOT).as_posix() for path in release_files()
            }
            for path in release_files():
                assert archive.read(path.relative_to(ROOT).as_posix()) == path.read_bytes()
        result["zipSha256"] = hashlib.sha256(args.zip.read_bytes()).hexdigest()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
