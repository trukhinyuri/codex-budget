import tempfile
import unittest
from pathlib import Path

from test_core import NOW, usage

from codex_budget.core import BudgetError, Ledger
from codex_budget.planning import advise


def catalog(model="gpt-6.1-sol", efforts=("medium", "high", "ultra")):
    return {
        "data": [
            {
                "model": model,
                "supportedReasoningEfforts": [{"reasoningEffort": item} for item in efforts],
            }
        ]
    }


class AdviceTests(unittest.TestCase):
    def test_quality_before_cost_and_minimal_width(self):
        result = advise(error_cost="high", independent_parts=7, catalog=catalog())
        self.assertEqual(
            (result["recommendedModel"], result["recommendedEffort"]), ("gpt-6.1-sol", "high")
        )
        self.assertTrue(result["independentReviewRecommended"])
        self.assertEqual(result["suggestedWorkerWidth"], 2)
        self.assertFalse(result["settingsApplied"])
        self.assertTrue(result["admissionRequired"])

    def test_repeatable_luna_requires_low_risk_and_objective_check(self):
        self.assertEqual(
            advise(complexity="simple", repeatable=True, error_cost="low")["recommendedModel"],
            "gpt-6-luna",
        )
        self.assertEqual(
            advise(complexity="simple", repeatable=True, error_cost="high")["recommendedModel"],
            "gpt-6.1-sol",
        )
        self.assertEqual(
            advise(complexity="simple", repeatable=True, error_cost="low", objective_check=False)[
                "recommendedModel"
            ],
            "gpt-6.1-sol",
        )

    def test_explicit_ultra_and_unsupported_qwen_are_never_substituted(self):
        result = advise(
            explicit_model="gpt-6.1-sol",
            explicit_effort="ultra",
            catalog=catalog(),
            planning={"remainingPercent": 24},
        )
        self.assertEqual(result["recommendedEffort"], "ultra")
        self.assertEqual(result["suggestedWorkerWidth"], 1)
        self.assertTrue(result["settingsReady"])
        self.assertTrue(result["newTurnOnly"])
        result = advise(
            explicit_model="research-qwen38:64k", explicit_effort="xhigh", catalog=catalog()
        )
        self.assertEqual(result["recommendedModel"], "research-qwen38:64k")
        self.assertFalse(result["settingsReady"])
        self.assertFalse(result["supportedBySuppliedCatalog"])

    def test_unknown_permission_and_history_pressure_never_widen(self):
        for planning in (
            {"remainingPercent": None},
            {
                "remainingPercent": 76,
                "reasons": ["ordinary_usage_permission_unavailable_or_blocked"],
            },
            {"remainingPercent": 76, "pace": {"reserveAtRisk": True}},
            {"remainingPercent": 76, "unknownPendingCount": 1},
        ):
            self.assertEqual(
                advise(independent_parts=3, planning=planning)["suggestedWorkerWidth"], 1
            )

    def test_model_only_preserves_unknown_effort_and_capability_is_unverified(self):
        result = advise(explicit_model="gpt-6.1-sol")
        self.assertIsNone(result["recommendedEffort"])
        self.assertIsNone(result["supportedBySuppliedCatalog"])
        self.assertFalse(result["settingsReady"])
        with self.assertRaises(BudgetError):
            advise(explicit_effort="ultra")
        with self.assertRaises(BudgetError):
            advise(catalog={"data": [{"model": "gpt-6.1-sol"}]})


class LedgerPlanningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.ledger = Ledger(root / "db.sqlite3", root / "state.json")
        self.ledger.ingest(usage(10), now=NOW)

    def test_rolling_forecast_and_shared_spending_unknown_capacity(self):
        self.ledger.ingest(usage(16), now=NOW + 2 * 86400)
        result = self.ledger.horizon(now=NOW + 2 * 86400)
        weekly, monthly = result["horizons"]
        self.assertEqual(weekly["observedPpPerDay"], 3)
        self.assertEqual(weekly["projectedDemandPp"], 21)
        self.assertEqual(monthly["projectedDemandPp"], 90)
        self.assertEqual(monthly["observedIntervalDays"], 2)
        self.assertIsNone(monthly["futureCapacityPp"])
        self.assertFalse(monthly["futurePeriodsConfirmed"])
        self.assertTrue(result["sharedSpendingUncertainty"])
        self.assertNotIn("decision", result)

    def test_month_forecast_never_joins_resets_accounts_or_decreased_usage(self):
        self.ledger.ingest(usage(14), now=NOW + 2 * 86400)
        self.ledger.ingest(usage(40, account="other"), now=NOW + 3 * 86400)
        self.ledger.ingest(usage(2, reset=NOW + 14 * 86400), now=NOW + 7 * 86400)
        self.ledger.ingest(usage(8, reset=NOW + 14 * 86400), now=NOW + 9 * 86400)
        result = self.ledger.horizon(now=NOW + 9 * 86400)
        self.assertEqual(result["horizons"][1]["observedChangePp"], 10)
        self.assertEqual(result["horizons"][1]["observedPpPerDay"], 2.5)
        self.assertEqual(result["horizons"][1]["skippedIntervalCount"], 1)
        self.ledger.ingest(usage(7, reset=NOW + 14 * 86400), now=NOW + 10 * 86400)
        self.assertEqual(
            self.ledger.horizon(now=NOW + 10 * 86400)["horizons"][1]["skippedIntervalCount"], 2
        )

    def test_sparse_or_zero_history_is_reported_honestly(self):
        self.assertIsNone(self.ledger.horizon(now=NOW)["horizons"][0]["observedPpPerDay"])
        self.ledger.ingest(usage(10), now=NOW + 3600)
        self.assertEqual(self.ledger.horizon(now=NOW + 3600)["horizons"][0]["observedPpPerDay"], 0)

    def test_amend_unknown_estimate_keeps_finished_reserve_and_audit(self):
        self.ledger.start("required", priority="required", now=NOW)
        self.ledger.finish("required", now=NOW + 1)
        result = self.ledger.amend(
            "required",
            "3",
            "complete-stage estimate now available",
            "verified pilot and all helpers",
            now=NOW + 2,
        )
        self.assertTrue(result["reservationRetained"])
        self.assertFalse(result["chargesReconciled"])
        status = self.ledger.status(now=NOW + 2)
        self.assertEqual(status["planning"]["unknownPendingCount"], 0)
        self.assertEqual(status["planning"]["pendingEstimatePp"], 3)
        self.assertEqual(status["stages"][0]["status"], "unreconciled")
        self.assertIsNone(status["amendments"][0]["previous_estimate"])
        self.assertFalse(
            self.ledger.amend("required", "3", "same estimate", "same evidence", now=NOW + 3)[
                "changed"
            ]
        )
        self.assertEqual(len(self.ledger.status(now=NOW + 3)["amendments"]), 1)

    def test_amend_cannot_launder_unknown_or_clear_decrease_change_old_period(self):
        self.ledger.start("required", priority="required", now=NOW)
        for estimate in ("0", "NaN", "-1"):
            with self.assertRaises(BudgetError):
                self.ledger.amend("required", estimate, "reason", "evidence", now=NOW)
        self.ledger.amend("required", "3", "reason", "evidence", now=NOW)
        with self.assertRaises(BudgetError):
            self.ledger.amend("required", "2", "reason", "evidence", now=NOW)
        with self.assertRaises(BudgetError):
            self.ledger.amend("required", "4", "reason", "", now=NOW)
        with self.assertRaises(BudgetError):
            self.ledger.amend("required", "4", "reason", "evidence", now=NOW + 7 * 86400)
        self.assertEqual(self.ledger.status(now=NOW)["planning"]["pendingEstimatePp"], 3)


if __name__ == "__main__":
    unittest.main()
