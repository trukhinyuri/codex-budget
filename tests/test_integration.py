import json
import os
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_budget.integration import (
    END,
    START,
    agents_candidate,
    atomic_bytes,
    config_candidate,
    install,
    rollback,
)


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.config, self.agents = self.folder / "config.toml", self.folder / "AGENTS.md"
        self.original_config = b'# keep comment\nmodel = "gpt-6.1-sol"\nmodel_reasoning_effort = "ultra" # default\nservice_tier = "default"\n[other]\nkeep = [1, 2]\n[profiles.deep]\nmodel_reasoning_effort = "high"\n'
        self.original_agents = "# Недельный бюджет\n\nВсе текущие инструкции.\n\n## frontier-research\nНе менять.\n".encode()
        self.config.write_bytes(self.original_config)
        self.agents.write_bytes(self.original_agents)
        self.config.chmod(0o600)
        self.snapshot = self.folder / "snapshot.json"
        self.snapshot.write_text('{"usedPercent":14}')

    def do_install(self):
        return install(
            self.config,
            self.agents,
            self.folder / "backups",
            self.snapshot,
            self.folder / "last-install.json",
        )

    def test_preserve_config_and_profiles_and_permissions(self):
        self.do_install()
        before = tomllib.loads(self.original_config.decode())
        before["model_reasoning_effort"] = "medium"
        self.assertEqual(tomllib.loads(self.config.read_text(encoding="utf-8")), before)
        self.assertIn("# keep comment", self.config.read_text(encoding="utf-8"))
        if os.name == "posix":
            self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertTrue(self.agents.read_bytes().startswith(self.original_agents))

    def test_idempotent_instruction_block_and_install(self):
        first = self.do_install()
        original_manifest = first["manifest"]
        second = self.do_install()
        self.assertFalse(second["changed"])
        self.assertEqual(self.agents.read_text(encoding="utf-8").count(START), 1)
        self.assertEqual(self.agents.read_text(encoding="utf-8").count(END), 1)
        self.assertEqual(
            json.loads((self.folder / "last-install.json").read_text(encoding="utf-8"))["manifest"],
            original_manifest,
        )

    def test_rollback_verified_and_idempotent_without_old_quota_restore(self):
        result = self.do_install()
        self.snapshot.write_text('{"usedPercent":15}')
        allowed = (self.config, self.agents)
        self.assertEqual(rollback(result["manifest"], allowed)["restoredFiles"], 2)
        self.assertEqual(self.config.read_bytes(), self.original_config)
        self.assertEqual(self.agents.read_bytes(), self.original_agents)
        self.assertEqual(json.loads(self.snapshot.read_text(encoding="utf-8"))["usedPercent"], 15)
        self.assertEqual(rollback(result["manifest"], allowed)["restoredFiles"], 0)

    def test_rollback_refuses_later_edits_and_corrupt_backup(self):
        result = self.do_install()
        self.agents.write_text(
            self.agents.read_text(encoding="utf-8") + "later edit\n", encoding="utf-8"
        )
        with self.assertRaises(ValueError):
            rollback(result["manifest"], (self.config, self.agents))
        self.assertEqual(
            tomllib.loads(self.config.read_text(encoding="utf-8"))["model_reasoning_effort"],
            "medium",
        )

    def test_rollback_disallows_arbitrary_targets(self):
        result = self.do_install()
        with self.assertRaises(ValueError):
            rollback(result["manifest"])

    def test_invalid_markers_and_unexpected_model_defaults(self):
        for value in (START, END, START + START + END + END, END + START):
            with self.assertRaises(ValueError):
                agents_candidate(value.encode())
        with self.assertRaises(ValueError):
            config_candidate(self.original_config.replace(b'"ultra"', b'"high"'))
        with self.assertRaises(ValueError):
            config_candidate(self.original_config.replace(b"gpt-6.1-sol", b"other-model"))

    def test_symlink_and_hardlink_refused_before_changes(self):
        target = self.folder / "target.toml"
        self.config.rename(target)
        self.config.symlink_to(target)
        with self.assertRaises(ValueError):
            self.do_install()
        self.assertTrue(self.config.is_symlink())
        self.assertEqual(target.read_bytes(), self.original_config)
        self.assertEqual(self.agents.read_bytes(), self.original_agents)
        self.config.unlink()
        os.link(target, self.config)
        with self.assertRaises(ValueError):
            self.do_install()

    def test_late_edit_detected_before_atomic_replace(self):
        fsync = os.fsync
        changed = False

        def edit_during_write(descriptor):
            nonlocal changed
            fsync(descriptor)
            if not changed:
                changed = True
                self.config.write_bytes(b"later edit")

        with patch("codex_budget.integration.os.fsync", side_effect=edit_during_write):
            with self.assertRaises(ValueError):
                atomic_bytes(self.config, b"candidate", expected=self.original_config)
        self.assertEqual(self.config.read_bytes(), b"later edit")

    def test_disposable_cache_refused_before_global_edits(self):
        with patch(
            "codex_budget.integration.PACKAGE",
            self.folder / "plugins/cache/marketplace/name/0.1.1/codex_budget",
        ):
            with self.assertRaises(ValueError):
                self.do_install()
        self.assertEqual(self.config.read_bytes(), self.original_config)
        self.assertEqual(self.agents.read_bytes(), self.original_agents)

    def test_windows_instruction_paths_are_powershell_literals(self):
        from codex_budget.integration import command_quote

        with patch("codex_budget.integration.os.name", "nt"):
            self.assertEqual(command_quote(r"C:\Temp\A&B\entry.py"), r"'C:\Temp\A&B\entry.py'")
            self.assertEqual(command_quote("a'b%v%"), "'a''b%v%'")


if __name__ == "__main__":
    unittest.main()
