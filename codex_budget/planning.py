"""Deterministic advice and observed forecasts; never changes runtime settings."""

from __future__ import annotations

from decimal import Decimal

from .core import BudgetError, identifier, integer, number, observed_timestamp


def history_forecast(snapshots, days, now):
    """Use only observed same-period intervals; resets and gaps confer no capacity."""
    days = integer(days, "horizon days")
    if days not in (7, 30):
        raise BudgetError("horizon days must be 7 or 30")
    cutoff = now - days * 86400
    observations = [
        item for item in snapshots if cutoff <= observed_timestamp(item["observedAt"]) <= now
    ]
    duration, change, intervals, skipped = Decimal(0), Decimal(0), 0, 0
    for first, second in zip(observations, observations[1:], strict=False):
        elapsed = observed_timestamp(second["observedAt"]) - observed_timestamp(first["observedAt"])
        delta = number(second["usedPercent"], "usedPercent") - number(
            first["usedPercent"], "usedPercent"
        )
        if elapsed <= 0:
            continue
        if (
            first["resetsAt"] != second["resetsAt"]
            or delta < 0
            or first.get("warnings")
            or second.get("warnings")
            or observed_timestamp(second["observedAt"]) >= second["resetsAt"]
        ):
            skipped += 1
            continue
        duration += Decimal(str(elapsed))
        change += delta
        intervals += 1
    rate = change * Decimal(86400) / duration if duration >= 3600 else None
    return {
        "horizonDays": days,
        "sampleCount": len(observations),
        "confirmedIntervalCount": intervals,
        "skippedIntervalCount": skipped,
        "observedIntervalDays": float(duration / Decimal(86400)),
        "observedChangePp": float(change),
        "observedPpPerDay": None if rate is None else float(rate),
        "projectedDemandPp": None if rate is None else float(rate * days),
        "futureCapacityPp": None,
        "futurePeriodsConfirmed": False,
        "sharedClientsIncluded": True,
        "perTaskAttribution": False,
        "estimateOnly": True,
    }


def advise(
    *,
    complexity="ordinary",
    error_cost="moderate",
    uncertainty="moderate",
    repeatable=False,
    independent_parts=1,
    objective_check=True,
    explicit_model=None,
    explicit_effort=None,
    catalog=None,
    planning=None,
):
    """Advise before a new task/turn; supplied model/list data is not a live read."""
    if complexity not in ("simple", "ordinary", "complex"):
        raise BudgetError("invalid complexity")
    if error_cost not in ("low", "moderate", "high"):
        raise BudgetError("invalid error cost")
    if uncertainty not in ("low", "moderate", "high"):
        raise BudgetError("invalid uncertainty")
    if type(repeatable) is not bool or type(objective_check) is not bool:
        raise BudgetError("repeatable and objective-check flags must be booleans")
    parts = integer(independent_parts, "independent parts")
    if parts > 32:
        raise BudgetError("independent parts must be at most 32")
    if explicit_model is not None:
        identifier(explicit_model, "explicit model")
    if explicit_effort is not None:
        identifier(explicit_effort, "explicit effort")
    if explicit_effort is not None and explicit_model is None:
        raise BudgetError("an explicit effort requires an explicit model")
    difficult = complexity == "complex" or error_cost == "high" or uncertainty == "high"
    if difficult:
        model, effort = "gpt-6.1-sol", "high"
    elif repeatable and complexity == "simple" and error_cost == "low" and objective_check:
        model, effort = "gpt-6-luna", "high"
    else:
        model, effort = "gpt-6.1-sol", "medium"
    if complexity == "complex" and uncertainty == "high" and error_cost == "high":
        model, effort = "gpt-6-astra", "high"
    if explicit_model is not None:
        model = explicit_model
        # A model-only selection does not authorize inventing its effort default.
        effort = explicit_effort
    supported = None
    if catalog is not None:
        if isinstance(catalog, dict) and isinstance(catalog.get("result"), dict):
            catalog = catalog["result"]
        if not isinstance(catalog, dict) or not isinstance(catalog.get("data"), list):
            raise BudgetError("catalog must be a model/list response")
        candidates = [item for item in catalog["data"] if isinstance(item, dict)]
        matches = [item for item in candidates if item.get("model") == model]
        if len(matches) > 1:
            raise BudgetError("ambiguous model catalog")
        supported = False
        if matches:
            efforts = matches[0].get("supportedReasoningEfforts")
            if not isinstance(efforts, list):
                raise BudgetError("catalog reasoning capabilities unavailable")
            supported = effort is None or effort in [
                item.get("reasoningEffort") for item in efforts if isinstance(item, dict)
            ]
    pressure = False
    if planning is not None:
        remaining = planning.get("remainingPercent")
        pressure = (
            remaining is None
            or number(remaining, "remainingPercent", Decimal(100)) <= 35
            or planning.get("unknownPendingCount", 0) > 0
            or bool((planning.get("pace") or {}).get("reserveAtRisk"))
            or bool(planning.get("reasons"))
        )
    width = min(parts, 2) if not pressure else 1
    return {
        "recommendedModel": model,
        "recommendedEffort": effort,
        "explicitChoicePreserved": explicit_model is not None,
        "supportedBySuppliedCatalog": supported,
        "catalogFreshnessVerified": False,
        "settingsReady": supported is True,
        "suggestedWorkerWidth": width,
        "widthIncludesCoordinator": False,
        "independentReviewRecommended": error_cost == "high",
        "objectiveVerificationRequired": True,
        "budgetPressure": pressure,
        "newTurnOnly": True,
        "settingsApplied": False,
        "serviceTier": "default",
        "admissionRequired": True,
        "estimateIncludesAllHelpersAndRetries": True,
        "bestEffort": True,
    }
