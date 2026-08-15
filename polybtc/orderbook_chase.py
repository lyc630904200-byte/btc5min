from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel, Field

from .btc_v8 import (
    BtcV8Engine,
    V8Position,
    V8Trade,
    v8_close_position,
    v8_confirmation,
    v8_decision_policy,
    v8_orderbook_chase_exit_decision,
    v8_sell_metrics,
)
from .config import AppConfig
from .models import Direction, MarketState, OrderBookSnapshot
from .orderbook import ExecutionResult, simulate_buy, simulate_sell
from .real_trading import PolymarketSdkAdapter, RealTradingCredentials
from .sqlite_common import SqliteControlEventStore


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ChaseAttemptStatus(StrEnum):
    INTENT = "INTENT"
    SIGNED = "SIGNED"
    MATCHED = "MATCHED"
    REJECTED = "REJECTED"
    UNMEASURABLE = "UNMEASURABLE"
    UNMEASURABLE_RESTART = "UNMEASURABLE_RESTART"


class ChasePositionStatus(StrEnum):
    OPEN = "OPEN"
    EXIT_PENDING = "EXIT_PENDING"
    HOLD_TO_SETTLEMENT = "HOLD_TO_SETTLEMENT"
    CLOSED = "CLOSED"
    SETTLED = "SETTLED"


class ChaseAttempt(BaseModel):
    attempt_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    display_order_number: int | None = None
    idempotency_key: str
    strategy_trade_id: str
    market_id: str
    market_slug: str
    lane: Literal["observed", "p95"]
    side: Literal["BUY", "SELL"]
    direction: Direction
    token_id: str
    status: ChaseAttemptStatus = ChaseAttemptStatus.INTENT
    reason: str = "intent_persisted"
    requested_quote: float | None = None
    requested_quantity: float | None = None
    limit_price: float
    target_probability: float | None = None
    signal_strength: float | None = None
    expected_avg_price: float | None = None
    expected_quantity: float | None = None
    expected_quote: float | None = None
    expected_fee_usd: float | None = None
    filled_avg_price: float | None = None
    filled_quantity: float = 0.0
    filled_quote: float = 0.0
    fee_usd: float = 0.0
    latency_ms: float | None = None
    build_sign_ms: float | None = None
    requested_at: datetime = Field(default_factory=utc_now)
    requested_book_received_at: datetime | None = None
    signed_at: datetime | None = None
    estimated_exchange_arrival_at: datetime | None = None
    due_at: datetime | None = None
    resolved_at: datetime | None = None
    execution_book_received_at: datetime | None = None
    decision_details: dict[str, Any] = Field(default_factory=dict)


class ChasePosition(BaseModel):
    position_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    strategy_trade_id: str
    market_id: str
    market_slug: str
    lane: Literal["observed", "p95"]
    direction: Direction
    token_id: str
    status: ChasePositionStatus = ChasePositionStatus.OPEN
    strategy_position: V8Position
    entry_attempt_id: str
    sell_attempts: int = 0
    last_sell_book_received_at: datetime | None = None
    exit_reason: str | None = None
    realized_pnl: float | None = None
    closed_at: datetime | None = None
    official_outcome: Direction | None = None


class ShadowSigningAdapter(Protocol):
    async def connect(self) -> bool: ...

    async def build_fok_buy(
        self, token_id: str, amount_usd: float, max_price: float
    ) -> Any: ...

    async def build_fok_sell(
        self, token_id: str, quantity: float, min_price: float
    ) -> Any: ...

    async def close(self) -> None: ...


class EphemeralPolymarketSigner:
    """Restricted view of the existing SDK adapter; it exposes no submit method."""

    def __init__(self):
        from eth_account import Account

        account = Account.create()
        private_key = account.key.hex()
        if not private_key.startswith("0x"):
            private_key = f"0x{private_key}"
        self._adapter = PolymarketSdkAdapter(
            RealTradingCredentials(private_key=private_key, wallet=account.address)
        )
        self.connected = False

    async def connect(self) -> bool:
        snapshot = await self._adapter.connect()
        self.connected = bool(snapshot.connected and not snapshot.geoblocked)
        return self.connected

    async def build_fok_buy(
        self, token_id: str, amount_usd: float, max_price: float
    ) -> Any:
        return await self._adapter.build_fok_buy(token_id, amount_usd, max_price)

    async def build_fok_sell(
        self, token_id: str, quantity: float, min_price: float
    ) -> Any:
        return await self._adapter.build_fok_sell(token_id, quantity, min_price)

    async def close(self) -> None:
        await self._adapter.close()


class OrderbookChaseRegistry:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS orderbook_chase_attempts (
                attempt_id TEXT PRIMARY KEY,
                display_order_number INTEGER NOT NULL UNIQUE,
                idempotency_key TEXT NOT NULL UNIQUE,
                market_id TEXT NOT NULL,
                lane TEXT NOT NULL,
                side TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_chase_attempt_market
            ON orderbook_chase_attempts(market_id, display_order_number);
            CREATE TABLE IF NOT EXISTS orderbook_chase_positions (
                position_id TEXT PRIMARY KEY,
                market_id TEXT NOT NULL,
                lane TEXT NOT NULL,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_chase_position_status
            ON orderbook_chase_positions(status, market_id);
            CREATE TABLE IF NOT EXISTS orderbook_chase_latency (
                sample_id INTEGER PRIMARY KEY AUTOINCREMENT,
                sampled_at TEXT NOT NULL,
                rtt_ms REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_chase_latency_time
            ON orderbook_chase_latency(sampled_at);
            CREATE TABLE IF NOT EXISTS orderbook_chase_controls (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS orderbook_chase_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            """
        )
        self.connection.commit()
        self.common = SqliteControlEventStore(
            self.connection,
            controls_table="orderbook_chase_controls",
            events_table="orderbook_chase_events",
        )
        self.mark_interrupted_attempts()

    def close(self) -> None:
        self.connection.close()

    def save_attempt(self, attempt: ChaseAttempt) -> ChaseAttempt:
        now = utc_now().isoformat()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            if attempt.display_order_number is None:
                row = self.connection.execute(
                    "SELECT COALESCE(MAX(display_order_number), 0) + 1 AS value "
                    "FROM orderbook_chase_attempts"
                ).fetchone()
                attempt.display_order_number = int(row["value"])
            payload = attempt.model_dump(mode="json")
            self.connection.execute(
                """
                INSERT INTO orderbook_chase_attempts (
                    attempt_id, display_order_number, idempotency_key, market_id,
                    lane, side, status, created_at, updated_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(attempt_id) DO UPDATE SET
                    status=excluded.status, updated_at=excluded.updated_at,
                    payload_json=excluded.payload_json
                """,
                (
                    attempt.attempt_id,
                    attempt.display_order_number,
                    attempt.idempotency_key,
                    attempt.market_id,
                    attempt.lane,
                    attempt.side,
                    attempt.status.value,
                    attempt.requested_at.isoformat(),
                    now,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            self.connection.commit()
            return attempt
        except Exception:
            self.connection.rollback()
            raise

    def save_position(self, position: ChasePosition) -> ChasePosition:
        payload = position.model_dump(mode="json")
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO orderbook_chase_positions (
                    position_id, market_id, lane, status, updated_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(position_id) DO UPDATE SET
                    status=excluded.status, updated_at=excluded.updated_at,
                    payload_json=excluded.payload_json
                """,
                (
                    position.position_id,
                    position.market_id,
                    position.lane,
                    position.status.value,
                    utc_now().isoformat(),
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                ),
            )
        return position

    def recent_attempts(self, limit: int = 300) -> list[ChaseAttempt]:
        rows = self.connection.execute(
            "SELECT payload_json FROM orderbook_chase_attempts "
            "ORDER BY display_order_number DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [ChaseAttempt.model_validate_json(row["payload_json"]) for row in rows]

    def recent_positions(self, limit: int = 300) -> list[ChasePosition]:
        rows = self.connection.execute(
            "SELECT payload_json FROM orderbook_chase_positions "
            "ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [ChasePosition.model_validate_json(row["payload_json"]) for row in rows]

    def open_positions(self) -> list[ChasePosition]:
        rows = self.connection.execute(
            "SELECT payload_json FROM orderbook_chase_positions "
            "WHERE status IN (?, ?, ?)",
            (
                ChasePositionStatus.OPEN.value,
                ChasePositionStatus.EXIT_PENDING.value,
                ChasePositionStatus.HOLD_TO_SETTLEMENT.value,
            ),
        ).fetchall()
        return [ChasePosition.model_validate_json(row["payload_json"]) for row in rows]

    def mark_interrupted_attempts(self) -> None:
        rows = self.connection.execute(
            "SELECT payload_json FROM orderbook_chase_attempts WHERE status IN (?, ?)",
            (ChaseAttemptStatus.INTENT.value, ChaseAttemptStatus.SIGNED.value),
        ).fetchall()
        for row in rows:
            attempt = ChaseAttempt.model_validate_json(row["payload_json"])
            attempt.status = ChaseAttemptStatus.UNMEASURABLE_RESTART
            attempt.reason = "process_restarted_before_shadow_result"
            attempt.resolved_at = utc_now()
            self.save_attempt(attempt)

    def save_latency(self, sampled_at: datetime, rtt_ms: float, window_minutes: float) -> None:
        cutoff = sampled_at - timedelta(minutes=window_minutes)
        with self.connection:
            self.connection.execute(
                "INSERT INTO orderbook_chase_latency(sampled_at, rtt_ms) VALUES (?, ?)",
                (sampled_at.isoformat(), rtt_ms),
            )
            self.connection.execute(
                "DELETE FROM orderbook_chase_latency WHERE sampled_at < ?",
                (cutoff.isoformat(),),
            )

    def load_latency(self, cutoff: datetime) -> list[tuple[datetime, float]]:
        rows = self.connection.execute(
            "SELECT sampled_at, rtt_ms FROM orderbook_chase_latency "
            "WHERE sampled_at >= ? ORDER BY sampled_at",
            (cutoff.isoformat(),),
        ).fetchall()
        return [(datetime.fromisoformat(row["sampled_at"]), float(row["rtt_ms"])) for row in rows]

    def set_control(self, key: str, value: Any) -> None:
        self.common.set_control(key, value)

    def get_control(self, key: str, default: Any = None) -> Any:
        return self.common.get_control(key, default)

    def event(self, event_type: str, payload: dict[str, Any]) -> None:
        self.common.event(event_type, payload)

    def summary(self, required_roundtrips: int, observed_threshold: float, p95_threshold: float) -> dict[str, Any]:
        attempt_rows = self.connection.execute(
            """
            SELECT lane,
                   SUM(CASE WHEN side = 'BUY' AND status IN (?, ?) THEN 1 ELSE 0 END) AS trials,
                   SUM(CASE WHEN side = 'BUY' AND status = ? THEN 1 ELSE 0 END) AS buy_fills,
                   SUM(CASE WHEN side = 'BUY' AND status = ? THEN 1 ELSE 0 END) AS rejected,
                   SUM(CASE WHEN side = 'BUY' AND status IN (?, ?) THEN 1 ELSE 0 END) AS unmeasurable
            FROM orderbook_chase_attempts
            GROUP BY lane
            """,
            (
                ChaseAttemptStatus.MATCHED.value,
                ChaseAttemptStatus.REJECTED.value,
                ChaseAttemptStatus.MATCHED.value,
                ChaseAttemptStatus.REJECTED.value,
                ChaseAttemptStatus.UNMEASURABLE.value,
                ChaseAttemptStatus.UNMEASURABLE_RESTART.value,
            ),
        ).fetchall()
        position_rows = self.connection.execute(
            """
            SELECT lane,
                   SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS roundtrips,
                   SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS settled,
                   COALESCE(SUM(
                       CASE WHEN status IN (?, ?)
                            THEN CAST(json_extract(payload_json, '$.realized_pnl') AS REAL)
                            ELSE 0 END
                   ), 0) AS realized_pnl
            FROM orderbook_chase_positions
            GROUP BY lane
            """,
            (
                ChasePositionStatus.CLOSED.value,
                ChasePositionStatus.SETTLED.value,
                ChasePositionStatus.CLOSED.value,
                ChasePositionStatus.SETTLED.value,
            ),
        ).fetchall()
        attempt_stats = {str(row["lane"]): row for row in attempt_rows}
        position_stats = {str(row["lane"]): row for row in position_rows}
        lanes: dict[str, dict[str, Any]] = {}
        for lane in ("observed", "p95"):
            attempts = attempt_stats.get(lane)
            positions = position_stats.get(lane)
            trials = int(attempts["trials"] or 0) if attempts else 0
            roundtrips = int(positions["roundtrips"] or 0) if positions else 0
            survival = roundtrips / trials if trials else 0.0
            lanes[lane] = {
                "trials": trials,
                "buy_fills": int(attempts["buy_fills"] or 0) if attempts else 0,
                "roundtrips": roundtrips,
                "settled": int(positions["settled"] or 0) if positions else 0,
                "survival_rate": survival,
                "realized_pnl": float(positions["realized_pnl"] or 0.0) if positions else 0.0,
                "rejected": int(attempts["rejected"] or 0) if attempts else 0,
                "unmeasurable": int(attempts["unmeasurable"] or 0) if attempts else 0,
            }
        observed = lanes["observed"]
        p95 = lanes["p95"]
        ready = observed["trials"] >= required_roundtrips and p95["trials"] >= required_roundtrips
        passed = bool(
            ready
            and observed["survival_rate"] >= observed_threshold
            and observed["realized_pnl"] > 0
            and p95["survival_rate"] >= p95_threshold
            and p95["realized_pnl"] >= 0
        )
        return {
            "lanes": lanes,
            "required_roundtrips": required_roundtrips,
            "ready_to_report": ready,
            "speed_verdict": "PASS" if passed else "FAIL" if ready else "COLLECTING",
            "real_trading_unlocked": False,
        }


class ClobLatencyProbe:
    def __init__(self, config: AppConfig, registry: OrderbookChaseRegistry):
        self.config = config
        self.registry = registry
        self.samples: deque[tuple[datetime, float]] = deque()
        self.last_error: str | None = None
        self._client = self._new_client()
        cutoff = utc_now() - timedelta(minutes=config.orderbook_chase.latency_window_minutes)
        self.samples.extend(registry.load_latency(cutoff))

    def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.config.sources.clob_url,
            timeout=2.0,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
        )

    async def _replace_client(self) -> None:
        stale_client = self._client
        self._client = self._new_client()
        try:
            await asyncio.wait_for(stale_client.aclose(), timeout=1.0)
        except Exception:
            pass

    def _prune(self, now: datetime) -> None:
        cutoff = now - timedelta(minutes=self.config.orderbook_chase.latency_window_minutes)
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()

    def record(self, sampled_at: datetime, rtt_ms: float) -> None:
        self.samples.append((sampled_at, rtt_ms))
        self._prune(sampled_at)
        self.registry.save_latency(
            sampled_at,
            rtt_ms,
            self.config.orderbook_chase.latency_window_minutes,
        )

    async def sample(self) -> None:
        started = time.perf_counter_ns()
        try:
            response = await self._client.get("/time")
            response.raise_for_status()
            rtt_ms = (time.perf_counter_ns() - started) / 1_000_000
            self.record(utc_now(), rtt_ms)
            self.last_error = None
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, httpx.PoolTimeout):
                await self._replace_client()

    def sampling_enabled(self) -> bool:
        return bool(
            self.config.orderbook_chase.enabled
            or self.config.btc_weighted.enabled
            or self.config.btc_lead_prediction.enabled
        )

    async def run(self) -> None:
        while True:
            if self.sampling_enabled():
                await self.sample()
            await asyncio.sleep(self.config.orderbook_chase.latency_probe_interval_seconds)

    def snapshot(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or utc_now()
        self._prune(now)
        values = sorted(value for _, value in self.samples)
        latest_at, latest = self.samples[-1] if self.samples else (None, None)
        p50 = values[math.ceil(len(values) * 0.50) - 1] if values else None
        p95 = values[math.ceil(len(values) * 0.95) - 1] if values else None
        age = (now - latest_at).total_seconds() if latest_at else None
        fresh = bool(
            age is not None
            and 0 <= age <= self.config.orderbook_chase.latency_max_age_seconds
        )
        warmed = fresh and len(values) >= self.config.orderbook_chase.latency_min_samples
        return {
            "latest_ms": latest,
            "p50_ms": p50,
            "p95_ms": p95,
            "sample_count": len(values),
            "latest_at": latest_at.isoformat() if latest_at else None,
            "age_seconds": age,
            "fresh": fresh,
            "warmed": warmed,
            "last_error": self.last_error,
        }

    async def close(self) -> None:
        await self._client.aclose()


class OrderbookChaseEngine:
    def __init__(
        self,
        config: AppConfig,
        registry: OrderbookChaseRegistry,
        source_v8: BtcV8Engine,
        signer: ShadowSigningAdapter | None = None,
    ):
        self.config = config
        self.registry = registry
        self.source_v8 = source_v8
        self.signer = signer or EphemeralPolymarketSigner()
        self.latency = ClobLatencyProbe(config, registry)
        self.signer_connected = False
        self.signer_error: str | None = None
        self.last_signer_attempt_at: datetime | None = None
        self._signer_task: asyncio.Task[bool] | None = None
        self.pending: dict[str, ChaseAttempt] = {}
        self.positions: dict[str, ChasePosition] = {
            item.position_id: item for item in registry.open_positions()
        }
        self.confirmations: dict[str, dict[str, Any]] = {}
        self.market: MarketState | None = None
        self.status = "disabled" if not config.orderbook_chase.enabled else "starting"
        self.last_reason = self.status
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.paused = bool(registry.get_control("paused", False))
        self.emergency_stopped = bool(registry.get_control("emergency_stopped", False))

    async def _connect_signer(self) -> bool:
        try:
            self.signer_connected = await self.signer.connect()
            self.signer_error = None if self.signer_connected else "ephemeral_signer_not_connected"
        except Exception as exc:
            self.signer_error = f"connect_failed:{type(exc).__name__}"
            self.signer_connected = False
        return self.signer_connected

    def start_signer_connection(self, now: datetime | None = None) -> None:
        now = now or utc_now()
        if self.signer_connected or (
            self._signer_task is not None and not self._signer_task.done()
        ):
            return
        if self.last_signer_attempt_at and (now - self.last_signer_attempt_at).total_seconds() < 30:
            return
        self.last_signer_attempt_at = now
        self._signer_task = asyncio.create_task(self._connect_signer())

    async def ensure_signer(self, now: datetime | None = None) -> bool:
        if self.signer_connected:
            return True
        self.start_signer_connection(now)
        task = self._signer_task
        if task is None:
            return False
        try:
            return await asyncio.shield(task)
        finally:
            if task.done() and self._signer_task is task:
                self._signer_task = None

    def set_market(self, market: MarketState, now: datetime | None = None) -> None:
        now = now or utc_now()
        self.market = market
        if not self.config.orderbook_chase.enabled:
            self.status = self.last_reason = "disabled"
            return
        self.status = "paused" if self.paused else "v8_signal_following"
        self.last_reason = self.status
        for position in self.positions.values():
            if position.market_id != market.condition_id and position.status in {
                ChasePositionStatus.OPEN,
                ChasePositionStatus.EXIT_PENDING,
            }:
                position.status = ChasePositionStatus.HOLD_TO_SETTLEMENT
                position.exit_reason = "market_changed_before_exit"
                self.registry.save_position(position)

    def _active_position(self, lane: str) -> ChasePosition | None:
        if self.market is None:
            return None
        return next(
            (
                item
                for item in self.positions.values()
                if item.lane == lane
                and item.market_id == self.market.condition_id
                and item.status in {
                    ChasePositionStatus.OPEN,
                    ChasePositionStatus.EXIT_PENDING,
                }
            ),
            None,
        )

    def _book_for(self, direction: Direction, books: dict[Direction, OrderBookSnapshot]) -> OrderBookSnapshot | None:
        return books.get(direction)

    def _book_reason(
        self,
        direction: Direction,
        book: OrderBookSnapshot | None,
        now: datetime,
    ) -> str | None:
        if self.market is None:
            return "market_missing"
        return self.source_v8._book_ready(self.market, direction, book, now)

    def _execution_after_limit(self, attempt: ChaseAttempt, book: OrderBookSnapshot) -> ExecutionResult:
        limited = book.model_copy(deep=True)
        if attempt.side == "BUY":
            limited.asks = [level for level in limited.asks if level.price <= attempt.limit_price + 1e-12]
            return simulate_buy(
                limited,
                float(attempt.requested_quote or 0.0),
                self.config.strategy.taker_fee_rate,
            )
        limited.bids = [level for level in limited.bids if level.price + 1e-12 >= attempt.limit_price]
        return simulate_sell(
            limited,
            float(attempt.requested_quantity or 0.0),
            self.config.strategy.taker_fee_rate,
        )

    async def _handle_instant_buy(
        self,
        trade: V8Trade,
        books: dict[Direction, OrderBookSnapshot],
        now: datetime,
    ) -> None:
        if self.market is None:
            return
        candidate = dict(self.source_v8.candidates.get(trade.direction.value) or {})
        limit = candidate.get("limit")
        if limit is None:
            return
        latency = self.latency.snapshot(now)
        book = books.get(trade.direction)
        attempts: list[ChaseAttempt] = []
        sequence = self.source_v8.current_round.entry_count if self.source_v8.current_round else 0
        for lane in ("observed", "p95"):
            if self._active_position(lane) is not None:
                continue
            delay = latency["latest_ms"] if lane == "observed" else latency["p95_ms"]
            usable = latency["fresh"] if lane == "observed" else latency["warmed"]
            attempt = ChaseAttempt(
                idempotency_key=(
                    f"orderbook_chase:{self.market.condition_id}:{sequence}:BUY:{lane}"
                ),
                strategy_trade_id=trade.trade_id,
                market_id=self.market.condition_id,
                market_slug=self.market.slug,
                lane=lane,
                side="BUY",
                direction=trade.direction,
                token_id=(
                    self.market.up_token_id
                    if trade.direction == Direction.UP
                    else self.market.down_token_id
                ),
                requested_quote=trade.quote,
                limit_price=float(limit),
                target_probability=candidate.get("valuation_probability"),
                signal_strength=(candidate.get("decision_details") or {}).get("signal_strength"),
                expected_avg_price=trade.avg_price,
                expected_quantity=trade.quantity,
                expected_quote=trade.quote,
                expected_fee_usd=trade.fee_usd,
                requested_at=now,
                requested_book_received_at=book.received_at if book else None,
                latency_ms=float(delay) if delay is not None else None,
                decision_details=dict(candidate.get("decision_details") or {}),
            )
            self.registry.save_attempt(attempt)
            if not usable or delay is None:
                attempt.status = ChaseAttemptStatus.UNMEASURABLE
                attempt.reason = "latency_probe_warming" if lane == "p95" else "latency_probe_stale"
                attempt.resolved_at = now
                self.registry.save_attempt(attempt)
            else:
                attempts.append(attempt)
        if not attempts:
            return
        if not await self.ensure_signer(now):
            for attempt in attempts:
                attempt.status = ChaseAttemptStatus.UNMEASURABLE
                attempt.reason = "ephemeral_signer_unavailable"
                attempt.resolved_at = utc_now()
                self.registry.save_attempt(attempt)
            return
        started = time.perf_counter_ns()
        try:
            await self.signer.build_fok_buy(
                attempts[0].token_id,
                float(attempts[0].requested_quote or 0.0),
                attempts[0].limit_price,
            )
            build_sign_ms = (time.perf_counter_ns() - started) / 1_000_000
        except Exception as exc:
            for attempt in attempts:
                attempt.status = ChaseAttemptStatus.UNMEASURABLE
                attempt.reason = f"signing_error:{type(exc).__name__}"
                attempt.resolved_at = utc_now()
                self.registry.save_attempt(attempt)
            self.signer_error = f"signing_failed:{type(exc).__name__}"
            return
        signed_at = utc_now()
        for attempt in attempts:
            delay = float(attempt.latency_ms or 0.0)
            attempt.status = ChaseAttemptStatus.SIGNED
            attempt.reason = "shadow_fok_signed"
            attempt.build_sign_ms = build_sign_ms
            attempt.signed_at = signed_at
            attempt.estimated_exchange_arrival_at = signed_at + timedelta(milliseconds=delay / 2)
            attempt.due_at = signed_at + timedelta(milliseconds=delay)
            self.registry.save_attempt(attempt)
            self.pending[attempt.attempt_id] = attempt
            self.events.append(("orderbook_chase_attempt", attempt.model_dump(mode="json")))

    def _open_position(self, attempt: ChaseAttempt, execution: ExecutionResult, now: datetime) -> None:
        strategy_position = V8Position(
            market_id=attempt.market_id,
            market_slug=attempt.market_slug,
            direction=attempt.direction,
            quantity=execution.quantity,
            entry_price=execution.avg_price,
            entry_quote=execution.quote,
            entry_fee_usd=execution.fee_usd,
            entry_model_probability=attempt.target_probability,
            entry_formula_probability=attempt.target_probability,
            strategy_mode="orderbook_chase",
            entry_target_probability=attempt.target_probability,
            entry_signal_strength=attempt.signal_strength,
            opened_at=now,
        )
        position = ChasePosition(
            strategy_trade_id=attempt.strategy_trade_id,
            market_id=attempt.market_id,
            market_slug=attempt.market_slug,
            lane=attempt.lane,
            direction=attempt.direction,
            token_id=attempt.token_id,
            strategy_position=strategy_position,
            entry_attempt_id=attempt.attempt_id,
        )
        self.positions[position.position_id] = position
        self.registry.save_position(position)
        self.events.append(("orderbook_chase_position", position.model_dump(mode="json")))

    async def _resolve_pending(
        self,
        books: dict[Direction, OrderBookSnapshot],
        now: datetime,
    ) -> None:
        for attempt in list(self.pending.values()):
            if attempt.due_at is None or now < attempt.due_at:
                continue
            book = books.get(attempt.direction)
            newer = bool(
                book is not None
                and (
                    attempt.requested_book_received_at is None
                    or book.received_at > attempt.requested_book_received_at
                )
            )
            reason = self._book_reason(attempt.direction, book, now)
            if not newer or reason is not None or book is None:
                overdue = (now - attempt.due_at).total_seconds()
                if overdue <= self.config.orderbook_chase.latency_max_age_seconds:
                    continue
                attempt.status = ChaseAttemptStatus.UNMEASURABLE
                attempt.reason = reason or "no_post_intent_book_update"
                attempt.resolved_at = now
                self.registry.save_attempt(attempt)
                self.pending.pop(attempt.attempt_id, None)
                self._return_position_to_open(attempt)
                continue
            execution = self._execution_after_limit(attempt, book)
            minimum = max(self.market.min_order_size, book.min_order_size) if self.market else book.min_order_size
            complete = execution.complete and (
                attempt.side == "SELL" or execution.quantity + 1e-12 >= minimum
            )
            attempt.execution_book_received_at = book.received_at
            attempt.resolved_at = now
            if not complete:
                attempt.status = ChaseAttemptStatus.REJECTED
                attempt.reason = "fok_depth_or_limit_unavailable"
                self.registry.save_attempt(attempt)
                self.pending.pop(attempt.attempt_id, None)
                self._return_position_to_open(attempt)
                continue
            attempt.status = ChaseAttemptStatus.MATCHED
            attempt.reason = "shadow_fok_matched"
            attempt.filled_avg_price = execution.avg_price
            attempt.filled_quantity = execution.quantity
            attempt.filled_quote = execution.quote
            attempt.fee_usd = execution.fee_usd
            self.registry.save_attempt(attempt)
            self.pending.pop(attempt.attempt_id, None)
            if attempt.side == "BUY":
                self._open_position(attempt, execution, now)
            else:
                self._close_position(attempt, execution, now)
            self.events.append(("orderbook_chase_attempt", attempt.model_dump(mode="json")))

    def _position_for_attempt(self, attempt: ChaseAttempt) -> ChasePosition | None:
        return next(
            (
                item
                for item in self.positions.values()
                if item.strategy_trade_id == attempt.strategy_trade_id
                and item.lane == attempt.lane
                and item.market_id == attempt.market_id
            ),
            None,
        )

    def _return_position_to_open(self, attempt: ChaseAttempt) -> None:
        if attempt.side != "SELL":
            return
        position = self._position_for_attempt(attempt)
        if position is None:
            return
        position.status = ChasePositionStatus.OPEN
        if attempt.execution_book_received_at is not None:
            position.last_sell_book_received_at = attempt.execution_book_received_at
        self.registry.save_position(position)

    def _close_position(self, attempt: ChaseAttempt, execution: ExecutionResult, now: datetime) -> None:
        position = self._position_for_attempt(attempt)
        if position is None:
            return
        strategy_position = position.strategy_position
        pnl = v8_close_position(
            strategy_position,
            execution,
            now,
            position.exit_reason,
        )
        position.status = ChasePositionStatus.CLOSED
        position.realized_pnl = pnl
        position.closed_at = now
        self.registry.save_position(position)
        self.confirmations.pop(f"sell:{position.position_id}", None)
        self.events.append(("orderbook_chase_position", position.model_dump(mode="json")))

    @staticmethod
    def _sell_min_price(book: OrderBookSnapshot, quantity: float) -> float | None:
        remaining = quantity
        worst: float | None = None
        for level in sorted(book.bids, key=lambda item: item.price, reverse=True):
            if remaining <= 1e-12:
                break
            taken = min(remaining, level.size)
            if taken > 0:
                worst = level.price
                remaining -= taken
        return worst if remaining <= 1e-9 else None

    async def _create_sell_attempt(
        self,
        position: ChasePosition,
        book: OrderBookSnapshot,
        execution: ExecutionResult,
        now: datetime,
    ) -> None:
        min_price = self._sell_min_price(book, position.strategy_position.quantity)
        if min_price is None:
            self.last_reason = "sell_depth_unavailable"
            return
        latency = self.latency.snapshot(now)
        delay = latency["latest_ms"] if position.lane == "observed" else latency["p95_ms"]
        usable = latency["fresh"] if position.lane == "observed" else latency["warmed"]
        position.sell_attempts += 1
        attempt = ChaseAttempt(
            idempotency_key=(
                f"orderbook_chase:{position.market_id}:{position.strategy_trade_id}:"
                f"SELL:{position.lane}:{position.sell_attempts}"
            ),
            strategy_trade_id=position.strategy_trade_id,
            market_id=position.market_id,
            market_slug=position.market_slug,
            lane=position.lane,
            side="SELL",
            direction=position.direction,
            token_id=position.token_id,
            requested_quantity=position.strategy_position.quantity,
            limit_price=min_price,
            expected_avg_price=execution.avg_price,
            expected_quantity=execution.quantity,
            expected_quote=execution.quote,
            expected_fee_usd=execution.fee_usd,
            requested_at=now,
            requested_book_received_at=book.received_at,
            latency_ms=float(delay) if delay is not None else None,
            decision_details={"exit_reason": position.exit_reason},
        )
        self.registry.save_attempt(attempt)
        position.last_sell_book_received_at = book.received_at
        self.registry.save_position(position)
        if not usable or delay is None:
            attempt.status = ChaseAttemptStatus.UNMEASURABLE
            attempt.reason = "latency_probe_warming" if position.lane == "p95" else "latency_probe_stale"
            attempt.resolved_at = now
            self.registry.save_attempt(attempt)
            return
        if not await self.ensure_signer(now):
            attempt.status = ChaseAttemptStatus.UNMEASURABLE
            attempt.reason = "ephemeral_signer_unavailable"
            attempt.resolved_at = now
            self.registry.save_attempt(attempt)
            return
        started = time.perf_counter_ns()
        try:
            await self.signer.build_fok_sell(
                attempt.token_id,
                float(attempt.requested_quantity or 0.0),
                attempt.limit_price,
            )
            attempt.build_sign_ms = (time.perf_counter_ns() - started) / 1_000_000
        except Exception as exc:
            attempt.status = ChaseAttemptStatus.UNMEASURABLE
            attempt.reason = f"signing_error:{type(exc).__name__}"
            attempt.resolved_at = utc_now()
            self.registry.save_attempt(attempt)
            return
        signed_at = utc_now()
        delay = float(attempt.latency_ms or 0.0)
        attempt.status = ChaseAttemptStatus.SIGNED
        attempt.reason = "shadow_fok_signed"
        attempt.signed_at = signed_at
        attempt.estimated_exchange_arrival_at = signed_at + timedelta(milliseconds=delay / 2)
        attempt.due_at = signed_at + timedelta(milliseconds=delay)
        position.status = ChasePositionStatus.EXIT_PENDING
        self.registry.save_attempt(attempt)
        self.registry.save_position(position)
        self.pending[attempt.attempt_id] = attempt
        self.events.append(("orderbook_chase_attempt", attempt.model_dump(mode="json")))

    async def _evaluate_positions(
        self,
        books: dict[Direction, OrderBookSnapshot],
        now: datetime,
    ) -> None:
        if self.market is None or self.market.start_time is None:
            return
        elapsed = (now - self.market.start_time).total_seconds()
        source_round = self.source_v8.current_round
        settings = (
            source_round.settings
            if source_round is not None and source_round.market_id == self.market.condition_id
            else self.config.btc_v8
        )
        policy = v8_decision_policy(settings)
        update_key = self.source_v8._update_key(books)
        for position in list(self.positions.values()):
            if position.market_id != self.market.condition_id:
                continue
            if position.status != ChasePositionStatus.OPEN:
                continue
            book = books.get(position.direction)
            if self._book_reason(position.direction, book, now) is not None or book is None:
                continue
            if now >= self.market.end_time:
                position.status = ChasePositionStatus.HOLD_TO_SETTLEMENT
                position.exit_reason = position.exit_reason or "sell_window_closed"
                self.registry.save_position(position)
                continue
            if (
                position.exit_reason
                and position.last_sell_book_received_at is not None
                and book.received_at > position.last_sell_book_received_at
            ):
                retry_execution = simulate_sell(
                    book,
                    position.strategy_position.quantity,
                    self.config.strategy.taker_fee_rate,
                )
                if retry_execution.complete:
                    await self._create_sell_attempt(position, book, retry_execution, now)
                continue
            execution = simulate_sell(
                book,
                position.strategy_position.quantity,
                self.config.strategy.taker_fee_rate,
            )
            if not execution.complete:
                continue
            strategy_position = position.strategy_position
            target_probability = (
                float(strategy_position.entry_target_probability)
                if strategy_position.entry_target_probability is not None
                else 1.0
            )
            net_value, _, pnl = v8_sell_metrics(
                strategy_position,
                execution,
                target_probability,
                settings.slippage_reserve_cents,
            )
            if strategy_position.peak_unrealized_pnl is None or pnl > strategy_position.peak_unrealized_pnl:
                strategy_position.peak_unrealized_pnl = pnl
                self.registry.save_position(position)
            decision = v8_orderbook_chase_exit_decision(
                position=strategy_position,
                net_value=net_value,
                pnl=pnl,
                diagnostics=self.source_v8.diagnostics,
                now=now,
                take_profit_arm_usd=settings.chase_take_profit_arm_usd,
                take_profit_drawdown_usd=settings.chase_take_profit_drawdown_usd,
                take_profit_drawdown_fraction=settings.chase_take_profit_drawdown_fraction,
                settings=settings,
            )
            reason = str(decision["reason"]) if decision.get("eligible") else None
            confirmation_seconds = float(policy["sell_confirmation_seconds"])
            confirmation_updates = int(policy["sell_confirmation_updates"])
            if reason in {
                "chase_timeout",
                "chase_hard_stop",
                "chase_emergency_stop",
            }:
                confirmation_seconds, confirmation_updates = 0.0, 1
            elif reason == "chase_signal_reversed":
                confirmation_seconds = settings.reversal_confirmation_seconds
                confirmation_updates = settings.reversal_confirmation_updates
            confirmed = v8_confirmation(
                self.confirmations,
                f"sell:{position.position_id}",
                reason,
                update_key,
                now,
                confirmation_seconds,
                confirmation_updates,
            )
            if reason and confirmed:
                position.exit_reason = reason
                await self._create_sell_attempt(position, book, execution, now)

    async def evaluate(
        self,
        market: MarketState | None,
        books: dict[Direction, OrderBookSnapshot],
        confirmed_trades: list[V8Trade] | tuple[V8Trade, ...] = (),
        now: datetime | None = None,
    ) -> None:
        now = now or utc_now()
        if not self.config.orderbook_chase.enabled:
            self.status = self.last_reason = "disabled"
            return
        if market is None:
            self.status = self.last_reason = "waiting_for_btc_market"
            return
        if self.paused or self.emergency_stopped:
            self.status = self.last_reason = "emergency_stopped" if self.emergency_stopped else "paused"
            return
        self.start_signer_connection(now)
        self.set_market(market, now)
        source_round = self.source_v8.current_round
        source_reason: str | None = None
        if not self.source_v8.config.btc_v8.enabled:
            source_reason = "source_v8_disabled"
        elif source_round is None or source_round.market_id != market.condition_id:
            source_reason = "source_v8_market_unavailable"
        if source_reason is None:
            for trade in confirmed_trades:
                if (
                    trade.action == "BUY"
                    and trade.market_id == market.condition_id
                    and trade.strategy_mode == "orderbook_chase"
                ):
                    await self._handle_instant_buy(trade, books, now)
        await self._resolve_pending(books, now)
        await self._evaluate_positions(books, now)
        self.status = source_reason or "v8_signal_following"
        self.last_reason = self.status

    def settle(self, market_slug: str, outcome: Direction, now: datetime | None = None) -> None:
        now = now or utc_now()
        for position in self.positions.values():
            if position.market_slug != market_slug or position.status in {
                ChasePositionStatus.CLOSED,
                ChasePositionStatus.SETTLED,
            }:
                continue
            strategy_position = position.strategy_position
            payout = strategy_position.quantity if position.direction == outcome else 0.0
            pnl = payout - strategy_position.entry_quote - strategy_position.entry_fee_usd
            strategy_position.status = "SETTLED"
            strategy_position.realized_pnl = pnl
            strategy_position.closed_at = now
            strategy_position.exit_reason = "official_settlement"
            position.status = ChasePositionStatus.SETTLED
            position.official_outcome = outcome
            position.realized_pnl = pnl
            position.closed_at = now
            position.exit_reason = "official_settlement"
            self.registry.save_position(position)

    def unresolved_slugs(self) -> list[str]:
        return sorted(
            {
                position.market_slug
                for position in self.positions.values()
                if position.status
                not in {ChasePositionStatus.CLOSED, ChasePositionStatus.SETTLED}
            }
        )

    def control(self, action: str) -> bool:
        if action == "pause":
            self.paused = True
            self.registry.set_control("paused", True)
        elif action == "resume":
            self.paused = False
            self.emergency_stopped = False
            self.registry.set_control("paused", False)
            self.registry.set_control("emergency_stopped", False)
        elif action == "emergency_stop":
            self.emergency_stopped = True
            self.registry.set_control("emergency_stopped", True)
        else:
            return False
        self.registry.event("control", {"action": action})
        return True

    def dashboard_state(self) -> dict[str, Any]:
        settings = self.config.orderbook_chase
        source_round = self.source_v8.current_round
        return {
            "mode": "SHADOW_ONLY",
            "enabled": settings.enabled,
            "status": self.status,
            "last_reason": self.last_reason,
            "paused": self.paused,
            "emergency_stopped": self.emergency_stopped,
            "signer": {
                "ephemeral": True,
                "connected": self.signer_connected,
                "error": self.signer_error,
                "private_key_persisted": False,
                "post_order_available": False,
            },
            "config": settings.model_dump(mode="json"),
            "latency": self.latency.snapshot(),
            "strategy": {
                "status": self.source_v8.status,
                "last_reason": self.source_v8.last_reason,
                "round": (
                    {
                        "market_id": source_round.market_id,
                        "market_slug": source_round.market_slug,
                        "entry_count": source_round.entry_count,
                    }
                    if source_round is not None
                    else None
                ),
            },
            "signal_source": {
                "engine": "btc_v8",
                "shared_instance": True,
                "duplicate_v8_engine": False,
                "confirmed_buy_results": True,
                "lane_local_sell_state": True,
            },
            "positions": [
                item.model_dump(mode="json")
                for item in self.positions.values()
                if item.status not in {ChasePositionStatus.CLOSED, ChasePositionStatus.SETTLED}
            ],
            "recent_attempts": [
                item.model_dump(mode="json") for item in self.registry.recent_attempts(300)
            ],
            "recent_positions": [
                item.model_dump(mode="json") for item in self.registry.recent_positions(300)
            ],
            "summary": self.registry.summary(
                settings.readiness_required_roundtrips,
                settings.observed_survival_threshold,
                settings.p95_survival_threshold,
            ),
        }

    def drain_events(self) -> list[tuple[str, dict[str, Any]]]:
        events, self.events = self.events, []
        return events

    async def close(self) -> None:
        if self._signer_task is not None and not self._signer_task.done():
            self._signer_task.cancel()
            await asyncio.gather(self._signer_task, return_exceptions=True)
        await self.latency.close()
        await self.signer.close()
