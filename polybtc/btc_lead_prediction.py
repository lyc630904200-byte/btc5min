from __future__ import annotations

import hashlib
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

from .config import AppConfig, BtcLeadPredictionConfig
from .models import Direction, MarketState, OrderBookSnapshot, PriceTick
from .orderbook import ExecutionResult, simulate_buy, simulate_sell, taker_fee_usd
from .signal_sources import SignalEvent


Lane = Literal["observed", "p95"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def weighted_median(values: list[tuple[float, float]]) -> float | None:
    rows = sorted((float(value), max(0.0, float(weight))) for value, weight in values)
    total = sum(weight for _, weight in rows)
    if total <= 0:
        return None
    cumulative = 0.0
    for value, weight in rows:
        cumulative += weight
        if cumulative >= total / 2.0:
            return value
    return rows[-1][0] if rows else None


def lead_config_version(settings: BtcLeadPredictionConfig) -> str:
    payload = json.dumps(
        settings.model_dump(mode="json"),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


class LatencySnapshotSource(Protocol):
    def snapshot(self, now: datetime | None = None) -> dict[str, Any]: ...


class LeadRound(BaseModel):
    round_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    market_id: str
    market_slug: str
    start_time: datetime
    end_time: datetime
    settings: BtcLeadPredictionConfig
    config_version: str
    trade_count: int = 0
    used_shock_ids: list[str] = Field(default_factory=list)
    status: Literal["ACTIVE", "SETTLED"] = "ACTIVE"
    official_outcome: Direction | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class LeadPrediction(BaseModel):
    prediction_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    market_id: str
    market_slug: str
    config_version: str
    created_at: datetime
    external_index: float
    chainlink_price: float
    target_price: float | None = None
    source_weights: dict[str, float] = Field(default_factory=dict)
    source_prices: dict[str, float] = Field(default_factory=dict)
    projections: dict[str, dict[str, Any]] = Field(default_factory=dict)
    initial_books: dict[str, dict[str, float | None]] = Field(default_factory=dict)
    actuals: dict[str, dict[str, Any]] = Field(default_factory=dict)
    matured_horizons: list[int] = Field(default_factory=list)


class LeadShock(BaseModel):
    shock_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    market_id: str
    market_slug: str
    direction: Direction
    started_at: datetime
    confirmed_at: datetime
    peak_at: datetime
    expires_at: datetime
    amplitude_bps: float
    velocity_bps_per_second: float
    acceleration_bps_per_second2: float
    sources: list[str] = Field(default_factory=list)
    source_weights: dict[str, float] = Field(default_factory=dict)
    submitted: bool = False
    status: str = "confirmed"


class LeadAttempt(BaseModel):
    attempt_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    attempt_number: int | None = None
    idempotency_key: str
    market_id: str
    market_slug: str
    lane: Lane
    side: Literal["BUY", "SELL"]
    direction: Direction
    token_id: str
    position_id: str | None = None
    shock_id: str | None = None
    config_version: str = ""
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


class LeadPosition(BaseModel):
    position_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    market_id: str
    market_slug: str
    lane: Lane
    direction: Direction
    token_id: str
    shock_id: str
    config_version: str
    status: Literal[
        "OPEN", "EXIT_PENDING", "HOLD_TO_SETTLEMENT", "CLOSED", "SETTLED"
    ] = "OPEN"
    entry_attempt_id: str
    entry_price: float
    quantity: float
    entry_quote: float
    entry_fee_usd: float
    predicted_target_bid: float
    predicted_expiry: datetime
    opened_at: datetime
    exit_attempts: int = 0
    exit_started_at: datetime | None = None
    last_sell_book_received_at: datetime | None = None
    liquidity_failure: bool = False
    exit_reason: str | None = None
    exit_price: float | None = None
    exit_quote: float = 0.0
    exit_fee_usd: float = 0.0
    realized_pnl: float | None = None
    closed_at: datetime | None = None
    official_outcome: Direction | None = None


class BtcLeadPredictionRegistry:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS btc_lead_rounds (
                round_id TEXT PRIMARY KEY, market_id TEXT NOT NULL UNIQUE,
                market_slug TEXT NOT NULL, status TEXT NOT NULL,
                updated_at TEXT NOT NULL, payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_btc_lead_round_slug
                ON btc_lead_rounds(market_slug, status);
            CREATE TABLE IF NOT EXISTS btc_lead_predictions (
                prediction_id TEXT PRIMARY KEY, market_id TEXT NOT NULL,
                created_at TEXT NOT NULL, payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_btc_lead_prediction_time
                ON btc_lead_predictions(created_at);
            CREATE TABLE IF NOT EXISTS btc_lead_shocks (
                shock_id TEXT PRIMARY KEY, market_id TEXT NOT NULL,
                confirmed_at TEXT NOT NULL, status TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS btc_lead_attempts (
                attempt_id TEXT PRIMARY KEY, attempt_number INTEGER NOT NULL UNIQUE,
                idempotency_key TEXT NOT NULL UNIQUE, market_id TEXT NOT NULL,
                lane TEXT NOT NULL, side TEXT NOT NULL, status TEXT NOT NULL,
                updated_at TEXT NOT NULL, payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_btc_lead_attempt_market
                ON btc_lead_attempts(market_id, attempt_number);
            CREATE TABLE IF NOT EXISTS btc_lead_positions (
                position_id TEXT PRIMARY KEY, market_id TEXT NOT NULL,
                lane TEXT NOT NULL, status TEXT NOT NULL,
                updated_at TEXT NOT NULL, payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_btc_lead_position_status
                ON btc_lead_positions(status, market_id);
            CREATE TABLE IF NOT EXISTS btc_lead_source_health (
                source TEXT NOT NULL, observed_second INTEGER NOT NULL,
                observed_at TEXT NOT NULL, payload_json TEXT NOT NULL,
                PRIMARY KEY(source, observed_second)
            );
            CREATE TABLE IF NOT EXISTS btc_lead_calibrations (
                calibration_id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            """
        )
        self.connection.commit()
        self._mark_interrupted_attempts()

    @staticmethod
    def _json(value: BaseModel | dict[str, Any]) -> str:
        payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)

    def close(self) -> None:
        self.connection.close()

    def save_round(self, round_: LeadRound) -> LeadRound:
        round_.updated_at = utc_now()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO btc_lead_rounds(
                    round_id, market_id, market_slug, status, updated_at, payload_json
                ) VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(market_id) DO UPDATE SET
                    market_slug=excluded.market_slug, status=excluded.status,
                    updated_at=excluded.updated_at, payload_json=excluded.payload_json
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

    def round_for_market(self, market_id: str) -> LeadRound | None:
        row = self.connection.execute(
            "SELECT payload_json FROM btc_lead_rounds WHERE market_id=?", (market_id,)
        ).fetchone()
        return LeadRound.model_validate_json(row["payload_json"]) if row else None

    def round_for_slug(self, market_slug: str) -> LeadRound | None:
        row = self.connection.execute(
            "SELECT payload_json FROM btc_lead_rounds WHERE market_slug=?", (market_slug,)
        ).fetchone()
        return LeadRound.model_validate_json(row["payload_json"]) if row else None

    def unresolved_slugs(self) -> list[str]:
        rows = self.connection.execute(
            "SELECT market_slug FROM btc_lead_rounds WHERE status!='SETTLED' ORDER BY updated_at"
        ).fetchall()
        return [str(row["market_slug"]) for row in rows]

    def save_prediction(self, prediction: LeadPrediction) -> LeadPrediction:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO btc_lead_predictions(prediction_id, market_id, created_at, payload_json)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(prediction_id) DO UPDATE SET payload_json=excluded.payload_json
                """,
                (
                    prediction.prediction_id,
                    prediction.market_id,
                    prediction.created_at.isoformat(),
                    self._json(prediction),
                ),
            )
        return prediction

    def recent_predictions(self, limit: int = 500) -> list[LeadPrediction]:
        rows = self.connection.execute(
            "SELECT payload_json FROM btc_lead_predictions ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [LeadPrediction.model_validate_json(row["payload_json"]) for row in rows]

    def pending_predictions(self, limit: int = 120) -> list[LeadPrediction]:
        return [
            item
            for item in reversed(self.recent_predictions(limit))
            if len(item.matured_horizons) < len(item.projections)
        ]

    def save_shock(self, shock: LeadShock) -> LeadShock:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO btc_lead_shocks(shock_id, market_id, confirmed_at, status, payload_json)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(shock_id) DO UPDATE SET
                    status=excluded.status, payload_json=excluded.payload_json
                """,
                (
                    shock.shock_id,
                    shock.market_id,
                    shock.confirmed_at.isoformat(),
                    shock.status,
                    self._json(shock),
                ),
            )
        return shock

    def recent_shocks(self, limit: int = 100) -> list[LeadShock]:
        rows = self.connection.execute(
            "SELECT payload_json FROM btc_lead_shocks ORDER BY confirmed_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [LeadShock.model_validate_json(row["payload_json"]) for row in rows]

    def save_attempt(self, attempt: LeadAttempt) -> LeadAttempt:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            if attempt.attempt_number is None:
                row = self.connection.execute(
                    "SELECT COALESCE(MAX(attempt_number),0)+1 value FROM btc_lead_attempts"
                ).fetchone()
                attempt.attempt_number = int(row["value"])
            self.connection.execute(
                """
                INSERT INTO btc_lead_attempts(
                    attempt_id, attempt_number, idempotency_key, market_id,
                    lane, side, status, updated_at, payload_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(attempt_id) DO UPDATE SET status=excluded.status,
                    updated_at=excluded.updated_at, payload_json=excluded.payload_json
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

    def recent_attempts(self, limit: int = 200) -> list[LeadAttempt]:
        rows = self.connection.execute(
            "SELECT payload_json FROM btc_lead_attempts ORDER BY attempt_number DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [LeadAttempt.model_validate_json(row["payload_json"]) for row in rows]

    def save_position(self, position: LeadPosition) -> LeadPosition:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO btc_lead_positions(
                    position_id, market_id, lane, status, updated_at, payload_json
                ) VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(position_id) DO UPDATE SET status=excluded.status,
                    updated_at=excluded.updated_at, payload_json=excluded.payload_json
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

    def open_positions(self) -> list[LeadPosition]:
        rows = self.connection.execute(
            "SELECT payload_json FROM btc_lead_positions "
            "WHERE status IN ('OPEN','EXIT_PENDING','HOLD_TO_SETTLEMENT')"
        ).fetchall()
        return [LeadPosition.model_validate_json(row["payload_json"]) for row in rows]

    def recent_positions(self, limit: int = 200) -> list[LeadPosition]:
        rows = self.connection.execute(
            "SELECT payload_json FROM btc_lead_positions ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [LeadPosition.model_validate_json(row["payload_json"]) for row in rows]

    def save_source_health(self, source: str, observed_at: datetime, payload: dict[str, Any]) -> None:
        second = int(ensure_utc(observed_at).timestamp())
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO btc_lead_source_health(source, observed_second, observed_at, payload_json)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(source, observed_second) DO UPDATE SET
                    observed_at=excluded.observed_at, payload_json=excluded.payload_json
                """,
                (source, second, observed_at.isoformat(), self._json(payload)),
            )

    def save_calibration(self, payload: dict[str, Any]) -> None:
        created_at = str(payload.get("created_at") or utc_now().isoformat())
        calibration_id = str(payload.get("calibration_id") or uuid.uuid4().hex)
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO btc_lead_calibrations(calibration_id, created_at, payload_json) "
                "VALUES(?, ?, ?)",
                (calibration_id, created_at, self._json(payload)),
            )

    def _mark_interrupted_attempts(self) -> None:
        rows = self.connection.execute(
            "SELECT payload_json FROM btc_lead_attempts WHERE status='INTENT'"
        ).fetchall()
        for row in rows:
            attempt = LeadAttempt.model_validate_json(row["payload_json"])
            attempt.status = "UNMEASURABLE"
            attempt.reason = "process_restarted_before_shadow_result"
            attempt.resolved_at = utc_now()
            self.save_attempt(attempt)

    def summary(self) -> dict[str, Any]:
        attempts = self.recent_attempts(100000)
        positions = self.recent_positions(100000)
        lanes: dict[str, dict[str, Any]] = {}
        for lane in ("observed", "p95"):
            buys = [item for item in attempts if item.lane == lane and item.side == "BUY"]
            done = [
                item for item in positions
                if item.lane == lane and item.status in {"CLOSED", "SETTLED"}
                and item.realized_pnl is not None
            ]
            pnls = [float(item.realized_pnl or 0.0) for item in done]
            equity = peak = drawdown = 0.0
            for pnl in sorted(
                done, key=lambda item: ensure_utc(item.closed_at or item.opened_at)
            ):
                equity += float(pnl.realized_pnl or 0.0)
                peak = max(peak, equity)
                drawdown = max(drawdown, peak - equity)
            lanes[lane] = {
                "trials": len(buys),
                "fills": sum(item.status == "MATCHED" for item in buys),
                "completed": len(done),
                "wins": sum(value > 0 for value in pnls),
                "losses": sum(value < 0 for value in pnls),
                "realized_pnl": sum(pnls),
                "average_pnl": statistics.fmean(pnls) if pnls else 0.0,
                "max_drawdown": drawdown,
            }
        return {"mode": "SHADOW_ONLY", "lanes": lanes}


class BtcLeadPredictionEngine:
    def __init__(
        self,
        config: AppConfig,
        registry: BtcLeadPredictionRegistry,
        latency: LatencySnapshotSource,
    ):
        self.config = config
        self.registry = registry
        self.latency = latency
        self.market: MarketState | None = None
        self.current_round: LeadRound | None = None
        self.chainlink_tick: PriceTick | None = None
        self.chainlink_history: deque[tuple[datetime, float]] = deque()
        self.books: dict[Direction, OrderBookSnapshot] = {}
        self.book_history: dict[Direction, deque[tuple[datetime, float]]] = {
            Direction.UP: deque(),
            Direction.DOWN: deque(),
        }
        self.sources: dict[str, dict[str, Any]] = {}
        self.source_history: dict[str, deque[tuple[datetime, float, datetime]]] = {
            source: deque() for source in ("binance", "coinbase", "kraken")
        }
        self.external_history: deque[tuple[datetime, float]] = deque()
        self.exchange_weights: dict[str, float] = {}
        self.source_samples: deque[dict[str, Any]] = deque()
        self.projection_samples: deque[dict[str, Any]] = deque()
        self.response_samples: deque[dict[str, Any]] = deque()
        recent_predictions = registry.recent_predictions(5000)
        self.pending_predictions: dict[str, LeadPrediction] = {
            item.prediction_id: item
            for item in recent_predictions
            if len(item.matured_horizons) < len(item.projections)
        }
        self.positions: dict[str, LeadPosition] = {
            item.position_id: item for item in registry.open_positions()
        }
        for position in self.positions.values():
            if position.status == "EXIT_PENDING":
                position.status = "OPEN"
                position.exit_reason = None
                registry.save_position(position)
        self.pending_attempts: dict[str, LeadAttempt] = {}
        self.active_shock: LeadShock | None = None
        self.shock_confirmation: dict[str, Any] = {}
        self.latest_projection: dict[str, Any] = {}
        self.latest_book_projection: dict[str, Any] = {"UP": {}, "DOWN": {}}
        self.latest_sources: dict[str, dict[str, Any]] = {}
        self.last_health_persist_second: dict[str, int] = {}
        self.prediction_history: deque[dict[str, Any]] = deque(
            (item.model_dump(mode="json") for item in reversed(recent_predictions[:120])),
            maxlen=300,
        )
        self._restore_calibration_samples(recent_predictions)
        self.last_prediction_at: datetime | None = None
        self.last_weight_update_at: datetime | None = None
        self.cooldown_until: datetime | None = None
        self.risk_status: dict[str, Any] = {"paused": False, "reason": "risk_ready"}
        self.last_risk_update_at: datetime | None = None
        self.summary_cache: dict[str, Any] = {}
        self.recent_attempts_cache: list[LeadAttempt] | None = None
        self.recent_positions_cache: list[LeadPosition] | None = None
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.status = "disabled" if not config.btc_lead_prediction.enabled else "starting"
        self.last_reason = self.status

    def _restore_calibration_samples(self, predictions: list[LeadPrediction]) -> None:
        cutoff = utc_now() - timedelta(
            minutes=self.config.btc_lead_prediction.calibration_window_minutes
        )
        for prediction in reversed(predictions):
            for key, actual in prediction.actuals.items():
                if actual.get("status") != "matured":
                    continue
                horizon = int(key)
                matured_at = ensure_utc(prediction.created_at) + timedelta(seconds=horizon)
                if matured_at < cutoff:
                    continue
                row = prediction.projections.get(key) or {}
                actual_price = actual.get("chainlink_price")
                if actual_price is None:
                    continue
                raw_delta = float(row.get("raw_twap_delta") or 0.0)
                actual_delta = float(actual_price) - prediction.chainlink_price
                self.projection_samples.append(
                    {
                        "matured_at": matured_at,
                        "horizon": horizon,
                        "raw_delta": raw_delta,
                        "actual_delta": actual_delta,
                        "error_price": float(row.get("predicted_twap") or actual_price)
                        - float(actual_price),
                    }
                )
                actual_delta_bps = math.log(
                    float(actual_price) / prediction.chainlink_price
                ) * 10_000.0
                for source, source_price in prediction.source_prices.items():
                    predicted_delta = math.log(
                        float(source_price) / prediction.chainlink_price
                    ) * 10_000.0
                    self.source_samples.append(
                        {
                            "source": source,
                            "matured_at": matured_at,
                            "direction_correct": predicted_delta * actual_delta_bps > 0,
                            "error_bps": predicted_delta - actual_delta_bps,
                        }
                    )
                for direction in Direction:
                    change = actual.get(f"{direction.value.lower()}_bid_change_cents")
                    probability_change = row.get("up_probability_change_points")
                    if direction == Direction.DOWN and probability_change is not None:
                        probability_change = -float(probability_change)
                    if change is None or probability_change is None or abs(float(probability_change)) < 0.05:
                        continue
                    coefficient = float(change) / float(probability_change)
                    if coefficient > 0:
                        self.response_samples.append(
                            {
                                "matured_at": matured_at,
                                "horizon": horizon,
                                "direction": direction.value,
                                "probability_change_points": float(probability_change),
                                "book_change_cents": float(change),
                                "coefficient": coefficient,
                            }
                        )

    @staticmethod
    def _append_history(
        history: deque,
        row: tuple,
        retention_seconds: float,
    ) -> None:
        observed_at = ensure_utc(row[0])
        row = (observed_at, *row[1:])
        if history and observed_at < history[-1][0]:
            return
        if history and observed_at == history[-1][0]:
            history[-1] = row
        else:
            history.append(row)
        cutoff = observed_at - timedelta(seconds=retention_seconds)
        while history and history[0][0] < cutoff:
            history.popleft()

    @staticmethod
    def _sample(history: deque, target: datetime, tolerance: float = 0.75) -> tuple | None:
        if not history:
            return None
        target = ensure_utc(target)
        nearest = min(history, key=lambda row: abs((row[0] - target).total_seconds()))
        return nearest if abs((nearest[0] - target).total_seconds()) <= tolerance else None

    @staticmethod
    def _sample_at_or_before(
        history: deque, target: datetime, tolerance: float = 0.75
    ) -> tuple | None:
        """Return an as-of sample without leaking a newer observation backward."""
        if not history:
            return None
        target = ensure_utc(target)
        sample = next((row for row in reversed(history) if row[0] <= target), None)
        if sample is None:
            return None
        return sample if (target - sample[0]).total_seconds() <= tolerance else None

    @staticmethod
    def _normal_cdf(value: float) -> float:
        return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))

    def _volatility(self, now: datetime, window_seconds: float = 60.0) -> float:
        cutoff = now - timedelta(seconds=window_seconds)
        rows = [(observed, price) for observed, price in self.chainlink_history if observed >= cutoff]
        squared_returns = 0.0
        elapsed = 0.0
        for (left_at, left), (right_at, right) in zip(rows, rows[1:]):
            seconds = (right_at - left_at).total_seconds()
            if seconds <= 0 or left <= 0 or right <= 0:
                continue
            squared_returns += math.log(right / left) ** 2
            elapsed += seconds
        realized_per_second = math.sqrt(squared_returns / elapsed) if elapsed > 0 else 0.0
        return max(realized_per_second, 0.5 / 10_000.0)

    def _probability(self, price: float, remaining: float, direction: Direction) -> float | None:
        if self.market is None or not self.market.threshold_price or price <= 0:
            return None
        gap = math.log(price / self.market.threshold_price)
        z = gap / (self._volatility(utc_now()) * math.sqrt(max(remaining, 1.0)))
        up = self._normal_cdf(clamp(z, -8.0, 8.0))
        return up if direction == Direction.UP else 1.0 - up

    def set_market(self, market: MarketState, now: datetime | None = None) -> None:
        now = ensure_utc(now or utc_now())
        changed = self.market is None or self.market.condition_id != market.condition_id
        self.market = market
        if not changed:
            return
        for attempt in list(self.pending_attempts.values()):
            attempt.status = "UNMEASURABLE"
            attempt.reason = "market_changed_before_shadow_result"
            attempt.resolved_at = now
            self._save_attempt(attempt)
        self.pending_attempts.clear()
        for position in self.positions.values():
            if position.status in {"OPEN", "EXIT_PENDING"} and position.market_id != market.condition_id:
                position.status = "HOLD_TO_SETTLEMENT"
                position.exit_reason = "market_changed_before_exit"
                self.registry.save_position(position)
        restored = self.registry.round_for_market(market.condition_id)
        start = ensure_utc(market.start_time or (market.end_time - timedelta(seconds=300)))
        settings = self.config.btc_lead_prediction.model_copy(deep=True)
        self.current_round = restored or LeadRound(
            market_id=market.condition_id,
            market_slug=market.slug,
            start_time=start,
            end_time=ensure_utc(market.end_time),
            settings=settings,
            config_version=lead_config_version(settings),
        )
        self.registry.save_round(self.current_round)
        self.chainlink_history.clear()
        self.external_history.clear()
        for history in self.book_history.values():
            history.clear()
        self.active_shock = None
        self.shock_confirmation = {}
        self.latest_projection = {}
        self.latest_book_projection = {"UP": {}, "DOWN": {}}
        self.status = "waiting_for_sources" if settings.enabled else "disabled"
        self.last_reason = self.status
        self.events.append(("btc_lead_round", self.current_round.model_dump(mode="json")))

    def add_chainlink_tick(self, tick: PriceTick) -> None:
        observed_at = ensure_utc(tick.exchange_timestamp or tick.received_at)
        self.chainlink_tick = tick
        self._append_history(self.chainlink_history, (observed_at, tick.price), 3700.0)

    def add_book(self, direction: Direction, book: OrderBookSnapshot) -> None:
        if self.market is not None:
            expected = self.market.up_token_id if direction == Direction.UP else self.market.down_token_id
            if book.token_id != expected:
                return
        existing = self.books.get(direction)
        if existing and ensure_utc(book.timestamp) < ensure_utc(existing.timestamp):
            return
        self.books[direction] = book
        if book.best_bid is not None:
            self._append_history(
                self.book_history[direction],
                (ensure_utc(book.received_at), book.best_bid),
                3700.0,
            )

    def add_signal(self, event: SignalEvent) -> None:
        source = event.source
        if source == "binance_futures":
            self.sources[source] = {
                "source": source,
                "diagnostic_only": True,
                "kind": event.kind,
                "price": event.price,
                "exchange_timestamp": ensure_utc(event.exchange_timestamp).isoformat(),
                "received_at": ensure_utc(event.received_at).isoformat(),
                "valid": event.valid,
                "reason": event.reason,
            }
            return
        if source not in self.config.btc_lead_prediction.spot_sources:
            return
        exchange_at = ensure_utc(event.exchange_timestamp)
        received_at = ensure_utc(event.received_at)
        state = self.sources.setdefault(source, {"source": source})
        previous_at = state.get("exchange_at")
        if isinstance(previous_at, datetime) and exchange_at < previous_at:
            state.update(valid=False, healthy=False, reason="exchange_time_reversed")
            return
        state.update(
            exchange_at=exchange_at,
            received_at=received_at,
            kind=event.kind,
            valid=event.valid,
            reason=event.reason,
            latency_ms=max(0.0, (received_at - exchange_at).total_seconds() * 1000.0),
        )
        if event.kind == "health" and not event.valid:
            state.update(healthy=False, reason=event.reason or "source_unhealthy")
            return
        if event.kind == "trade":
            state.update(
                last_trade_price=event.price,
                last_trade_side=event.taker_side,
                last_trade_quantity=event.quantity,
            )
            return
        if event.kind != "book" or not event.bids or not event.asks:
            return
        best_bid = max(level.price for level in event.bids)
        best_ask = min(level.price for level in event.asks)
        if best_bid <= 0 or best_ask <= best_bid:
            state.update(valid=False, healthy=False, reason="invalid_spot_book")
            return
        mid = (best_bid + best_ask) / 2.0
        state.update(
            bid=best_bid,
            ask=best_ask,
            mid=mid,
            sequence=event.sequence,
            valid=True,
            reason=None,
        )
        # Source velocity is an arrival-time signal. Exchange clocks can lead or
        # lag the local clock slightly, which must not distort the lookback.
        self._append_history(
            self.source_history[source],
            (received_at, mid, exchange_at),
            3700.0,
        )

    def add_signals(self, events: list[SignalEvent]) -> None:
        for event in sorted(events, key=lambda item: ensure_utc(item.exchange_timestamp)):
            self.add_signal(event)

    def _basis(self, source: str, now: datetime) -> tuple[float, float]:
        settings = self.current_round.settings if self.current_round else self.config.btc_lead_prediction
        cutoff = now - timedelta(seconds=settings.source_basis_window_seconds)
        values: list[float] = []
        for observed_at, price, _ in self.source_history[source]:
            if observed_at < cutoff:
                continue
            chain = self._sample(self.chainlink_history, observed_at, 1.25)
            if chain and price > 0 and chain[1] > 0:
                values.append(math.log(price / chain[1]))
        if not values:
            return 0.0, 0.0
        median = statistics.median(values)
        mad = statistics.median(abs(value - median) for value in values)
        return median, mad

    def _source_snapshot(self, now: datetime) -> dict[str, dict[str, Any]]:
        settings = self.current_round.settings if self.current_round else self.config.btc_lead_prediction
        clock_skew_tolerance = 0.5
        result: dict[str, dict[str, Any]] = {}
        for source in settings.spot_sources:
            state = self.sources.get(source, {})
            exchange_at = state.get("exchange_at")
            received_at = state.get("received_at")
            mid = state.get("mid")
            basis, mad = self._basis(source, now)
            age = (now - received_at).total_seconds() if isinstance(received_at, datetime) else None
            exchange_age = (now - exchange_at).total_seconds() if isinstance(exchange_at, datetime) else None
            current_basis = (
                math.log(mid / self.chainlink_tick.price)
                if mid and self.chainlink_tick and self.chainlink_tick.price > 0
                else None
            )
            basis_limit = max(10.0 / 10_000.0, 6.0 * mad)
            anomalous = bool(
                current_basis is not None and abs(current_basis - basis) > basis_limit
                and len(self.source_history[source]) >= 10
            )
            healthy = bool(
                state.get("valid")
                and mid
                and age is not None
                and exchange_age is not None
                and -clock_skew_tolerance <= age <= settings.external_max_age_seconds
                and -clock_skew_tolerance <= exchange_age <= settings.external_max_age_seconds
                and not anomalous
            )
            reason = (
                "healthy" if healthy else "missing_book" if not mid
                else "source_time_ahead" if age is not None and age < -clock_skew_tolerance
                else "exchange_time_ahead" if exchange_age is not None and exchange_age < -clock_skew_tolerance
                else "source_stale" if age is None or age > settings.external_max_age_seconds
                else "exchange_time_stale" if exchange_age is None or exchange_age > settings.external_max_age_seconds
                else "abnormal_basis" if anomalous else str(state.get("reason") or "source_invalid")
            )
            velocity = self._source_velocity(source, now)
            row = {
                "source": source,
                "price": mid,
                "bid": state.get("bid"),
                "ask": state.get("ask"),
                "calibrated_price": mid / math.exp(basis) if mid else None,
                "basis_bps": basis * 10_000.0,
                "basis_mad_bps": mad * 10_000.0,
                "age_seconds": age,
                "exchange_age_seconds": exchange_age,
                "latency_ms": state.get("latency_ms"),
                "velocity_bps_per_second": velocity[0],
                "acceleration_bps_per_second2": velocity[1],
                "taker_side": state.get("last_trade_side"),
                "weight": self.exchange_weights.get(source, 0.0),
                "healthy": healthy,
                "reason": reason,
                "exchange_timestamp": exchange_at.isoformat() if isinstance(exchange_at, datetime) else None,
                "received_at": received_at.isoformat() if isinstance(received_at, datetime) else None,
            }
            result[source] = row
            second = int(now.timestamp())
            if self.last_health_persist_second.get(source) != second:
                self.registry.save_source_health(source, now, row)
                self.last_health_persist_second[source] = second
        return result

    def _source_velocity(self, source: str, now: datetime) -> tuple[float | None, float | None]:
        current = self._sample(self.source_history[source], now, 0.75)
        if current is None:
            return None, None
        half = self._sample_at_or_before(
            self.source_history[source], current[0] - timedelta(seconds=0.5), 0.75
        )
        one = self._sample_at_or_before(
            self.source_history[source], current[0] - timedelta(seconds=1.0), 0.75
        )
        if not current or not half or current[1] <= 0 or half[1] <= 0:
            return None, None
        recent = math.log(current[1] / half[1]) * 10_000.0 / max(
            (current[0] - half[0]).total_seconds(), 0.05
        )
        if not one or one[1] <= 0:
            return recent, None
        prior = math.log(half[1] / one[1]) * 10_000.0 / max(
            (half[0] - one[0]).total_seconds(), 0.05
        )
        return recent, (recent - prior) / 0.5

    def _update_weights(self, sources: dict[str, dict[str, Any]], now: datetime) -> None:
        settings = self.current_round.settings if self.current_round else self.config.btc_lead_prediction
        if self.last_weight_update_at and (
            now - self.last_weight_update_at
        ).total_seconds() < settings.calibration_update_seconds:
            return
        self.last_weight_update_at = now
        cutoff = now - timedelta(minutes=settings.calibration_window_minutes)
        while self.source_samples and self.source_samples[0]["matured_at"] < cutoff:
            self.source_samples.popleft()
        healthy = [name for name, row in sources.items() if row["healthy"]]
        raw: dict[str, float] = {}
        diagnostics: dict[str, Any] = {}
        for source in healthy:
            samples = [row for row in self.source_samples if row["source"] == source]
            latency = max(float(sources[source].get("latency_ms") or 0.0) / 1000.0, 0.0)
            if len(samples) < settings.calibration_min_samples:
                raw[source] = 1.0
                diagnostics[source] = {"samples": len(samples), "mode": "equal_weight_warmup"}
                continue
            accuracy = sum(bool(row["direction_correct"]) for row in samples) / len(samples)
            rmse = math.sqrt(statistics.fmean(float(row["error_bps"]) ** 2 for row in samples))
            raw[source] = max(accuracy, 0.05) / max(rmse, 0.1) / (1.0 + latency)
            diagnostics[source] = {
                "samples": len(samples),
                "direction_accuracy": accuracy,
                "rmse_bps": rmse,
                "latency_seconds": latency,
            }
        total = sum(raw.values())
        self.exchange_weights = {
            source: (raw.get(source, 0.0) / total * 100.0 if total > 0 else 0.0)
            for source in settings.spot_sources
        }
        payload = {
            "calibration_id": uuid.uuid4().hex,
            "created_at": now.isoformat(),
            "exchange_weights": self.exchange_weights,
            "sources": diagnostics,
        }
        self.registry.save_calibration(payload)

    def _external_index(
        self, sources: dict[str, dict[str, Any]], now: datetime
    ) -> tuple[float | None, float | None, list[str], datetime | None]:
        healthy = [name for name, row in sources.items() if row["healthy"]]
        values = [float(sources[name]["calibrated_price"]) for name in healthy]
        dispersion = (
            (max(values) - min(values)) / statistics.median(values) * 10_000.0
            if len(values) >= 2 and statistics.median(values) > 0
            else 0.0 if values else None
        )
        weighted = [
            (float(sources[name]["calibrated_price"]), self.exchange_weights.get(name, 0.0))
            for name in healthy
        ]
        index = weighted_median(weighted)
        index_at = max(
            (
                self.sources[name].get("received_at")
                for name in healthy
                if isinstance(self.sources[name].get("received_at"), datetime)
            ),
            default=None,
        )
        if index is not None:
            self._append_history(
                self.external_history,
                (now, index),
                3700.0,
            )
        return index, dispersion, healthy, index_at

    def _index_velocity(self, now: datetime) -> tuple[float | None, float | None]:
        current = self._sample(self.external_history, now, 0.75)
        if current is None:
            return None, None
        half = self._sample_at_or_before(
            self.external_history, current[0] - timedelta(seconds=0.5), 0.75
        )
        one = self._sample_at_or_before(
            self.external_history, current[0] - timedelta(seconds=1.0), 0.75
        )
        if not current or not half or current[1] <= 0 or half[1] <= 0:
            return None, None
        elapsed = max((current[0] - half[0]).total_seconds(), 0.05)
        velocity = math.log(current[1] / half[1]) * 10_000.0 / elapsed
        if not one or one[1] <= 0:
            return velocity, None
        prior_elapsed = max((half[0] - one[0]).total_seconds(), 0.05)
        prior_velocity = math.log(half[1] / one[1]) * 10_000.0 / prior_elapsed
        return velocity, (velocity - prior_velocity) / 0.5

    def _calibration_coefficient(self, horizon: int, now: datetime) -> tuple[float, int]:
        settings = self.current_round.settings if self.current_round else self.config.btc_lead_prediction
        cutoff = now - timedelta(minutes=settings.calibration_window_minutes)
        samples = [
            row for row in self.projection_samples
            if row["horizon"] == horizon and row["matured_at"] >= cutoff
            and abs(float(row["raw_delta"])) > 1e-12
        ]
        if len(samples) < settings.calibration_min_samples:
            return 1.0, len(samples)
        ratios = [float(row["actual_delta"]) / float(row["raw_delta"]) for row in samples]
        return clamp(statistics.median(ratios), 0.0, 2.0), len(samples)

    def _forecast_error(self, horizon: int, now: datetime, current_price: float) -> tuple[float, int]:
        settings = self.current_round.settings if self.current_round else self.config.btc_lead_prediction
        cutoff = now - timedelta(minutes=settings.calibration_window_minutes)
        errors = [
            float(row["error_price"]) for row in self.projection_samples
            if row["horizon"] == horizon and row["matured_at"] >= cutoff
        ]
        if len(errors) >= 10:
            return max(statistics.pstdev(errors) * 1.645, current_price * 0.1 / 10_000.0), len(errors)
        sigma = self._volatility(now)
        return max(current_price * sigma * math.sqrt(max(horizon, 1)) * 1.645, current_price * 0.1 / 10_000.0), len(errors)

    def _future_external_price(
        self, current: float, velocity_bps_per_second: float, seconds: float, half_life: float
    ) -> float:
        decay = math.log(2.0) / max(half_life, 0.05)
        integrated_log_return = (
            velocity_bps_per_second / 10_000.0
            * (1.0 - math.exp(-decay * seconds))
            / decay
        )
        return current * math.exp(integrated_log_return)

    def _projection(self, now: datetime, external_index: float) -> dict[str, Any]:
        if self.chainlink_tick is None:
            return {}
        settings = self.current_round.settings if self.current_round else self.config.btc_lead_prediction
        velocity, acceleration = self._index_velocity(now)
        if velocity is None:
            return {}
        chainlink = float(self.chainlink_tick.price)
        remaining = max(
            1.0,
            (ensure_utc(self.market.end_time) - now).total_seconds() if self.market else 300.0,
        )
        projections: dict[str, Any] = {}
        for horizon in settings.prediction_horizons_seconds:
            step = 0.25
            raw_integral = 0.0
            sample_count = max(1, int(round(horizon / step)))
            complete = True
            for index in range(1, sample_count + 1):
                seconds = min(index * step, float(horizon))
                incoming = self._future_external_price(
                    external_index,
                    velocity,
                    seconds,
                    settings.velocity_half_life_seconds,
                )
                outgoing = self._sample(
                    self.external_history,
                    now - timedelta(seconds=30.0 - seconds),
                    0.75,
                )
                if outgoing is None:
                    complete = False
                    break
                raw_integral += (incoming - outgoing[1]) * step
            raw_delta = raw_integral / 30.0 if complete else 0.0
            coefficient, calibration_samples = self._calibration_coefficient(horizon, now)
            predicted = chainlink + coefficient * raw_delta
            half_width, error_samples = self._forecast_error(horizon, now, chainlink)
            lower = predicted - half_width
            upper = predicted + half_width
            target = self.market.threshold_price if self.market else None
            up_probability = self._probability_at(predicted, remaining - horizon, Direction.UP, now)
            current_up_probability = self._probability_at(chainlink, remaining, Direction.UP, now)
            projections[str(horizon)] = {
                "horizon_seconds": horizon,
                "ready": complete,
                "replacement_pressure": raw_integral,
                "raw_twap_delta": raw_delta,
                "calibration_coefficient": coefficient,
                "calibration_samples": calibration_samples,
                "predicted_twap": predicted,
                "lower_90": lower,
                "upper_90": upper,
                "interval_error_samples": error_samples,
                "target_distance_bps": (
                    math.log(predicted / target) * 10_000.0 if target and target > 0 else None
                ),
                "up_probability": up_probability,
                "down_probability": 1.0 - up_probability if up_probability is not None else None,
                "up_probability_change_points": (
                    (up_probability - current_up_probability) * 100.0
                    if up_probability is not None and current_up_probability is not None else None
                ),
                "valid_until": (now + timedelta(seconds=horizon)).isoformat(),
            }
        crossing = self._crossing_time(projections, chainlink)
        return {
            "created_at": now.isoformat(),
            "external_index": external_index,
            "velocity_bps_per_second": velocity,
            "acceleration_bps_per_second2": acceleration,
            "crossing_time_seconds": crossing,
            "horizons": projections,
        }

    def _probability_at(
        self, price: float, remaining: float, direction: Direction, now: datetime
    ) -> float | None:
        if self.market is None or not self.market.threshold_price or price <= 0:
            return None
        gap = math.log(price / self.market.threshold_price)
        z = gap / (self._volatility(now) * math.sqrt(max(remaining, 1.0)))
        up = self._normal_cdf(clamp(z, -8.0, 8.0))
        return up if direction == Direction.UP else 1.0 - up

    def _crossing_time(self, projections: dict[str, Any], current: float) -> float | None:
        if self.market is None or not self.market.threshold_price:
            return None
        target = self.market.threshold_price
        previous_time, previous_price = 0.0, current
        for key in sorted(projections, key=int):
            row = projections[key]
            if not row.get("ready"):
                continue
            current_time = float(key)
            current_price = float(row["predicted_twap"])
            if (previous_price - target) * (current_price - target) <= 0 and current_price != previous_price:
                ratio = (target - previous_price) / (current_price - previous_price)
                return previous_time + clamp(ratio, 0.0, 1.0) * (current_time - previous_time)
            previous_time, previous_price = current_time, current_price
        return None

    def _make_prediction(
        self,
        now: datetime,
        sources: dict[str, dict[str, Any]],
        projection: dict[str, Any],
    ) -> None:
        if self.market is None or self.current_round is None or self.chainlink_tick is None:
            return
        if not projection.get("horizons"):
            return
        initial_books = {
            direction.value: {
                "bid": self.books.get(direction).best_bid if self.books.get(direction) else None,
                "ask": self.books.get(direction).best_ask if self.books.get(direction) else None,
            }
            for direction in Direction
        }
        prediction = LeadPrediction(
            market_id=self.market.condition_id,
            market_slug=self.market.slug,
            config_version=self.current_round.config_version,
            created_at=now,
            external_index=float(projection["external_index"]),
            chainlink_price=float(self.chainlink_tick.price),
            target_price=self.market.threshold_price,
            source_weights=dict(self.exchange_weights),
            source_prices={
                name: float(row["calibrated_price"])
                for name, row in sources.items() if row.get("healthy") and row.get("calibrated_price")
            },
            projections=dict(projection["horizons"]),
            initial_books=initial_books,
        )
        self.registry.save_prediction(prediction)
        self.pending_predictions[prediction.prediction_id] = prediction
        self.prediction_history.append(prediction.model_dump(mode="json"))
        self.last_prediction_at = now

    def _mature_predictions(self, now: datetime) -> None:
        settings = self.current_round.settings if self.current_round else self.config.btc_lead_prediction
        cutoff = now - timedelta(minutes=settings.calibration_window_minutes)
        while self.projection_samples and self.projection_samples[0]["matured_at"] < cutoff:
            self.projection_samples.popleft()
        while self.response_samples and self.response_samples[0]["matured_at"] < cutoff:
            self.response_samples.popleft()
        for prediction in list(self.pending_predictions.values()):
            changed = False
            for key, row in prediction.projections.items():
                horizon = int(key)
                if horizon in prediction.matured_horizons:
                    continue
                due = ensure_utc(prediction.created_at) + timedelta(seconds=horizon)
                if now < due:
                    continue
                actual = self._sample(self.chainlink_history, due, 0.75)
                if actual is None:
                    if (now - due).total_seconds() < 2.0:
                        continue
                    prediction.matured_horizons.append(horizon)
                    prediction.actuals[key] = {"status": "missing_chainlink_at_expiry"}
                    changed = True
                    continue
                actual_price = float(actual[1])
                predicted_price = float(row["predicted_twap"])
                raw_delta = float(row["raw_twap_delta"])
                actual_delta = actual_price - prediction.chainlink_price
                error = predicted_price - actual_price
                actual_row: dict[str, Any] = {
                    "status": "matured",
                    "observed_at": actual[0].isoformat(),
                    "chainlink_price": actual_price,
                    "error_price": error,
                    "error_bps": error / prediction.chainlink_price * 10_000.0,
                }
                for direction in Direction:
                    book = self._sample(self.book_history[direction], due, 0.75)
                    initial_bid = (prediction.initial_books.get(direction.value) or {}).get("bid")
                    if book and initial_bid is not None:
                        change_cents = (float(book[1]) - float(initial_bid)) * 100.0
                        actual_row[f"{direction.value.lower()}_bid"] = float(book[1])
                        actual_row[f"{direction.value.lower()}_bid_change_cents"] = change_cents
                        probability_change = row.get(
                            f"{direction.value.lower()}_probability_change_points"
                        )
                        if probability_change is None and direction == Direction.DOWN:
                            up_change = row.get("up_probability_change_points")
                            probability_change = -float(up_change) if up_change is not None else None
                        if probability_change is not None and abs(float(probability_change)) >= 0.05:
                            self.response_samples.append(
                                {
                                    "matured_at": now,
                                    "horizon": horizon,
                                    "direction": direction.value,
                                    "probability_change_points": float(probability_change),
                                    "book_change_cents": change_cents,
                                    "coefficient": change_cents / float(probability_change),
                                }
                            )
                self.projection_samples.append(
                    {
                        "matured_at": now,
                        "horizon": horizon,
                        "raw_delta": raw_delta,
                        "actual_delta": actual_delta,
                        "error_price": error,
                    }
                )
                for source, source_price in prediction.source_prices.items():
                    predicted_delta = math.log(source_price / prediction.chainlink_price) * 10_000.0
                    actual_delta_bps = math.log(actual_price / prediction.chainlink_price) * 10_000.0
                    self.source_samples.append(
                        {
                            "source": source,
                            "matured_at": now,
                            "direction_correct": predicted_delta * actual_delta_bps > 0,
                            "error_bps": predicted_delta - actual_delta_bps,
                        }
                    )
                prediction.matured_horizons.append(horizon)
                prediction.actuals[key] = actual_row
                changed = True
            if changed:
                self.registry.save_prediction(prediction)
                for index, item in enumerate(self.prediction_history):
                    if item.get("prediction_id") == prediction.prediction_id:
                        self.prediction_history[index] = prediction.model_dump(mode="json")
                        break
            if len(prediction.matured_horizons) >= len(prediction.projections):
                self.pending_predictions.pop(prediction.prediction_id, None)

    def _response_coefficient(self, direction: Direction, now: datetime) -> tuple[float | None, int]:
        settings = self.current_round.settings if self.current_round else self.config.btc_lead_prediction
        cutoff = now - timedelta(minutes=settings.calibration_window_minutes)
        samples = [
            row for row in self.response_samples
            if row["matured_at"] >= cutoff
            and row["horizon"] == settings.primary_horizon_seconds
            and row["direction"] == direction.value
            and float(row["coefficient"]) > 0
        ]
        if len(samples) < settings.calibration_min_samples:
            return None, len(samples)
        return clamp(statistics.median(float(row["coefficient"]) for row in samples), 0.0, 5.0), len(samples)

    def _shock_signal(
        self,
        sources: dict[str, dict[str, Any]],
        index: float | None,
        dispersion: float | None,
        now: datetime,
    ) -> dict[str, Any]:
        settings = self.current_round.settings if self.current_round else self.config.btc_lead_prediction
        current_index = self._sample(self.external_history, now, 0.75)
        prior_index = (
            self._sample_at_or_before(
                self.external_history,
                current_index[0] - timedelta(seconds=0.5),
                0.75,
            )
            if current_index is not None
            else None
        )
        if index is None or prior_index is None or prior_index[1] <= 0:
            return {"condition": False, "reason": "shock_history_warming"}
        elapsed = max(
            ((current_index[0] if current_index is not None else now) - prior_index[0]).total_seconds(),
            0.05,
        )
        change_bps = math.log(index / prior_index[1]) * 10_000.0 * min(1.0, 0.5 / elapsed)
        if abs(change_bps) < settings.shock_threshold_bps:
            return {
                "condition": False,
                "reason": "shock_below_threshold",
                "change_bps": change_bps,
            }
        direction = Direction.UP if change_bps > 0 else Direction.DOWN
        orientations: dict[str, int] = {}
        orientation_floor = min(0.01, max(settings.shock_threshold_bps, 1e-9))
        for source, row in sources.items():
            velocity = row.get("velocity_bps_per_second")
            if (
                row.get("healthy")
                and velocity is not None
                and abs(float(velocity)) > orientation_floor
            ):
                orientations[source] = 1 if float(velocity) > 0 else -1
        target_sign = 1 if direction == Direction.UP else -1
        supporting = [source for source, sign in orientations.items() if sign == target_sign]
        agreement = len(supporting) / len(orientations) if orientations else 0.0
        velocity, acceleration = self._index_velocity(now)
        condition = bool(
            len(supporting) >= settings.min_healthy_sources
            and agreement >= 0.67
            and dispersion is not None
            and dispersion <= settings.max_source_dispersion_bps
        )
        reason = (
            "shock_candidate" if condition
            else "insufficient_direction_sources" if len(supporting) < settings.min_healthy_sources
            else "source_agreement_below_67pct" if agreement < 0.67
            else "source_dispersion_too_wide"
        )
        return {
            "condition": condition,
            "reason": reason,
            "direction": direction,
            "change_bps": change_bps,
            "velocity_bps_per_second": velocity,
            "acceleration_bps_per_second2": acceleration,
            "supporting_sources": supporting,
            "agreement": agreement,
            "dispersion_bps": dispersion,
        }

    def _update_shock(self, signal: dict[str, Any], now: datetime, update_key: str) -> None:
        settings = self.current_round.settings if self.current_round else self.config.btc_lead_prediction
        if self.active_shock and now >= ensure_utc(self.active_shock.expires_at):
            self.active_shock.status = "expired"
            self.registry.save_shock(self.active_shock)
            self.active_shock = None
        if not signal.get("condition"):
            self.shock_confirmation = {}
            return
        direction = signal["direction"]
        state = self.shock_confirmation
        if state.get("direction") != direction or state.get("last_update_key") == update_key:
            if state.get("direction") != direction:
                state = {
                    "direction": direction,
                    "started_at": now,
                    "updates": 0,
                    "peak_bps": 0.0,
                }
                self.shock_confirmation = state
            elif state.get("last_update_key") == update_key:
                return
        state["updates"] = int(state.get("updates", 0)) + 1
        state["last_update_key"] = update_key
        state["last_seen_at"] = now
        if abs(float(signal["change_bps"])) >= abs(float(state.get("peak_bps", 0.0))):
            state["peak_bps"] = float(signal["change_bps"])
            state["peak_at"] = now
        elapsed = (now - ensure_utc(state["started_at"])).total_seconds()
        if elapsed < settings.confirmation_seconds or state["updates"] < settings.confirmation_updates:
            return
        if self.active_shock and self.active_shock.direction == direction:
            return
        if state.get("confirmed_shock_id"):
            return
        if self.market is None:
            return
        shock = LeadShock(
            market_id=self.market.condition_id,
            market_slug=self.market.slug,
            direction=direction,
            started_at=state["started_at"],
            confirmed_at=now,
            peak_at=state.get("peak_at", now),
            expires_at=now + timedelta(seconds=max(8.0, float(settings.primary_horizon_seconds))),
            amplitude_bps=abs(float(state["peak_bps"])),
            velocity_bps_per_second=float(signal.get("velocity_bps_per_second") or 0.0),
            acceleration_bps_per_second2=float(signal.get("acceleration_bps_per_second2") or 0.0),
            sources=list(signal.get("supporting_sources") or []),
            source_weights={source: self.exchange_weights.get(source, 0.0) for source in signal.get("supporting_sources") or []},
        )
        self.active_shock = shock
        state["confirmed_shock_id"] = shock.shock_id
        self.registry.save_shock(shock)
        self.events.append(("btc_lead_shock", shock.model_dump(mode="json")))

    @staticmethod
    def _buy_limit(book: OrderBookSnapshot, quote: float) -> float | None:
        remaining = quote
        worst: float | None = None
        for level in sorted(book.asks, key=lambda item: item.price):
            take = min(remaining, level.price * level.size)
            if take > 0:
                worst = level.price
                remaining -= take
            if remaining <= 1e-9:
                return worst
        return None

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

    def _risk(self, now: datetime) -> dict[str, Any]:
        if (
            self.last_risk_update_at is not None
            and (now - self.last_risk_update_at).total_seconds() < 1.0
        ):
            return dict(self.risk_status)
        self.last_risk_update_at = now
        settings = self.current_round.settings if self.current_round else self.config.btc_lead_prediction
        p95 = sorted(
            [
                item for item in self.registry.recent_positions(100000)
                if item.lane == "p95" and item.closed_at is not None and item.realized_pnl is not None
            ],
            key=lambda item: ensure_utc(item.closed_at),
        )
        streak = 0
        for item in reversed(p95):
            if float(item.realized_pnl or 0.0) >= 0:
                break
            streak += 1
        pause_until = None
        if streak >= settings.loss_streak_pause_count and p95:
            pause_until = ensure_utc(p95[-1].closed_at) + timedelta(minutes=settings.loss_streak_pause_minutes)
        china_tz = timezone(timedelta(hours=8))
        local_day = now.astimezone(china_tz).date()
        daily_pnl = sum(
            float(item.realized_pnl or 0.0)
            for item in p95
            if ensure_utc(item.closed_at).astimezone(china_tz).date() == local_day
        )
        streak_paused = pause_until is not None and now < pause_until
        daily_paused = daily_pnl <= -settings.daily_p95_loss_limit_usd
        paused = settings.risk_pause_enabled and (streak_paused or daily_paused)
        return {
            "enabled": settings.risk_pause_enabled,
            "paused": paused,
            "reason": "loss_streak_pause" if streak_paused else "daily_loss_limit" if daily_paused else "risk_ready",
            "loss_streak": streak,
            "daily_p95_pnl": daily_pnl,
            "daily_loss_limit_usd": settings.daily_p95_loss_limit_usd,
            "pause_until": pause_until.isoformat() if pause_until else None,
        }

    def _book_projection(
        self, direction: Direction, projection: dict[str, Any], now: datetime
    ) -> dict[str, Any]:
        settings = self.current_round.settings if self.current_round else self.config.btc_lead_prediction
        book = self.books.get(direction)
        primary = (projection.get("horizons") or {}).get(str(settings.primary_horizon_seconds), {})
        probability_change = primary.get(
            "up_probability_change_points" if direction == Direction.UP else "down_probability_change_points"
        )
        if probability_change is None and direction == Direction.DOWN:
            up_change = primary.get("up_probability_change_points")
            probability_change = -float(up_change) if up_change is not None else None
        coefficient, samples = self._response_coefficient(direction, now)
        expected = (
            max(0.0, float(probability_change)) * coefficient
            if probability_change is not None and coefficient is not None else None
        )
        current_bid = book.best_bid if book else None
        predicted_bid = (
            min(0.99, current_bid + expected / 100.0)
            if current_bid is not None and expected is not None else None
        )
        remaining = (
            (ensure_utc(self.market.end_time) - now).total_seconds()
            if self.market else 300.0
        )
        adverse_price = primary.get("lower_90" if direction == Direction.UP else "upper_90")
        current_probability = (
            self._probability_at(self.chainlink_tick.price, remaining, direction, now)
            if self.chainlink_tick else None
        )
        adverse_probability = (
            self._probability_at(
                float(adverse_price),
                remaining - settings.primary_horizon_seconds,
                direction,
                now,
            )
            if adverse_price is not None else None
        )
        p95_probability_change = (
            (adverse_probability - current_probability) * 100.0
            if adverse_probability is not None and current_probability is not None else None
        )
        p95_expected = (
            max(0.0, p95_probability_change) * coefficient
            if p95_probability_change is not None and coefficient is not None else None
        )
        p95_predicted_bid = (
            min(0.99, current_bid + p95_expected / 100.0)
            if current_bid is not None and p95_expected is not None else None
        )
        return {
            "response_coefficient": coefficient,
            "response_samples": samples,
            "probability_change_points": probability_change,
            "expected_reprice_cents": expected,
            "current_bid": current_bid,
            "predicted_bid": predicted_bid,
            "p95_probability_change_points": p95_probability_change,
            "p95_expected_reprice_cents": p95_expected,
            "p95_predicted_bid": p95_predicted_bid,
        }

    def _entry_candidate(self, direction: Direction, now: datetime) -> dict[str, Any]:
        assert self.current_round is not None
        settings = self.current_round.settings
        book = self.books.get(direction)
        remaining = (ensure_utc(self.current_round.end_time) - now).total_seconds()
        chain_age = (
            (now - ensure_utc(self.chainlink_tick.received_at)).total_seconds()
            if self.chainlink_tick else None
        )
        book_age = (now - ensure_utc(book.received_at)).total_seconds() if book else None
        spread_cents = (
            (book.best_ask - book.best_bid) * 100.0
            if book and book.best_ask is not None and book.best_bid is not None else None
        )
        execution = simulate_buy(
            book.model_copy(deep=True), settings.quote_amount_usd, self.config.strategy.taker_fee_rate
        ) if book else None
        exit_execution = (
            simulate_sell(
                book.model_copy(deep=True),
                execution.quantity,
                self.config.strategy.taker_fee_rate,
            )
            if book and execution and execution.complete else None
        )
        book_limit_price = self._buy_limit(book, settings.quote_amount_usd) if book else None
        limit_price = (
            min(0.99, book_limit_price + settings.buy_limit_buffer_cents / 100.0)
            if book_limit_price is not None else None
        )
        primary = (self.latest_projection.get("horizons") or {}).get(str(settings.primary_horizon_seconds), {})
        h3 = (self.latest_projection.get("horizons") or {}).get("3", {})
        h5 = (self.latest_projection.get("horizons") or {}).get("5", {})
        movement3 = float(h3.get("predicted_twap", 0.0)) - float(self.chainlink_tick.price if self.chainlink_tick else 0.0)
        movement5 = float(h5.get("predicted_twap", 0.0)) - float(self.chainlink_tick.price if self.chainlink_tick else 0.0)
        orientation = 1.0 if direction == Direction.UP else -1.0
        adverse = primary.get("lower_90" if direction == Direction.UP else "upper_90")
        adverse_move = (
            orientation * (float(adverse) - float(self.chainlink_tick.price))
            if adverse is not None and self.chainlink_tick else None
        )
        book_projection = self.latest_book_projection.get(direction.value, {})
        predicted_bid = book_projection.get("predicted_bid")
        p95_predicted_bid = book_projection.get("p95_predicted_bid")
        simulated_buy_fee_share = (
            execution.fee_usd / execution.quantity if execution and execution.quantity else 0.0
        )
        entry_cost_price = (
            max(float(execution.avg_price), float(limit_price))
            if execution and execution.complete and limit_price is not None else None
        )
        entry_cost_fee_share = (
            taker_fee_usd(1.0, entry_cost_price, self.config.strategy.taker_fee_rate)
            if entry_cost_price is not None else 0.0
        )
        expected_sell_fee_share = (
            taker_fee_usd(1.0, float(predicted_bid), self.config.strategy.taker_fee_rate)
            if predicted_bid is not None else 0.0
        )
        p95_sell_fee_share = (
            taker_fee_usd(1.0, float(p95_predicted_bid), self.config.strategy.taker_fee_rate)
            if p95_predicted_bid is not None else 0.0
        )
        p95_latency = self.latency.snapshot(now).get("p95_ms")
        book_velocity = self._book_velocity(direction, now)
        latency_penalty = (
            max(0.0, book_velocity) * float(p95_latency) / 1000.0
            if book_velocity is not None and p95_latency is not None else 0.0
        )
        expected_net_edge = (
            float(predicted_bid) - entry_cost_price - entry_cost_fee_share - expected_sell_fee_share
            - latency_penalty
            if predicted_bid is not None and entry_cost_price is not None else None
        )
        p95_net_edge = (
            float(p95_predicted_bid) - entry_cost_price - entry_cost_fee_share - p95_sell_fee_share
            - latency_penalty
            if p95_predicted_bid is not None and entry_cost_price is not None else None
        )
        horizon_direction = orientation * movement5 > 0
        adverse_interval_direction = adverse_move is not None and adverse_move > 0
        gates = {
            "strategy_enabled": settings.enabled,
            "target_verified": bool(self.market and self.market.threshold_verified and self.market.threshold_price),
            "remaining_time": remaining > settings.stop_entry_remaining_seconds,
            "market_trade_limit": self.current_round.trade_count < settings.max_trades_per_market,
            "no_open_position": not any(
                item.market_id == self.current_round.market_id
                and item.status in {"OPEN", "EXIT_PENDING", "HOLD_TO_SETTLEMENT"}
                for item in self.positions.values()
            ),
            "cooldown": self.cooldown_until is None or now >= self.cooldown_until,
            "risk": not self.risk_status.get("paused"),
            "chainlink_fresh": chain_age is not None and 0 <= chain_age <= settings.chainlink_max_age_seconds,
            "book_fresh": book_age is not None and 0 <= book_age <= settings.book_max_age_seconds,
            "book_trusted": bool(book and book.depth_trusted),
            "spread": spread_cents is not None and spread_cents <= settings.max_spread_cents,
            "buy_range": bool(
                execution and execution.best_price is not None
                and settings.min_buy_price_cents <= execution.best_price * 100.0 <= settings.max_buy_price_cents
            ),
            "buy_depth": bool(execution and execution.complete and limit_price is not None),
            "expected_exit_depth": bool(exit_execution and exit_execution.complete),
            # The primary-horizon expected-reprice gate below already enforces
            # direction. Requiring both h3 and h5 to agree made brief horizon
            # crossings suppress otherwise measurable shadow attempts.
            "horizon_direction": horizon_direction,
            "response_warmed": book_projection.get("response_coefficient") is not None,
            "expected_reprice": (
                book_projection.get("expected_reprice_cents") is not None
                and float(book_projection["expected_reprice_cents"]) >= settings.min_expected_reprice_cents
            ),
            "expected_net_edge": (
                expected_net_edge is not None
                and expected_net_edge * 100.0 >= settings.target_net_profit_cents
            ),
            "p95_net_edge": (
                p95_net_edge is not None
                and p95_net_edge * 100.0 >= settings.min_p95_net_edge_cents
            ),
        }
        # This engine is shadow-only. Keep the strict direction and adverse-P95
        # results visible for evaluation, but do not let them suppress measurable
        # shadow attempts. The expected-reprice gate still enforces a positive
        # modelled move, while freshness, spread, depth and risk remain blocking.
        diagnostic_gate_names = ("horizon_direction", "p95_net_edge")
        blocking_gates = {
            name: passed for name, passed in gates.items()
            if name not in diagnostic_gate_names
        }
        diagnostic_gates = {
            name: gates[name] for name in diagnostic_gate_names
        }
        failed = next((name for name, passed in blocking_gates.items() if not passed), None)
        return {
            "direction": direction.value,
            "eligible": failed is None,
            "failed_gate": failed,
            "gates": gates,
            "blocking_gates": blocking_gates,
            "diagnostic_gates": diagnostic_gates,
            "remaining_seconds": remaining,
            "chainlink_age_seconds": chain_age,
            "book_age_seconds": book_age,
            "spread_cents": spread_cents,
            "best_bid": book.best_bid if book else None,
            "best_ask": book.best_ask if book else None,
            "book_limit_price": book_limit_price,
            "buy_limit_buffer_cents": settings.buy_limit_buffer_cents,
            "limit_price": limit_price,
            "simulated_quantity": execution.quantity if execution else None,
            "simulated_exit_depth_quantity": (
                exit_execution.quantity if exit_execution else None
            ),
            "book_min_order_size": book.min_order_size if book else None,
            "below_book_min_order_size": bool(
                execution and book and execution.quantity < book.min_order_size
            ),
            "expected_reprice_cents": book_projection.get("expected_reprice_cents"),
            "predicted_bid": predicted_bid,
            "p95_predicted_bid": p95_predicted_bid,
            "buy_fee_per_share": simulated_buy_fee_share,
            "entry_cost_price": entry_cost_price,
            "entry_cost_fee_per_share": entry_cost_fee_share,
            "sell_fee_per_share": expected_sell_fee_share,
            "p95_sell_fee_per_share": p95_sell_fee_share,
            "slippage_cents": execution.slippage * 100.0 if execution else None,
            "expected_latency_penalty_cents": latency_penalty * 100.0,
            "p95_latency_penalty_cents": latency_penalty * 100.0,
            "expected_net_edge_cents": (
                expected_net_edge * 100.0 if expected_net_edge is not None else None
            ),
            "p95_net_edge_cents": p95_net_edge * 100.0 if p95_net_edge is not None else None,
            "adverse_interval_direction": adverse_interval_direction,
            "execution": execution,
        }

    def _book_velocity(self, direction: Direction, now: datetime) -> float | None:
        current = self._sample(self.book_history[direction], now, 0.75)
        prior = self._sample(self.book_history[direction], now - timedelta(seconds=0.5), 0.35)
        if not current or not prior:
            return None
        return (current[1] - prior[1]) / max((current[0] - prior[0]).total_seconds(), 0.05)

    def _save_attempt(self, attempt: LeadAttempt) -> None:
        self.registry.save_attempt(attempt)
        self.summary_cache = {}
        self.recent_attempts_cache = None
        self.events.append(("btc_lead_attempt", attempt.model_dump(mode="json")))

    def _create_buy_attempts(
        self, shock: LeadShock, candidate: dict[str, Any], now: datetime
    ) -> None:
        assert self.current_round is not None and self.market is not None
        settings = self.current_round.settings
        book = self.books[shock.direction]
        execution: ExecutionResult = candidate["execution"]
        latency = self.latency.snapshot(now)
        latest_delay = latency.get("latest_ms")
        p95_delay = latency.get("p95_ms")
        if (
            latest_delay is None
            or p95_delay is None
            or not latency.get("fresh")
            or not latency.get("warmed")
        ):
            self.status = "waiting_for_latency_probe"
            self.last_reason = (
                "latency_probe_stale"
                if int(latency.get("sample_count") or 0) > 0
                and latest_delay is not None
                and not latency.get("fresh")
                else "latency_probe_warming"
            )
            return
        self.current_round.trade_count += 1
        if shock.shock_id not in self.current_round.used_shock_ids:
            self.current_round.used_shock_ids.append(shock.shock_id)
        shock.submitted = True
        shock.status = "submitted"
        self.registry.save_round(self.current_round)
        self.registry.save_shock(shock)
        for lane in ("observed", "p95"):
            delay = latest_delay if lane == "observed" else p95_delay
            usable = latency.get("fresh") if lane == "observed" else latency.get("warmed")
            attempt = LeadAttempt(
                idempotency_key=(
                    f"btc_lead:{self.market.condition_id}:{shock.shock_id}:BUY:{lane}"
                ),
                market_id=self.market.condition_id,
                market_slug=self.market.slug,
                lane=lane,
                side="BUY",
                direction=shock.direction,
                token_id=book.token_id,
                shock_id=shock.shock_id,
                config_version=self.current_round.config_version,
                requested_quote=settings.quote_amount_usd,
                limit_price=float(candidate["limit_price"]),
                expected_avg_price=execution.avg_price,
                expected_quantity=execution.quantity,
                expected_quote=execution.quote,
                expected_fee_usd=execution.fee_usd,
                latency_ms=float(delay) if delay is not None else None,
                requested_at=now,
                requested_book_received_at=book.received_at,
                decision_details={
                    key: value for key, value in candidate.items() if key != "execution"
                } | {
                    "mode": "SHADOW_ONLY",
                    "shock": shock.model_dump(mode="json"),
                    "quote_amount_usd": settings.quote_amount_usd,
                    "min_order_size_is_diagnostic": True,
                },
            )
            if not usable or delay is None:
                attempt.status = "UNMEASURABLE"
                attempt.reason = "latency_probe_warming" if lane == "p95" else "latency_probe_stale"
                attempt.resolved_at = now
            else:
                attempt.reason = "shadow_latency_scheduled"
                attempt.due_at = now + timedelta(milliseconds=float(delay))
                self.pending_attempts[attempt.attempt_id] = attempt
            self._save_attempt(attempt)
        self.status = "entry_submitted"
        self.last_reason = "shock_entry_submitted"

    def _execution_after_limit(
        self, attempt: LeadAttempt, book: OrderBookSnapshot
    ) -> ExecutionResult:
        limited = book.model_copy(deep=True)
        if attempt.side == "BUY":
            limited.asks = [level for level in limited.asks if level.price <= attempt.limit_price + 1e-12]
            return simulate_buy(
                limited,
                float(attempt.requested_quote or 0.0),
                self.config.strategy.taker_fee_rate,
            )
        limited.bids = [level for level in limited.bids if level.price >= attempt.limit_price - 1e-12]
        return simulate_sell(
            limited,
            float(attempt.requested_quantity or 0.0),
            self.config.strategy.taker_fee_rate,
        )

    def _position_for_attempt(self, attempt: LeadAttempt) -> LeadPosition | None:
        if attempt.position_id:
            return self.positions.get(attempt.position_id)
        return next(
            (
                item for item in self.positions.values()
                if item.entry_attempt_id == attempt.attempt_id
            ),
            None,
        )

    def _resolve_pending(self, now: datetime) -> None:
        max_wait = self.config.orderbook_chase.latency_max_age_seconds
        for attempt in list(self.pending_attempts.values()):
            if attempt.due_at is None or now < ensure_utc(attempt.due_at):
                continue
            book = self.books.get(attempt.direction)
            newer = bool(
                book and book.token_id == attempt.token_id
                and (
                    attempt.requested_book_received_at is None
                    or ensure_utc(book.received_at) > ensure_utc(attempt.requested_book_received_at)
                )
            )
            if not newer or book is None:
                if (now - ensure_utc(attempt.due_at)).total_seconds() <= max_wait:
                    continue
                attempt.status = "UNMEASURABLE"
                attempt.reason = "no_post_intent_book_update"
                attempt.resolved_at = now
                self.pending_attempts.pop(attempt.attempt_id, None)
                position = self._position_for_attempt(attempt)
                if position and attempt.side == "SELL":
                    position.status = "OPEN"
                    self.registry.save_position(position)
                self._save_attempt(attempt)
                continue
            execution = self._execution_after_limit(attempt, book)
            exit_execution = (
                simulate_sell(
                    book.model_copy(deep=True),
                    execution.quantity,
                    self.config.strategy.taker_fee_rate,
                )
                if attempt.side == "BUY" and execution.complete else None
            )
            attempt.execution_book_received_at = book.received_at
            attempt.resolved_at = now
            self.pending_attempts.pop(attempt.attempt_id, None)
            if not execution.complete or (
                attempt.side == "BUY"
                and (exit_execution is None or not exit_execution.complete)
            ):
                attempt.status = "REJECTED"
                attempt.reason = (
                    "expected_exit_depth_unavailable_after_latency"
                    if execution.complete and attempt.side == "BUY"
                    else "fok_depth_or_limit_unavailable"
                )
                position = self._position_for_attempt(attempt)
                if position and attempt.side == "SELL":
                    position.status = "OPEN"
                    position.last_sell_book_received_at = book.received_at
                    self.registry.save_position(position)
                self._save_attempt(attempt)
                continue
            attempt.status = "MATCHED"
            attempt.reason = "shadow_fok_matched"
            attempt.filled_avg_price = execution.avg_price
            attempt.filled_quantity = execution.quantity
            attempt.filled_quote = execution.quote
            attempt.fee_usd = execution.fee_usd
            self._save_attempt(attempt)
            if attempt.side == "BUY":
                self._open_position(attempt, execution, now)
            else:
                self._close_position(attempt, execution, now)

    def _open_position(
        self, attempt: LeadAttempt, execution: ExecutionResult, now: datetime
    ) -> None:
        predicted_bid = float(attempt.decision_details.get("predicted_bid") or execution.avg_price)
        primary = self.current_round.settings.primary_horizon_seconds if self.current_round else 5
        position = LeadPosition(
            market_id=attempt.market_id,
            market_slug=attempt.market_slug,
            lane=attempt.lane,
            direction=attempt.direction,
            token_id=attempt.token_id,
            shock_id=str(attempt.shock_id or ""),
            config_version=attempt.config_version,
            entry_attempt_id=attempt.attempt_id,
            entry_price=execution.avg_price,
            quantity=execution.quantity,
            entry_quote=execution.quote,
            entry_fee_usd=execution.fee_usd,
            predicted_target_bid=predicted_bid,
            predicted_expiry=now + timedelta(seconds=primary),
            opened_at=now,
        )
        self.positions[position.position_id] = position
        self.registry.save_position(position)
        self.summary_cache = {}
        self.recent_positions_cache = None
        self.events.append(("btc_lead_position", position.model_dump(mode="json")))

    def _close_position(
        self, attempt: LeadAttempt, execution: ExecutionResult, now: datetime
    ) -> None:
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
        position.exit_reason = str(attempt.decision_details.get("exit_reason") or "lead_exit")
        position.closed_at = now
        self.registry.save_position(position)
        self.summary_cache = {}
        self.recent_positions_cache = None
        settings = self.current_round.settings if self.current_round else self.config.btc_lead_prediction
        self.cooldown_until = now + timedelta(seconds=settings.cooldown_seconds)
        self.events.append(("btc_lead_position", position.model_dump(mode="json")))

    def _exit_signal(self, position: LeadPosition, now: datetime) -> dict[str, Any]:
        settings = self.current_round.settings if self.current_round else self.config.btc_lead_prediction
        book = self.books.get(position.direction)
        bid = book.best_bid if book else None
        held = (now - ensure_utc(position.opened_at)).total_seconds()
        remaining = (
            (ensure_utc(self.market.end_time) - now).total_seconds() if self.market else 300.0
        )
        fee = (
            taker_fee_usd(position.quantity, bid, self.config.strategy.taker_fee_rate)
            if bid is not None else 0.0
        )
        pnl_per_share = (
            (position.quantity * bid - fee - position.entry_quote - position.entry_fee_usd)
            / position.quantity
            if bid is not None and position.quantity > 0 else None
        )
        primary_key = str(settings.primary_horizon_seconds)
        primary = (self.latest_projection.get("horizons") or {}).get(primary_key, {})
        predicted = primary.get("predicted_twap")
        orientation = 1.0 if position.direction == Direction.UP else -1.0
        chainlink = self.chainlink_tick.price if self.chainlink_tick else None
        lead_reversed = bool(
            predicted is not None and chainlink is not None
            and orientation * (float(predicted) - float(chainlink)) <= 0
        )
        projected_bid = (self.latest_book_projection.get(position.direction.value) or {}).get("p95_predicted_bid")
        p95_edge = (
            float(projected_bid) - position.entry_price
            - position.entry_fee_usd / position.quantity
            - taker_fee_usd(1.0, float(projected_bid), self.config.strategy.taker_fee_rate)
            if projected_bid is not None and position.quantity > 0 else None
        )
        checks = [
            ("prediction_target", bid is not None and bid >= position.predicted_target_bid),
            ("net_profit_target", pnl_per_share is not None and pnl_per_share * 100.0 >= settings.target_net_profit_cents),
            ("external_or_twap_reversed", lead_reversed),
            ("adverse_move", bid is not None and (bid - position.entry_price) * 100.0 <= -settings.adverse_move_cents),
            ("max_hold", held >= settings.max_hold_seconds),
            ("force_exit_remaining", remaining <= settings.force_exit_remaining_seconds),
        ]
        reason = next((name for name, condition in checks if condition), None)
        return {
            "condition": reason is not None,
            "reason": reason,
            "held_seconds": held,
            "remaining_seconds": remaining,
            "current_bid": bid,
            "target_bid": position.predicted_target_bid,
            "net_pnl_per_share_cents": pnl_per_share * 100.0 if pnl_per_share is not None else None,
            "p95_projected_edge_cents": p95_edge * 100.0 if p95_edge is not None else None,
            "lead_reversed": lead_reversed,
        }

    def _create_sell_attempt(
        self,
        position: LeadPosition,
        signal: dict[str, Any],
        now: datetime,
    ) -> bool:
        book = self.books.get(position.direction)
        if book is None:
            return False
        execution = simulate_sell(
            book.model_copy(deep=True), position.quantity, self.config.strategy.taker_fee_rate
        )
        limit_price = self._sell_limit(book, position.quantity)
        if not execution.complete or limit_price is None:
            position.exit_started_at = position.exit_started_at or now
            position.last_sell_book_received_at = book.received_at
            settings = self.current_round.settings if self.current_round else self.config.btc_lead_prediction
            if (now - ensure_utc(position.exit_started_at)).total_seconds() >= settings.exit_liquidity_retry_seconds:
                position.liquidity_failure = True
                position.exit_reason = "exit_liquidity_failure_retrying"
            self.registry.save_position(position)
            self.last_reason = "exit_depth_unavailable"
            return False
        latency = self.latency.snapshot(now)
        delay = latency.get("latest_ms") if position.lane == "observed" else latency.get("p95_ms")
        usable = latency.get("fresh") if position.lane == "observed" else latency.get("warmed")
        position.exit_attempts += 1
        attempt = LeadAttempt(
            idempotency_key=(
                f"btc_lead:{position.market_id}:{position.position_id}:SELL:"
                f"{position.lane}:{position.exit_attempts}"
            ),
            market_id=position.market_id,
            market_slug=position.market_slug,
            lane=position.lane,
            side="SELL",
            direction=position.direction,
            token_id=position.token_id,
            position_id=position.position_id,
            shock_id=position.shock_id,
            config_version=position.config_version,
            requested_quantity=position.quantity,
            limit_price=limit_price,
            expected_avg_price=execution.avg_price,
            expected_quantity=execution.quantity,
            expected_quote=execution.quote,
            expected_fee_usd=execution.fee_usd,
            latency_ms=float(delay) if delay is not None else None,
            requested_at=now,
            requested_book_received_at=book.received_at,
            decision_details={**signal, "exit_reason": signal["reason"]},
        )
        position.last_sell_book_received_at = book.received_at
        position.exit_started_at = position.exit_started_at or now
        if not usable or delay is None:
            attempt.status = "UNMEASURABLE"
            attempt.reason = "latency_probe_warming" if position.lane == "p95" else "latency_probe_stale"
            attempt.resolved_at = now
            position.status = "OPEN"
        else:
            attempt.reason = "shadow_latency_scheduled"
            attempt.due_at = now + timedelta(milliseconds=float(delay))
            self.pending_attempts[attempt.attempt_id] = attempt
            position.status = "EXIT_PENDING"
        self.registry.save_position(position)
        self._save_attempt(attempt)
        return usable and delay is not None

    def _evaluate_positions(self, now: datetime) -> None:
        if self.market is None:
            return
        for position in list(self.positions.values()):
            if position.market_id != self.market.condition_id or position.status != "OPEN":
                continue
            if now >= ensure_utc(self.market.end_time):
                position.status = "HOLD_TO_SETTLEMENT"
                position.exit_reason = position.exit_reason or "market_ended"
                self.registry.save_position(position)
                continue
            signal = self._exit_signal(position, now)
            if not signal["condition"]:
                continue
            book = self.books.get(position.direction)
            fresh = bool(
                book and (
                    position.last_sell_book_received_at is None
                    or ensure_utc(book.received_at) > ensure_utc(position.last_sell_book_received_at)
                )
            )
            if fresh:
                self._create_sell_attempt(position, signal, now)

    def evaluate(self, now: datetime | None = None, update_key: str | None = None) -> None:
        now = ensure_utc(now or utc_now())
        self._resolve_pending(now)
        self._mature_predictions(now)
        if self.market is None or self.current_round is None:
            self.status = self.last_reason = "waiting_for_market"
            return
        settings = self.current_round.settings
        if not settings.enabled:
            self.status = self.last_reason = "disabled"
            return
        if now >= ensure_utc(self.current_round.end_time):
            self._evaluate_positions(now)
            self.status = self.last_reason = "waiting_for_official_settlement"
            return
        sources = self._source_snapshot(now)
        self.latest_sources = sources
        self._update_weights(sources, now)
        for name, row in sources.items():
            row["weight"] = self.exchange_weights.get(name, 0.0)
        index, dispersion, healthy, index_at = self._external_index(sources, now)
        self.latest_projection = self._projection(now, index) if index is not None else {}
        if self.latest_projection.get("horizons") and (
            self.last_prediction_at is None
            or (now - self.last_prediction_at).total_seconds() >= 1.0
        ):
            self._make_prediction(now, sources, self.latest_projection)
        for direction in Direction:
            self.latest_book_projection[direction.value] = self._book_projection(
                direction, self.latest_projection, now
            )
        signal = self._shock_signal(sources, index, dispersion, now)
        self._update_shock(
            signal,
            now,
            f"external:{ensure_utc(index_at).isoformat()}" if index_at else "external:missing",
        )
        self.risk_status = self._risk(now)
        self._evaluate_positions(now)
        if len(healthy) < settings.min_healthy_sources:
            self.status = "waiting_for_sources"
            self.last_reason = "insufficient_healthy_sources"
            return
        if dispersion is not None and dispersion > settings.max_source_dispersion_bps:
            self.status = "waiting_for_sources"
            self.last_reason = "source_dispersion_too_wide"
            return
        response_count = min(
            self._response_coefficient(Direction.UP, now)[1],
            self._response_coefficient(Direction.DOWN, now)[1],
        )
        if response_count < settings.calibration_min_samples:
            self.status = "prediction_warmup"
            self.last_reason = (
                f"book_response_samples_{response_count}_of_{settings.calibration_min_samples}"
            )
            return
        if any(
            item.market_id == self.current_round.market_id and item.status in {"OPEN", "EXIT_PENDING"}
            for item in self.positions.values()
        ):
            self.status = "holding"
            self.last_reason = "position_open"
            return
        if self.cooldown_until and now < self.cooldown_until:
            self.status = "cooldown"
            self.last_reason = "post_exit_cooldown"
            return
        if self.risk_status.get("paused"):
            self.status = "risk_paused"
            self.last_reason = str(self.risk_status.get("reason"))
            return
        if self.active_shock is None:
            self.status = "waiting_for_shock"
            self.last_reason = str(signal.get("reason") or "waiting_for_shock")
            return
        if self.active_shock.submitted or self.active_shock.shock_id in self.current_round.used_shock_ids:
            self.status = "waiting_for_shock"
            self.last_reason = "shock_already_used"
            return
        candidate = self._entry_candidate(self.active_shock.direction, now)
        self.latest_book_projection[self.active_shock.direction.value]["candidate"] = {
            key: value for key, value in candidate.items() if key != "execution"
        }
        if not candidate["eligible"]:
            self.status = "waiting_for_lagging_book"
            self.last_reason = str(candidate["failed_gate"])
            return
        self._create_buy_attempts(self.active_shock, candidate, now)

    def settle(
        self, market_slug: str, outcome: Direction, now: datetime | None = None
    ) -> bool:
        now = ensure_utc(now or utc_now())
        round_ = self.registry.round_for_slug(market_slug)
        if round_ is None or round_.status == "SETTLED":
            return False
        for attempt in list(self.pending_attempts.values()):
            if attempt.market_id != round_.market_id:
                continue
            attempt.status = "UNMEASURABLE"
            attempt.reason = "official_settlement_before_shadow_result"
            attempt.resolved_at = now
            self.pending_attempts.pop(attempt.attempt_id, None)
            self._save_attempt(attempt)
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
            self.summary_cache = {}
            self.recent_positions_cache = None
            self.events.append(("btc_lead_position", position.model_dump(mode="json")))
        if self.current_round and self.current_round.market_slug == market_slug:
            self.current_round = round_
            self.status = self.last_reason = "official_settlement"
        self.events.append(("btc_lead_settlement", round_.model_dump(mode="json")))
        return True

    def unresolved_slugs(self) -> list[str]:
        return self.registry.unresolved_slugs()

    @staticmethod
    def _serialize_source(row: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value.isoformat() if isinstance(value, datetime) else value
            for key, value in row.items()
        }

    def dashboard_state(self) -> dict[str, Any]:
        now = utc_now()
        source_state = {
            name: dict(row) for name, row in self.latest_sources.items()
        }
        for name, row in source_state.items():
            row["weight"] = self.exchange_weights.get(name, 0.0)
        round_ = self.current_round
        remaining = (
            max(0.0, (ensure_utc(round_.end_time) - now).total_seconds()) if round_ else None
        )
        response = {
            direction.value: {
                "coefficient": self._response_coefficient(direction, now)[0],
                "samples": self._response_coefficient(direction, now)[1],
            }
            for direction in Direction
        }
        if not self.summary_cache:
            self.summary_cache = self.registry.summary()
        summary = json.loads(json.dumps(self.summary_cache))
        primary = str(
            round_.settings.primary_horizon_seconds
            if round_ else self.config.btc_lead_prediction.primary_horizon_seconds
        )
        errors = [
            float(item["error_price"]) / float(self.chainlink_tick.price) * 10_000.0
            for item in self.projection_samples
            if str(item["horizon"]) == primary and self.chainlink_tick
        ]
        summary["prediction"] = {
            "matured_samples": len(errors),
            "primary_rmse_bps": (
                math.sqrt(statistics.fmean(value * value for value in errors)) if errors else None
            ),
            "response": response,
        }
        return {
            "mode": "SHADOW_ONLY",
            "enabled": self.config.btc_lead_prediction.enabled,
            "status": self.status,
            "last_reason": self.last_reason,
            "config": self.config.btc_lead_prediction.model_dump(mode="json"),
            "round": round_.model_dump(mode="json") if round_ else None,
            "remaining_seconds": remaining,
            "sources": {name: self._serialize_source(row) for name, row in source_state.items()},
            "exchange_weights": dict(self.exchange_weights),
            "external_index": self.latest_projection.get("external_index"),
            "twap_projection": self.latest_projection,
            "book_projection": self.latest_book_projection,
            "active_shock": self.active_shock.model_dump(mode="json") if self.active_shock else None,
            "confirmation": self._serialize_source(self.shock_confirmation),
            "positions": [
                item.model_dump(mode="json") for item in self.positions.values()
                if item.status not in {"CLOSED", "SETTLED"}
            ],
            "recent_attempts": [
                item.model_dump(mode="json") for item in self._recent_attempts()
            ],
            "recent_positions": [
                item.model_dump(mode="json") for item in self._recent_positions()
            ],
            "prediction_history": list(self.prediction_history)[-120:],
            "summary": summary,
            "risk_status": dict(self.risk_status),
            "cooldown_until": self.cooldown_until.isoformat() if self.cooldown_until else None,
        }

    def _recent_attempts(self) -> list[LeadAttempt]:
        if self.recent_attempts_cache is None:
            self.recent_attempts_cache = self.registry.recent_attempts(100)
        return self.recent_attempts_cache

    def _recent_positions(self) -> list[LeadPosition]:
        if self.recent_positions_cache is None:
            self.recent_positions_cache = self.registry.recent_positions(100)
        return self.recent_positions_cache

    def drain_events(self) -> list[tuple[str, dict[str, Any]]]:
        events, self.events = self.events, []
        return events
