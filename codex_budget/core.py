"""Conservative planning ledger. No model calls, account enforcement or token pricing."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path

from .paths import data_directory

VERSION = 1
RESERVE = Decimal(20)
MARGIN = Decimal(3)
WEEK_MINUTES = 10080
FRESH_SECONDS = 60
SHORT_SECONDS = 12 * 3600
DEFAULT_STATE = data_directory() / "budget-state.json"
DEFAULT_DB = data_directory() / "ledger.sqlite3"


class BudgetError(Exception):
    pass


def strict_json_loads(value):
    """Reject ambiguous JSON before any quota or identity fields are selected."""

    def unique_object(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = item
        return result

    def reject_constant(_value):
        raise ValueError("non-finite JSON number")

    try:
        return json.loads(value, object_pairs_hook=unique_object, parse_constant=reject_constant)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise BudgetError("invalid or ambiguous JSON payload") from None


def number(value, label, maximum=None):
    if isinstance(value, bool) or value is None:
        raise BudgetError(f"{label}: required finite non-negative number")
    if not isinstance(value, (str, int, float, Decimal)):
        raise BudgetError(f"{label}: invalid number")
    try:
        text = str(value)
        if len(text) > 256:
            raise ValueError
        n = Decimal(text)
    except (InvalidOperation, ValueError, TypeError):
        raise BudgetError(f"{label}: invalid number") from None
    if not n.is_finite() or n < 0 or (maximum is not None and n > maximum):
        raise BudgetError(f"{label}: out of range")
    # Bound pathological exponents before integer expansion or exact arithmetic.
    if n.adjusted() > 256 or n.as_tuple().exponent < -256:
        raise BudgetError(f"{label}: numeric scale too large")
    return n


def integer(value, label, minimum=1):
    n = number(value, label)
    if n != n.to_integral_value() or n < minimum:
        raise BudgetError(f"{label}: invalid integer")
    return int(n)


def json_number(value):
    return int(value) if value == value.to_integral_value() else float(value)


def exact_sum(values):
    values = list(values)
    if not values:
        return Decimal(0)
    # Preserve accepted decimal estimates, including tiny boundary differences.
    precision = (
        max(0, max(value.adjusted() for value in values))
        + 1
        + max(0, -min(value.as_tuple().exponent for value in values))
        + len(str(len(values)))
    )
    with localcontext() as context:
        context.prec = max(28, precision)
        return sum(values, Decimal(0))


def estimate_text(value):
    if value is None:
        return None
    n = number(value, "estimate", Decimal(100))
    if not n:
        return "0"
    # Strip trailing zeroes without Decimal context rounding or expanding a huge exponent.
    sign, digits, exponent = n.as_tuple()
    digits = list(digits)
    while digits[-1] == 0:
        digits.pop()
        exponent += 1
    return str(Decimal((sign, tuple(digits), exponent)))


def identifier(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise BudgetError(f"{label}: required 1..256 character identifier")
    return value


def quota_number(value, label, maximum=None):
    if type(value) not in (int, float):
        raise BudgetError(f"{label}: required JSON number")
    return number(value, label, maximum)


def quota_integer(value, label, minimum=1):
    quota_number(value, label)
    return integer(value, label, minimum)


def clock_value(value):
    n = quota_number(value, "current time", Decimal("253402300799"))
    return float(n)


def observed_timestamp(value):
    if not isinstance(value, str):
        raise BudgetError("observedAt: missing timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError
        return result.timestamp()
    except (ValueError, OverflowError):
        raise BudgetError("observedAt: invalid timezone-aware timestamp") from None


def unwrap(payload):
    for _ in range(6):
        if isinstance(payload, str):
            payload = strict_json_loads(payload)
        elif (
            isinstance(payload, dict)
            and "isError" in payload
            and type(payload["isError"]) is not bool
        ):
            raise BudgetError("invalid usage tool error flag")
        elif isinstance(payload, dict) and payload.get("isError"):
            raise BudgetError("usage tool returned an error")
        elif isinstance(payload, dict) and "rateLimits" in payload:
            return payload
        elif isinstance(payload, dict) and isinstance(payload.get("result"), dict):
            payload = payload["result"]
        elif isinstance(payload, dict) and isinstance(payload.get("content"), list):
            texts = [
                item.get("text")
                for item in payload["content"]
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            if len(texts) != 1:
                raise BudgetError("expected one usage JSON text block")
            payload = texts[0]
        else:
            raise BudgetError("missing rateLimits response")
    raise BudgetError("usage payload nesting too deep")


def normalize(payload, limit_id="codex", now=None, source="get_usage_limits", observed_at=None):
    now = clock_value(time.time() if now is None else now)
    identifier(limit_id, "limit ID")
    identifier(source, "source")
    payload = unwrap(payload)
    account = payload.get("accountId")
    if not isinstance(account, str) or not account.strip():
        raise BudgetError("accountId unavailable; refresh an identity-backed usage snapshot")
    buckets = payload.get("rateLimitsByLimitId")
    if buckets is not None:
        if not isinstance(buckets, dict) or limit_id not in buckets:
            raise BudgetError(f"limit bucket {limit_id!r} unavailable")
        bucket = buckets[limit_id]
    else:
        bucket = payload.get("rateLimits")
    if not isinstance(bucket, dict) or bucket.get("limitId") != limit_id:
        raise BudgetError("rate limit identity mismatch")
    windows = []
    for name in ("primary", "secondary"):
        value = bucket.get(name)
        if value is None:
            continue
        if not isinstance(value, dict):
            raise BudgetError(f"{name}: invalid quota window")
        windows.append(
            {
                "name": name,
                "usedPercent": json_number(
                    quota_number(value.get("usedPercent"), "usedPercent", Decimal(100))
                ),
                "windowDurationMins": quota_integer(
                    value.get("windowDurationMins"), "windowDurationMins"
                ),
                "resetsAt": quota_integer(value.get("resetsAt"), "resetsAt"),
            }
        )
    weekly = [w for w in windows if w["windowDurationMins"] == WEEK_MINUTES]
    if len(weekly) != 1:
        raise BudgetError("expected exactly one explicit 10080-minute weekly window")
    permission = payload.get("ordinaryUsageAllowed")
    if permission is not None and not isinstance(permission, bool):
        raise BudgetError("ordinaryUsageAllowed: invalid permission")
    if (
        bucket.get("spendControlReached") is not None
        and type(bucket["spendControlReached"]) is not bool
    ):
        raise BudgetError("spendControlReached: invalid state")
    reached = bucket.get("rateLimitReachedType")
    if reached is not None and (not isinstance(reached, str) or not reached):
        raise BudgetError("rateLimitReachedType: invalid state")
    receipt = now if observed_at is None else observed_timestamp(observed_at)
    if receipt > now + 5:
        raise BudgetError("usage observation is in the future")
    window = weekly[0]
    result = {
        "schemaVersion": VERSION,
        "observedAt": datetime.fromtimestamp(receipt, UTC).isoformat(),
        "accountFingerprint": hashlib.sha256(account.encode()).hexdigest(),
        "limitId": limit_id,
        "planType": bucket.get("planType"),
        "usedPercent": window["usedPercent"],
        "remainingPercent": json_number(
            Decimal(100) - number(window["usedPercent"], "usedPercent")
        ),
        "windowDurationMins": WEEK_MINUTES,
        "resetsAt": window["resetsAt"],
        "weeklyWindow": window["name"],
        "windows": windows,
        "ordinaryUsageAllowed": permission,
        "rateLimitReachedType": bucket.get("rateLimitReachedType"),
        "spendControlReached": bucket.get("spendControlReached") is True,
        "reservePercent": int(RESERVE),
        "marginPercent": int(MARGIN),
        "source": source,
        "bestEffort": True,
    }
    validate_snapshot(result)
    return result


def validate_snapshot(snapshot):
    """Validate persisted quota facts; custom metadata does not establish permission."""
    if (
        not isinstance(snapshot, dict)
        or type(snapshot.get("schemaVersion")) is not int
        or snapshot["schemaVersion"] != VERSION
    ):
        raise BudgetError("unknown or damaged snapshot schema; refresh required")
    fingerprint = snapshot.get("accountFingerprint")
    if (
        not isinstance(fingerprint, str)
        or len(fingerprint) != 64
        or any(c not in "0123456789abcdef" for c in fingerprint)
    ):
        raise BudgetError("snapshot account identity invalid")
    identifier(snapshot.get("limitId"), "snapshot limit ID")
    observed_timestamp(snapshot.get("observedAt"))
    used = quota_number(snapshot.get("usedPercent"), "usedPercent", Decimal(100))
    reset = quota_integer(snapshot.get("resetsAt"), "resetsAt")
    if (
        reset > 2**63 - 1
        or quota_integer(snapshot.get("windowDurationMins"), "windowDurationMins") != WEEK_MINUTES
    ):
        raise BudgetError("snapshot weekly window invalid")
    windows = snapshot.get("windows")
    if not isinstance(windows, list) or not 1 <= len(windows) <= 2:
        raise BudgetError("snapshot quota windows unavailable")
    names, weekly = set(), []
    for window in windows:
        if (
            not isinstance(window, dict)
            or window.get("name") not in ("primary", "secondary")
            or window["name"] in names
        ):
            raise BudgetError("snapshot quota window identity invalid")
        names.add(window["name"])
        quota_number(window.get("usedPercent"), "window usage", Decimal(100))
        duration = quota_integer(window.get("windowDurationMins"), "window duration")
        window_reset = quota_integer(window.get("resetsAt"), "window reset")
        if window_reset > 2**63 - 1:
            raise BudgetError("snapshot window reset invalid")
        if duration == WEEK_MINUTES:
            weekly.append(window)
    if (
        len(weekly) != 1
        or snapshot.get("weeklyWindow") != weekly[0]["name"]
        or quota_number(weekly[0]["usedPercent"], "weekly usage") != used
        or weekly[0]["resetsAt"] != reset
    ):
        raise BudgetError("snapshot weekly facts are inconsistent")
    if (
        snapshot.get("ordinaryUsageAllowed") is not None
        and type(snapshot["ordinaryUsageAllowed"]) is not bool
    ):
        raise BudgetError("snapshot permission invalid")
    if type(snapshot.get("spendControlReached")) is not bool:
        raise BudgetError("snapshot spend-control state unavailable")
    reached = snapshot.get("rateLimitReachedType")
    if reached is not None and (not isinstance(reached, str) or not reached):
        raise BudgetError("snapshot server-limit state invalid")
    warnings = snapshot.get("warnings", [])
    if not isinstance(warnings, list) or any(
        not isinstance(item, str) or not item for item in warnings
    ):
        raise BudgetError("snapshot warnings invalid")
    if snapshot.get("transitionUntil") is not None:
        quota_integer(snapshot["transitionUntil"], "window transition")
    return snapshot


def fsync_directory(path):
    """POSIX persists the rename; Windows has file fsync and atomic replacement only."""
    if os.name == "nt":
        return
    directory = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        if path.exists() and os.name != "nt" and hasattr(os, "fchmod"):
            os.fchmod(descriptor, path.stat().st_mode & 0o777)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def identity(snapshot):
    return (snapshot.get("accountFingerprint"), snapshot.get("limitId"), snapshot.get("resetsAt"))


class Ledger:
    def __init__(self, db_path=DEFAULT_DB, state_path=DEFAULT_STATE):
        self.db_path, self.state_path = Path(db_path), Path(state_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY, account_fp TEXT NOT NULL, limit_id TEXT NOT NULL,
                    reset_at INTEGER NOT NULL, observed REAL NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS stages (
                    account_fp TEXT NOT NULL, limit_id TEXT NOT NULL, reset_at INTEGER NOT NULL,
                    task_id TEXT NOT NULL, root_id TEXT NOT NULL, priority TEXT NOT NULL,
                    estimate TEXT, status TEXT NOT NULL, started REAL NOT NULL,
                    finished REAL, PRIMARY KEY(account_fp,limit_id,reset_at,task_id));
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        try:
            yield db
        finally:
            db.close()

    def _latest(self, db):
        row = db.execute("SELECT * FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
        if row:
            return self._snapshot_row(row)
        if self.state_path.exists():
            try:
                data = strict_json_loads(self.state_path.read_text(encoding="utf-8"))
            except (BudgetError, OSError, UnicodeError):
                raise BudgetError("cached snapshot unreadable; refresh required") from None
            return validate_snapshot(data)
        raise BudgetError("no usage snapshot; run status --fresh or ingest")

    def _snapshot_row(self, row):
        data = validate_snapshot(strict_json_loads(row["data"]))
        if (
            identity(data) != (row["account_fp"], row["limit_id"], row["reset_at"])
            or observed_timestamp(data["observedAt"]) != row["observed"]
        ):
            raise BudgetError("SQLite snapshot identity or observation mismatch")
        return data

    def ingest(self, payload, **kwargs):
        now = kwargs.get("now")
        now = clock_value(time.time() if now is None else now)
        snapshot = normalize(payload, **kwargs)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                # A damaged canonical DB row cannot be replaced as though no history existed.
                if db.execute("SELECT 1 FROM snapshots LIMIT 1").fetchone():
                    cached_previous = self._latest(db)
                else:
                    try:
                        cached_previous = self._latest(db)
                    except BudgetError:
                        cached_previous = {}
                account_previous = db.execute(
                    "SELECT * FROM snapshots WHERE account_fp=? AND limit_id=? ORDER BY id DESC LIMIT 1",
                    identity(snapshot)[:2],
                ).fetchone()
                previous = (
                    self._snapshot_row(account_previous)
                    if account_previous
                    else (
                        cached_previous
                        if identity(cached_previous)[:2] == identity(snapshot)[:2]
                        else {}
                    )
                )
                if previous.get("accountFingerprint") == snapshot[
                    "accountFingerprint"
                ] and observed_timestamp(snapshot["observedAt"]) < observed_timestamp(
                    previous["observedAt"]
                ):
                    raise BudgetError("out-of-order usage observation")
                # Unknown fields in the existing public cache remain intact.
                merged = dict(cached_previous)
                if self.state_path.exists():
                    try:
                        cache = strict_json_loads(self.state_path.read_text(encoding="utf-8"))
                    except (BudgetError, OSError, UnicodeError):
                        cache = {}
                    if isinstance(cache, dict):
                        for key, value in cache.items():
                            if key not in snapshot and key not in (
                                "previous",
                                "warnings",
                                "transitionUntil",
                            ):
                                merged[key] = value
                merged.update(snapshot)
                merged["previous"] = {
                    k: previous.get(k)
                    for k in (
                        "observedAt",
                        "accountFingerprint",
                        "limitId",
                        "resetsAt",
                        "usedPercent",
                    )
                }
                warnings = []
                same_account = (
                    previous.get("accountFingerprint"),
                    previous.get("limitId"),
                ) == identity(snapshot)[:2]
                if same_account and previous.get("resetsAt") != snapshot["resetsAt"]:
                    if now < previous["resetsAt"] or snapshot["resetsAt"] <= previous["resetsAt"]:
                        warnings.append("window_transition_unconfirmed")
                if identity(previous) == identity(snapshot) and number(
                    snapshot["usedPercent"], "usedPercent"
                ) < number(previous["usedPercent"], "usedPercent"):
                    warnings.append("usage_decreased_same_window")
                # A transition warning persists until the old period really ends.
                old_transition = previous.get("transitionUntil")
                if old_transition and now < old_transition:
                    warnings.append("window_transition_unconfirmed")
                merged["transitionUntil"] = (
                    previous.get("resetsAt")
                    if "window_transition_unconfirmed" in warnings
                    and previous.get("transitionUntil") is None
                    else old_transition
                )
                merged["warnings"] = sorted(set(warnings))
                merged.setdefault("lastAlert", None)
                db.execute(
                    "INSERT INTO snapshots(account_fp,limit_id,reset_at,observed,data) VALUES(?,?,?,?,?)",
                    (
                        *identity(merged),
                        observed_timestamp(merged["observedAt"]),
                        json.dumps(merged, ensure_ascii=False, allow_nan=False),
                    ),
                )
                # Commit the canonical DB first. Reacquire the DB write lock and publish
                # its latest version, so a slow writer cannot overwrite a newer cache.
                db.execute("COMMIT")
                db.execute("BEGIN IMMEDIATE")
                published = self._latest(db)
                atomic_json(self.state_path, published)
                db.execute("COMMIT")
                return published
            except Exception:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    def snapshot(self):
        with self.connection() as db:
            return self._latest(db)

    def _pending(self, db, snapshot):
        # Early/reset anomalies retain older pending work in this account bucket.
        if "window_transition_unconfirmed" in snapshot.get("warnings", []):
            rows = db.execute(
                "SELECT * FROM stages WHERE account_fp=? AND limit_id=?", identity(snapshot)[:2]
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM stages WHERE account_fp=? AND limit_id=? AND reset_at=?",
                identity(snapshot),
            ).fetchall()
        estimates, unknown = [], 0
        for row in rows:
            try:
                identifier(row["task_id"], "stored task ID")
                identifier(row["root_id"], "stored root ID")
                if row["status"] not in ("running", "unreconciled") or row["priority"] not in (
                    "ordinary",
                    "required",
                    "urgent",
                ):
                    raise BudgetError("stored stage state invalid")
                if row["estimate"] is None:
                    raise BudgetError("stored estimate unavailable")
                estimates.append(number(row["estimate"], "stored estimate", Decimal(100)))
            except BudgetError:
                # Keep the row and its uncertainty; corrupt estimates must never become zero cost.
                unknown += 1
        return rows, exact_sum(estimates), unknown

    def _pace(self, db, snapshot, now):
        rows = db.execute(
            "SELECT * FROM snapshots WHERE account_fp=? AND limit_id=? AND reset_at=? AND observed>=? ORDER BY observed",
            (*identity(snapshot), now - 86400),
        ).fetchall()
        if not rows:
            return None
        first = self._snapshot_row(rows[0])
        span = observed_timestamp(snapshot["observedAt"]) - rows[0]["observed"]
        if span < 3600:
            return None
        delta = number(snapshot["usedPercent"], "usedPercent") - number(
            first["usedPercent"], "usedPercent"
        )
        if delta <= 0:
            return None
        daily_rate = delta * Decimal(86400) / Decimal(str(span))
        projected = number(snapshot["usedPercent"], "usedPercent") + daily_rate * Decimal(
            str(max(0, snapshot["resetsAt"] - now))
        ) / Decimal(86400)
        return {
            "observedPpPerDay": float(daily_rate),
            "projectedUsedAtReset": float(projected),
            "reserveAtRisk": projected > 80,
            "estimateOnly": True,
        }

    def _assess(self, db, snapshot, task_id, root_id, priority, estimate, short, background, now):
        if priority not in ("ordinary", "required", "urgent"):
            raise BudgetError("invalid priority")
        identifier(task_id, "task ID")
        identifier(root_id, "root ID")
        if type(short) is not bool or type(background) is not bool:
            raise BudgetError("short and background flags must be booleans")
        now = clock_value(now)
        estimate = None if estimate is None else number(estimate, "estimate", Decimal(100))
        rows, pending, unknown = self._pending(db, snapshot)
        reasons = []
        age = None
        try:
            age = now - observed_timestamp(snapshot.get("observedAt"))
            if age < 0 or age > (SHORT_SECONDS if short else FRESH_SECONDS):
                reasons.append("snapshot_not_fresh")
        except BudgetError:
            reasons.append("snapshot_not_fresh")
        try:
            used = number(snapshot.get("usedPercent"), "usedPercent", Decimal(100))
            reset = integer(snapshot.get("resetsAt"), "resetsAt")
        except BudgetError:
            used, reset = None, None
            reasons.append("quota_unknown")
        if not snapshot.get("accountFingerprint"):
            reasons.append("account_identity_unknown")
        if reset is not None and now >= reset:
            reasons.append("reset_requires_refresh")
        if snapshot.get("ordinaryUsageAllowed") is not True:
            reasons.append("ordinary_usage_permission_unavailable_or_blocked")
        if snapshot.get("spendControlReached") or snapshot.get("rateLimitReachedType"):
            reasons.append("server_limit_state")
        for window in snapshot.get("windows", []):
            if (
                window.get("usedPercent") is None
                or number(window["usedPercent"], "window usage", Decimal(100)) >= 100
            ):
                reasons.append("another_window_exhausted_or_unknown")
            if window.get("resetsAt") is None or window["resetsAt"] <= now:
                reasons.append("another_window_requires_refresh")
            if window.get("windowDurationMins") != WEEK_MINUTES:
                reasons.append("additional_window_cost_unestimated")
        reasons.extend(snapshot.get("warnings", []))
        if estimate is None:
            reasons.append("cost_estimate_unknown")
        if unknown:
            reasons.append("pending_cost_unknown")
        active_other = {
            row["root_id"]
            for row in rows
            if row["status"] == "running" and row["root_id"] != root_id
        }
        if active_other:
            reasons.append("another_root_active")
        headroom = (
            None
            if used is None
            else exact_sum(
                (Decimal(80), used.copy_negate(), pending.copy_negate(), MARGIN.copy_negate())
            )
        )
        if headroom is not None and (
            headroom < 0 or (estimate is not None and estimate > headroom)
        ):
            reasons.append("reserve_or_buffer_would_be_used")
        remaining = None if used is None else Decimal(100) - used
        if remaining is not None and remaining <= 20:
            reasons.append("reserve_only")
        try:
            pace = (
                self._pace(db, snapshot, now)
                if snapshot.get("accountFingerprint") and reset
                else None
            )
        except BudgetError:
            pace = None
            reasons.append("quota_history_unknown")
        if background and (
            (remaining is not None and remaining <= 35) or (pace and pace["reserveAtRisk"])
        ):
            reasons.append("optional_background_deferred")
        days = None if reset is None else max(1, math.ceil(max(0, reset - now) / 86400))
        available = None if headroom is None else max(Decimal(0), headroom)
        return {
            "decision": ("defer" if reasons else "allow_estimated")
            if priority == "ordinary"
            else ("proceed_with_warning" if reasons else "allow_estimated"),
            "priority": priority,
            "taskId": task_id,
            "rootId": root_id,
            "estimatePp": None if estimate is None else float(estimate),
            "usedPercent": None if used is None else float(used),
            "remainingPercent": None if remaining is None else float(remaining),
            "reservePercent": 20,
            "marginPercent": 3,
            "pendingEstimatePp": float(pending),
            "unknownPendingCount": unknown,
            "availableOrdinaryPp": None if available is None else float(available),
            "dailyPortionsRemaining": days,
            "dailyOrdinaryPp": None
            if available is None or days is None
            else float(available / Decimal(days)),
            "snapshotAgeSeconds": age,
            "pace": pace,
            "reasons": sorted(set(reasons)),
            "bestEffort": True,
            "costIsUpperBound": False,
        }

    def assess(
        self,
        task_id,
        root_id=None,
        priority="ordinary",
        estimate=None,
        short=False,
        background=False,
        now=None,
    ):
        with self.connection() as db:
            db.execute("BEGIN")
            try:
                try:
                    snapshot = self._latest(db)
                except BudgetError:
                    snapshot = {}
                result = self._assess(
                    db,
                    snapshot,
                    task_id,
                    task_id if root_id is None else root_id,
                    priority,
                    estimate,
                    short,
                    background,
                    time.time() if now is None else now,
                )
                db.execute("COMMIT")
                return result
            except Exception:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    def start(
        self,
        task_id,
        root_id=None,
        priority="ordinary",
        estimate=None,
        short=False,
        background=False,
        now=None,
    ):
        now = clock_value(time.time() if now is None else now)
        identifier(task_id, "task ID")
        root_id = task_id if root_id is None else root_id
        identifier(root_id, "root ID")
        if (
            priority not in ("ordinary", "required", "urgent")
            or type(short) is not bool
            or type(background) is not bool
        ):
            raise BudgetError("invalid priority or boolean flags")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                snapshot = self._latest(db)
                if not snapshot.get("accountFingerprint"):
                    raise BudgetError("identity-backed snapshot required before reservation")
                existing = db.execute(
                    "SELECT * FROM stages WHERE account_fp=? AND limit_id=? AND reset_at=? AND task_id=?",
                    (*identity(snapshot), task_id),
                ).fetchone()
                if existing:
                    requested = (root_id, priority, estimate_text(estimate))
                    actual = (existing["root_id"], existing["priority"], existing["estimate"])
                    if requested != actual:
                        raise BudgetError("task ID already reserved with different parameters")
                    if existing["status"] != "running":
                        raise BudgetError("stage has ended; use a new unique task ID for new work")
                    # Recheck current state but do not add this already reserved estimate twice.
                    recheck = self._assess(
                        db, snapshot, task_id, root_id, priority, Decimal(0), short, background, now
                    )
                    db.execute("COMMIT")
                    recheck["estimatePp"] = (
                        None
                        if estimate is None
                        else float(number(estimate, "estimate", Decimal(100)))
                    )
                    recheck["alreadyRecorded"] = True
                    recheck["status"] = existing["status"]
                    if recheck["decision"] == "allow_estimated":
                        recheck["decision"] = "already_recorded"
                    return recheck
                result = self._assess(
                    db, snapshot, task_id, root_id, priority, estimate, short, background, now
                )
                if result["decision"] == "defer":
                    db.execute("ROLLBACK")
                    return result
                db.execute(
                    "INSERT INTO stages VALUES(?,?,?,?,?,?,?,?,?,NULL)",
                    (
                        *identity(snapshot),
                        task_id,
                        root_id,
                        priority,
                        estimate_text(estimate),
                        "running",
                        now,
                    ),
                )
                db.execute("COMMIT")
                result["status"] = "running"
                return result
            except Exception:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    def finish(self, task_id, now=None):
        now = clock_value(time.time() if now is None else now)
        identifier(task_id, "task ID")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                matches = db.execute(
                    'SELECT * FROM stages WHERE task_id=? AND status="running"', (task_id,)
                ).fetchall()
                if len(matches) > 1:
                    raise BudgetError(
                        "ambiguous task ID across windows/accounts; use globally unique stage IDs"
                    )
                if matches:
                    row = matches[0]
                    db.execute(
                        'UPDATE stages SET status="unreconciled",finished=? WHERE account_fp=? AND limit_id=? AND reset_at=? AND task_id=?',
                        (now, row["account_fp"], row["limit_id"], row["reset_at"], task_id),
                    )
                elif not db.execute("SELECT 1 FROM stages WHERE task_id=?", (task_id,)).fetchone():
                    raise BudgetError("unknown task ID")
                db.execute("COMMIT")
                return {
                    "taskId": task_id,
                    "status": "unreconciled",
                    "reservationRetained": True,
                    "bestEffort": True,
                }
            except Exception:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    def status(self, now=None):
        now = clock_value(time.time() if now is None else now)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                snapshot = self._latest(db)
                # Repair a missing/stale mirror after a crash; the DB stays canonical.
                try:
                    cache = strict_json_loads(self.state_path.read_text(encoding="utf-8"))
                except (BudgetError, OSError, UnicodeError):
                    cache = None
                if cache != snapshot:
                    # Preserve external alert/custom metadata while repairing quota fields.
                    if isinstance(cache, dict):
                        canonical_keys = {
                            "schemaVersion",
                            "observedAt",
                            "accountFingerprint",
                            "limitId",
                            "planType",
                            "usedPercent",
                            "remainingPercent",
                            "windowDurationMins",
                            "resetsAt",
                            "weeklyWindow",
                            "windows",
                            "ordinaryUsageAllowed",
                            "rateLimitReachedType",
                            "spendControlReached",
                            "reservePercent",
                            "marginPercent",
                            "source",
                            "bestEffort",
                            "previous",
                            "warnings",
                            "transitionUntil",
                        }
                        snapshot.update({k: v for k, v in cache.items() if k not in canonical_keys})
                    atomic_json(self.state_path, snapshot)
                rows, pending, unknown = self._pending(db, snapshot)
                plan = self._assess(
                    db, snapshot, "status", "status", "ordinary", Decimal(0), True, False, now
                )
                # Status is a report, not permission to start a task.
                plan.pop("decision")
                stages = [
                    {
                        k: row[k]
                        for k in (
                            "task_id",
                            "root_id",
                            "priority",
                            "estimate",
                            "status",
                            "started",
                            "finished",
                        )
                    }
                    for row in rows
                ]
                db.execute("COMMIT")
                return {
                    "snapshot": snapshot,
                    "planning": plan,
                    "stages": stages,
                    "bestEffort": True,
                }
            except Exception:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
