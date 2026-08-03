from __future__ import annotations

import json
import math
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .btc_recovery import simulate_buy_quantity_limit
from .config import AppConfig, BtcDynamicConfig
from .models import Direction, MarketState, OrderBookSnapshot, PriceTick
from .orderbook import simulate_buy


FEATURE_NAMES = (
    "remaining_time",
    "volatility_ratio",
    "open_crossings",
    "up_market_gap",
    "up_depth_imbalance",
    "up_spread",
    "down_depth_imbalance",
    "down_spread",
    "binance_momentum_1s",
    "binance_momentum_3s",
    "binance_momentum_5s",
    "momentum_gap_1s",
    "momentum_gap_3s",
    "momentum_gap_5s",
    "binance_missing",
)
LOSS_COOLDOWN_CONTROL = "online_loss_cooldown_state"
DEFAULT_MODEL_KEY = "online"
ACTIVE_MODEL_CONTROL = "active_model_key"
PENDING_MODEL_CONTROL = "pending_model_key"
PENDING_MODEL_NOT_BEFORE_CONTROL = "pending_model_not_before"


def local_calendar_date(value: datetime) -> str:
    """Return the calendar date shown by the computer's local clock."""
    return value.astimezone().date().isoformat()


def training_snapshot_seconds(settings: BtcDynamicConfig) -> tuple[float, ...]:
    interval = (
        settings.exit_seconds_after_open - settings.entry_seconds_after_open
    ) / 4.0
    return tuple(
        round(settings.entry_seconds_after_open + interval * index, 6)
        for index in range(4)
    )


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def sigmoid(value: float) -> float:
    if value >= 0:
        inverse = math.exp(-min(value, 60.0))
        return 1.0 / (1.0 + inverse)
    exponent = math.exp(max(value, -60.0))
    return exponent / (1.0 + exponent)


def logit(probability: float) -> float:
    probability = clamp(probability, 1e-9, 1.0 - 1e-9)
    return math.log(probability / (1.0 - probability))


def normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def dynamic_max_price(
    probability: float,
    fee_rate: float,
    slippage_reserve: float,
    min_edge: float,
    tick_size: float,
) -> float | None:
    def margin(price: float) -> float:
        return (
            probability
            - price
            - fee_rate * price * (1.0 - price)
            - slippage_reserve
            - min_edge
        )

    if margin(0.0) < 0:
        return None
    low, high = 0.0, 0.999999
    if margin(high) >= 0:
        raw = high
    else:
        while high - low > 1e-6:
            middle = (low + high) / 2.0
            if margin(middle) >= 0:
                low = middle
            else:
                high = middle
        raw = low
    tick = tick_size if tick_size > 0 else 0.01
    floored = math.floor((raw + 1e-12) / tick) * tick
    return clamp(round(floored, 8), 0.0, 0.99)


class DynamicModel(BaseModel):
    model_key: str = DEFAULT_MODEL_KEY
    frozen: bool = False
    source_model_key: str | None = None
    source_version: int | None = None
    source_as_of: datetime | None = None
    bias: float = 0.0
    weights: dict[str, float] = Field(
        default_factory=lambda: {name: 0.0 for name in FEATURE_NAMES}
    )
    trained_markets: int = 0
    version: int = 1
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def probability(
        self,
        formula_probability: float,
        features: dict[str, float],
        maximum_correction: float,
    ) -> float:
        score = logit(formula_probability) + self.bias
        score += sum(
            self.weights.get(name, 0.0) * clamp(features.get(name, 0.0), -1.0, 1.0)
            for name in FEATURE_NAMES
        )
        raw = sigmoid(score)
        corrected = clamp(
            raw,
            formula_probability - maximum_correction,
            formula_probability + maximum_correction,
        )
        return clamp(corrected, 0.01, 0.99)


class DynamicOrder(BaseModel):
    order_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    order_number: int | None = None
    market_id: str
    market_slug: str
    variant: str
    model_key: str = DEFAULT_MODEL_KEY
    direction: Direction
    model_probability: float
    formula_probability: float
    sizing_mode: str = "quantity"
    requested_quantity: float | None = None
    requested_quote_usd: float | None = None
    max_price: float
    avg_price: float
    quantity: float
    quote: float
    fee_usd: float
    net_edge_per_share: float
    created_at: datetime
    official_outcome: Direction | None = None
    payout_usd: float = 0.0
    realized_pnl: float | None = None
    settled_at: datetime | None = None


class DynamicRound(BaseModel):
    round_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    market_id: str
    market_slug: str
    model_key: str = DEFAULT_MODEL_KEY
    start_time: datetime
    end_time: datetime
    settings: BtcDynamicConfig
    created_at: datetime
    updated_at: datetime
    online_order: DynamicOrder | None = None
    formula_order: DynamicOrder | None = None
    official_outcome: Direction | None = None
    trained: bool = False
    closed_at: datetime | None = None


class DynamicSnapshot(BaseModel):
    market_id: str
    model_key: str = DEFAULT_MODEL_KEY
    snapshot_second: float
    formula_probability: float
    online_probability: float
    features: dict[str, float]
    created_at: datetime


class BtcDynamicRegistry:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS btc_dynamic_rounds (
                round_id TEXT PRIMARY KEY,
                market_id TEXT NOT NULL UNIQUE,
                market_slug TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS btc_dynamic_pending_idx
                ON btc_dynamic_rounds(end_time);
            CREATE TABLE IF NOT EXISTS btc_dynamic_snapshots (
                market_id TEXT NOT NULL,
                snapshot_second INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY(market_id, snapshot_second)
            );
            CREATE TABLE IF NOT EXISTS btc_dynamic_ticks (
                market_id TEXT NOT NULL,
                source TEXT NOT NULL,
                second INTEGER NOT NULL,
                price REAL NOT NULL,
                received_at TEXT NOT NULL,
                PRIMARY KEY(market_id, source, second)
            );
            CREATE INDEX IF NOT EXISTS btc_dynamic_ticks_age_idx
                ON btc_dynamic_ticks(received_at);
            CREATE TABLE IF NOT EXISTS btc_dynamic_orders (
                order_id TEXT PRIMARY KEY,
                order_number INTEGER NOT NULL UNIQUE,
                market_id TEXT NOT NULL,
                variant TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                UNIQUE(market_id, variant)
            );
            CREATE TABLE IF NOT EXISTS btc_dynamic_model (
                key TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS btc_dynamic_controls (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    @staticmethod
    def _payload(model: BaseModel) -> str:
        return json.dumps(model.model_dump(mode="json"), ensure_ascii=False)

    def load_model(self, model_key: str = DEFAULT_MODEL_KEY) -> DynamicModel:
        row = self.connection.execute(
            "SELECT payload_json FROM btc_dynamic_model WHERE key = ?",
            (model_key,),
        ).fetchone()
        if row:
            model = DynamicModel.model_validate_json(row["payload_json"])
            model.model_key = model_key
            return model
        return DynamicModel(model_key=model_key)

    def save_model(
        self, model: DynamicModel, model_key: str | None = None
    ) -> None:
        key = model_key or model.model_key
        model.model_key = key
        self.connection.execute(
            """
            INSERT INTO btc_dynamic_model(key, payload_json, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (key, self._payload(model), model.updated_at.isoformat()),
        )
        self.connection.commit()

    def model_exists(self, model_key: str) -> bool:
        return bool(
            self.connection.execute(
                "SELECT 1 FROM btc_dynamic_model WHERE key = ?", (model_key,)
            ).fetchone()
        )

    def control(self, key: str, default: str | None = None) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM btc_dynamic_controls WHERE key = ?", (key,)
        ).fetchone()
        return str(row["value"]) if row else default

    def set_control(self, key: str, value: str, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        self.connection.execute(
            """
            INSERT INTO btc_dynamic_controls(key, value, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, value, now.isoformat()),
        )
        self.connection.commit()

    def delete_control(self, key: str) -> None:
        self.connection.execute(
            "DELETE FROM btc_dynamic_controls WHERE key = ?", (key,)
        )
        self.connection.commit()

    def active_model_key(self) -> str:
        key = self.control(ACTIVE_MODEL_CONTROL, DEFAULT_MODEL_KEY)
        return key if key and self.model_exists(key) else DEFAULT_MODEL_KEY

    def pending_model_key(self) -> str | None:
        key = self.control(PENDING_MODEL_CONTROL)
        return key if key and self.model_exists(key) else None

    def schedule_model_activation(
        self, model_key: str, not_before: datetime
    ) -> None:
        if not self.model_exists(model_key):
            raise ValueError(f"dynamic model does not exist: {model_key}")
        self.set_control(PENDING_MODEL_CONTROL, model_key, not_before)
        self.set_control(
            PENDING_MODEL_NOT_BEFORE_CONTROL,
            not_before.isoformat(),
            not_before,
        )

    def model_key_for_new_market(self, market_start: datetime) -> str:
        active = self.active_model_key()
        pending = self.pending_model_key()
        raw_not_before = self.control(PENDING_MODEL_NOT_BEFORE_CONTROL)
        if pending is None or raw_not_before is None:
            return active
        try:
            not_before = datetime.fromisoformat(raw_not_before)
            if not_before.tzinfo is None:
                not_before = not_before.replace(tzinfo=timezone.utc)
        except ValueError:
            return active
        if market_start <= not_before:
            return active
        self.set_control(ACTIVE_MODEL_CONTROL, pending, market_start)
        self.delete_control(PENDING_MODEL_CONTROL)
        self.delete_control(PENDING_MODEL_NOT_BEFORE_CONTROL)
        return pending

    def reset_statistics(self, now: datetime) -> None:
        self.set_control("statistics_reset_at", now.isoformat(), now)

    def request_model_reset(self, now: datetime) -> bool:
        model_key = self.active_model_key()
        if self.pending_model_key() is not None or self.load_model(model_key).frozen:
            return False
        self.set_control("model_reset_pending", model_key, now)
        return True

    def apply_pending_model_reset(self, model_key: str, now: datetime) -> bool:
        pending = self.control("model_reset_pending")
        if not pending:
            return False
        target = pending if self.model_exists(pending) else DEFAULT_MODEL_KEY
        if target != model_key:
            return False
        model = DynamicModel(model_key=model_key, updated_at=now)
        self.save_model(model, model_key)
        self.connection.execute(
            "DELETE FROM btc_dynamic_controls WHERE key = 'model_reset_pending'"
        )
        self.connection.commit()
        return True

    def get_round(self, market_id: str) -> DynamicRound | None:
        row = self.connection.execute(
            "SELECT payload_json FROM btc_dynamic_rounds WHERE market_id = ?",
            (market_id,),
        ).fetchone()
        return DynamicRound.model_validate_json(row["payload_json"]) if row else None

    def save_round(self, round_: DynamicRound) -> None:
        self.connection.execute(
            """
            INSERT INTO btc_dynamic_rounds(
                round_id, market_id, market_slug, start_time, end_time, payload_json
            ) VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(market_id) DO UPDATE SET payload_json = excluded.payload_json
            """,
            (
                round_.round_id,
                round_.market_id,
                round_.market_slug,
                round_.start_time.isoformat(),
                round_.end_time.isoformat(),
                self._payload(round_),
            ),
        )
        self.connection.commit()

    def save_snapshot(self, snapshot: DynamicSnapshot) -> bool:
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO btc_dynamic_snapshots(
                market_id, snapshot_second, created_at, payload_json
            ) VALUES(?, ?, ?, ?)
            """,
            (
                snapshot.market_id,
                snapshot.snapshot_second,
                snapshot.created_at.isoformat(),
                self._payload(snapshot),
            ),
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def snapshots(self, market_id: str) -> list[DynamicSnapshot]:
        rows = self.connection.execute(
            """
            SELECT payload_json FROM btc_dynamic_snapshots
            WHERE market_id = ? ORDER BY snapshot_second
            """,
            (market_id,),
        ).fetchall()
        return [DynamicSnapshot.model_validate_json(row["payload_json"]) for row in rows]

    def save_tick(self, market_id: str, source: str, tick: PriceTick) -> None:
        second = int(tick.received_at.timestamp())
        self.connection.execute(
            """
            INSERT INTO btc_dynamic_ticks(market_id, source, second, price, received_at)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(market_id, source, second) DO UPDATE SET
                price = excluded.price, received_at = excluded.received_at
            """,
            (market_id, source, second, tick.price, tick.received_at.isoformat()),
        )
        self.connection.commit()

    def load_ticks(self, market_id: str, source: str) -> list[tuple[datetime, float]]:
        rows = self.connection.execute(
            """
            SELECT received_at, price FROM btc_dynamic_ticks
            WHERE market_id = ? AND source = ? ORDER BY second
            """,
            (market_id, source),
        ).fetchall()
        return [
            (datetime.fromisoformat(row["received_at"]), float(row["price"]))
            for row in rows
        ]

    def cleanup_ticks(self, now: datetime) -> None:
        cutoff = now - timedelta(days=90)
        self.connection.execute(
            "DELETE FROM btc_dynamic_ticks WHERE received_at < ?", (cutoff.isoformat(),)
        )
        self.connection.execute(
            "DELETE FROM btc_dynamic_snapshots WHERE created_at < ?", (cutoff.isoformat(),)
        )
        self.connection.commit()

    def save_order(self, order: DynamicOrder) -> DynamicOrder:
        existing = self.connection.execute(
            """
            SELECT payload_json FROM btc_dynamic_orders
            WHERE market_id = ? AND variant = ?
            """,
            (order.market_id, order.variant),
        ).fetchone()
        if existing:
            return DynamicOrder.model_validate_json(existing["payload_json"])
        row = self.connection.execute(
            "SELECT COALESCE(MAX(order_number), 0) + 1 AS value FROM btc_dynamic_orders"
        ).fetchone()
        order.order_number = int(row["value"])
        self.connection.execute(
            """
            INSERT INTO btc_dynamic_orders(
                order_id, order_number, market_id, variant, created_at, payload_json
            ) VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                order.order_id,
                order.order_number,
                order.market_id,
                order.variant,
                order.created_at.isoformat(),
                self._payload(order),
            ),
        )
        self.connection.commit()
        return order

    def update_order(self, order: DynamicOrder) -> None:
        self.connection.execute(
            "UPDATE btc_dynamic_orders SET payload_json = ? WHERE order_id = ?",
            (self._payload(order), order.order_id),
        )
        self.connection.commit()

    def unresolved_slugs(self) -> list[str]:
        rows = self.connection.execute(
            "SELECT payload_json FROM btc_dynamic_rounds ORDER BY end_time"
        ).fetchall()
        now = datetime.now(timezone.utc)
        return [
            round_.market_slug
            for row in rows
            if (
                (round_ := DynamicRound.model_validate_json(row["payload_json"])).official_outcome
                is None
                and round_.end_time <= now
            )
        ]

    def settle(
        self, market_slug: str, outcome: Direction, now: datetime
    ) -> tuple[DynamicRound, DynamicModel, bool] | None:
        row = self.connection.execute(
            """
            SELECT payload_json FROM btc_dynamic_rounds
            WHERE market_slug = ? ORDER BY start_time DESC LIMIT 1
            """,
            (market_slug,),
        ).fetchone()
        if not row:
            return None
        round_ = DynamicRound.model_validate_json(row["payload_json"])
        if round_.official_outcome is not None:
            return round_, self.load_model(round_.model_key), False
        round_.official_outcome = outcome
        round_.closed_at = now
        round_.updated_at = now
        for order in (round_.online_order, round_.formula_order):
            if order is None:
                continue
            order.official_outcome = outcome
            order.payout_usd = order.quantity if order.direction == outcome else 0.0
            order.realized_pnl = order.payout_usd - order.quote - order.fee_usd
            order.settled_at = now
            self.update_order(order)
        model = self.load_model(round_.model_key)
        if not round_.trained:
            samples = self.snapshots(round_.market_id)
            if samples and not model.frozen:
                label = 1.0 if outcome == Direction.UP else 0.0
                learning_rate = 0.05 / math.sqrt(1.0 + model.trained_markets / 500.0)
                sample_weight = 1.0 / len(samples)
                bias_gradient = 0.0
                gradients = {name: 0.0 for name in FEATURE_NAMES}
                for sample in samples:
                    error = sample.online_probability - label
                    bias_gradient += sample_weight * error
                    for name in FEATURE_NAMES:
                        gradients[name] += (
                            sample_weight * error * sample.features.get(name, 0.0)
                        )
                model.bias = clamp(model.bias - learning_rate * bias_gradient, -3.0, 3.0)
                for name in FEATURE_NAMES:
                    weight = model.weights.get(name, 0.0)
                    gradient = gradients[name] + 0.001 * weight
                    model.weights[name] = clamp(
                        weight - learning_rate * gradient, -3.0, 3.0
                    )
                model.trained_markets += 1
                model.version += 1
                model.updated_at = now
                self.save_model(model, round_.model_key)
            round_.trained = True
        self.save_round(round_)
        return round_, model, True

    def recent_orders(self, limit: int = 300) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT payload_json FROM btc_dynamic_orders
            ORDER BY order_number DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            DynamicOrder.model_validate_json(row["payload_json"]).model_dump(mode="json")
            for row in rows
        ]

    def recent_rounds(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT payload_json FROM btc_dynamic_rounds
            ORDER BY start_time DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            DynamicRound.model_validate_json(row["payload_json"]).model_dump(mode="json")
            for row in rows
        ]

    def summary(self) -> dict[str, Any]:
        cutoff = self.control("statistics_reset_at")
        rows = self.connection.execute(
            "SELECT payload_json FROM btc_dynamic_orders ORDER BY order_number"
        ).fetchall()
        orders = [DynamicOrder.model_validate_json(row["payload_json"]) for row in rows]
        if cutoff:
            cutoff_dt = datetime.fromisoformat(cutoff)
            orders = [order for order in orders if order.created_at >= cutoff_dt]
        online = [order for order in orders if order.variant == "online"]
        formula = [order for order in orders if order.variant == "formula"]

        def metrics(items: list[DynamicOrder]) -> dict[str, Any]:
            settled = [order for order in items if order.realized_pnl is not None]
            return {
                "orders": len(items),
                "settled_orders": len(settled),
                "wins": sum((order.realized_pnl or 0.0) > 0 for order in settled),
                "win_rate": (
                    sum((order.realized_pnl or 0.0) > 0 for order in settled) / len(settled)
                    if settled
                    else 0.0
                ),
                "realized_pnl": sum(order.realized_pnl or 0.0 for order in settled),
                "fees_usd": sum(order.fee_usd for order in items),
            }

        by_model = {
            model_key: {
                "online": metrics(
                    [
                        order
                        for order in online
                        if order.model_key == model_key
                    ]
                ),
                "formula": metrics(
                    [
                        order
                        for order in formula
                        if order.model_key == model_key
                    ]
                ),
            }
            for model_key in sorted({order.model_key for order in orders})
        }

        snapshot_rows = self.connection.execute(
            """
            SELECT snapshots.payload_json, rounds.payload_json AS round_json
            FROM btc_dynamic_snapshots AS snapshots
            JOIN btc_dynamic_rounds AS rounds
              ON rounds.market_id = snapshots.market_id
            """
        ).fetchall()
        brier_online: list[float] = []
        brier_formula: list[float] = []
        accurate_online: list[bool] = []
        for row in snapshot_rows:
            snapshot = DynamicSnapshot.model_validate_json(row["payload_json"])
            round_ = DynamicRound.model_validate_json(row["round_json"])
            if round_.official_outcome is None:
                continue
            if cutoff and snapshot.created_at < datetime.fromisoformat(cutoff):
                continue
            label = 1.0 if round_.official_outcome == Direction.UP else 0.0
            brier_online.append((snapshot.online_probability - label) ** 2)
            brier_formula.append((snapshot.formula_probability - label) ** 2)
            accurate_online.append(
                (snapshot.online_probability >= 0.5) == bool(label)
            )
        daily: dict[str, dict[str, Any]] = {}
        for order in orders:
            day = local_calendar_date(order.created_at)
            row = daily.setdefault(
                day,
                {
                    "date": day,
                    "online_orders": 0,
                    "online_pnl": 0.0,
                    "formula_orders": 0,
                    "formula_pnl": 0.0,
                },
            )
            prefix = "online" if order.variant == "online" else "formula"
            row[f"{prefix}_orders"] += 1
            row[f"{prefix}_pnl"] += order.realized_pnl or 0.0
        return {
            "online": metrics(online),
            "formula": metrics(formula),
            "by_model": by_model,
            "brier_online": sum(brier_online) / len(brier_online) if brier_online else None,
            "brier_formula": sum(brier_formula) / len(brier_formula) if brier_formula else None,
            "forward_accuracy": (
                sum(accurate_online) / len(accurate_online) if accurate_online else None
            ),
            "evaluated_snapshots": len(brier_online),
            "statistics_reset_at": cutoff,
            "daily": [daily[key] for key in sorted(daily, reverse=True)[:31]],
        }


class BtcDynamicEngine:
    def __init__(self, config: AppConfig, registry: BtcDynamicRegistry):
        self.config = config
        self.registry = registry
        self.model = registry.load_model(registry.active_model_key())
        self.current_round: DynamicRound | None = None
        self.chainlink_ticks: list[tuple[datetime, float]] = []
        self.chainlink_open_fallback: tuple[datetime, float] | None = None
        self.binance_ticks: list[tuple[datetime, float]] = []
        self.status = "disabled" if not config.btc_dynamic.enabled else "waiting_for_btc_market"
        self.last_reason = self.status
        self.diagnostics: dict[str, Any] = {}
        self.candidates: dict[str, dict[str, Any]] = {}
        self.confirmations: dict[str, dict[str, Any]] = {}
        self.snapshot_seconds: set[float] = set()
        self.events: list[tuple[str, Any]] = []
        self._recent_orders: list[dict[str, Any]] = []
        self._recent_rounds: list[dict[str, Any]] = []
        self._summary: dict[str, Any] = {}
        self._refresh_views()

    def _refresh_views(self) -> None:
        self._recent_orders = self.registry.recent_orders()
        self._recent_rounds = self.registry.recent_rounds()
        self._summary = self.registry.summary()

    def _loss_cooldown_state(self, now: datetime) -> dict[str, Any]:
        default = {"consecutive_losses": 0, "cooldown_until": None}
        raw = self.registry.control(LOSS_COOLDOWN_CONTROL)
        if not raw:
            return default
        try:
            payload = json.loads(raw)
            streak = max(0, int(payload.get("consecutive_losses", 0)))
            value = payload.get("cooldown_until")
            cooldown_until = datetime.fromisoformat(value) if value else None
            if cooldown_until is not None and cooldown_until.tzinfo is None:
                cooldown_until = cooldown_until.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError, json.JSONDecodeError):
            self._save_loss_cooldown_state(default, now)
            return default
        if cooldown_until is not None and cooldown_until <= now:
            self._save_loss_cooldown_state(default, now)
            return default
        return {
            "consecutive_losses": streak,
            "cooldown_until": cooldown_until,
        }

    def _save_loss_cooldown_state(
        self, state: dict[str, Any], now: datetime
    ) -> None:
        cooldown_until = state.get("cooldown_until")
        self.registry.set_control(
            LOSS_COOLDOWN_CONTROL,
            json.dumps(
                {
                    "consecutive_losses": int(state.get("consecutive_losses", 0)),
                    "cooldown_until": (
                        cooldown_until.isoformat()
                        if isinstance(cooldown_until, datetime)
                        else None
                    ),
                }
            ),
            now,
        )

    def _record_online_result(
        self, round_: DynamicRound, now: datetime
    ) -> None:
        order = round_.online_order
        if order is None or order.realized_pnl is None:
            return
        state = self._loss_cooldown_state(now)
        if state["cooldown_until"] is not None:
            return
        if order.realized_pnl >= 0:
            if state["consecutive_losses"]:
                self._save_loss_cooldown_state(
                    {"consecutive_losses": 0, "cooldown_until": None}, now
                )
            return
        streak = state["consecutive_losses"] + 1
        cooldown_until = None
        if streak >= round_.settings.loss_streak_limit:
            cooldown_until = now + timedelta(
                minutes=round_.settings.loss_cooldown_minutes
            )
        next_state = {
            "consecutive_losses": streak,
            "cooldown_until": cooldown_until,
        }
        self._save_loss_cooldown_state(next_state, now)
        if cooldown_until is not None:
            self.events.append(
                (
                    "btc_dynamic_loss_cooldown",
                    {
                        "consecutive_losses": streak,
                        "cooldown_until": cooldown_until.isoformat(),
                    },
                )
            )

    def _loss_cooldown_dashboard_state(
        self, now: datetime | None = None
    ) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        state = self._loss_cooldown_state(now)
        cooldown_until = state["cooldown_until"]
        return {
            "active": cooldown_until is not None,
            "consecutive_losses": state["consecutive_losses"],
            "cooldown_until": (
                cooldown_until.isoformat() if cooldown_until is not None else None
            ),
            "remaining_seconds": (
                max(0.0, (cooldown_until - now).total_seconds())
                if cooldown_until is not None
                else 0.0
            ),
        }

    def drain_events(self) -> list[tuple[str, Any]]:
        events, self.events = self.events, []
        return events

    def set_market(self, market: MarketState, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        if market.asset.upper() != "BTC" or market.start_time is None:
            return
        if self.current_round and self.current_round.market_id == market.condition_id:
            return
        if not self.config.btc_dynamic.enabled:
            self.current_round = None
            self.status = self.last_reason = "disabled"
            return
        existing = self.registry.get_round(market.condition_id)
        model_key = (
            existing.model_key
            if existing is not None
            else self.registry.model_key_for_new_market(market.start_time)
        )
        if existing is None and self.registry.apply_pending_model_reset(model_key, now):
            self.events.append(
                ("btc_dynamic_model_reset", self.registry.load_model(model_key))
            )
        self.model = self.registry.load_model(model_key)
        self.current_round = existing or DynamicRound(
            market_id=market.condition_id,
            market_slug=market.slug,
            model_key=model_key,
            start_time=market.start_time,
            end_time=market.end_time,
            settings=self.config.btc_dynamic.model_copy(deep=True),
            created_at=now,
            updated_at=now,
        )
        if existing is None:
            self.registry.save_round(self.current_round)
        self.chainlink_ticks = self.registry.load_ticks(market.condition_id, "chainlink")
        self.chainlink_open_fallback = None
        self.binance_ticks = self.registry.load_ticks(market.condition_id, "binance")
        self.snapshot_seconds = {
            snapshot.snapshot_second
            for snapshot in self.registry.snapshots(market.condition_id)
        }
        self.confirmations.clear()
        self.status = self.last_reason = "collecting_market_data"
        self.registry.cleanup_ticks(now)
        self.events.append(("btc_dynamic_round", self.current_round))

    def add_chainlink_tick(self, tick: PriceTick) -> None:
        self._add_tick("chainlink", tick)

    def add_binance_tick(self, tick: PriceTick) -> None:
        self._add_tick("binance", tick)

    def _add_tick(self, source: str, tick: PriceTick) -> None:
        round_ = self.current_round
        if round_ is None or tick.price <= 0:
            return
        if tick.received_at < round_.start_time - timedelta(seconds=2):
            return
        if source == "chainlink":
            observed_at = tick.exchange_timestamp or tick.received_at
            if observed_at.tzinfo is None:
                observed_at = observed_at.replace(tzinfo=timezone.utc)
            else:
                observed_at = observed_at.astimezone(timezone.utc)
            if (
                round_.start_time
                <= observed_at
                <= round_.start_time + timedelta(seconds=2)
                and (
                    self.chainlink_open_fallback is None
                    or observed_at < self.chainlink_open_fallback[0]
                )
            ):
                self.chainlink_open_fallback = (observed_at, tick.price)
        target = self.chainlink_ticks if source == "chainlink" else self.binance_ticks
        second = int(tick.received_at.timestamp())
        if target and int(target[-1][0].timestamp()) == second:
            target[-1] = (tick.received_at, tick.price)
            return
        else:
            target.append((tick.received_at, tick.price))
        cutoff = tick.received_at - timedelta(seconds=310)
        while target and target[0][0] < cutoff:
            target.pop(0)
        self.registry.save_tick(round_.market_id, source, tick)

    @staticmethod
    def _valid_rtds_start_candidate(market: MarketState) -> bool:
        if market.start_time is None:
            return False
        return bool(
            not market.threshold_candidate_conflicted
            and market.threshold_candidate_price is not None
            and math.isfinite(market.threshold_candidate_price)
            and market.threshold_candidate_source == "polymarket_rtds_start_tick"
            and market.threshold_candidate_observed_at == market.start_time
            and market.threshold_candidate_received_at is not None
            and market.start_time - timedelta(seconds=1)
            <= market.threshold_candidate_received_at
            <= market.start_time + timedelta(seconds=2)
        )

    @classmethod
    def _chainlink_open_for_display(
        cls, market: MarketState
    ) -> tuple[float | None, str | None, bool]:
        if (
            market.threshold_verified is True
            and market.threshold_price is not None
            and market.threshold_price > 0
        ):
            return market.threshold_price, market.threshold_source, True
        if cls._valid_rtds_start_candidate(market):
            return (
                market.threshold_candidate_price,
                market.threshold_candidate_source,
                False,
            )
        return None, None, False

    def _chainlink_open_for_diagnostics(
        self, market: MarketState
    ) -> tuple[float | None, str | None, bool]:
        open_price, open_source, open_verified = self._chainlink_open_for_display(market)
        if open_price is not None:
            return open_price, open_source, open_verified
        if self.chainlink_open_fallback is not None:
            return (
                self.chainlink_open_fallback[1],
                "polymarket_rtds_first_tick_after_start_unverified",
                False,
            )
        return None, None, False

    def _set_chainlink_waiting_diagnostics(
        self,
        market: MarketState,
        now: datetime,
        reason: str,
    ) -> None:
        open_price, open_source, open_verified = self._chainlink_open_for_diagnostics(market)
        current_time: datetime | None = None
        current_price: float | None = None
        if self.chainlink_ticks:
            current_time, current_price = self.chainlink_ticks[-1]
        diagnostics: dict[str, Any] = {
            "chainlink_open_price": open_price,
            "chainlink_open_source": open_source,
            "chainlink_open_verified": open_verified,
            "chainlink_current_price": current_price,
            "chainlink_tick_at": current_time.isoformat() if current_time else None,
            "remaining_seconds": max(0.0, (market.end_time - now).total_seconds()),
            "reason": reason,
            "threshold_source": market.threshold_source,
            "threshold_fetched_at": (
                market.threshold_fetched_at.isoformat()
                if market.threshold_fetched_at is not None
                else None
            ),
        }
        if open_price is not None and current_price is not None and open_price > 0 and current_price > 0:
            diagnostics["chainlink_log_return"] = math.log(current_price / open_price)
        self.diagnostics = diagnostics

    @staticmethod
    def _window_ticks(
        ticks: list[tuple[datetime, float]], now: datetime, seconds: int
    ) -> list[tuple[datetime, float]]:
        cutoff = now - timedelta(seconds=seconds)
        return [(timestamp, price) for timestamp, price in ticks if cutoff <= timestamp <= now]

    @staticmethod
    def _volatility(ticks: list[tuple[datetime, float]]) -> float:
        values: list[float] = []
        for (left_time, left), (right_time, right) in zip(ticks, ticks[1:]):
            elapsed = (right_time - left_time).total_seconds()
            if left > 0 and right > 0 and elapsed > 0:
                values.append(math.log(right / left) / math.sqrt(elapsed))
        return math.sqrt(sum(value * value for value in values) / len(values)) if values else 0.0

    @staticmethod
    def _return_for_window(
        ticks: list[tuple[datetime, float]], now: datetime, seconds: int
    ) -> float | None:
        current = next((price for timestamp, price in reversed(ticks) if timestamp <= now), None)
        prior = next(
            (
                price
                for timestamp, price in reversed(ticks)
                if timestamp <= now - timedelta(seconds=seconds)
            ),
            None,
        )
        if current is None or prior is None or current <= 0 or prior <= 0:
            return None
        return math.log(current / prior)

    @staticmethod
    def _book_features(book: OrderBookSnapshot | None) -> tuple[float, float, float | None]:
        if book is None or book.best_bid is None or book.best_ask is None:
            return 0.0, 1.0, None
        bids = sum(level.size for level in sorted(book.bids, key=lambda x: x.price, reverse=True)[:5])
        asks = sum(level.size for level in sorted(book.asks, key=lambda x: x.price)[:5])
        imbalance = (bids - asks) / (bids + asks) if bids + asks > 0 else 0.0
        spread = clamp((book.best_ask - book.best_bid) / 0.1, 0.0, 1.0)
        midpoint = (book.best_ask + book.best_bid) / 2.0
        return clamp(imbalance, -1.0, 1.0), spread, midpoint

    def _probabilities(
        self,
        market: MarketState,
        books: dict[Direction, OrderBookSnapshot],
        now: datetime,
    ) -> tuple[float, float, dict[str, float], dict[str, Any]] | None:
        open_price, open_source, open_verified = self._chainlink_open_for_display(market)
        if open_price is None:
            diagnostic_open, _, _ = self._chainlink_open_for_diagnostics(market)
            self.last_reason = (
                "chainlink_open_unverified"
                if diagnostic_open is not None
                else "chainlink_open_unavailable"
            )
            self._set_chainlink_waiting_diagnostics(market, now, self.last_reason)
            return None
        if not self.chainlink_ticks:
            self.last_reason = "chainlink_current_unavailable"
            self._set_chainlink_waiting_diagnostics(market, now, self.last_reason)
            return None
        current_time, current_price = self.chainlink_ticks[-1]
        if (now - current_time).total_seconds() > self.config.sources.rtds_stale_seconds:
            self.last_reason = "chainlink_stale"
            self._set_chainlink_waiting_diagnostics(market, now, self.last_reason)
            return None
        if not open_verified:
            self.last_reason = "chainlink_open_unverified"
            self._set_chainlink_waiting_diagnostics(market, now, self.last_reason)
            return None
        settings = self.current_round.settings if self.current_round else self.config.btc_dynamic
        short_ticks = self._window_ticks(
            self.chainlink_ticks, now, settings.short_volatility_window_seconds
        )
        long_ticks = self._window_ticks(
            self.chainlink_ticks, now, settings.long_volatility_window_seconds
        )
        short_volatility = self._volatility(short_ticks)
        long_volatility = self._volatility(long_ticks)
        floor = settings.volatility_floor_bps / 10_000.0
        sigma = max(short_volatility, long_volatility, floor)
        remaining = max(0.001, (market.end_time - now).total_seconds())
        log_return = math.log(current_price / open_price)
        z_score = log_return / (sigma * math.sqrt(remaining))
        formula_up = clamp(normal_cdf(z_score), 0.01, 0.99)
        up_imbalance, up_spread, up_midpoint = self._book_features(books.get(Direction.UP))
        down_imbalance, down_spread, _ = self._book_features(books.get(Direction.DOWN))
        crossings = 0
        prior_side: bool | None = None
        for _, price in long_ticks:
            side = price >= open_price
            if prior_side is not None and side != prior_side:
                crossings += 1
            prior_side = side
        chainlink_returns = {
            seconds: self._return_for_window(self.chainlink_ticks, now, seconds)
            for seconds in (1, 3, 5)
        }
        binance_returns = {
            seconds: self._return_for_window(self.binance_ticks, now, seconds)
            for seconds in (1, 3, 5)
        }
        binance_missing = any(value is None for value in binance_returns.values())
        features: dict[str, float] = {
            "remaining_time": clamp((remaining - 15.0) / 15.0, -1.0, 1.0),
            "volatility_ratio": clamp(
                (short_volatility / max(long_volatility, floor) - 1.0) / 2.0,
                -1.0,
                1.0,
            ),
            "open_crossings": clamp(crossings / 10.0, 0.0, 1.0),
            "up_market_gap": clamp(
                ((up_midpoint if up_midpoint is not None else formula_up) - formula_up)
                / 0.2,
                -1.0,
                1.0,
            ),
            "up_depth_imbalance": up_imbalance,
            "up_spread": up_spread,
            "down_depth_imbalance": down_imbalance,
            "down_spread": down_spread,
            "binance_missing": 1.0 if binance_missing else 0.0,
        }
        for seconds in (1, 3, 5):
            chainlink_standard = (
                chainlink_returns[seconds] / (sigma * math.sqrt(seconds))
                if chainlink_returns[seconds] is not None
                else 0.0
            )
            binance_standard = (
                binance_returns[seconds] / (sigma * math.sqrt(seconds))
                if binance_returns[seconds] is not None
                else 0.0
            )
            features[f"binance_momentum_{seconds}s"] = clamp(
                binance_standard / 3.0, -1.0, 1.0
            )
            features[f"momentum_gap_{seconds}s"] = clamp(
                (binance_standard - chainlink_standard) / 3.0, -1.0, 1.0
            )
        maximum_correction = settings.max_probability_correction_points / 100.0
        online_up = self.model.probability(formula_up, features, maximum_correction)
        diagnostics = {
            "chainlink_open_price": open_price,
            "chainlink_open_source": open_source,
            "chainlink_open_verified": open_verified,
            "chainlink_current_price": current_price,
            "chainlink_tick_at": current_time.isoformat(),
            "chainlink_log_return": log_return,
            "short_volatility": short_volatility,
            "long_volatility": long_volatility,
            "sigma": sigma,
            "z_score": z_score,
            "remaining_seconds": remaining,
            "open_crossings_60s": crossings,
            "binance_missing": binance_missing,
            "features": features,
        }
        return formula_up, online_up, features, diagnostics

    def _book_ready(
        self,
        market: MarketState,
        direction: Direction,
        book: OrderBookSnapshot | None,
        now: datetime,
        quantity: float | None,
    ) -> str | None:
        if book is None:
            return "book_missing"
        expected = market.up_token_id if direction == Direction.UP else market.down_token_id
        if book.token_id != expected or book.market_id not in {None, market.condition_id}:
            return "book_market_mismatch"
        if not book.depth_trusted:
            return "book_depth_untrusted"
        if (now - book.received_at).total_seconds() * 1000 > self.config.risk.max_data_age_ms:
            return "book_stale"
        if (
            quantity is not None
            and quantity + 1e-12
            < max(market.min_order_size, book.min_order_size)
        ):
            return "quantity_below_market_minimum"
        return None

    def _candidate(
        self,
        market: MarketState,
        direction: Direction,
        probability: float,
        formula_probability: float,
        books: dict[Direction, OrderBookSnapshot],
        now: datetime,
    ) -> dict[str, Any]:
        settings = self.current_round.settings
        book = books.get(direction)
        requested_quantity = (
            settings.quantity if settings.sizing_mode == "quantity" else None
        )
        requested_quote_usd = (
            settings.quote_amount_usd
            if settings.sizing_mode == "quote"
            else None
        )
        reason = self._book_ready(
            market,
            direction,
            book,
            now,
            requested_quantity,
        )
        tick_size = book.tick_size if book else market.tick_size
        limit = dynamic_max_price(
            probability,
            self.config.strategy.taker_fee_rate,
            settings.slippage_reserve_cents / 100.0,
            settings.min_net_edge_cents / 100.0,
            tick_size,
        )
        payload: dict[str, Any] = {
            "direction": direction.value,
            "probability": probability,
            "formula_probability": formula_probability,
            "dynamic_max_price": limit,
            "eligible": False,
            "reason": reason,
            "avg_price": None,
            "quantity": None,
            "quote": None,
            "fee_usd": None,
            "net_edge_per_share": None,
            "sizing_mode": settings.sizing_mode,
            "requested_quantity": requested_quantity,
            "requested_quote_usd": requested_quote_usd,
        }
        if reason is not None:
            return payload
        if limit is None:
            payload["reason"] = "model_edge_below_threshold"
            return payload
        assert book is not None
        if settings.sizing_mode == "quote":
            limited = book.model_copy(deep=True)
            limited.asks = [
                level
                for level in limited.asks
                if level.price <= limit + 1e-12
            ]
            execution = simulate_buy(
                limited,
                settings.quote_amount_usd,
                self.config.strategy.taker_fee_rate,
            )
        else:
            execution = simulate_buy_quantity_limit(
                book,
                settings.quantity,
                limit,
                self.config.strategy.taker_fee_rate,
            )
        if not execution.complete:
            payload["reason"] = "depth_below_dynamic_limit"
            return payload
        if execution.quantity + 1e-12 < max(
            market.min_order_size,
            book.min_order_size,
        ):
            payload["reason"] = "quantity_below_market_minimum"
            return payload
        edge = (
            probability
            - execution.avg_price
            - execution.fee_usd / execution.quantity
            - settings.slippage_reserve_cents / 100.0
        )
        payload.update(
            {
                "avg_price": execution.avg_price,
                "quantity": execution.quantity,
                "quote": execution.quote,
                "fee_usd": execution.fee_usd,
                "levels_used": execution.levels_used,
                "net_edge_per_share": edge,
            }
        )
        if edge + 1e-12 < settings.min_net_edge_cents / 100.0:
            payload["reason"] = "actual_edge_below_threshold"
            return payload
        payload["eligible"] = True
        payload["reason"] = "eligible"
        return payload

    @staticmethod
    def _choose(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
        eligible = [candidate for candidate in candidates if candidate["eligible"]]
        if not eligible:
            return None
        return sorted(
            eligible,
            key=lambda item: (-item["net_edge_per_share"], item["avg_price"]),
        )[0]

    def _confirmed(
        self,
        variant: str,
        candidate: dict[str, Any] | None,
        update_key: str,
        now: datetime,
    ) -> bool:
        if candidate is None:
            self.confirmations.pop(variant, None)
            return False
        direction = candidate["direction"]
        state = self.confirmations.get(variant)
        if state is None or state["direction"] != direction:
            state = {
                "direction": direction,
                "started_at": now,
                "updates": set(),
            }
            self.confirmations[variant] = state
        state["updates"].add(update_key)
        settings = self.current_round.settings
        candidate["confirmation_seconds"] = (now - state["started_at"]).total_seconds()
        candidate["confirmation_updates"] = len(state["updates"])
        return (
            candidate["confirmation_seconds"] + 1e-12 >= settings.confirmation_seconds
            and len(state["updates"]) >= settings.confirmation_updates
        )

    def _place(
        self,
        variant: str,
        candidate: dict[str, Any],
        formula_probability: float,
        now: datetime,
    ) -> DynamicOrder:
        direction = Direction(candidate["direction"])
        order = DynamicOrder(
            market_id=self.current_round.market_id,
            market_slug=self.current_round.market_slug,
            variant=variant,
            model_key=self.current_round.model_key,
            direction=direction,
            model_probability=candidate["probability"],
            formula_probability=(
                formula_probability
                if direction == Direction.UP
                else 1.0 - formula_probability
            ),
            sizing_mode=candidate["sizing_mode"],
            requested_quantity=candidate["requested_quantity"],
            requested_quote_usd=candidate["requested_quote_usd"],
            max_price=candidate["dynamic_max_price"],
            avg_price=candidate["avg_price"],
            quantity=candidate["quantity"],
            quote=candidate["quote"],
            fee_usd=candidate["fee_usd"],
            net_edge_per_share=candidate["net_edge_per_share"],
            created_at=now,
        )
        order = self.registry.save_order(order)
        if variant == "online":
            self.current_round.online_order = order
        else:
            self.current_round.formula_order = order
        self.current_round.updated_at = now
        self.registry.save_round(self.current_round)
        self.events.append(("btc_dynamic_order", order))
        self._refresh_views()
        return order

    def evaluate(
        self,
        market: MarketState | None,
        books: dict[Direction, OrderBookSnapshot],
        now: datetime | None = None,
    ) -> None:
        now = now or datetime.now(timezone.utc)
        if not self.config.btc_dynamic.enabled:
            self.status = self.last_reason = "disabled"
            return
        if market is None or market.asset.upper() != "BTC" or market.start_time is None:
            self.status = self.last_reason = "waiting_for_btc_market"
            return
        self.set_market(market, now)
        if self.current_round is None:
            return
        probabilities = self._probabilities(market, books, now)
        if probabilities is None:
            self.status = self.last_reason
            return
        formula_up, online_up, features, diagnostics = probabilities
        settings = self.current_round.settings
        self.diagnostics = {
            **diagnostics,
            "formula_probability_up": formula_up,
            "formula_probability_down": 1.0 - formula_up,
            "online_probability_up": online_up,
            "online_probability_down": 1.0 - online_up,
            "probability_correction_points": (online_up - formula_up) * 100.0,
            "model": self.model.model_dump(mode="json"),
            "training_snapshot_seconds": training_snapshot_seconds(settings),
        }
        elapsed = (now - market.start_time).total_seconds()
        for target in training_snapshot_seconds(settings):
            if target not in self.snapshot_seconds and target <= elapsed < target + 2.0:
                if self.registry.save_snapshot(
                    DynamicSnapshot(
                        market_id=market.condition_id,
                        model_key=self.current_round.model_key,
                        snapshot_second=target,
                        formula_probability=formula_up,
                        online_probability=online_up,
                        features=features,
                        created_at=now,
                    )
                ):
                    self.snapshot_seconds.add(target)
        if elapsed < settings.entry_seconds_after_open:
            self.status = self.last_reason = "before_entry_window"
            return
        if elapsed >= settings.exit_seconds_after_open:
            self.confirmations.clear()
            self.status = self.last_reason = (
                "order_held_for_settlement"
                if self.current_round.online_order
                else "entry_window_closed"
            )
            return
        online_candidates = [
            self._candidate(
                market,
                Direction.UP,
                online_up,
                formula_up,
                books,
                now,
            ),
            self._candidate(
                market,
                Direction.DOWN,
                1.0 - online_up,
                1.0 - formula_up,
                books,
                now,
            ),
        ]
        formula_candidates = [
            self._candidate(
                market,
                Direction.UP,
                formula_up,
                formula_up,
                books,
                now,
            ),
            self._candidate(
                market,
                Direction.DOWN,
                1.0 - formula_up,
                1.0 - formula_up,
                books,
                now,
            ),
        ]
        self.candidates = {
            "UP": online_candidates[0],
            "DOWN": online_candidates[1],
            "formula_UP": formula_candidates[0],
            "formula_DOWN": formula_candidates[1],
        }
        loss_cooldown = self._loss_cooldown_state(now)
        online_cooling_down = loss_cooldown["cooldown_until"] is not None
        if online_cooling_down:
            self.confirmations.pop("online", None)
            self.confirmations.pop("formula", None)
            for candidate in (*online_candidates, *formula_candidates):
                candidate["eligible"] = False
                candidate["reason"] = "loss_streak_cooldown"
                candidate["cooldown_until"] = loss_cooldown[
                    "cooldown_until"
                ].isoformat()
        update_key = "|".join(
            [
                self.chainlink_ticks[-1][0].isoformat(),
                *(
                    (books.get(direction).timestamp.isoformat() if books.get(direction) else "")
                    for direction in (Direction.UP, Direction.DOWN)
                ),
            ]
        )
        online_choice = self._choose(online_candidates)
        formula_choice = self._choose(formula_candidates)
        if self.current_round.online_order is None and self._confirmed(
            "online", online_choice, update_key, now
        ):
            self._place("online", online_choice, formula_up, now)
        if self.current_round.formula_order is None and self._confirmed(
            "formula", formula_choice, update_key, now
        ):
            self._place("formula", formula_choice, formula_up, now)
        if self.current_round.online_order is not None:
            self.status = self.last_reason = "order_held_for_settlement"
        elif online_cooling_down:
            self.status = self.last_reason = "loss_streak_cooldown"
        elif online_choice is not None:
            self.status = self.last_reason = "confirming"
        else:
            self.status = "waiting_for_edge"
            self.last_reason = next(
                (
                    candidate["reason"]
                    for candidate in online_candidates
                    if candidate["reason"] != "eligible"
                ),
                self.status,
            )

    def settle(
        self, market_slug: str, outcome: Direction, now: datetime | None = None
    ) -> DynamicRound | None:
        result = self.registry.settle(
            market_slug, outcome, now or datetime.now(timezone.utc)
        )
        if result is None:
            return None
        round_, model, newly_settled = result
        if (
            self.current_round is None
            or self.current_round.model_key == round_.model_key
        ):
            self.model = model
        if newly_settled:
            self._record_online_result(round_, now or datetime.now(timezone.utc))
        if self.current_round and self.current_round.market_slug == market_slug:
            self.current_round = round_
            self.status = self.last_reason = "official_settlement"
        self.events.append(("btc_dynamic_settlement", round_))
        self._refresh_views()
        return round_

    def unresolved_slugs(self) -> list[str]:
        return self.registry.unresolved_slugs()

    def reset_statistics(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        self.registry.reset_statistics(now)
        self._refresh_views()
        self.events.append(("btc_dynamic_statistics_reset", {"reset_at": now.isoformat()}))

    def request_model_reset(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        accepted = self.registry.request_model_reset(now)
        event = (
            "btc_dynamic_model_reset_pending"
            if accepted
            else "btc_dynamic_model_reset_refused"
        )
        self.events.append(
            (
                event,
                {
                    "requested_at": now.isoformat(),
                    "model_key": self.registry.active_model_key(),
                    "reason": None if accepted else "frozen_or_pending_activation",
                },
            )
        )

    def dashboard_state(self, now: datetime | None = None) -> dict[str, Any]:
        settings = (
            self.current_round.settings.model_dump(mode="json")
            if self.current_round is not None
            else self.config.btc_dynamic.model_dump(mode="json")
        )
        return {
            "status": self.status,
            "last_reason": self.last_reason,
            "config": settings,
            "round": (
                self.current_round.model_dump(mode="json")
                if self.current_round is not None
                else None
            ),
            "model": self.model.model_dump(mode="json"),
            "active_model_key": self.registry.active_model_key(),
            "pending_model_key": self.registry.pending_model_key(),
            "pending_model_not_before": self.registry.control(
                PENDING_MODEL_NOT_BEFORE_CONTROL
            ),
            "diagnostics": self.diagnostics,
            "candidates": self.candidates,
            "confirmations": {
                key: {
                    "direction": value["direction"],
                    "started_at": value["started_at"].isoformat(),
                    "updates": len(value["updates"]),
                }
                for key, value in self.confirmations.items()
            },
            "loss_cooldown": self._loss_cooldown_dashboard_state(now),
            "model_reset_pending": bool(self.registry.control("model_reset_pending")),
            "summary": self._summary,
            "recent_orders": self._recent_orders,
            "recent_rounds": self._recent_rounds,
        }
