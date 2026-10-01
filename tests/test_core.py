import json
import multiprocessing
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_budget.core import (
    BudgetError,
    Ledger,
    atomic_json,
    estimate_text,
    fsync_directory,
    normalize,
    strict_json_loads,
)

NOW = 1790826000


def usage(used=14, account="test-account", reset=NOW + 7 * 86400):
    bucket = {
        "limitId": "codex",
        "planType": "promax",
        "primary": {"usedPercent": used, "windowDurationMins": 10080, "resetsAt": reset},
        "secondary": None,
    }
    return {
        "accountId": account,
        "ordinaryUsageAllowed": True,
        "rateLimits": bucket,
        "rateLimitsByLimitId": {"codex": bucket},
    }


def concurrent_start(db, state, barrier, queue, name):
    ledger = Ledger(db, state)
    barrier.wait()
    queue.put(ledger.start(name, root_id="root", estimate="2", now=NOW)["decision"])


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "ledger.sqlite3"
        self.state = Path(self.temp.name) / "snapshot.json"
        self.ledger = Ledger(self.db, self.state)
        self.ledger.ingest(usage(), now=NOW)

    def assess(self, **kwargs):
        defaults = dict(task_id="next", root_id="root", estimate="2", now=NOW)
        defaults.update(kwargs)
        return self.ledger.assess(**defaults)

    def test_daily_calculation_and_preserved_reserve(self):
        result = self.assess()
        self.assertEqual(result["decision"], "allow_estimated")
        self.assertEqual(result["availableOrdinaryPp"], 63)
        self.assertEqual(result["dailyOrdinaryPp"], 9)
        self.assertFalse(result["costIsUpperBound"])

    def test_exact_decimal_boundary_and_zero_overdraw(self):
        self.ledger.ingest(usage(76.7), now=NOW)
        self.assertEqual(self.assess(estimate="0.3")["decision"], "allow_estimated")
        self.assertEqual(self.assess(estimate="0.3001")["decision"], "defer")
        self.ledger.ingest(usage(78), now=NOW)
        self.assertEqual(self.assess(estimate="0")["decision"], "defer")

    def test_pending_boundary_does_not_round_down_long_decimal(self):
        self.ledger.ingest(usage(76.4), now=NOW)
        self.assertEqual(
            self.ledger.start(
                "precise", root_id="root", estimate="0.30000000000000000000000000001", now=NOW
            )["decision"],
            "allow_estimated",
        )
        self.assertEqual(
            self.ledger.start("boundary", root_id="root", estimate="0.3", now=NOW)["decision"],
            "defer",
        )
        with self.ledger.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM stages").fetchone()[0], 1)

    def test_freshness_and_short_steps(self):
        self.assertEqual(self.assess(now=NOW + 61)["decision"], "defer")
        self.assertEqual(self.assess(now=NOW + 43200, short=True)["decision"], "allow_estimated")
        self.assertEqual(self.assess(now=NOW + 43201, short=True)["decision"], "defer")
        self.assertEqual(self.assess(now=NOW - 1)["decision"], "defer")

    def test_unknown_estimate_and_required_priority(self):
        self.assertEqual(self.assess(estimate=None)["decision"], "defer")
        self.assertEqual(
            self.assess(estimate=None, priority="required")["decision"], "proceed_with_warning"
        )
        self.ledger.start("urgent", priority="urgent", estimate=None, now=NOW)
        self.assertIn("pending_cost_unknown", self.assess()["reasons"])

    def test_thresholds_35_and_20(self):
        self.ledger.ingest(usage(65), now=NOW)
        self.assertEqual(self.assess(background=True)["decision"], "defer")
        self.assertEqual(self.assess(background=False)["decision"], "allow_estimated")
        self.ledger.ingest(usage(80), now=NOW)
        self.assertIn("reserve_only", self.assess()["reasons"])
        self.assertEqual(self.assess(priority="urgent")["decision"], "proceed_with_warning")

    def test_another_root_active(self):
        self.ledger.start("first", root_id="one", estimate="1", now=NOW)
        self.assertIn("another_root_active", self.assess()["reasons"])
        self.ledger.finish("first", now=NOW)
        self.assertNotIn("another_root_active", self.assess()["reasons"])
        self.assertEqual(self.assess()["pendingEstimatePp"], 1)

    def test_idempotence_conflict_and_finish_retention(self):
        self.ledger.start("first", root_id="root", estimate="2", now=NOW)
        self.assertEqual(
            self.ledger.start("first", root_id="root", estimate="2", now=NOW)["decision"],
            "already_recorded",
        )
        self.assertEqual(
            self.ledger.start("first", root_id="root", estimate="2.0", now=NOW)["decision"],
            "already_recorded",
        )
        self.assertEqual(
            self.ledger.start("first", root_id="root", estimate="2", now=NOW + 61)["decision"],
            "defer",
        )
        with self.assertRaises(BudgetError):
            self.ledger.start("first", root_id="root", estimate="3", now=NOW)
        self.ledger.finish("first", now=NOW)
        with self.assertRaises(BudgetError):
            self.ledger.start("first", root_id="root", estimate="2", now=NOW)
        self.ledger.finish("first", now=NOW)
        self.assertEqual(self.assess()["pendingEstimatePp"], 2)
        self.assertEqual(self.ledger.status(now=NOW)["stages"][0]["status"], "unreconciled")

    def test_concurrent_reservation_is_transactional(self):
        self.ledger.ingest(usage(74), now=NOW)
        context = multiprocessing.get_context("spawn")
        barrier, queue = context.Barrier(2), context.Queue()
        jobs = [
            context.Process(
                target=concurrent_start, args=(self.db, self.state, barrier, queue, str(i))
            )
            for i in range(2)
        ]
        for job in jobs:
            job.start()
        decisions = [queue.get(timeout=15) for _ in jobs]
        for job in jobs:
            job.join(15)
            self.assertEqual(job.exitcode, 0)
        self.assertCountEqual(decisions, ["allow_estimated", "defer"])
        self.assertEqual(self.assess()["pendingEstimatePp"], 2)

    def test_snapshot_identity_separates_accounts(self):
        self.ledger.start("first", estimate="2", now=NOW)
        self.ledger.ingest(usage(account="other-account"), now=NOW)
        self.assertEqual(self.assess()["pendingEstimatePp"], 0)
        self.assertIsNone(self.assess()["pace"])
        self.ledger.ingest(usage(), now=NOW)
        self.assertEqual(self.assess()["pendingEstimatePp"], 2)

    def test_account_switch_does_not_allow_old_sample_or_copy_transition(self):
        self.ledger.ingest(usage(70), now=NOW + 10)
        self.ledger.ingest(usage(account="other-account"), now=NOW + 11)
        with self.assertRaises(BudgetError):
            self.ledger.ingest(usage(14), now=NOW + 9)
        self.ledger.ingest(usage(70, reset=NOW + 8 * 86400), now=NOW + 12)
        self.ledger.ingest(usage(account="other-account"), now=NOW + 13)
        self.assertNotIn("window_transition_unconfirmed", self.assess(now=NOW + 13)["reasons"])

    def test_external_metadata_and_cache_recovery(self):
        data = json.loads(self.state.read_text())
        data["lastAlert"] = {"keep": True}
        data["external"] = "new"
        self.state.write_text(json.dumps(data))
        self.ledger.ingest(usage(15), now=NOW + 1)
        self.assertEqual(json.loads(self.state.read_text())["external"], "new")
        self.assertEqual(json.loads(self.state.read_text())["lastAlert"], {"keep": True})
        self.state.unlink()
        self.ledger.status(now=NOW + 1)
        self.assertEqual(json.loads(self.state.read_text())["usedPercent"], 15)

    def test_missing_snapshot_defers_ordinary_and_warns_required(self):
        with self.ledger.connection() as db:
            db.execute("DELETE FROM snapshots")
        self.state.unlink()
        self.assertEqual(self.assess()["decision"], "defer")
        self.assertEqual(self.assess(priority="required")["decision"], "proceed_with_warning")

    def test_reset_needs_live_identity_and_old_reserve_survives_early_change(self):
        self.ledger.start("first", estimate="2", now=NOW)
        later = NOW + 7 * 86400
        self.assertIn("reset_requires_refresh", self.assess(now=later)["reasons"])
        self.ledger.ingest(usage(reset=later + 7 * 86400), now=NOW + 1)
        self.assertIn("window_transition_unconfirmed", self.assess(now=NOW + 1)["reasons"])
        self.assertEqual(self.assess(now=NOW + 1)["pendingEstimatePp"], 2)
        self.ledger.ingest(usage(0, reset=later + 7 * 86400), now=later + 1)
        self.assertEqual(self.assess(now=later + 1)["pendingEstimatePp"], 0)

    def test_weekly_secondary_and_other_window_exhaustion(self):
        data = usage()
        data["rateLimits"]["secondary"] = data["rateLimits"]["primary"]
        data["rateLimits"]["primary"] = {
            "usedPercent": 100,
            "windowDurationMins": 300,
            "resetsAt": NOW + 300,
        }
        self.ledger.ingest(data, now=NOW)
        self.assertEqual(self.ledger.snapshot()["weeklyWindow"], "secondary")
        self.assertIn("another_window_exhausted_or_unknown", self.assess()["reasons"])

    def test_invalid_windows_permission_and_payloads(self):
        for val in (None, -1, 101, float("nan"), True):
            data = usage(val)
            with self.assertRaises(BudgetError):
                normalize(data, now=NOW)
        for data in ({}, {"rateLimits": {}}, {"isError": True}):
            with self.assertRaises(BudgetError):
                normalize(data, now=NOW)
        data = usage()
        data["ordinaryUsageAllowed"] = None
        self.ledger.ingest(data, now=NOW)
        self.assertIn("ordinary_usage_permission_unavailable_or_blocked", self.assess()["reasons"])

    def test_mcp_json_ingest_and_unknown_fields_preserved(self):
        with self.ledger.connection() as db:
            db.execute("DELETE FROM snapshots")
        self.state.write_text(json.dumps({"custom": "preserve", "lastAlert": {"note": "keep"}}))
        payload = {"content": [{"type": "text", "text": json.dumps(usage())}]}
        self.ledger.ingest(payload, now=NOW)
        snapshot = json.loads(self.state.read_text())
        self.assertEqual(snapshot["custom"], "preserve")
        self.assertEqual(snapshot["lastAlert"], {"note": "keep"})
        self.assertNotIn("test-account", self.state.read_text())

    def test_usage_decrease_and_out_of_order_are_not_assumed_reset(self):
        self.ledger.ingest(usage(13), now=NOW + 1)
        self.assertIn("usage_decreased_same_window", self.assess(now=NOW + 1)["reasons"])
        with self.assertRaises(BudgetError):
            self.ledger.ingest(usage(15), now=NOW)

    def test_pace_defers_optional_background(self):
        self.ledger.ingest(usage(30), now=NOW + 3600)
        result = self.assess(now=NOW + 3600, background=True)
        self.assertTrue(result["pace"]["reserveAtRisk"])
        self.assertIn("optional_background_deferred", result["reasons"])

    def test_database_recovers_after_cache_write_failure_and_stage_restart(self):
        with patch("codex_budget.core.atomic_json", side_effect=OSError("cache unavailable")):
            with self.assertRaises(OSError):
                self.ledger.ingest(usage(15), now=NOW + 1)
        reopened = Ledger(self.db, self.state)
        self.assertEqual(reopened.snapshot()["usedPercent"], 15)
        reopened.start("survives", estimate="2", now=NOW + 1)
        another = Ledger(self.db, self.state)
        self.assertEqual(another.status(now=NOW + 1)["planning"]["pendingEstimatePp"], 2)

    def test_sql_transaction_rollback_after_failure(self):
        self.ledger.start("first", estimate="2", now=NOW)
        with self.assertRaises(BudgetError):
            self.ledger.start("first", estimate="invalid", now=NOW)
        self.assertEqual(self.assess()["pendingEstimatePp"], 2)

    def test_atomic_json_failure_does_not_destroy_old_snapshot(self):
        original = self.state.read_bytes()
        with patch("codex_budget.core.os.replace", side_effect=OSError("injected")):
            with self.assertRaises(OSError):
                atomic_json(self.state, {"new": True})
        self.assertEqual(self.state.read_bytes(), original)

    def test_file_fsync_failure_preserves_old_snapshot_and_cleans_temp(self):
        original = self.state.read_bytes()
        with patch("codex_budget.core.os.fsync", side_effect=OSError("file fsync unavailable")):
            with self.assertRaises(OSError):
                atomic_json(self.state, {"new": True})
        self.assertEqual(self.state.read_bytes(), original)
        self.assertEqual(list(self.state.parent.glob(self.state.name + ".*")), [])

    def test_windows_directory_fsync_is_not_attempted(self):
        parent = self.state.parent
        with patch("codex_budget.core.os.name", "nt"), patch("codex_budget.core.os.open") as opened:
            fsync_directory(parent)
        opened.assert_not_called()

    def test_concurrent_cache_publication_cannot_regress(self):
        writing_first, release_first, second_started = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        errors = []
        real_write = atomic_json

        def paused_write(path, data):
            if data.get("usedPercent") == 15:
                writing_first.set()
                if not release_first.wait(5):
                    raise RuntimeError("test synchronization timeout")
            real_write(path, data)

        def ingest(used):
            try:
                if used == 16:
                    second_started.set()
                self.ledger.ingest(usage(used), now=NOW + used)
            except Exception as error:
                errors.append(error)

        with patch("codex_budget.core.atomic_json", side_effect=paused_write):
            first = threading.Thread(target=ingest, args=(15,))
            second = threading.Thread(target=ingest, args=(16,))
            first.start()
            self.assertTrue(writing_first.wait(5))
            second.start()
            self.assertTrue(second_started.wait(5))
            release_first.set()
            first.join(10)
            second.join(10)
            self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.ledger.snapshot()["usedPercent"], 16)
        self.assertEqual(json.loads(self.state.read_text())["usedPercent"], 16)

    def test_source_types_and_ambiguous_json_are_rejected(self):
        for field, bad_values in (
            ("usedPercent", ["14", True]),
            ("windowDurationMins", ["10080", True]),
            ("resetsAt", [str(NOW + 604800), True, 2**63]),
        ):
            for value in bad_values:
                with self.subTest(field=field, value=value):
                    data = usage()
                    data["rateLimits"]["primary"][field] = value
                    with self.assertRaises(BudgetError):
                        normalize(data, now=NOW)
        for field, value in (("spendControlReached", "false"), ("rateLimitReachedType", {})):
            data = usage()
            data["rateLimits"][field] = value
            with self.assertRaises(BudgetError):
                normalize(data, now=NOW)
        raw = json.dumps(usage()).replace(
            '"usedPercent": 14', '"usedPercent": 100, "usedPercent": 14', 1
        )
        with self.assertRaises(BudgetError):
            normalize({"content": [{"type": "text", "text": raw}]}, now=NOW)
        for raw in ('{"a":1,"a":2}', '{"x":NaN}', '{"x":Infinity}', b"\xff"):
            with self.assertRaises(BudgetError):
                strict_json_loads(raw)

    def test_incomplete_or_damaged_cache_cannot_authorize_start(self):
        valid = self.ledger.snapshot()
        with self.ledger.connection() as db:
            db.execute("DELETE FROM snapshots")
        mutations = [
            {"schemaVersion": 2},
            {"schemaVersion": True},
            {"accountFingerprint": "account"},
            {"limitId": None},
            {"usedPercent": "14"},
            {"usedPercent": True},
            {"windows": []},
            {"windows": "unknown"},
            {"windows": [None]},
            {"weeklyWindow": "secondary"},
            {"windowDurationMins": 300},
            {"resetsAt": NOW + 604801},
            {"ordinaryUsageAllowed": "true"},
            {"spendControlReached": None},
            {"warnings": "unknown"},
        ]
        for fields in mutations:
            with self.subTest(fields=fields):
                self.state.write_text(json.dumps({**valid, **fields}))
                self.assertEqual(self.assess()["decision"], "defer")
                self.assertEqual(
                    self.assess(priority="required")["decision"], "proceed_with_warning"
                )
                with self.assertRaises(BudgetError):
                    self.ledger.start("broken-cache", estimate=1, now=NOW)
        for raw in (
            "[]",
            "{",
            json.dumps(valid).replace(
                '"usedPercent": 14', '"usedPercent": 100, "usedPercent": 14', 1
            ),
        ):
            self.state.write_text(raw)
            self.assertEqual(self.assess()["decision"], "defer")
        with self.ledger.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM stages").fetchone()[0], 0)

    def test_corrupt_sqlite_snapshot_does_not_hide_its_identity(self):
        valid = self.ledger.snapshot()
        for raw in (
            "[]",
            "{",
            json.dumps({**valid, "accountFingerprint": "b" * 64}),
            json.dumps({**valid, "resetsAt": NOW + 604801}),
        ):
            with self.subTest(raw=raw[:50]):
                with self.ledger.connection() as db:
                    db.execute("UPDATE snapshots SET data=?", (raw,))
                self.assertEqual(self.assess()["decision"], "defer")
                self.assertIn("quota_unknown", self.assess()["reasons"])
                with self.assertRaises(BudgetError):
                    self.ledger.start("broken-db", estimate=1, now=NOW)
                with self.assertRaises(BudgetError):
                    self.ledger.ingest(usage(15), now=NOW + 1)
                with self.ledger.connection() as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM snapshots").fetchone()[0], 1)
                    self.assertEqual(db.execute("SELECT count(*) FROM stages").fetchone()[0], 0)

    def test_corrupt_pending_estimates_remain_unknown_and_retained(self):
        self.ledger.start("pending", root_id="root", estimate=2, now=NOW)
        for estimate in ("NaN", "unknown", "-1", "101"):
            with self.subTest(estimate=estimate):
                with self.ledger.connection() as db:
                    db.execute("UPDATE stages SET estimate=?", (estimate,))
                result = self.assess()
                self.assertEqual(result["decision"], "defer")
                self.assertEqual(result["unknownPendingCount"], 1)
                self.assertEqual(self.assess(priority="urgent")["decision"], "proceed_with_warning")
                with self.ledger.connection() as db:
                    self.assertEqual(
                        db.execute("SELECT estimate FROM stages").fetchone()[0], estimate
                    )
        with self.ledger.connection() as db:
            db.execute('UPDATE stages SET estimate="2",status="unexpected"')
        self.assertIn("pending_cost_unknown", self.assess()["reasons"])

    def test_invalid_action_inputs_are_validation_errors(self):
        for fields in (
            {"task_id": True},
            {"task_id": 123},
            {"task_id": "  "},
            {"root_id": False},
            {"root_id": ""},
            {"short": "false"},
            {"background": 1},
            {"now": True},
            {"now": "1790826000"},
            {"now": float("nan")},
            {"estimate": True},
            {"estimate": "1e-1000000"},
            {"estimate": "1e1000000"},
        ):
            with self.subTest(fields=fields):
                params = dict(task_id="invalid", root_id="root", estimate=1, now=NOW)
                params.update(fields)
                with self.assertRaises(BudgetError):
                    self.ledger.start(**params)
                with self.assertRaises(BudgetError):
                    self.ledger.assess(**params)
        with self.ledger.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM stages").fetchone()[0], 0)
        self.assertEqual(estimate_text("2.000"), estimate_text("2"))
        self.assertLess(len(estimate_text("1e-256")), 20)
        with self.assertRaises(BudgetError):
            estimate_text(10**5000)

    def test_assess_snapshot_and_reservations_share_a_read_transaction(self):
        with self.ledger.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
        read_snapshot, release_read = threading.Event(), threading.Event()
        original = self.ledger._latest
        results, errors = [], []

        def pause_reader(db):
            result = original(db)
            if threading.current_thread().name == "assessment-reader":
                read_snapshot.set()
                if not release_read.wait(5):
                    raise RuntimeError("test synchronization timeout")
            return result

        def assess():
            try:
                results.append(self.assess())
            except Exception as error:
                errors.append(error)

        with patch.object(self.ledger, "_latest", side_effect=pause_reader):
            reader = threading.Thread(target=assess, name="assessment-reader")
            reader.start()
            self.assertTrue(read_snapshot.wait(5))
            try:
                self.ledger.start("concurrent-stage", root_id="root", estimate=2, now=NOW)
            finally:
                release_read.set()
            reader.join(5)
        self.assertFalse(reader.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results[0]["pendingEstimatePp"], 0)
        self.assertEqual(self.assess()["pendingEstimatePp"], 2)


if __name__ == "__main__":
    unittest.main()
