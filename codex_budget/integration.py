"""Idempotent installation of the explicitly authorized config/instruction changes."""

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import sys
import tempfile
import tomllib
from datetime import UTC, datetime
from pathlib import Path

from .core import DEFAULT_STATE, atomic_json, fsync_directory
from .paths import data_directory

CONFIG = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"
AGENTS = CONFIG.parent / "AGENTS.md"
PACKAGE = Path(__file__).resolve().parent


def command_quote(value):
    # Windows instructions are explicit PowerShell commands, not cmd /c strings.
    return "'" + str(value).replace("'", "''") + "'" if os.name == "nt" else shlex.quote(str(value))


BOOTSTRAP = (
    ("& " if os.name == "nt" else "")
    + command_quote(sys.executable)
    + " "
    + command_quote(PACKAGE / "entry.py")
)
START = "<!-- codex-budget:start -->"
END = "<!-- codex-budget:end -->"
BLOCK = f"""{START}
## Weekly Codex budget planning

Use `{BOOTSTRAP}`. This is best effort, not interception of Desktop turns or a guarantee of unlimited usage.

- Before substantial work, ingest a fresh get_usage_limits result (preserve its actual observation time), or use `status --fresh`, which reads account/rateLimits/read without a model turn. Short steps may use a current-window snapshot up to 12 hours.
- Assess the complete stage, including helpers. Use `start --task-id <unique-stage> --root-id <stable-chat> --priority ordinary|required|urgent --estimate-pp <justified-estimate>`. Omit an unknown estimate; never claim it is a proven upper cost bound. Add `--short` or `--background` only when applicable.
- Ordinary defer/error means postpone new ordinary work. Required/urgent warnings require an assistant decision, explicit risk and necessary scope, while preserving quality. Do not increase priority just to bypass a deferral. Respect the user's model and effort.
- One ordinary root active. Other clients spend shared quota and remain uncertainty. Keep a planning reserve of 20 pp and a selected 3 pp buffer. At <=35% or dangerous pace defer optional background work; at <=20% prioritize only required/urgent work.
- After completion/cancellation use `finish --task-id <stage>`. It retains an unreconciled reservation in its period. Never clear reserves manually or reuse an ended ID. A new confirmed week is separate; status is a report, not launch permission.

No hooks, scheduler, auth/payment/security changes or quota resets. See the bundled README for the support contract.
{END}
"""


def digest(data):
    return hashlib.sha256(data).hexdigest()


def config_candidate(original):
    text = original.decode("utf-8")
    before = tomllib.loads(text)
    if before.get("model") != "gpt-6.1-sol" or before.get("service_tier") != "default":
        raise ValueError("model/tier changed since approval; refusing to overwrite them")
    lines = text.splitlines(keepends=True)
    matched = []
    for index, line in enumerate(lines):
        if re.match(r"^\s*\[", line):
            break
        match = re.match(
            r'^(\s*model_reasoning_effort\s*=\s*)("[^"\n]*"|\x27[^\x27\n]*\x27)(\s*(?:#.*)?)(\r?\n)?$',
            line,
        )
        if match:
            matched.append(index)
            if before.get("model_reasoning_effort") != "medium":
                if before.get("model_reasoning_effort") != "ultra":
                    raise ValueError("reasoning default changed since approval")
                lines[index] = match[1] + '"medium"' + match[3] + (match[4] or "")
    if len(matched) != 1:
        raise ValueError("expected one plain top-level reasoning default")
    candidate = "".join(lines).encode()
    after = tomllib.loads(candidate.decode())
    expected = dict(before)
    expected["model_reasoning_effort"] = "medium"
    if after != expected:
        raise ValueError("unexpected configuration changes")
    return candidate


def agents_candidate(original):
    text = original.decode("utf-8")
    start_count, end_count = text.count(START), text.count(END)
    if start_count != end_count or start_count > 1:
        raise ValueError("ambiguous budget instruction markers")
    if start_count:
        start, end_start = text.index(START), text.index(END)
        if end_start < start:
            raise ValueError("invalid budget instruction markers")
        end = end_start + len(END)
        # Keep the original trailing newline outside the managed block.
        return (text[:start] + BLOCK.rstrip("\n") + text[end:]).encode()
    return (
        text + ("" if text.endswith("\n\n") else "\n" if text.endswith("\n") else "\n\n") + BLOCK
    ).encode()


def regular_target(path):
    info = Path(path).lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("installation requires ordinary files without symlink/hardlink aliases")
    return info


def atomic_bytes(path, data, expected=None):
    path = Path(path)
    info = regular_target(path)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, info.st_mode & 0o777)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        regular_target(path)
        if expected is not None and path.read_bytes() != expected:
            raise ValueError("concurrent edit detected immediately before publication")
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install(
    config=CONFIG, agents=AGENTS, backup_root=None, snapshot=DEFAULT_STATE, last_install_path=None
):
    if any(
        PACKAGE.parts[index : index + 2] == ("plugins", "cache")
        for index in range(len(PACKAGE.parts) - 1)
    ):
        raise ValueError(
            "global integration requires a stable checkout or installed wheel, not a disposable plugin cache"
        )
    config, agents = Path(config), Path(agents)
    regular_target(config)
    regular_target(agents)
    originals = {config: config.read_bytes(), agents: agents.read_bytes()}
    candidates = {
        config: config_candidate(originals[config]),
        agents: agents_candidate(originals[agents]),
    }
    changed = [path for path in originals if candidates[path] != originals[path]]
    if not changed:
        return {
            "changed": False,
            "message": "configuration and managed instructions already current",
        }
    backup_root = Path(backup_root or data_directory() / "integration-backups")
    folder = backup_root / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    folder.mkdir(parents=True)
    entries = []
    for index, path in enumerate(changed):
        backup = folder / f"{index}-{path.name}"
        shutil.copy2(path, backup)
        if backup.read_bytes() != originals[path]:
            raise ValueError("file changed during backup; refusing installation")
        entries.append(
            {
                "path": str(path.resolve()),
                "backup": str(backup.resolve()),
                "beforeSha256": digest(originals[path]),
                "afterSha256": digest(candidates[path]),
            }
        )
    # Preserve the legacy cache for recovery, but rollback must never restore an old quota.
    if snapshot and Path(snapshot).exists():
        shutil.copy2(snapshot, folder / "budget-state-before.json")
    manifest = folder / "manifest.json"
    atomic_json(manifest, {"schemaVersion": 1, "files": entries, "status": "prepared"})
    applied = []
    try:
        for path in changed:
            if path.read_bytes() != originals[path]:
                raise ValueError("concurrent configuration edit; refusing overwrite")
            atomic_bytes(path, candidates[path], expected=originals[path])
            applied.append(path)
        for path in changed:
            if path.read_bytes() != candidates[path]:
                raise ValueError("installation readback failed")
        atomic_json(manifest, {"schemaVersion": 1, "files": entries, "status": "installed"})
    except Exception:
        for path in reversed(applied):
            if path.read_bytes() == candidates[path]:
                atomic_bytes(path, originals[path], expected=candidates[path])
        raise
    atomic_json(
        last_install_path or data_directory() / "last-install.json",
        {"manifest": str(manifest.resolve())},
    )
    return {
        "changed": True,
        "manifest": str(manifest.resolve()),
        "rollbackCommand": f"{BOOTSTRAP} --integration rollback --manifest {command_quote(manifest.resolve())}",
    }


def rollback(manifest, allowed_paths=None):
    manifest = Path(manifest)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    entries = data["files"]
    allowed = {Path(path).resolve() for path in (allowed_paths or (CONFIG, AGENTS))}
    # Validate every target first, so later edits are not overwritten piecemeal.
    candidates = []
    for entry in entries:
        path, backup = Path(entry["path"]), Path(entry["backup"])
        if path.resolve() not in allowed or backup.resolve().parent != manifest.resolve().parent:
            raise ValueError("rollback target outside the approved installation")
        regular_target(path)
        regular_target(backup)
        original = backup.read_bytes()
        if digest(original) != entry["beforeSha256"]:
            raise ValueError("backup checksum mismatch")
        current = digest(path.read_bytes())
        if current == entry["beforeSha256"]:
            continue
        if current != entry["afterSha256"]:
            raise ValueError("file edited after installation; rollback refuses to overwrite it")
        candidates.append((path, original, entry["afterSha256"], path.read_bytes()))
    for path, original, installed_hash, installed_bytes in candidates:
        if digest(path.read_bytes()) != installed_hash:
            raise ValueError("concurrent edit during rollback; refusing overwrite")
        atomic_bytes(path, original, expected=installed_bytes)
    return {"restoredFiles": len(candidates), "quotaCacheRestored": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    apply = sub.add_parser("install")
    apply.add_argument(
        "--apply-sol-medium-defaults",
        action="store_true",
        required=True,
        help="explicitly opt in to global Sol/Medium defaults and instructions",
    )
    restore = sub.add_parser("rollback")
    restore.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    if args.command == "install":
        result = install()
    else:
        manifest = args.manifest
        if manifest is None:
            manifest = Path(
                json.loads((data_directory() / "last-install.json").read_text(encoding="utf-8"))[
                    "manifest"
                ]
            )
        result = rollback(manifest)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
