from __future__ import annotations

import json
import math
import sqlite3
import statistics
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from .config import AppConfig, BtcWeightedConfig
from .models import Direction, MarketState, OrderBookSnapshot, PriceTick
from .orderbook import ExecutionResult, simulate_buy, simulate_sell


COMPONENT_NAMES = (
    "time",
    "contract_price",
    "distance",
    "book_velocity_3s",
    "gap_velocity_3s",
    "book_acceleration_3s",
    "gap_acceleration_3s",
)

BASE_WEIGHT_ANCHORS: dict[float, dict[str, float]] = {
    300.0: {
        "time": 10.0,
        "contract_price": 10.0,
        "distance": 15.0,
        "book_velocity_3s": 20.0,
        "gap_velocity_3s": 25.0,
        "book_acceleration_3s": 8.0,
        "gap_acceleration_3s": 12.0,
    },
    180.0: {
        "time": 10.0,
        "contract_price": 15.0,
        "distance": 25.0,
        "book_velocity_3s": 15.0,
        "gap_velocity_3s": 20.0,
        "book_acceleration_3s": 5.0,
        "gap_acceleration_3s": 10.0,
    },
    60.0: {
        "time": 10.0,
        "contract_price": 20.0,
        "distance": 35.0,
        "book_velocity_3s": 15.0,
        "gap_velocity_3s": 5.0,
        "book_acceleration_3s": 5.0,
        "gap_acceleration_3s": 10.0,
    },
}

LOW_VOL_ADJUSTMENT = {
    "contract_price": -5.0,
    "distance": 5.0,
    "book_velocity_3s": -3.0,
    "gap_velocity_3s": 3.0,
    "book_acceleration_3s": -2.0,
    "gap_acceleration_3s": 2.0,
}

HIGH_VOL_ADJUSTMENT = {
    "contract_price": 5.0,
    "book_velocity_3s": 3.0,
    "book_acceleration_3s": 2.0,
    "gap_velocity_3s": -7.0,
    "gap_acceleration_3s": -3.0,
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def normalize_weights(weights: dict[str, float]) -> dict[str, float]:
    positive = {name: max(0.0, float(weights.get(name, 0.0))) for name in COMPONENT_NAMES}
    total = sum(positive.values())
    if total <= 0:
        return {name: 100.0 / len(COMPONENT_NAMES) for name in COMPONENT_NAMES}
    return {name: value / total * 100.0 for name, value in positive.items()}


def interpolate_value(value: float, points: tuple[tuple[float, float], ...]) -> float:
    ordered = sorted(points)
    if value <= ordered[0][0]:
        return ordered[0][1]
    if value >= ordered[-1][0]:
        return ordered[-1][1]
    for (left_x, left_y), (right_x, right_y) in zip(ordered, ordered[1:]):
        if left_x <= value <= right_x:
            ratio = (value - left_x) / (right_x - left_x)
            return left_y + ratio * (right_y - left_y)
    return ordered[-1][1]


def time_score(remaining_seconds: float) -> float:
    return interpolate_value(
        remaining_seconds,
        ((10.0, 0.0), (60.0, 100.0), (180.0, 80.0), (300.0, 50.0)),
    )


def contract_price_score(price_cents: float) -> float:
    return interpolate_value(
        price_cents,
        ((15.0, 0.0), (40.0, 40.0), (55.0, 80.0), (75.0, 100.0), (90.0, 100.0)),
    )


def base_weights(remaining_seconds: float) -> dict[str, float]:
    if remaining_seconds >= 300.0:
        return dict(BASE_WEIGHT_ANCHORS[300.0])
    if remaining_seconds <= 60.0:
        return dict(BASE_WEIGHT_ANCHORS[60.0])
    upper = 300.0 if remaining_seconds >= 180.0 else 180.0
    lower = 180.0 if remaining_seconds >= 180.0 else 60.0
    ratio = (remaining_seconds - lower) / (upper - lower)
    return {
        name: BASE_WEIGHT_ANCHORS[lower][name]
        + ratio * (BASE_WEIGHT_ANCHORS[upper][name] - BASE_WEIGHT_ANCHORS[lower][name])
        for name in COMPONENT_NAMES
    }


def volatility_adjusted_weights(remaining_seconds: float, ratio: float) -> dict[str, float]:
    weights = base_weights(remaining_seconds)
    if ratio < 1.0:
        factor = clamp((1.0 - ratio) / 0.25, 0.0, 1.0)
        adjustment = LOW_VOL_ADJUSTMENT
    elif ratio > 1.0:
        factor = clamp((ratio - 1.0) / 0.5, 0.0, 1.0)
        adjustment = HIGH_VOL_ADJUSTMENT
    else:
        factor = 0.0
        adjustment = {}
    for name, delta in adjustment.items():
        weights[name] += delta * factor
    return normalize_weights(weights)


class LatencySnapshotSource(Protocol):
    def snapshot(self, now: datetime | None = None) -> dict[str, Any]: ...


class WeightedRound(BaseModel):
    round_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    market_id: str
    market_slug: str
    start_time: datetime
    end_time: datetime
    settings: BtcWeightedConfig
    entry_count: int = 0
    status: Literal["ACTIVE", "SETTLED"] = "ACTIVE"
    official_outcome: Direction | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class WeightedAttempt(BaseModel):
    attempt_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    attempt_number: int | None = None
    idempotency_key: str
    market_id: str
    market_slug: str
    lane: Literal["observed", "p95"]
    side: Literal["BUY", "SELL"]
    direction: Direction
    token_id: str
    status: Literal["INTENT", "MATCHED", "REJECTED", "UNMEASURABLE"] = "INTENT"
    reason: str = "intent_persisted"
    requested_quote: float | None = None
    requested_quantity: float | None = None
    limit_price: float
    expected_avg_price: float | None = None
    expected_quantity: float | None = None
    expected_quote: float | None = None
    expected_fee_usd: float | None = None
    filled_avg_price: float | None = None
    filled_quantity: float = 0.0
    filled_quote: float = 0.0
    fee_usd: float = 0.0
    latency_ms: float | None = None
    requested_at: datetime = Field(default_factory=utc_now)
    requested_book_received_at: datetime | None = None
    due_at: datetime | None = None
    resolved_at: datetime | None = None
    execution_book_received_at: datetime | None = None
    decision_details: dict[str, Any] = Field(default_factory=dict)


class WeightedPosition(BaseModel):
    position_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    market_id: str
    market_slug: str
    lane: Literal["observed", "p95"]
    direction: Direction
    token_id: str
    status: Literal["OPEN", "EXIT_PENDING", "HOLD_TO_SETTLEMENT", "CLOSED", "SETTLED"] = "OPEN"
    entry_attempt_id: str
    entry_price: float
    quantity: float
    entry_quote: float
    entry_fee_usd: float
    entry_score: float
    opened_at: datetime
    exit_attempts: int = 0
    last_sell_book_received_at: datetime | None = None
    exit_reason: str | None = None
    exit_price: float | None = None
    exit_quote: float = 0.0
    exit_fee_usd: float = 0.0
    realized_pnl: float | None = None
    closed_at: datetime | None = None
    official_outcome: Direction | None = None


class BtcWeightedRegistry:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS btc_weighted_rounds (
                round_id TEXT PRIMARY KEY,
                market_id TEXT NOT NULL UNIQUE,
                market_slug TEXT NOT NULL,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_btc_weighted_round_slug
                ON btc_weighted_rounds(market_slug, status);
            CREATE TABLE IF NOT EXISTS btc_weighted_scores (
                market_id TEXT NOT NULL,
                second INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY(market_id, second)
            );
            CREATE INDEX IF NOT EXISTS idx_btc_weighted_score_time
                ON btc_weighted_scores(created_at);
            CREATE TABLE IF NOT EXISTS btc_weighted_attempts (
                attempt_id TEXT PRIMARY KEY,
                attempt_number INTEGER NOT NULL UNIQUE,
                idempotency_key TEXT NOT NULL UNIQUE,
                market_id TEXT NOT NULL,
                lane TEXT NOT NULL,
                side TEXT NOT NULL,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_btc_weighted_attempt_market
                ON btc_weighted_attempts(market_id, attempt_number);
            CREATE TABLE IF NOT EXISTS btc_weighted_positions (
                position_id TEXT PRIMARY KEY,
                market_id TEXT NOT NULL,
                lane TEXT NOT NULL,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_btc_weighted_position_status
                ON btc_weighted_positions(status, market_id);
            """
        )
        self.connection.commit()
        self._mark_interrupted_attempts()

    @staticmethod
    def _json(model: BaseModel | dict[str, Any]) -> str:
        payload = model.model_dump(mode="json") if isinstance(model, BaseModel) else model
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def close(self) -> None:
        self.connection.close()

    def save_round(self, round_: WeightedRound) -> WeightedRound:
        round_.updated_at = utc_now()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO btc_weighted_rounds(
                    round_id, market_id, market_slug, status, updated_at, payload_json
                ) VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(market_id) DO UPDATE SET
                    market_slug=excluded.market_slug,
                    status=excluded.status,
                    updated_at=excluded.updated_at,
                    payload_json=excluded.payload_json
                """,
                (
                    round_.round_id,
                    round_.market_id,
                    round_.market_slug,
                    round_.status,
                    round_.updated_at.isoformat(),
                    self._json(round_),
                ),
            )
        return round_

    def round_for_market(self, market_id: str) -> WeightedRound | None:
        row = self.connection.execute(
            "SELECT payload_json FROM btc_weighted_rounds WHERE market_id = ?", (market_id,)
        ).fetchone()
        return WeightedRound.model_validate_json(row["payload_json"]) if row else None

    def round_for_slug(self, market_slug: str) -> WeightedRound | None:
        row = self.connection.execute(
            "SELECT payload_json FROM btc_weighted_rounds WHERE market_slug = ?", (market_slug,)
        ).fetchone()
        return WeightedRound.model_validate_json(row["payload_json"]) if row else None

    def unresolved_slugs(self) -> list[str]:
        rows = self.connection.execute(
            "SELECT market_slug FROM btc_weighted_rounds WHERE status != 'SETTLED' ORDER BY updated_at"
        ).fetchall()
        return [str(row["market_slug"]) for row in rows]

    def save_score(self, market_id: str, second: int, payload: dict[str, Any]) -> None:
        created_at = str(payload.get("created_at") or utc_now().isoformat())
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO btc_weighted_scores(market_id, second, created_at, payload_json)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(market_id, second) DO UPDATE SET
                    created_at=excluded.created_at, payload_json=excluded.payload_json
                """,
                (market_id, second, created_at, self._json(payload)),
            )

    def score_history(self, market_id: str, limit: int = 300) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT payload_json FROM btc_weighted_scores WHERE market_id = ? "
            "ORDER BY second DESC LIMIT ?",
            (market_id, limit),
        ).fetchall()
        return [json.loads(row["payload_json"]) for row in reversed(rows)]

    def save_attempt(self, attempt: WeightedAttempt) -> WeightedAttempt:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            if attempt.attempt_number is None:
                row = self.connection.execute(
                    "SELECT COALESCE(MAX(attempt_number), 0) + 1 AS value FROM btc_weighted_attempts"
                ).fetchone()
                attempt.attempt_number = int(row["value"])
            self.connection.execute(
                """
                INSERT INTO btc_weighted_attempts(
                    attempt_id, attempt_number, idempotency_key, market_id,
                    lane, side, status, updated_at, payload_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(attempt_id) DO UPDATE SET
                    status=excluded.status, updated_at=excluded.updated_at,
                    payload_json=excluded.payload_json
                """,
                (
                    attempt.attempt_id,
                    attempt.attempt_number,
                    attempt.idempotency_key,
                    attempt.market_id,
                    attempt.lane,
                    attempt.side,
                    attempt.status,
                    utc_now().isoformat(),
                    self._json(attempt),
                ),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return attempt

    def save_position(self, position: WeightedPosition) -> WeightedPosition:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO btc_weighted_positions(
                    position_id, market_id, lane, status, updated_at, payload_json
                ) VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(position_id) DO UPDATE SET
                    status=excluded.status, updated_at=excluded.updated_at,
                    payload_json=excluded.payload_json
                """,
                (
                    position.position_id,
                    position.market_id,
                    position.lane,
                    position.status,
                    utc_now().isoformat(),
                    self._json(position),
                ),
            )
        return position

    def open_positions(self) -> list[WeightedPosition]:
        rows = self.connection.execute(
            "SELECT payload_json FROM btc_weighted_positions WHERE status IN ('OPEN','EXIT_PENDING','HOLD_TO_SETTLEMENT')"
        ).fetchall()
        return [WeightedPosition.model_validate_json(row["payload_json"]) for row in rows]

    def recent_attempts(self, limit: int = 200) -> list[WeightedAttempt]:
        rows = self.connection.execute(
            "SELECT payload_json FROM btc_weighted_attempts ORDER BY attempt_number DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [WeightedAttempt.model_validate_json(row["payload_json"]) for row in rows]

    def recent_positions(self, limit: int = 200) -> list[WeightedPosition]:
        rows = self.connection.execute(
            "SELECT payload_json FROM btc_weighted_positions ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [WeightedPosition.model_validate_json(row["payload_json"]) for row in rows]

    def _mark_interrupted_attempts(self) -> None:
        rows = self.connection.execute(
            "SELECT payload_json FROM btc_weighted_attempts WHERE status = 'INTENT'"
        ).fetchall()
        for row in rows:
            attempt = WeightedAttempt.model_validate_json(row["payload_json"])
            attempt.status = "UNMEASURABLE"
            attempt.reason = "process_restarted_before_shadow_result"
            attempt.resolved_at = utc_now()
            self.save_attempt(attempt)

    def summary(self) -> dict[str, Any]:
        attempts = self.connection.execute(
            """
            SELECT lane,
                   SUM(CASE WHEN side='BUY' THEN 1 ELSE 0 END) AS trials,
                   SUM(CASE WHEN side='BUY' AND status='MATCHED' THEN 1 ELSE 0 END) AS fills,
                   SUM(CASE WHEN status='REJECTED' THEN 1 ELSE 0 END) AS rejected,
                   SUM(CASE WHEN status='UNMEASURABLE' THEN 1 ELSE 0 END) AS unmeasurable
            FROM btc_weighted_attempts GROUP BY lane
            """
        ).fetchall()
        positions = self.connection.execute(
            """
            SELECT lane,
                   SUM(CASE WHEN status IN ('CLOSED','SETTLED') THEN 1 ELSE 0 END) AS completed,
                   SUM(CASE WHEN status='SETTLED' THEN 1 ELSE 0 END) AS settled,
                   COALESCE(SUM(CASE WHEN status IN ('CLOSED','SETTLED')
                       THEN CAST(json_extract(payload_json, '$.realized_pnl') AS REAL) ELSE 0 END), 0) AS pnl
            FROM btc_weighted_positions GROUP BY lane
            """
        ).fetchall()
        attempt_by_lane = {str(row["lane"]): row for row in attempts}
        position_by_lane = {str(row["lane"]): row for row in positions}
        lanes: dict[str, dict[str, Any]] = {}
        for lane in ("observed", "p95"):
            arow = attempt_by_lane.get(lane)
            prow = position_by_lane.get(lane)
            trials = int(arow["trials"] or 0) if arow else 0
            fills = int(arow["fills"] or 0) if arow else 0
            lanes[lane] = {
                "trials": trials,
                "fills": fills,
                "fill_rate": fills / trials if trials else 0.0,
                "rejected": int(arow["rejected"] or 0) if arow else 0,
                "unmeasurable": int(arow["unmeasurable"] or 0) if arow else 0,
                "completed": int(prow["completed"] or 0) if prow else 0,
                "settled": int(prow["settled"] or 0) if prow else 0,
                "realized_pnl": float(prow["pnl"] or 0.0) if prow else 0.0,
            }
        return {"mode": "SHADOW_ONLY", "lanes": lanes}


class BtcWeightedEngine:
    def __init__(
        self,
        config: AppConfig,
        registry: BtcWeightedRegistry,
        latency: LatencySnapshotSource,
    ):
        self.config = config
        self.registry = registry
        self.latency = latency
        self.market: MarketState | None = None
        self.current_round: WeightedRound | None = None
        self.chainlink_tick: PriceTick | None = None
        self.chainlink_history: deque[tuple[datetime, float]] = deque()
        self.book_history: dict[Direction, deque[tuple[datetime, float]]] = {
            Direction.UP: deque(),
            Direction.DOWN: deque(),
        }
        self.metric_history: dict[str, deque[tuple[datetime, float]]] = {}
        self.weights: dict[str, float] = normalize_weights(BASE_WEIGHT_ANCHORS[300.0])
        self._weight_at: datetime | None = None
        self.scores: dict[str, float | None] = {"UP": None, "DOWN": None}
        self.components: dict[str, dict[str, Any]] = {"UP": {}, "DOWN": {}}
        self.candidates: dict[str, dict[str, Any]] = {"UP": {}, "DOWN": {}}
        self.hard_gates: dict[str, dict[str, Any]] = {"UP": {}, "DOWN": {}}
        self.confirmations: dict[str, dict[str, Any]] = {}
        self.positions: dict[str, WeightedPosition] = {
            item.position_id: item for item in registry.open_positions()
        }
        for position in self.positions.values():
            if position.status == "EXIT_PENDING":
                position.status = "OPEN"
                position.exit_reason = "retry_after_restart"
                registry.save_position(position)
        self.pending: dict[str, WeightedAttempt] = {}
        self.score_history: list[dict[str, Any]] = []
        self.diagnostics: dict[str, Any] = {}
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.status = "disabled" if not config.btc_weighted.enabled else "starting"
        self.last_reason = self.status

    @staticmethod
    def _append_history(
        history: deque[tuple[datetime, float]],
        observed_at: datetime,
        value: float,
        retention_seconds: float = 370.0,
    ) -> None:
        observed_at = ensure_utc(observed_at)
        if history and observed_at < history[-1][0]:
            return
        if history and observed_at == history[-1][0]:
            history[-1] = (observed_at, float(value))
        else:
            history.append((observed_at, float(value)))
        cutoff = observed_at - timedelta(seconds=retention_seconds)
        while history and history[0][0] < cutoff:
            history.popleft()

    @staticmethod
    def _midpoint(book: OrderBookSnapshot | None) -> float | None:
        if book is None or book.best_bid is None or book.best_ask is None:
            return None
        return (book.best_bid + book.best_ask) / 2.0

    @staticmethod
    def _sample(
        history: deque[tuple[datetime, float]], target: datetime, tolerance_seconds: float
    ) -> tuple[datetime, float] | None:
        if not history:
            return None
        nearest = min(history, key=lambda row: abs((row[0] - target).total_seconds()))
        return nearest if abs((nearest[0] - target).total_seconds()) <= tolerance_seconds else None

    def _cancel_pending_for_market(
        self, market_id: str, now: datetime, reason: str
    ) -> None:
        for attempt in list(self.pending.values()):
            if attempt.market_id != market_id:
                continue
            self.pending.pop(attempt.attempt_id, None)
            attempt.status = "UNMEASURABLE"
            attempt.reason = reason
            attempt.resolved_at = now
            self._save_attempt_event(attempt)

    def set_market(self, market: MarketState, now: datetime | None = None) -> None:
        now = ensure_utc(now or utc_now())
        changed = self.market is None or self.market.condition_id != market.condition_id
        if changed:
            if self.market is not None:
                self._cancel_pending_for_market(
                    self.market.condition_id,
                    now,
                    "market_changed_before_shadow_result",
                )
            for position in self.positions.values():
                if position.market_id != market.condition_id and position.status in {"OPEN", "EXIT_PENDING"}:
                    position.status = "HOLD_TO_SETTLEMENT"
                    position.exit_reason = "market_changed_before_exit"
                    self.registry.save_position(position)
            self.chainlink_history.clear()
            self.book_history = {Direction.UP: deque(), Direction.DOWN: deque()}
            self.metric_history.clear()
            self.confirmations.clear()
            self.scores = {"UP": None, "DOWN": None}
            self.components = {"UP": {}, "DOWN": {}}
            self.candidates = {"UP": {}, "DOWN": {}}
            self.hard_gates = {"UP": {}, "DOWN": {}}
            self.weights = normalize_weights(BASE_WEIGHT_ANCHORS[300.0])
            self._weight_at = None
        self.market = market
        if market.start_time is None:
            self.current_round = None
            self.score_history = []
            self.status = self.last_reason = "market_start_unavailable"
            return
        existing = self.registry.round_for_market(market.condition_id)
        if existing is None:
            existing = WeightedRound(
                market_id=market.condition_id,
                market_slug=market.slug,
                start_time=ensure_utc(market.start_time),
                end_time=ensure_utc(market.end_time),
                settings=self.config.btc_weighted.model_copy(deep=True),
                created_at=now,
                updated_at=now,
            )
            self.registry.save_round(existing)
            self.events.append(("btc_weighted_round", existing.model_dump(mode="json")))
        self.current_round = existing
        if changed:
            self.score_history = self.registry.score_history(
                market.condition_id, existing.settings.score_history_seconds
            )
        if existing.status == "SETTLED":
            self.status = self.last_reason = "official_settlement"
        else:
            self.status = self.last_reason = "scoring" if existing.settings.enabled else "disabled"

    def add_chainlink_tick(self, tick: PriceTick) -> None:
        self.chainlink_tick = tick
        self._append_history(self.chainlink_history, tick.received_at, tick.price)

    def add_book(self, direction: Direction, book: OrderBookSnapshot) -> None:
        midpoint = self._midpoint(book)
        if midpoint is not None:
            self._append_history(self.book_history[direction], book.received_at, midpoint)

    def _volatility(self, now: datetime, seconds: int, floor_bps: float) -> float:
        cutoff = now - timedelta(seconds=seconds + 2)
        per_second: dict[int, tuple[datetime, float]] = {}
        for observed_at, price in self.chainlink_history:
            if observed_at >= cutoff and price > 0:
                per_second[int(observed_at.timestamp())] = (observed_at, price)
        samples = [per_second[key] for key in sorted(per_second)]
        returns: list[float] = []
        for (left_at, left), (right_at, right) in zip(samples, samples[1:]):
            elapsed = (right_at - left_at).total_seconds()
            if elapsed > 0 and left > 0 and right > 0:
                returns.append(math.log(right / left) / math.sqrt(elapsed))
        raw = statistics.pstdev(returns) if len(returns) >= 2 else 0.0
        return max(raw, floor_bps / 10_000.0)

    def _smoothed_weights(self, remaining: float, vol_ratio: float, now: datetime) -> dict[str, float]:
        target = volatility_adjusted_weights(remaining, vol_ratio)
        if self._weight_at is None:
            self.weights = target
        else:
            elapsed = max(0.0, (now - self._weight_at).total_seconds())
            alpha = 1.0 - math.exp(-elapsed / self.current_round.settings.weight_ema_seconds)
            self.weights = normalize_weights(
                {
                    name: self.weights.get(name, target[name])
                    + alpha * (target[name] - self.weights.get(name, target[name]))
                    for name in COMPONENT_NAMES
                }
            )
        self._weight_at = now
        return self.weights

    def _record_metric(self, key: str, now: datetime, value: float) -> float:
        history = self.metric_history.setdefault(key, deque())
        second = int(now.timestamp())
        if history and int(history[-1][0].timestamp()) == second:
            history[-1] = (now, value)
        else:
            history.append((now, value))
        cutoff = now - timedelta(seconds=60)
        while history and history[0][0] < cutoff:
            history.popleft()
        values = [item[1] for item in history]
        return statistics.pstdev(values) if len(values) >= 2 else 0.0

    @staticmethod
    def _component(
        raw: float | None,
        unit: str,
        score: float | None,
        weight: float,
        std: float | None = None,
        z: float | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        return {
            "available": score is not None,
            "raw": raw,
            "unit": unit,
            "std": std,
            "z": z,
            "score": score,
            "weight": weight,
            "contribution": score * weight / 100.0 if score is not None else None,
            "reason": reason,
        }

    def _direction_components(
        self,
        direction: Direction,
        books: dict[Direction, OrderBookSnapshot],
        now: datetime,
        remaining: float,
        sigma_long: float,
    ) -> dict[str, Any]:
        assert self.current_round is not None
        settings = self.current_round.settings
        orientation = 1.0 if direction == Direction.UP else -1.0
        target = self.market.threshold_price if self.market else None
        current = self.chainlink_tick.price if self.chainlink_tick else None
        midpoint = self._midpoint(books.get(direction))
        time_value = time_score(remaining)
        result = {
            "time": self._component(remaining, "seconds", time_value, self.weights["time"]),
            "contract_price": self._component(
                midpoint * 100.0 if midpoint is not None else None,
                "cents",
                contract_price_score(midpoint * 100.0) if midpoint is not None else None,
                self.weights["contract_price"],
                reason=None if midpoint is not None else "book_midpoint_unavailable",
            ),
        }
        gap = math.log(current / target) if current and target and current > 0 and target > 0 else None
        distance_z = orientation * gap / (sigma_long * math.sqrt(max(remaining, 1.0))) if gap is not None else None
        distance_score = normal_cdf(clamp(distance_z, -8.0, 8.0)) * 100.0 if distance_z is not None else None
        result["distance"] = self._component(
            gap * 10_000.0 if gap is not None else None,
            "bps",
            distance_score,
            self.weights["distance"],
            sigma_long * 10_000.0,
            distance_z,
            None if gap is not None else "target_or_chainlink_unavailable",
        )

        tolerance = settings.sample_tolerance_seconds
        chain_3 = self._sample(self.chainlink_history, now - timedelta(seconds=3), tolerance)
        chain_6 = self._sample(self.chainlink_history, now - timedelta(seconds=6), tolerance)
        book_now = self._sample(self.book_history[direction], now, tolerance)
        book_3 = self._sample(self.book_history[direction], now - timedelta(seconds=3), tolerance)
        book_6 = self._sample(self.book_history[direction], now - timedelta(seconds=6), tolerance)

        book_velocity = (book_now[1] - book_3[1]) * 100.0 if book_now and book_3 else None
        book_acceleration = (
            (book_now[1] - book_3[1]) - (book_3[1] - book_6[1])
        ) * 100.0 if book_now and book_3 and book_6 else None
        gap_velocity = orientation * math.log(current / chain_3[1]) * 10_000.0 if current and chain_3 else None
        gap_acceleration = (
            orientation
            * (math.log(current / chain_3[1]) - math.log(chain_3[1] / chain_6[1]))
            * 10_000.0
            if current and chain_3 and chain_6
            else None
        )

        metric_specs = (
            ("book_velocity_3s", book_velocity, "cents", settings.book_std_floor_cents),
            ("gap_velocity_3s", gap_velocity, "bps", settings.gap_std_floor_bps),
            ("book_acceleration_3s", book_acceleration, "cents/3s^2", settings.book_std_floor_cents),
            ("gap_acceleration_3s", gap_acceleration, "bps/3s^2", settings.gap_std_floor_bps),
        )
        for name, raw, unit, floor in metric_specs:
            if raw is None:
                result[name] = self._component(
                    None, unit, None, self.weights[name], reason="t_minus_3_or_6_unavailable"
                )
                continue
            std = max(self._record_metric(f"{direction.value}:{name}", now, raw), floor)
            z = clamp(raw / std, -8.0, 8.0)
            result[name] = self._component(
                raw, unit, normal_cdf(z) * 100.0, self.weights[name], std, z
            )
        return result

    def _score(self, components: dict[str, Any]) -> float | None:
        if any(not components.get(name, {}).get("available") for name in COMPONENT_NAMES):
            return None
        return clamp(sum(float(components[name]["contribution"]) for name in COMPONENT_NAMES), 0.0, 100.0)

    @staticmethod
    def _gate(passed: bool, value: Any, limit: Any, reason: str) -> dict[str, Any]:
        return {"passed": bool(passed), "value": value, "limit": limit, "reason": reason}

    def _entry_candidate(
        self,
        direction: Direction,
        books: dict[Direction, OrderBookSnapshot],
        now: datetime,
        remaining: float,
        lead: float | None,
    ) -> dict[str, Any]:
        assert self.market is not None and self.current_round is not None
        settings = self.current_round.settings
        score = self.scores[direction.value]
        book = books.get(direction)
        chain_age = (
            (now - ensure_utc(self.chainlink_tick.received_at)).total_seconds()
            if self.chainlink_tick else None
        )
        book_age = (now - ensure_utc(book.received_at)).total_seconds() if book else None
        best_bid = book.best_bid if book else None
        best_ask = book.best_ask if book else None
        spread_cents = (best_ask - best_bid) * 100.0 if best_bid is not None and best_ask is not None else None
        limited = book.model_copy(deep=True) if book else None
        if limited is not None:
            limited.asks = [level for level in limited.asks if level.price <= settings.max_buy_price_cents / 100.0 + 1e-12]
        execution = simulate_buy(limited, settings.quote_amount_usd, self.config.strategy.taker_fee_rate) if limited else None
        minimum = max(self.market.min_order_size, book.min_order_size) if book else self.market.min_order_size
        avg_cents = execution.avg_price * 100.0 if execution and execution.quantity else None
        tick_size = max(self.market.tick_size, book.tick_size) if book else self.market.tick_size
        tick_risk = execution.quantity * tick_size if execution else None
        gates = {
            "strategy_enabled": self._gate(settings.enabled, settings.enabled, True, "strategy_disabled"),
            "market_open": self._gate(
                self.market.accepting_orders and now < ensure_utc(self.market.end_time),
                self.market.accepting_orders,
                True,
                "market_not_accepting_orders",
            ),
            "official_target": self._gate(
                bool(self.market.threshold_verified and self.market.threshold_price),
                self.market.threshold_price,
                "verified",
                "official_target_unverified",
            ),
            "score_ready": self._gate(score is not None, score, "ready", "score_history_warming"),
            "score_threshold": self._gate(
                score is not None and score >= settings.entry_score_threshold,
                score,
                settings.entry_score_threshold,
                "entry_score_below_threshold",
            ),
            "score_lead": self._gate(
                lead is not None and lead >= settings.entry_lead_points,
                lead,
                settings.entry_lead_points,
                "entry_lead_below_threshold",
            ),
            "entry_time": self._gate(
                remaining > settings.min_entry_remaining_seconds,
                remaining,
                settings.min_entry_remaining_seconds,
                "entry_window_closed",
            ),
            "chainlink_fresh": self._gate(
                chain_age is not None and 0 <= chain_age <= settings.chainlink_max_age_seconds,
                chain_age,
                settings.chainlink_max_age_seconds,
                "chainlink_stale",
            ),
            "book_fresh": self._gate(
                book_age is not None and 0 <= book_age <= settings.book_max_age_seconds,
                book_age,
                settings.book_max_age_seconds,
                "book_stale",
            ),
            "depth_trusted": self._gate(bool(book and book.depth_trusted), bool(book and book.depth_trusted), True, "book_depth_untrusted"),
            "spread": self._gate(
                spread_cents is not None and spread_cents <= settings.max_spread_cents,
                spread_cents,
                settings.max_spread_cents,
                "spread_too_wide",
            ),
            "complete_fill": self._gate(bool(execution and execution.complete), bool(execution and execution.complete), True, "depth_below_fixed_limit"),
            "price_range": self._gate(
                avg_cents is not None and settings.min_buy_price_cents <= avg_cents <= settings.max_buy_price_cents,
                avg_cents,
                [settings.min_buy_price_cents, settings.max_buy_price_cents],
                "buy_price_out_of_range",
            ),
            "minimum_quantity": self._gate(
                bool(execution and execution.quantity + 1e-12 >= minimum),
                execution.quantity if execution else None,
                minimum,
                "minimum_quantity_unavailable",
            ),
            "one_tick_risk": self._gate(
                tick_risk is not None and tick_risk <= settings.max_one_tick_loss_usd,
                tick_risk,
                settings.max_one_tick_loss_usd,
                "one_tick_risk_exceeded",
            ),
        }
        failed = next((gate["reason"] for gate in gates.values() if not gate["passed"]), None)
        distance_probability = (self.components[direction.value].get("distance") or {}).get("score")
        fee_per_share = execution.fee_usd / execution.quantity if execution and execution.quantity else 0.0
        diagnostic_edge = (
            distance_probability / 100.0 - execution.avg_price - fee_per_share - execution.slippage
            if distance_probability is not None and execution and execution.complete
            else None
        )
        return {
            "eligible": failed is None,
            "reason": failed or "entry_ready",
            "score": score,
            "lead": lead,
            "execution": execution.model_dump(mode="json") if execution else None,
            "diagnostics": {
                "distance_probability": distance_probability,
                "fee_per_share": fee_per_share,
                "slippage_cents": execution.slippage * 100.0 if execution else None,
                "diagnostic_edge_cents": diagnostic_edge * 100.0 if diagnostic_edge is not None else None,
            },
            "gates": gates,
        }

    def _update_key(self, books: dict[Direction, OrderBookSnapshot]) -> str:
        values = [
            self.chainlink_tick.received_at.isoformat() if self.chainlink_tick else "",
            *(books.get(direction).received_at.isoformat() if books.get(direction) else "" for direction in Direction),
        ]
        return "|".join(values)

    def _confirmation(
        self,
        key: str,
        condition: bool,
        update_key: str,
        now: datetime,
        seconds: float,
        updates: int,
    ) -> bool:
        if not condition:
            self.confirmations.pop(key, None)
            return False
        state = self.confirmations.get(key)
        if state is None:
            state = {"started_at": now, "last_update_key": None, "updates": 0}
            self.confirmations[key] = state
        if state["last_update_key"] != update_key:
            state["last_update_key"] = update_key
            state["updates"] += 1
        elapsed = max(0.0, (now - state["started_at"]).total_seconds())
        state["elapsed_seconds"] = elapsed
        state["required_seconds"] = seconds
        state["required_updates"] = updates
        state["confirmed"] = elapsed >= seconds and state["updates"] >= updates
        return bool(state["confirmed"])

    def _save_attempt_event(self, attempt: WeightedAttempt) -> None:
        self.registry.save_attempt(attempt)
        self.events.append(("btc_weighted_attempt", attempt.model_dump(mode="json")))

    def _create_buy_attempts(
        self, direction: Direction, candidate: dict[str, Any], book: OrderBookSnapshot, now: datetime
    ) -> None:
        assert self.market is not None and self.current_round is not None
        execution = ExecutionResult.model_validate(candidate["execution"])
        latency = self.latency.snapshot(now)
        sequence = self.current_round.entry_count + 1
        for lane in ("observed", "p95"):
            delay = latency.get("latest_ms") if lane == "observed" else latency.get("p95_ms")
            usable = latency.get("fresh") if lane == "observed" else latency.get("warmed")
            attempt = WeightedAttempt(
                idempotency_key=f"btc_weighted:{self.market.condition_id}:{sequence}:BUY:{lane}",
                market_id=self.market.condition_id,
                market_slug=self.market.slug,
                lane=lane,
                side="BUY",
                direction=direction,
                token_id=self.market.up_token_id if direction == Direction.UP else self.market.down_token_id,
                requested_quote=self.current_round.settings.quote_amount_usd,
                limit_price=self.current_round.settings.max_buy_price_cents / 100.0,
                expected_avg_price=execution.avg_price,
                expected_quantity=execution.quantity,
                expected_quote=execution.quote,
                expected_fee_usd=execution.fee_usd,
                latency_ms=float(delay) if delay is not None else None,
                requested_at=now,
                requested_book_received_at=book.received_at,
                decision_details={
                    "score": candidate["score"],
                    "lead": candidate["lead"],
                    "components": self.components[direction.value],
                    "weights": self.weights,
                    "diagnostics": candidate["diagnostics"],
                },
            )
            if not usable or delay is None:
                attempt.status = "UNMEASURABLE"
                attempt.reason = "latency_probe_warming" if lane == "p95" else "latency_probe_stale"
                attempt.resolved_at = now
            else:
                attempt.reason = "shadow_latency_scheduled"
                attempt.due_at = now + timedelta(milliseconds=float(delay))
                self.pending[attempt.attempt_id] = attempt
            self._save_attempt_event(attempt)
        self.current_round.entry_count += 1
        self.registry.save_round(self.current_round)

    def _execution_after_limit(self, attempt: WeightedAttempt, book: OrderBookSnapshot) -> ExecutionResult:
        limited = book.model_copy(deep=True)
        if attempt.side == "BUY":
            limited.asks = [level for level in limited.asks if level.price <= attempt.limit_price + 1e-12]
            return simulate_buy(limited, float(attempt.requested_quote or 0.0), self.config.strategy.taker_fee_rate)
        limited.bids = [level for level in limited.bids if level.price + 1e-12 >= attempt.limit_price]
        return simulate_sell(limited, float(attempt.requested_quantity or 0.0), self.config.strategy.taker_fee_rate)

    def _position_for_attempt(self, attempt: WeightedAttempt) -> WeightedPosition | None:
        return next(
            (
                position for position in self.positions.values()
                if position.market_id == attempt.market_id
                and position.lane == attempt.lane
                and position.direction == attempt.direction
                and position.status in {"OPEN", "EXIT_PENDING"}
            ),
            None,
        )

    def _resolve_pending(
        self, books: dict[Direction, OrderBookSnapshot], now: datetime
    ) -> None:
        latency_max_age = self.config.orderbook_chase.latency_max_age_seconds
        for attempt in list(self.pending.values()):
            if self.market is None or attempt.market_id != self.market.condition_id:
                continue
            if attempt.due_at is None or now < attempt.due_at:
                continue
            book = books.get(attempt.direction)
            if book is not None and book.token_id != attempt.token_id:
                book = None
            newer = bool(
                book and (
                    attempt.requested_book_received_at is None
                    or ensure_utc(book.received_at) > ensure_utc(attempt.requested_book_received_at)
                )
            )
            if not newer or book is None:
                if (now - attempt.due_at).total_seconds() <= latency_max_age:
                    continue
                attempt.status = "UNMEASURABLE"
                attempt.reason = "no_post_intent_book_update"
                attempt.resolved_at = now
                self.pending.pop(attempt.attempt_id, None)
                position = self._position_for_attempt(attempt)
                if position and attempt.side == "SELL":
                    position.status = "OPEN"
                    self.registry.save_position(position)
                self._save_attempt_event(attempt)
                continue
            execution = self._execution_after_limit(attempt, book)
            minimum = max(self.market.min_order_size, book.min_order_size) if self.market else book.min_order_size
            complete = execution.complete and (attempt.side == "SELL" or execution.quantity + 1e-12 >= minimum)
            attempt.execution_book_received_at = book.received_at
            attempt.resolved_at = now
            self.pending.pop(attempt.attempt_id, None)
            if not complete:
                attempt.status = "REJECTED"
                attempt.reason = "fok_depth_or_limit_unavailable"
                position = self._position_for_attempt(attempt)
                if position and attempt.side == "SELL":
                    position.status = "OPEN"
                    position.last_sell_book_received_at = book.received_at
                    self.registry.save_position(position)
                self._save_attempt_event(attempt)
                continue
            attempt.status = "MATCHED"
            attempt.reason = "shadow_fok_matched"
            attempt.filled_avg_price = execution.avg_price
            attempt.filled_quantity = execution.quantity
            attempt.filled_quote = execution.quote
            attempt.fee_usd = execution.fee_usd
            self._save_attempt_event(attempt)
            if attempt.side == "BUY":
                self._open_position(attempt, execution, now)
            else:
                self._close_position(attempt, execution, now)

    def _open_position(self, attempt: WeightedAttempt, execution: ExecutionResult, now: datetime) -> None:
        position = WeightedPosition(
            market_id=attempt.market_id,
            market_slug=attempt.market_slug,
            lane=attempt.lane,
            direction=attempt.direction,
            token_id=attempt.token_id,
            entry_attempt_id=attempt.attempt_id,
            entry_price=execution.avg_price,
            quantity=execution.quantity,
            entry_quote=execution.quote,
            entry_fee_usd=execution.fee_usd,
            entry_score=float(attempt.decision_details.get("score") or 0.0),
            opened_at=now,
        )
        self.positions[position.position_id] = position
        self.registry.save_position(position)
        self.events.append(("btc_weighted_position", position.model_dump(mode="json")))

    def _close_position(self, attempt: WeightedAttempt, execution: ExecutionResult, now: datetime) -> None:
        position = self._position_for_attempt(attempt)
        if position is None:
            return
        position.status = "CLOSED"
        position.exit_price = execution.avg_price
        position.exit_quote = execution.quote
        position.exit_fee_usd = execution.fee_usd
        position.realized_pnl = (
            execution.quote - execution.fee_usd - position.entry_quote - position.entry_fee_usd
        )
        position.closed_at = now
        position.exit_reason = "score_reversal"
        self.registry.save_position(position)
        self.confirmations.pop(f"exit:{position.position_id}", None)
        self.events.append(("btc_weighted_position", position.model_dump(mode="json")))

    @staticmethod
    def _sell_limit(book: OrderBookSnapshot, quantity: float) -> float | None:
        remaining = quantity
        worst: float | None = None
        for level in sorted(book.bids, key=lambda item: item.price, reverse=True):
            take = min(remaining, level.size)
            if take > 0:
                worst = level.price
                remaining -= take
            if remaining <= 1e-9:
                return worst
        return None

    def _create_sell_attempt(
        self, position: WeightedPosition, book: OrderBookSnapshot, now: datetime
    ) -> bool:
        execution = simulate_sell(book.model_copy(deep=True), position.quantity, self.config.strategy.taker_fee_rate)
        limit_price = self._sell_limit(book, position.quantity)
        if not execution.complete or limit_price is None:
            position.exit_reason = "score_reversal"
            position.last_sell_book_received_at = book.received_at
            self.registry.save_position(position)
            self.last_reason = "sell_depth_unavailable"
            return False
        latency = self.latency.snapshot(now)
        delay = latency.get("latest_ms") if position.lane == "observed" else latency.get("p95_ms")
        usable = latency.get("fresh") if position.lane == "observed" else latency.get("warmed")
        position.exit_attempts += 1
        attempt = WeightedAttempt(
            idempotency_key=(
                f"btc_weighted:{position.market_id}:{position.position_id}:"
                f"SELL:{position.lane}:{position.exit_attempts}"
            ),
            market_id=position.market_id,
            market_slug=position.market_slug,
            lane=position.lane,
            side="SELL",
            direction=position.direction,
            token_id=position.token_id,
            requested_quantity=position.quantity,
            limit_price=limit_price,
            expected_avg_price=execution.avg_price,
            expected_quantity=execution.quantity,
            expected_quote=execution.quote,
            expected_fee_usd=execution.fee_usd,
            latency_ms=float(delay) if delay is not None else None,
            requested_at=now,
            requested_book_received_at=book.received_at,
            decision_details={"exit_reason": "score_reversal"},
        )
        position.last_sell_book_received_at = book.received_at
        if not usable or delay is None:
            attempt.status = "UNMEASURABLE"
            attempt.reason = "latency_probe_warming" if position.lane == "p95" else "latency_probe_stale"
            attempt.resolved_at = now
            position.status = "OPEN"
        else:
            attempt.reason = "shadow_latency_scheduled"
            attempt.due_at = now + timedelta(milliseconds=float(delay))
            self.pending[attempt.attempt_id] = attempt
            position.status = "EXIT_PENDING"
        self.registry.save_position(position)
        self._save_attempt_event(attempt)
        return usable and delay is not None

    def _evaluate_positions(
        self, books: dict[Direction, OrderBookSnapshot], now: datetime, update_key: str
    ) -> None:
        if self.market is None or self.current_round is None:
            return
        settings = self.current_round.settings
        for position in list(self.positions.values()):
            if position.market_id != self.market.condition_id or position.status != "OPEN":
                continue
            if now >= ensure_utc(self.market.end_time):
                position.status = "HOLD_TO_SETTLEMENT"
                position.exit_reason = position.exit_reason or "market_ended"
                self.registry.save_position(position)
                continue
            book = books.get(position.direction)
            if position.exit_reason and book and (
                position.last_sell_book_received_at is None
                or ensure_utc(book.received_at) > ensure_utc(position.last_sell_book_received_at)
            ):
                self._create_sell_attempt(position, book, now)
                continue
            held = (now - ensure_utc(position.opened_at)).total_seconds()
            opposite = Direction.DOWN if position.direction == Direction.UP else Direction.UP
            opposite_score = self.scores.get(opposite.value)
            held_score = self.scores.get(position.direction.value)
            lead = opposite_score - held_score if opposite_score is not None and held_score is not None else None
            condition = bool(
                held >= settings.min_hold_seconds
                and opposite_score is not None
                and opposite_score >= settings.exit_score_threshold
                and lead is not None
                and lead >= settings.exit_lead_points
            )
            confirmed = self._confirmation(
                f"exit:{position.position_id}",
                condition,
                update_key,
                now,
                settings.exit_confirmation_seconds,
                settings.exit_confirmation_updates,
            )
            if confirmed and book is not None:
                position.exit_reason = "score_reversal"
                self._create_sell_attempt(position, book, now)

    def _record_score_snapshot(self, now: datetime, remaining: float) -> None:
        if self.current_round is None:
            return
        second = int(now.timestamp())
        payload = {
            "created_at": now.isoformat(),
            "remaining_seconds": remaining,
            "scores": dict(self.scores),
            "entry_threshold": self.current_round.settings.entry_score_threshold,
            "exit_threshold": self.current_round.settings.exit_score_threshold,
            "weights": dict(self.weights),
        }
        if self.score_history and int(datetime.fromisoformat(self.score_history[-1]["created_at"]).timestamp()) == second:
            self.score_history[-1] = payload
        else:
            self.score_history.append(payload)
        self.score_history = self.score_history[-self.current_round.settings.score_history_seconds :]
        self.registry.save_score(self.current_round.market_id, second, payload)

    def evaluate(
        self,
        market: MarketState | None,
        books: dict[Direction, OrderBookSnapshot],
        now: datetime | None = None,
    ) -> None:
        now = ensure_utc(now or utc_now())
        if market is None:
            self.status = self.last_reason = "waiting_for_btc_market"
            return
        self.set_market(market, now)
        if self.current_round is None:
            return
        if self.current_round.status == "SETTLED":
            self.status = self.last_reason = "official_settlement"
            return
        for direction, book in books.items():
            self.add_book(direction, book)
        remaining = (ensure_utc(market.end_time) - now).total_seconds()
        if remaining <= 0:
            self._cancel_pending_for_market(
                market.condition_id,
                now,
                "market_ended_before_shadow_result",
            )
            for position in self.positions.values():
                if position.market_id == market.condition_id and position.status in {"OPEN", "EXIT_PENDING"}:
                    position.status = "HOLD_TO_SETTLEMENT"
                    position.exit_reason = position.exit_reason or "market_ended"
                    self.registry.save_position(position)
            chain_age = (
                (now - ensure_utc(self.chainlink_tick.received_at)).total_seconds()
                if self.chainlink_tick else None
            )
            self.diagnostics = {
                **self.diagnostics,
                "target_price": market.threshold_price,
                "target_verified": market.threshold_verified,
                "chainlink_price": self.chainlink_tick.price if self.chainlink_tick else None,
                "chainlink_age_seconds": chain_age,
                "remaining_seconds": remaining,
                "latency": self.latency.snapshot(now),
            }
            self.status = self.last_reason = "waiting_official_settlement"
            return
        self._resolve_pending(books, now)
        settings = self.current_round.settings
        sigma_short = self._volatility(now, settings.short_volatility_window_seconds, settings.volatility_floor_bps)
        sigma_long = self._volatility(now, settings.long_volatility_window_seconds, settings.volatility_floor_bps)
        vol_ratio = sigma_short / sigma_long if sigma_long > 0 else 1.0
        self._smoothed_weights(remaining, vol_ratio, now)
        for direction in Direction:
            direction_components = self._direction_components(
                direction, books, now, remaining, sigma_long
            )
            self.components[direction.value] = direction_components
            self.scores[direction.value] = self._score(direction_components)
        up_score, down_score = self.scores["UP"], self.scores["DOWN"]
        leads = {
            "UP": up_score - down_score if up_score is not None and down_score is not None else None,
            "DOWN": down_score - up_score if up_score is not None and down_score is not None else None,
        }
        for direction in Direction:
            candidate = self._entry_candidate(direction, books, now, remaining, leads[direction.value])
            self.candidates[direction.value] = candidate
            self.hard_gates[direction.value] = candidate["gates"]
        update_key = self._update_key(books)
        self._evaluate_positions(books, now, update_key)
        self._record_score_snapshot(now, remaining)

        target_direction: Direction | None = None
        if up_score is not None and down_score is not None:
            target_direction = Direction.UP if up_score >= down_score else Direction.DOWN
        for direction in Direction:
            candidate = self.candidates[direction.value]
            confirmed = self._confirmation(
                f"entry:{direction.value}",
                bool(
                    direction == target_direction
                    and candidate["eligible"]
                    and self.current_round.entry_count < settings.max_entries_per_market
                ),
                update_key,
                now,
                settings.entry_confirmation_seconds,
                settings.entry_confirmation_updates,
            )
            if confirmed:
                book = books.get(direction)
                if book is not None:
                    self._create_buy_attempts(direction, candidate, book, now)
                    self.confirmations.pop(f"entry:{direction.value}", None)
                    self.status = self.last_reason = "entry_shadow_submitted"
                break
        else:
            if not settings.enabled:
                self.status = self.last_reason = "disabled"
            elif self.current_round.entry_count >= settings.max_entries_per_market:
                self.status = self.last_reason = "market_entry_limit"
            elif target_direction is None:
                self.status = self.last_reason = "score_history_warming"
            else:
                self.status = self.last_reason = self.candidates[target_direction.value]["reason"]
        chain_age = (
            (now - ensure_utc(self.chainlink_tick.received_at)).total_seconds()
            if self.chainlink_tick else None
        )
        self.diagnostics = {
            "target_price": market.threshold_price,
            "target_verified": market.threshold_verified,
            "chainlink_price": self.chainlink_tick.price if self.chainlink_tick else None,
            "chainlink_age_seconds": chain_age,
            "remaining_seconds": remaining,
            "short_volatility_bps": sigma_short * 10_000.0,
            "long_volatility_bps": sigma_long * 10_000.0,
            "volatility_ratio": vol_ratio,
            "score_leads": leads,
            "latency": self.latency.snapshot(now),
        }

    def settle(self, market_slug: str, outcome: Direction, now: datetime | None = None) -> bool:
        now = ensure_utc(now or utc_now())
        round_ = self.registry.round_for_slug(market_slug)
        if round_ is None or round_.status == "SETTLED":
            return False
        self._cancel_pending_for_market(
            round_.market_id,
            now,
            "official_settlement_before_shadow_result",
        )
        round_.status = "SETTLED"
        round_.official_outcome = outcome
        self.registry.save_round(round_)
        for position in self.positions.values():
            if position.market_slug != market_slug or position.status in {"CLOSED", "SETTLED"}:
                continue
            payout = position.quantity if position.direction == outcome else 0.0
            position.status = "SETTLED"
            position.official_outcome = outcome
            position.exit_price = 1.0 if position.direction == outcome else 0.0
            position.exit_quote = payout
            position.realized_pnl = payout - position.entry_quote - position.entry_fee_usd
            position.closed_at = now
            position.exit_reason = "official_settlement"
            self.registry.save_position(position)
        self.events.append(("btc_weighted_settlement", round_.model_dump(mode="json")))
        if self.current_round and self.current_round.market_slug == market_slug:
            self.current_round = round_
            self.status = self.last_reason = "official_settlement"
        return True

    def unresolved_slugs(self) -> list[str]:
        return self.registry.unresolved_slugs()

    @staticmethod
    def _confirmation_payload(state: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value.isoformat() if isinstance(value, datetime) else value
            for key, value in state.items()
        }

    def dashboard_state(self) -> dict[str, Any]:
        return {
            "mode": "SHADOW_ONLY",
            "enabled": self.config.btc_weighted.enabled,
            "status": self.status,
            "last_reason": self.last_reason,
            "config": self.config.btc_weighted.model_dump(mode="json"),
            "round": self.current_round.model_dump(mode="json") if self.current_round else None,
            "scores": dict(self.scores),
            "components": self.components,
            "weights": dict(self.weights),
            "confirmations": {
                key: self._confirmation_payload(value) for key, value in self.confirmations.items()
            },
            "positions": [
                position.model_dump(mode="json")
                for position in self.positions.values()
                if position.status not in {"CLOSED", "SETTLED"}
            ],
            "candidates": self.candidates,
            "hard_gates": self.hard_gates,
            "diagnostics": self.diagnostics,
            "recent_attempts": [
                item.model_dump(mode="json") for item in self.registry.recent_attempts()
            ],
            "recent_positions": [
                item.model_dump(mode="json") for item in self.registry.recent_positions()
            ],
            "score_history": list(self.score_history),
            "summary": self.registry.summary(),
        }

    def drain_events(self) -> list[tuple[str, dict[str, Any]]]:
        events, self.events = self.events, []
        return events
