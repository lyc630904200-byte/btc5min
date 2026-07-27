from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .config import AppConfig, BtcRecoveryConfig
from .models import Direction, MarketState, OrderBookSnapshot, OrderSide
from .orderbook import ExecutionResult, simulate_buy_quantity, simulate_sell


class RecoveryPhase(StrEnum):
    WAITING_ENTRY_WINDOW = "WAITING_ENTRY_WINDOW"
    OBSERVING_ENTRY = "OBSERVING_ENTRY"
    ENTRY_LOCKED = "ENTRY_LOCKED"
    INITIAL_OPEN = "INITIAL_OPEN"
    RECOVERY_OPEN = "RECOVERY_OPEN"
    PENDING_SETTLEMENT = "PENDING_SETTLEMENT"
    CLOSED = "CLOSED"
    NO_TRADE = "NO_TRADE"
    SKIPPED_RESTART = "SKIPPED_RESTART"


class RecoveryFill(BaseModel):
    order_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    order_number: int | None = None
    trade_order_number: int | None = None
    round_id: str
    market_id: str
    market_slug: str
    stage: str
    direction: Direction
    side: OrderSide
    avg_price: float
    quantity: float
    quote: float
    fee_usd: float
    levels_used: int
    reason: str
    created_at: datetime


class RecoveryRound(BaseModel):
    round_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    market_id: str
    market_slug: str
    start_time: datetime
    end_time: datetime
    settings: BtcRecoveryConfig
    phase: RecoveryPhase
    created_at: datetime
    updated_at: datetime
    entry_observation_started: bool = False
    locked_direction: Direction | None = None
    initial_stop_requested: bool = False
    initial_fill: RecoveryFill | None = None
    recovery_fill: RecoveryFill | None = None
    exit_fills: list[RecoveryFill] = Field(default_factory=list)
    official_outcome: Direction | None = None
    payout_usd: float = 0.0
    realized_pnl: float | None = None
    close_reason: str | None = None
    closed_at: datetime | None = None

    @property
    def recovery_direction(self) -> Direction | None:
        if self.locked_direction is None:
            return None
        return opposite_direction(self.locked_direction)


def opposite_direction(direction: Direction) -> Direction:
    return Direction.DOWN if direction == Direction.UP else Direction.UP


def simulate_buy_quantity_limit(
    book: OrderBookSnapshot,
    quantity: float,
    max_price: float,
    fee_rate: float,
) -> ExecutionResult:
    limited = book.model_copy(deep=True)
    limited.asks = [level for level in limited.asks if level.price <= max_price + 1e-12]
    return simulate_buy_quantity(limited, quantity, fee_rate)


def open_quantity(round_: RecoveryRound, direction: Direction) -> float:
    bought = sum(
        fill.quantity
        for fill in (round_.initial_fill, round_.recovery_fill)
        if fill is not None and fill.direction == direction
    )
    sold = sum(
        fill.quantity
        for fill in round_.exit_fills
        if fill.direction == direction and fill.side == OrderSide.SELL
    )
    return max(0.0, bought - sold)


def round_fees(round_: RecoveryRound) -> float:
    fills = [
        fill
        for fill in (round_.initial_fill, round_.recovery_fill)
        if fill is not None
    ] + list(round_.exit_fills)
    return sum(fill.fee_usd for fill in fills)


def round_quote(round_: RecoveryRound) -> float:
    fills = [
        fill
        for fill in (round_.initial_fill, round_.recovery_fill)
        if fill is not None
    ] + list(round_.exit_fills)
    return sum(fill.quote for fill in fills)


def round_cash_pnl(round_: RecoveryRound, payout: float | None = None) -> float:
    buys = [
        fill
        for fill in (round_.initial_fill, round_.recovery_fill)
        if fill is not None
    ]
    sell_proceeds = sum(fill.quote - fill.fee_usd for fill in round_.exit_fills)
    buy_cost = sum(fill.quote + fill.fee_usd for fill in buys)
    return sell_proceeds + (round_.payout_usd if payout is None else payout) - buy_cost


def direction_cash_pnl(
    round_: RecoveryRound,
    direction: Direction,
) -> float | None:
    buys = [
        fill
        for fill in (round_.initial_fill, round_.recovery_fill)
        if fill is not None and fill.direction == direction
    ]
    if not buys:
        return None
    remaining = open_quantity(round_, direction)
    if remaining > 1e-12 and round_.official_outcome is None:
        return None
    sells = [
        fill
        for fill in round_.exit_fills
        if fill.direction == direction and fill.side == OrderSide.SELL
    ]
    sell_proceeds = sum(fill.quote - fill.fee_usd for fill in sells)
    payout = (
        remaining
        if round_.official_outcome == direction
        else 0.0
    )
    buy_cost = sum(fill.quote + fill.fee_usd for fill in buys)
    return sell_proceeds + payout - buy_cost


def recovery_order_summaries(
    fills: list[RecoveryFill],
    rounds_by_id: dict[str, RecoveryRound],
) -> list[dict[str, Any]]:
    grouped: dict[int, list[RecoveryFill]] = {}
    for fill in fills:
        display_number = fill.trade_order_number or fill.order_number
        if display_number is None:
            continue
        grouped.setdefault(display_number, []).append(fill)

    summaries: list[dict[str, Any]] = []
    for display_number in sorted(grouped, reverse=True):
        order_fills = grouped[display_number]
        buy_fills = [
            fill for fill in order_fills if fill.side == OrderSide.BUY
        ]
        sell_fills = [
            fill for fill in order_fills if fill.side == OrderSide.SELL
        ]
        reference = buy_fills[0] if buy_fills else order_fills[0]
        related_round = rounds_by_id.get(reference.round_id)
        buy_quantity = sum(fill.quantity for fill in buy_fills)
        sell_quantity = sum(fill.quantity for fill in sell_fills)
        buy_quote = sum(fill.quote for fill in buy_fills)
        sell_quote = sum(fill.quote for fill in sell_fills)
        summaries.append(
            {
                "display_order_number": display_number,
                "round_id": reference.round_id,
                "market_id": reference.market_id,
                "market_slug": reference.market_slug,
                "direction": reference.direction.value,
                "buy_created_at": (
                    min(fill.created_at for fill in buy_fills).isoformat()
                    if buy_fills
                    else None
                ),
                "buy_avg_price": (
                    buy_quote / buy_quantity if buy_quantity > 1e-12 else None
                ),
                "buy_quote": buy_quote if buy_fills else None,
                "sell_created_at": (
                    max(fill.created_at for fill in sell_fills).isoformat()
                    if sell_fills
                    else None
                ),
                "sell_avg_price": (
                    sell_quote / sell_quantity if sell_quantity > 1e-12 else None
                ),
                "sell_quote": sell_quote if sell_fills else None,
                "quantity": buy_quantity if buy_fills else sell_quantity,
                "fee_usd": sum(fill.fee_usd for fill in order_fills),
                "official_outcome": (
                    related_round.official_outcome.value
                    if related_round is not None
                    and related_round.official_outcome is not None
                    else None
                ),
                "net_pnl": (
                    direction_cash_pnl(related_round, reference.direction)
                    if related_round is not None
                    else None
                ),
            }
        )
    return summaries


class BtcRecoveryRegistry:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS btc_recovery_rounds (
                round_id TEXT PRIMARY KEY,
                market_id TEXT NOT NULL UNIQUE,
                market_slug TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                phase TEXT NOT NULL,
                close_reason TEXT,
                realized_pnl REAL,
                payload_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS btc_recovery_fills (
                order_id TEXT PRIMARY KEY,
                order_number INTEGER NOT NULL UNIQUE,
                trade_order_number INTEGER,
                round_id TEXT NOT NULL,
                market_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(round_id) REFERENCES btc_recovery_rounds(round_id)
            )
            """
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS btc_recovery_pending_idx "
            "ON btc_recovery_rounds(phase, end_time)"
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS btc_recovery_controls (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        fill_columns = {
            str(row["name"])
            for row in self.connection.execute(
                "PRAGMA table_info(btc_recovery_fills)"
            ).fetchall()
        }
        if "trade_order_number" not in fill_columns:
            self.connection.execute(
                "ALTER TABLE btc_recovery_fills ADD COLUMN trade_order_number INTEGER"
            )
        self.connection.commit()
        self._backfill_trade_order_numbers()

    def _backfill_trade_order_numbers(self) -> None:
        rows = self.connection.execute(
            """
            SELECT order_id, round_id, payload_json
            FROM btc_recovery_fills
            ORDER BY order_number
            """
        ).fetchall()
        numbers: dict[tuple[str, str], int] = {}
        next_number = 1
        fill_updates: list[tuple[int, str, str]] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            key = (str(row["round_id"]), str(payload.get("direction") or ""))
            if str(payload.get("side") or "") == OrderSide.BUY.value and key not in numbers:
                numbers[key] = next_number
                next_number += 1
            trade_number = numbers.get(key)
            if trade_number is None:
                trade_number = next_number
                numbers[key] = trade_number
                next_number += 1
            payload["trade_order_number"] = trade_number
            fill_updates.append(
                (
                    trade_number,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    str(row["order_id"]),
                )
            )

        round_updates: list[tuple[str, str]] = []
        round_rows = self.connection.execute(
            "SELECT round_id, payload_json FROM btc_recovery_rounds"
        ).fetchall()
        for row in round_rows:
            payload = json.loads(row["payload_json"])
            embedded = [
                payload.get("initial_fill"),
                payload.get("recovery_fill"),
                *(payload.get("exit_fills") or []),
            ]
            changed = False
            for fill in embedded:
                if not isinstance(fill, dict):
                    continue
                key = (
                    str(row["round_id"]),
                    str(fill.get("direction") or ""),
                )
                trade_number = numbers.get(key)
                if trade_number is not None:
                    fill["trade_order_number"] = trade_number
                    changed = True
            if changed:
                round_updates.append(
                    (
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                        str(row["round_id"]),
                    )
                )

        with self.connection:
            self.connection.executemany(
                """
                UPDATE btc_recovery_fills
                SET trade_order_number = ?, payload_json = ?
                WHERE order_id = ?
                """,
                fill_updates,
            )
            self.connection.executemany(
                "UPDATE btc_recovery_rounds SET payload_json = ? WHERE round_id = ?",
                round_updates,
            )

    def close(self) -> None:
        self.connection.close()

    def control_value(self, key: str) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM btc_recovery_controls WHERE key = ?",
            (key,),
        ).fetchone()
        return str(row["value"]) if row is not None else None

    def set_control_value(
        self,
        key: str,
        value: str,
        updated_at: datetime,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO btc_recovery_controls (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, updated_at.isoformat()),
            )

    def control_enabled(self, key: str, default: bool = False) -> bool:
        value = self.control_value(key)
        if value is None:
            return default
        return value.lower() == "true"

    def set_control_enabled(
        self,
        key: str,
        enabled: bool,
        updated_at: datetime,
    ) -> None:
        self.set_control_value(
            key,
            "true" if enabled else "false",
            updated_at,
        )

    def statistics_reset_at(self) -> datetime | None:
        value = self.control_value("statistics_reset_at")
        if value is None:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    def reset_statistics(self, reset_at: datetime) -> None:
        self.set_control_value(
            "statistics_reset_at",
            reset_at.isoformat(),
            reset_at,
        )

    def _row_to_round(self, row: sqlite3.Row) -> RecoveryRound:
        return RecoveryRound.model_validate(json.loads(row["payload_json"]))

    def _row_to_fill(self, row: sqlite3.Row) -> RecoveryFill:
        return RecoveryFill.model_validate(json.loads(row["payload_json"]))

    def get_market_round(self, market_id: str) -> RecoveryRound | None:
        row = self.connection.execute(
            "SELECT * FROM btc_recovery_rounds WHERE market_id = ?",
            (market_id,),
        ).fetchone()
        return self._row_to_round(row) if row else None

    def save_round(self, round_: RecoveryRound) -> None:
        payload = round_.model_dump(mode="json")
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO btc_recovery_rounds (
                    round_id, market_id, market_slug, start_time, end_time, phase,
                    close_reason, realized_pnl, payload_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(market_id) DO UPDATE SET
                    phase = excluded.phase,
                    close_reason = excluded.close_reason,
                    realized_pnl = excluded.realized_pnl,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    payload["round_id"],
                    payload["market_id"],
                    payload["market_slug"],
                    payload["start_time"],
                    payload["end_time"],
                    payload["phase"],
                    payload["close_reason"],
                    payload["realized_pnl"],
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    payload["updated_at"],
                ),
            )

    def record_transition(
        self,
        round_: RecoveryRound,
        fills: list[RecoveryFill],
    ) -> list[RecoveryFill]:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            next_row = self.connection.execute(
                "SELECT COALESCE(MAX(order_number), 0) + 1 AS value FROM btc_recovery_fills"
            ).fetchone()
            next_number = int(next_row["value"] if next_row else 1)
            next_trade_row = self.connection.execute(
                """
                SELECT COALESCE(MAX(trade_order_number), 0) + 1 AS value
                FROM btc_recovery_fills
                """
            ).fetchone()
            next_trade_number = int(next_trade_row["value"] if next_trade_row else 1)
            for fill in fills:
                fill.order_number = next_number
                if fill.trade_order_number is None:
                    fill.trade_order_number = next_trade_number
                    next_trade_number += 1
                next_number += 1
                payload = fill.model_dump(mode="json")
                self.connection.execute(
                    """
                    INSERT INTO btc_recovery_fills (
                        order_id, order_number, trade_order_number, round_id,
                        market_id, created_at, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        payload["order_id"],
                        payload["order_number"],
                        payload["trade_order_number"],
                        payload["round_id"],
                        payload["market_id"],
                        payload["created_at"],
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    ),
                )
            payload = round_.model_dump(mode="json")
            self.connection.execute(
                """
                INSERT INTO btc_recovery_rounds (
                    round_id, market_id, market_slug, start_time, end_time, phase,
                    close_reason, realized_pnl, payload_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(market_id) DO UPDATE SET
                    phase = excluded.phase,
                    close_reason = excluded.close_reason,
                    realized_pnl = excluded.realized_pnl,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    payload["round_id"],
                    payload["market_id"],
                    payload["market_slug"],
                    payload["start_time"],
                    payload["end_time"],
                    payload["phase"],
                    payload["close_reason"],
                    payload["realized_pnl"],
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    payload["updated_at"],
                ),
            )
            self.connection.commit()
            return fills
        except sqlite3.IntegrityError:
            self.connection.rollback()
            return []
        except Exception:
            self.connection.rollback()
            raise

    def recent_fills(self, limit: int = 100) -> list[RecoveryFill]:
        rows = self.connection.execute(
            "SELECT * FROM btc_recovery_fills ORDER BY order_number DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_fill(row) for row in rows]

    def recent_order_fills(self, limit: int = 100) -> list[RecoveryFill]:
        rows = self.connection.execute(
            """
            WITH recent_orders AS (
                SELECT
                    COALESCE(trade_order_number, order_number)
                        AS display_order_number
                FROM btc_recovery_fills
                GROUP BY COALESCE(trade_order_number, order_number)
                ORDER BY display_order_number DESC
                LIMIT ?
            )
            SELECT fills.*
            FROM btc_recovery_fills AS fills
            JOIN recent_orders
              ON COALESCE(fills.trade_order_number, fills.order_number)
                 = recent_orders.display_order_number
            ORDER BY recent_orders.display_order_number DESC, fills.order_number
            """,
            (limit,),
        ).fetchall()
        return [self._row_to_fill(row) for row in rows]

    def rounds_by_ids(self, round_ids: set[str]) -> list[RecoveryRound]:
        if not round_ids:
            return []
        placeholders = ",".join("?" for _ in round_ids)
        rows = self.connection.execute(
            f"""
            SELECT * FROM btc_recovery_rounds
            WHERE round_id IN ({placeholders})
            """,
            tuple(round_ids),
        ).fetchall()
        return [self._row_to_round(row) for row in rows]

    def recent_rounds(self, limit: int = 20) -> list[RecoveryRound]:
        rows = self.connection.execute(
            "SELECT * FROM btc_recovery_rounds ORDER BY start_time DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_round(row) for row in rows]

    def unresolved_slugs(self, limit: int = 50) -> list[str]:
        rows = self.connection.execute(
            """
            SELECT payload_json FROM btc_recovery_rounds
            WHERE end_time <= ?
            ORDER BY end_time DESC
            """,
            (datetime.now(timezone.utc).isoformat(),),
        ).fetchall()
        slugs: list[str] = []
        for row in rows:
            round_ = RecoveryRound.model_validate(json.loads(row["payload_json"]))
            if round_.official_outcome is not None:
                continue
            slugs.append(round_.market_slug)
            if len(slugs) >= limit:
                break
        return slugs

    def record_official_outcome(
        self,
        market_slug: str,
        outcome: Direction,
        settled_at: datetime,
    ) -> RecoveryRound | None:
        row = self.connection.execute(
            """
            SELECT * FROM btc_recovery_rounds
            WHERE market_slug = ?
            """,
            (market_slug,),
        ).fetchone()
        if not row:
            return None
        round_ = self._row_to_round(row)
        if round_.official_outcome is not None:
            return None
        round_.official_outcome = outcome
        if round_.phase == RecoveryPhase.PENDING_SETTLEMENT:
            payout = open_quantity(round_, outcome)
            round_.payout_usd = payout
            round_.realized_pnl = round_cash_pnl(round_, payout)
            round_.phase = RecoveryPhase.CLOSED
            round_.close_reason = "official_settlement"
            round_.closed_at = settled_at
        round_.updated_at = settled_at
        self.save_round(round_)
        return round_

    def settle(
        self,
        market_slug: str,
        outcome: Direction,
        settled_at: datetime,
    ) -> RecoveryRound | None:
        return self.record_official_outcome(market_slug, outcome, settled_at)

    def summary(self) -> dict[str, float | int | str | None]:
        rows = self.connection.execute(
            "SELECT payload_json FROM btc_recovery_rounds"
        ).fetchall()
        rounds = [
            RecoveryRound.model_validate(json.loads(row["payload_json"]))
            for row in rows
        ]
        reset_at = self.statistics_reset_at()
        if reset_at is not None:
            rounds = [round_ for round_ in rounds if round_.created_at >= reset_at]
        closed = [
            round_
            for round_ in rounds
            if round_.phase == RecoveryPhase.CLOSED and round_.realized_pnl is not None
        ]
        wins = sum((round_.realized_pnl or 0.0) > 0 for round_ in closed)
        return {
            "observed_markets": len(rounds),
            "no_trade_markets": sum(
                round_.phase in {RecoveryPhase.NO_TRADE, RecoveryPhase.SKIPPED_RESTART}
                for round_ in rounds
            ),
            "initial_orders": sum(round_.initial_fill is not None for round_ in rounds),
            "recovery_orders": sum(round_.recovery_fill is not None for round_ in rounds),
            "direct_target_exits": sum(round_.close_reason == "direct_target" for round_ in rounds),
            "recovery_target_exits": sum(
                round_.close_reason == "recovery_target" for round_ in rounds
            ),
            "stop_exits": sum(
                round_.close_reason in {"initial_stop", "recovery_stop"}
                for round_ in rounds
            ),
            "timed_exits": sum(round_.close_reason == "timed_exit" for round_ in rounds),
            "official_settlements": sum(
                round_.close_reason == "official_settlement" for round_ in rounds
            ),
            "official_outcomes_recorded": sum(
                round_.official_outcome is not None for round_ in rounds
            ),
            "pending_settlements": sum(
                round_.phase == RecoveryPhase.PENDING_SETTLEMENT for round_ in rounds
            ),
            "completed_markets": len(closed),
            "winning_markets": wins,
            "win_rate": wins / len(closed) if closed else 0.0,
            "total_quote_usd": sum(round_quote(round_) for round_ in rounds),
            "fees_usd": sum(round_fees(round_) for round_ in rounds),
            "realized_pnl": sum(round_.realized_pnl or 0.0 for round_ in closed),
            "statistics_reset_at": reset_at.isoformat() if reset_at is not None else None,
        }


class BtcRecoveryEngine:
    def __init__(self, config: AppConfig, registry: BtcRecoveryRegistry):
        self.config = config
        self.registry = registry
        self.recovery_orders_stopped = registry.control_enabled(
            "recovery_orders_stopped"
        )
        self.current_round: RecoveryRound | None = None
        self.current_market: MarketState | None = None
        self.status = "disabled" if not config.btc_recovery.enabled else "waiting_for_btc_market"
        self.last_reason = self.status
        self._events: list[tuple[str, Any]] = []
        self.refresh_history()

    def refresh_history(self) -> None:
        self._recent_fills = self.registry.recent_order_fills(300)
        self._recent_rounds = self.registry.recent_rounds(20)
        self._rounds_by_id = {
            round_.round_id: round_
            for round_ in self.registry.rounds_by_ids(
                {fill.round_id for fill in self._recent_fills}
            )
        }
        self._recent_order_summaries = recovery_order_summaries(
            self._recent_fills,
            self._rounds_by_id,
        )
        self._recent_round_payloads = [
            item.model_dump(mode="json") for item in self._recent_rounds
        ]
        self._summary = self.registry.summary()

    def drain_events(self) -> list[tuple[str, Any]]:
        events = list(self._events)
        self._events.clear()
        return events

    def _emit(self, event_type: str, payload: Any) -> None:
        self._events.append((event_type, payload))

    def set_recovery_orders_stopped(
        self,
        stopped: bool,
        now: datetime | None = None,
    ) -> None:
        now = now or datetime.now(timezone.utc)
        self.recovery_orders_stopped = stopped
        self.registry.set_control_enabled(
            "recovery_orders_stopped",
            stopped,
            now,
        )
        round_ = self.current_round
        if (
            not stopped
            and round_ is not None
            and round_.initial_stop_requested
            and round_.recovery_fill is None
            and round_.phase == RecoveryPhase.INITIAL_OPEN
        ):
            round_.initial_stop_requested = False
            round_.updated_at = now
            self.registry.save_round(round_)
        if stopped:
            self.last_reason = "recovery_orders_stopped"
        elif self.last_reason == "recovery_orders_stopped":
            self.last_reason = self.status
        self._emit(
            "btc_recovery_control",
            {
                "recovery_orders_stopped": stopped,
                "updated_at": now,
            },
        )

    def reset_statistics(self, now: datetime | None = None) -> datetime:
        now = now or datetime.now(timezone.utc)
        self.registry.reset_statistics(now)
        self.refresh_history()
        self._emit(
            "btc_recovery_statistics_reset",
            {"statistics_reset_at": now},
        )
        return now

    def set_market(self, market: MarketState, now: datetime | None = None) -> None:
        if market.asset.upper() != "BTC":
            return
        now = now or datetime.now(timezone.utc)
        if self.current_market and self.current_market.condition_id == market.condition_id:
            self.current_market = market
            return
        if self.current_round and self.current_round.phase not in {
            RecoveryPhase.CLOSED,
            RecoveryPhase.NO_TRADE,
            RecoveryPhase.SKIPPED_RESTART,
            RecoveryPhase.PENDING_SETTLEMENT,
        }:
            self._finish_at_expiry(now)
        self.current_market = market
        existing = self.registry.get_market_round(market.condition_id)
        if existing is not None:
            self.current_round = existing
            self.status = existing.phase.value.lower()
            self.last_reason = self.status
            self.refresh_history()
            return
        if not self.config.btc_recovery.enabled or market.start_time is None:
            self.current_round = None
            self.status = "disabled" if not self.config.btc_recovery.enabled else "market_start_unavailable"
            self.last_reason = self.status
            return
        settings = self.config.btc_recovery.model_copy(deep=True)
        entry_at = market.start_time + timedelta(seconds=settings.entry_seconds_after_open)
        late_tolerance = timedelta(
            seconds=max(2.0, self.config.risk.max_data_age_ms / 1000)
        )
        if now > entry_at + late_tolerance:
            phase = RecoveryPhase.SKIPPED_RESTART
            close_reason = "started_after_entry_window_began"
            closed_at = now
        else:
            phase = RecoveryPhase.WAITING_ENTRY_WINDOW
            close_reason = None
            closed_at = None
        round_ = RecoveryRound(
            market_id=market.condition_id,
            market_slug=market.slug,
            start_time=market.start_time,
            end_time=market.end_time,
            settings=settings,
            phase=phase,
            close_reason=close_reason,
            closed_at=closed_at,
            created_at=now,
            updated_at=now,
        )
        self.registry.save_round(round_)
        self.current_round = round_
        self.status = phase.value.lower()
        self.last_reason = self.status
        self.refresh_history()
        self._emit("btc_recovery_round", round_)

    def unresolved_slugs(self, limit: int = 50) -> list[str]:
        return self.registry.unresolved_slugs(limit)

    def pending_slugs(self) -> list[str]:
        return self.unresolved_slugs()

    def settle(
        self,
        market_slug: str,
        outcome: Direction,
        now: datetime | None = None,
    ) -> RecoveryRound | None:
        settled = self.registry.settle(
            market_slug,
            outcome,
            now or datetime.now(timezone.utc),
        )
        if settled is None:
            return None
        if self.current_round and self.current_round.round_id == settled.round_id:
            self.current_round = settled
        if (
            self.current_round
            and self.current_round.round_id == settled.round_id
            and settled.close_reason == "official_settlement"
        ):
            self.status = "official_settlement"
            self.last_reason = self.status
        self.refresh_history()
        self._emit("btc_recovery_result", settled)
        return settled

    def _book_ready(
        self,
        book: OrderBookSnapshot | None,
        now: datetime,
    ) -> tuple[bool, str]:
        if book is None:
            return False, "book_unavailable"
        age = (now - book.received_at).total_seconds()
        if age < 0 or age > self.config.risk.max_data_age_ms / 1000:
            return False, "book_stale"
        if not book.depth_trusted:
            return False, "book_depth_untrusted"
        return True, "ready"

    def _buy_fill(
        self,
        round_: RecoveryRound,
        market: MarketState,
        direction: Direction,
        book: OrderBookSnapshot,
        quantity: float,
        max_price: float | None,
        stage: str,
        reason: str,
        now: datetime,
    ) -> RecoveryFill | None:
        ready, why = self._book_ready(book, now)
        if not ready:
            self.last_reason = why
            return None
        if quantity < max(market.min_order_size, book.min_order_size):
            self.last_reason = "below_min_order_size"
            return None
        if max_price is None:
            result = simulate_buy_quantity(
                book,
                quantity,
                self.config.strategy.taker_fee_rate,
            )
        else:
            result = simulate_buy_quantity_limit(
                book,
                quantity,
                max_price,
                self.config.strategy.taker_fee_rate,
            )
        if not result.complete:
            self.last_reason = (
                "insufficient_buy_depth"
                if max_price is None
                else "strict_limit_or_depth_unavailable"
            )
            return None
        if max_price is not None and result.avg_price > max_price + 1e-12:
            self.last_reason = "strict_limit_or_depth_unavailable"
            return None
        return RecoveryFill(
            round_id=round_.round_id,
            market_id=round_.market_id,
            market_slug=round_.market_slug,
            stage=stage,
            direction=direction,
            side=OrderSide.BUY,
            avg_price=result.avg_price,
            quantity=result.quantity,
            quote=result.quote,
            fee_usd=result.fee_usd,
            levels_used=result.levels_used,
            reason=reason,
            created_at=now,
        )

    def _sell_result(
        self,
        direction: Direction,
        books: dict[Direction, OrderBookSnapshot],
        quantity: float,
        now: datetime,
    ) -> tuple[ExecutionResult | None, str]:
        book = books.get(direction)
        ready, why = self._book_ready(book, now)
        if not ready or book is None:
            return None, why
        result = simulate_sell(book, quantity, self.config.strategy.taker_fee_rate)
        if not result.complete:
            return None, "insufficient_sell_depth"
        return result, "ready"

    def _sell_fill(
        self,
        round_: RecoveryRound,
        direction: Direction,
        result: ExecutionResult,
        stage: str,
        reason: str,
        now: datetime,
    ) -> RecoveryFill:
        entry = next(
            (
                fill
                for fill in (round_.initial_fill, round_.recovery_fill)
                if fill is not None and fill.direction == direction
            ),
            None,
        )
        return RecoveryFill(
            trade_order_number=(
                entry.trade_order_number or entry.order_number
                if entry is not None
                else None
            ),
            round_id=round_.round_id,
            market_id=round_.market_id,
            market_slug=round_.market_slug,
            stage=stage,
            direction=direction,
            side=OrderSide.SELL,
            avg_price=result.avg_price,
            quantity=result.quantity,
            quote=result.quote,
            fee_usd=result.fee_usd,
            levels_used=result.levels_used,
            reason=reason,
            created_at=now,
        )

    def _projected_exit(
        self,
        round_: RecoveryRound,
        books: dict[Direction, OrderBookSnapshot],
        now: datetime,
    ) -> tuple[dict[Direction, ExecutionResult], float | None, str]:
        results: dict[Direction, ExecutionResult] = {}
        has_open_position = False
        for direction in (Direction.UP, Direction.DOWN):
            quantity = open_quantity(round_, direction)
            if quantity <= 1e-12:
                continue
            has_open_position = True
            result, reason = self._sell_result(direction, books, quantity, now)
            if result is None:
                return {}, None, reason
            results[direction] = result
        if not has_open_position:
            return {}, None, "no_open_position"
        projected = round_cash_pnl(round_) + sum(
            result.quote - result.fee_usd for result in results.values()
        )
        return results, projected, "ready"

    def _record_buys(
        self,
        round_: RecoveryRound,
        fills: list[RecoveryFill],
        phase: RecoveryPhase,
        now: datetime,
    ) -> bool:
        if not fills:
            return False
        if phase == RecoveryPhase.INITIAL_OPEN:
            round_.initial_fill = fills[0]
        elif phase == RecoveryPhase.RECOVERY_OPEN:
            round_.recovery_fill = fills[0]
        round_.phase = phase
        round_.updated_at = now
        recorded = self.registry.record_transition(round_, fills)
        if not recorded:
            self.last_reason = "duplicate_transition"
            return False
        for fill in recorded:
            self._emit("btc_recovery_fill", fill)
        self.refresh_history()
        return True

    def _close_with_sells(
        self,
        round_: RecoveryRound,
        results: dict[Direction, ExecutionResult],
        reason: str,
        now: datetime,
    ) -> bool:
        fills = [
            self._sell_fill(round_, direction, result, "exit", reason, now)
            for direction, result in results.items()
        ]
        if not fills:
            return False
        round_.exit_fills.extend(fills)
        round_.phase = RecoveryPhase.CLOSED
        round_.close_reason = reason
        round_.closed_at = now
        round_.updated_at = now
        round_.realized_pnl = round_cash_pnl(round_)
        recorded = self.registry.record_transition(round_, fills)
        if not recorded:
            self.last_reason = "duplicate_transition"
            return False
        self.status = reason
        self.last_reason = reason
        for fill in recorded:
            self._emit("btc_recovery_fill", fill)
        self._emit("btc_recovery_result", round_)
        self.refresh_history()
        return True

    def _finish_at_expiry(self, now: datetime) -> None:
        round_ = self.current_round
        if round_ is None or round_.phase in {
            RecoveryPhase.CLOSED,
            RecoveryPhase.NO_TRADE,
            RecoveryPhase.SKIPPED_RESTART,
            RecoveryPhase.PENDING_SETTLEMENT,
        }:
            return
        if round_.initial_fill is None:
            round_.phase = RecoveryPhase.NO_TRADE
            round_.close_reason = "no_trade"
            round_.closed_at = now
        else:
            round_.phase = RecoveryPhase.PENDING_SETTLEMENT
            round_.close_reason = "awaiting_official_settlement"
        round_.updated_at = now
        self.registry.save_round(round_)
        self.status = round_.phase.value.lower()
        self.last_reason = self.status
        self.refresh_history()
        self._emit("btc_recovery_round", round_)

    def evaluate(
        self,
        market: MarketState | None,
        books: dict[Direction, OrderBookSnapshot],
        now: datetime | None = None,
    ) -> None:
        now = now or datetime.now(timezone.utc)
        if market is None or market.asset.upper() != "BTC":
            self.status = "waiting_for_btc_market"
            self.last_reason = self.status
            return
        if self.current_market is None or self.current_market.condition_id != market.condition_id:
            self.set_market(market, now)
        round_ = self.current_round
        if round_ is None:
            self.status = "disabled" if not self.config.btc_recovery.enabled else "waiting_for_round"
            self.last_reason = self.status
            return
        if round_.phase in {
            RecoveryPhase.CLOSED,
            RecoveryPhase.NO_TRADE,
            RecoveryPhase.SKIPPED_RESTART,
            RecoveryPhase.PENDING_SETTLEMENT,
        }:
            self.status = round_.phase.value.lower()
            self.last_reason = self.status
            return
        if now >= round_.end_time:
            self._finish_at_expiry(now)
            return

        elapsed = (now - round_.start_time).total_seconds()
        settings = round_.settings
        exit_window_closed = elapsed >= settings.exit_seconds_after_open
        hold_initial_for_official = (
            round_.initial_fill is not None
            and round_.recovery_fill is None
            and settings.target_price_cents >= 100.0
        )
        if exit_window_closed:
            if round_.initial_fill is None:
                round_.phase = RecoveryPhase.NO_TRADE
                round_.close_reason = "entry_window_closed"
                round_.closed_at = now
                round_.updated_at = now
                self.registry.save_round(round_)
                self.status = "no_trade"
                self.last_reason = self.status
                self.refresh_history()
                self._emit("btc_recovery_result", round_)
                return
            if not hold_initial_for_official:
                results, _, reason = self._projected_exit(round_, books, now)
                if results:
                    self._close_with_sells(round_, results, "timed_exit", now)
                else:
                    self.status = "timed_exit_waiting_for_depth"
                    self.last_reason = reason
                return
        if elapsed < settings.entry_seconds_after_open:
            self.status = "waiting_entry_window"
            self.last_reason = self.status
            return

        entry_books: dict[Direction, OrderBookSnapshot] = {}
        if round_.initial_fill is None and round_.locked_direction is None:
            for direction in (Direction.UP, Direction.DOWN):
                book = books.get(direction)
                ready, reason = self._book_ready(book, now)
                if not ready or book is None:
                    self.status = "entry_books_waiting"
                    self.last_reason = reason
                    return
                entry_books[direction] = book

        if not round_.entry_observation_started:
            round_.entry_observation_started = True
            round_.phase = RecoveryPhase.OBSERVING_ENTRY
            round_.updated_at = now
            self.registry.save_round(round_)

        entry_limit = settings.entry_price_cents / 100.0
        max_entry_price = settings.max_entry_price_cents / 100.0
        if round_.initial_fill is None:
            if round_.locked_direction is None:
                candidates: list[tuple[datetime, str, Direction]] = []
                price_above_limit = False
                for direction in (Direction.UP, Direction.DOWN):
                    book = entry_books[direction]
                    ask = book.best_ask
                    if ask is not None and ask > max_entry_price:
                        price_above_limit = True
                    elif ask is not None and ask > entry_limit:
                        candidates.append(
                            (book.received_at, direction.value, direction)
                        )
                if candidates:
                    candidates.sort()
                    round_.locked_direction = candidates[0][2]
                    round_.phase = RecoveryPhase.ENTRY_LOCKED
                    round_.updated_at = now
                    self.registry.save_round(round_)
                    self.status = "entry_direction_locked"
                    self.last_reason = self.status
                else:
                    self.status = "observing_entry"
                    self.last_reason = (
                        "entry_price_above_limit"
                        if price_above_limit
                        else self.status
                    )
                    return
            direction = round_.locked_direction
            assert direction is not None
            book = books.get(direction)
            ready, reason = self._book_ready(book, now)
            if not ready or book is None:
                self.status = "entry_direction_locked"
                self.last_reason = reason
                return
            if book.best_ask is None or book.best_ask <= entry_limit:
                round_.locked_direction = None
                round_.phase = RecoveryPhase.OBSERVING_ENTRY
                round_.updated_at = now
                self.registry.save_round(round_)
                self.status = "observing_entry"
                self.last_reason = "entry_price_not_above_threshold"
                return
            if book.best_ask > max_entry_price:
                self.status = "entry_direction_locked"
                self.last_reason = "entry_price_above_limit"
                return
            fill = self._buy_fill(
                round_,
                market,
                direction,
                book,
                settings.initial_quantity,
                max_entry_price,
                "initial",
                "initial_entry",
                now,
            )
            if fill is not None and self._record_buys(
                round_, [fill], RecoveryPhase.INITIAL_OPEN, now
            ):
                self.status = "initial_open"
                self.last_reason = self.status
            return

        results, projected, projected_reason = self._projected_exit(round_, books, now)
        initial_direction = round_.initial_fill.direction
        if round_.recovery_fill is None:
            if not self.recovery_orders_stopped and round_.initial_stop_requested:
                round_.initial_stop_requested = False
                round_.updated_at = now
                self.registry.save_round(round_)
            initial_sell = results.get(initial_direction)
            if (
                settings.target_price_cents < 100.0
                and initial_sell is not None
                and initial_sell.avg_price >= settings.target_price_cents / 100.0
                and projected is not None
                and projected > 0
            ):
                self._close_with_sells(round_, results, "direct_target", now)
                return
            if self.recovery_orders_stopped and round_.initial_stop_requested:
                if initial_sell is not None:
                    self._close_with_sells(round_, results, "initial_stop", now)
                else:
                    self.status = "initial_stop_waiting_for_depth"
                    self.last_reason = self.status
                return
            if (
                settings.recovery_trigger_cents > 0.0
                and initial_sell is not None
                and initial_sell.avg_price <= settings.recovery_trigger_cents / 100.0
            ):
                if self.recovery_orders_stopped:
                    round_.initial_stop_requested = True
                    self._close_with_sells(round_, results, "initial_stop", now)
                    return
                if exit_window_closed:
                    self.status = "holding_for_official_settlement"
                    self.last_reason = self.status
                    return
                recovery_direction = opposite_direction(initial_direction)
                recovery_book = books.get(recovery_direction)
                if recovery_book is not None:
                    fill = self._buy_fill(
                        round_,
                        market,
                        recovery_direction,
                        recovery_book,
                        settings.recovery_quantity,
                        None,
                        "recovery",
                        "recovery_entry",
                        now,
                    )
                    if fill is not None and self._record_buys(
                        round_, [fill], RecoveryPhase.RECOVERY_OPEN, now
                    ):
                        self.status = "recovery_open"
                        self.last_reason = self.status
                        return
            elif self.recovery_orders_stopped and initial_sell is None:
                initial_book = books.get(initial_direction)
                ready, _ = self._book_ready(initial_book, now)
                if (
                    settings.recovery_trigger_cents > 0.0
                    and ready
                    and initial_book is not None
                    and initial_book.best_bid is not None
                    and initial_book.best_bid
                    <= settings.recovery_trigger_cents / 100.0
                ):
                    round_.initial_stop_requested = True
                    round_.updated_at = now
                    self.registry.save_round(round_)
                    self.status = "initial_stop_waiting_for_depth"
                    self.last_reason = self.status
                    return
            self.status = (
                "holding_for_official_settlement"
                if exit_window_closed
                else "initial_open"
            )
            self.last_reason = projected_reason if projected is None else self.status
            return

        recovery_direction = round_.recovery_fill.direction
        recovery_sell = results.get(recovery_direction)
        if (
            recovery_sell is not None
            and recovery_sell.avg_price
            >= settings.recovery_target_price_cents / 100.0
            and projected is not None
            and projected > 0
        ):
            self._close_with_sells(round_, results, "recovery_target", now)
            return
        if (
            recovery_sell is not None
            and recovery_sell.avg_price <= settings.stop_price_cents / 100.0
            and len(results) == 2
        ):
            self._close_with_sells(round_, results, "recovery_stop", now)
            return
        self.status = "recovery_open"
        self.last_reason = projected_reason if projected is None else self.status

    def dashboard_state(
        self,
        books: dict[Direction, OrderBookSnapshot] | None = None,
        now: datetime | None = None,
        pair_paused: bool = False,
    ) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        books = books or {}
        round_ = self.current_round
        positions: dict[str, dict[str, Any]] = {}
        projected_results: dict[Direction, ExecutionResult] = {}
        projected_pnl: float | None = None
        projected_reason = "no_open_position"
        if round_ is not None:
            projected_results, projected_pnl, projected_reason = self._projected_exit(
                round_, books, now
            )
            for direction in (Direction.UP, Direction.DOWN):
                quantity = open_quantity(round_, direction)
                if quantity <= 1e-12:
                    continue
                entry = next(
                    (
                        fill
                        for fill in (round_.initial_fill, round_.recovery_fill)
                        if fill is not None and fill.direction == direction
                    ),
                    None,
                )
                result = projected_results.get(direction)
                positions[direction.value] = {
                    "quantity": quantity,
                    "entry_avg_price": entry.avg_price if entry else None,
                    "entry_quote": entry.quote if entry else None,
                    "entry_fee_usd": entry.fee_usd if entry else None,
                    "sell_avg_price": result.avg_price if result else None,
                    "sell_quote": result.quote if result else None,
                    "sell_fee_usd": result.fee_usd if result else None,
                }
        up_sell = projected_results.get(Direction.UP)
        down_sell = projected_results.get(Direction.DOWN)
        yes_no_sum = (
            up_sell.avg_price + down_sell.avg_price
            if up_sell is not None and down_sell is not None
            else None
        )
        settings = (
            round_.settings.model_dump(mode="json")
            if round_ is not None
            else self.config.btc_recovery.model_dump(mode="json")
        )
        return {
            "status": self.status,
            "last_reason": self.last_reason,
            "pair_match_paused": pair_paused,
            "recovery_orders_stopped": self.recovery_orders_stopped,
            "config": settings,
            "round": round_.model_dump(mode="json") if round_ else None,
            "positions": positions,
            "arbitrage_check": {
                "yes_sell_avg": up_sell.avg_price if up_sell else None,
                "no_sell_avg": down_sell.avg_price if down_sell else None,
                "yes_no_sell_sum": yes_no_sum,
                "projected_exit_fees_usd": (
                    sum(result.fee_usd for result in projected_results.values())
                    if projected_results
                    else None
                ),
                "projected_net_pnl": projected_pnl,
                "reason": projected_reason,
            },
            "summary": self._summary,
            "recent_orders": self._recent_order_summaries,
            "recent_rounds": self._recent_round_payloads,
        }
