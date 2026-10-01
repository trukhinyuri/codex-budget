import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_core import usage


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name)
        self.command = [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "scripts" / "budget.py"),
            "--state",
            str(folder / "cache.json"),
            "--db",
            str(folder / "ledger.sqlite3"),
        ]

    def run_cli(self, *args, payload=None):
        process = subprocess.run(
            self.command + list(args), input=payload, text=True, capture_output=True, timeout=10
        )
        return process.returncode, json.loads(process.stdout)

    def ingest(self):
        import time

        return self.run_cli("ingest", payload=json.dumps(usage(reset=int(time.time()) + 7 * 86400)))

    def test_ingest_assess_start_finish_and_status(self):
        self.assertEqual(self.ingest()[0], 0)
        code, data = self.run_cli(
            "assess", "--task-id", "phase", "--root-id", "chat", "--estimate-pp", "2"
        )
        self.assertEqual((code, data["decision"]), (0, "allow_estimated"))
        self.assertEqual(
            self.run_cli("start", "--task-id", "phase", "--root-id", "chat", "--estimate-pp", "2")[
                0
            ],
            0,
        )
        self.assertEqual(self.run_cli("finish", "--task-id", "phase")[1]["status"], "unreconciled")
        self.assertEqual(self.run_cli("status")[1]["planning"]["pendingEstimatePp"], 2)
        self.assertEqual(
            self.run_cli("start", "--task-id", "phase", "--root-id", "chat", "--estimate-pp", "2")[
                0
            ],
            2,
        )

    def test_unknown_cost_defer_and_required_warning(self):
        self.ingest()
        self.assertEqual(self.run_cli("assess", "--task-id", "ordinary")[0], 3)
        code, data = self.run_cli("assess", "--task-id", "mandatory", "--priority", "required")
        self.assertEqual((code, data["decision"]), (0, "proceed_with_warning"))

    def test_errors_do_not_report_safe_allow(self):
        self.assertEqual(self.run_cli("ingest", payload="{")[0], 2)
        code, data = self.run_cli("status", "--fresh", "--cli", "/unavailable/codex")
        self.assertEqual((code, data["decision"]), (2, "defer"))
        self.assertEqual(self.run_cli("assess", "--task-id", "ordinary")[0], 3)

    def test_horizon_advice_and_audited_amendment_commands(self):
        self.ingest()
        self.assertEqual(self.run_cli("horizon")[1]["horizons"][1]["horizonDays"], 30)
        code, advice = self.run_cli(
            "advise", "--explicit-model", "gpt-6.1-sol", "--explicit-effort", "ultra"
        )
        self.assertEqual(code, 0)
        self.assertEqual(advice["recommendedEffort"], "ultra")
        self.assertFalse(advice["settingsApplied"])
        self.run_cli("start", "--task-id", "required", "--priority", "required")
        self.run_cli("finish", "--task-id", "required")
        code, amended = self.run_cli(
            "amend",
            "--task-id",
            "required",
            "--estimate-pp",
            "2",
            "--reason",
            "verified complete-stage estimate",
            "--evidence",
            "all workers and retry allowance",
        )
        self.assertEqual((code, amended["status"]), (0, "unreconciled"))
        status = self.run_cli("status")[1]
        self.assertEqual(status["planning"]["pendingEstimatePp"], 2)
        self.assertEqual(status["amendments"][0]["estimate"], "2")

    def test_advice_with_unknown_quota_keeps_one_worker_and_unverified_settings(self):
        code, data = self.run_cli("advise", "--independent-parts", "3")
        self.assertEqual(code, 0)
        self.assertEqual(data["suggestedWorkerWidth"], 1)
        self.assertFalse(data["settingsReady"])
        self.assertTrue(data["admissionRequired"])

    def test_duplicate_and_nonfinite_stdin_fail_closed(self):
        for payload in ['{"rateLimits":{},"rateLimits":{}}', '{"rateLimits":NaN}']:
            code, data = self.run_cli("ingest", payload=payload)
            self.assertEqual((code, data["decision"]), (2, "defer"))

    def test_version_and_integration_requires_explicit_opt_in(self):
        command = [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "scripts" / "budget.py"),
            "--version",
        ]
        result = subprocess.run(command, text=True, capture_output=True, timeout=5)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "codex-budget 0.2.0"))
        result = subprocess.run(
            [sys.executable, "-m", "codex_budget.integration", "install"],
            text=True,
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("--apply-sol-medium-defaults", result.stderr)


if __name__ == "__main__":
    unittest.main()
