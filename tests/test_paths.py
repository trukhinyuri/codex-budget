import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_budget.paths import database_path, settings_path, state_path


class PathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.environment = patch.dict(
            os.environ,
            {
                "XDG_DATA_HOME": str(self.folder / "data"),
                "XDG_CONFIG_HOME": str(self.folder / "config"),
                "CODEX_BUDGET_DATA_DIR": "",
                "CODEX_BUDGET_DB": "",
                "CODEX_BUDGET_STATE": "",
                "CODEX_BUDGET_SETTINGS": "",
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_stable_default_and_explicit_overrides(self):
        self.assertEqual(database_path(), self.folder / "data/codex-budget/ledger.sqlite3")
        with patch.dict(os.environ, {"CODEX_BUDGET_DATA_DIR": str(self.folder / "other")}):
            self.assertEqual(state_path(), self.folder / "other/budget-state.json")
        with patch.dict(os.environ, {"CODEX_BUDGET_STATE": str(self.folder / "chosen.json")}):
            self.assertEqual(state_path(), self.folder / "chosen.json")

    def test_shared_ledger_settings_and_test_isolation(self):
        path = settings_path()
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "database": str(self.folder / "existing.sqlite3"),
                    "state": str(self.folder / "existing.json"),
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(database_path(), self.folder / "existing.sqlite3")
        with patch.dict(os.environ, {"CODEX_BUDGET_DATA_DIR": str(self.folder / "isolated")}):
            self.assertEqual(database_path(), self.folder / "isolated/ledger.sqlite3")

    def test_invalid_settings_do_not_fall_back_silently(self):
        path = settings_path()
        path.parent.mkdir(parents=True)
        for value in ["[]", "{", '{"unknown":"path"}', '{"database":"relative"}', '{"state":0}']:
            path.write_text(value, encoding="utf-8")
            with self.assertRaises(ValueError):
                state_path()


if __name__ == "__main__":
    unittest.main()
