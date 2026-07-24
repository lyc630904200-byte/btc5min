from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from .config import AppConfig
from .market import market_is_active
from .models import Direction, MarketState, OrderBookSnapshot
from .orderbook import ExecutionResult, simulate_buy_quantity

if TYPE_CHECKING:
    from .engine import PaperEngine


class PairDirection(StrEnum):
    BTC_UP_ETH_DOWN = "BTC_UP_ETH_DOWN"
    BTC_DOWN_ETH_UP = "BTC_DOWN_ETH_UP"

    @property
    def opposite(self) -> "PairDirection":
        if self == PairDirection.BTC_UP_ETH_DOWN:
            return PairDirection.BTC_DOWN_ETH_UP
        return PairDirection.BTC_UP_ETH_DOWN


class PairLeg(BaseModel):
    asset: str
    market_id: str
    market_slug: str
    token_id: str
    direction: Direction
    avg_price: float
    quantity: float
    quote: float
    fee_usd: float
    slippage: float
    levels_used: int


class PairCandidate(BaseModel):
    direction: PairDirection
    available: bool
    meets_spread: bool = False
    meets_leg_price_gap: bool = False
    reason: str
    spread_cents: float | None = None
    leg_price_gap_cents: float | None = None
    btc_leg: PairLeg | None = None
    eth_leg: PairLeg | None = None
    total_cost_usd: float | None = None
    scenario_pnl: dict[str, float] = Field(default_factory=dict)


class PairOrder(BaseModel):
    order_id: str
    order_number: int | None = None
    interval_key: str
    start_time: datetime
    end_time: datetime
    direction: PairDirection
    fingerprint: str
    opened_at: datetime
    btc_leg: PairLeg
    eth_leg: PairLeg
    spread_cents: float
    total_cost_usd: float
    scenario_pnl: dict[str, float]
    status: str = "PENDING"
    btc_outcome: Direction | None = None
    eth_outcome: Direction | None = None
    payout_usd: float | None = None
    realized_pnl: float | None = None
    settled_at: datetime | None = None


def pair_interval_key(btc_market: MarketState, eth_market: MarketState) -> str:
    assert btc_market.start_time is not None
    return f"{int(btc_market.start_time.timestamp())}:{btc_market.condition_id}:{eth_market.condition_id}"


def pair_scenario_pnl(btc_leg: PairLeg, eth_leg: PairLeg) -> dict[str, float]:
    total_cost = btc_leg.quote + btc_leg.fee_usd + eth_leg.quote + eth_leg.fee_usd
    return {
        "both_lose": -total_cost,
        "btc_only_wins": btc_leg.quantity - total_cost,
        "eth_only_wins": eth_leg.quantity - total_cost,
        "both_win": btc_leg.quantity + eth_leg.quantity - total_cost,
    }


def spread_cents(btc_fill: ExecutionResult, eth_fill: ExecutionResult) -> float | None:
    if btc_fill.quantity <= 0 or eth_fill.quantity <= 0:
        return None
    btc_fee_per_share = btc_fill.fee_usd / btc_fill.quantity
    eth_fee_per_share = eth_fill.fee_usd / eth_fill.quantity
    return 100.0 * (
        1.0 - btc_fill.avg_price - eth_fill.avg_price - btc_fee_per_share - eth_fee_per_share
    )


def simulate_equal_quantity_buys(
    btc_book: OrderBookSnapshot,
    eth_book: OrderBookSnapshot,
    total_quote_usd: float,
    fee_rate: float,
) -> tuple[ExecutionResult, ExecutionResult]:
    """Spend a combined quote budget while buying exactly the same shares on both legs."""
    btc_asks = sorted(
        [level for level in btc_book.asks if level.price > 0 and level.size > 0],
        key=lambda level: level.price,
    )
    eth_asks = sorted(
        [level for level in eth_book.asks if level.price > 0 and level.size > 0],
        key=lambda level: level.price,
    )
    if total_quote_usd <= 0 or not btc_asks or not eth_asks:
        return (
            simulate_buy_quantity(btc_book, 0, fee_rate),
            simulate_buy_quantity(eth_book, 0, fee_rate),
        )

    btc_index = 0
    eth_index = 0
    btc_remaining = btc_asks[0].size
    eth_remaining = eth_asks[0].size
    remaining_quote = total_quote_usd
    quantity = 0.0
    while remaining_quote > 1e-9 and btc_index < len(btc_asks) and eth_index < len(eth_asks):
        unit_quote = btc_asks[btc_index].price + eth_asks[eth_index].price
        available_quantity = min(btc_remaining, eth_remaining)
        take_quantity = min(available_quantity, remaining_quote / unit_quote)
        quantity += take_quantity
        remaining_quote -= take_quantity * unit_quote
        btc_remaining -= take_quantity
        eth_remaining -= take_quantity
        if btc_remaining <= 1e-12:
            btc_index += 1
            if btc_index < len(btc_asks):
                btc_remaining = btc_asks[btc_index].size
        if eth_remaining <= 1e-12:
            eth_index += 1
            if eth_index < len(eth_asks):
                eth_remaining = eth_asks[eth_index].size

    btc_fill = simulate_buy_quantity(btc_book, quantity, fee_rate)
    eth_fill = simulate_buy_quantity(eth_book, quantity, fee_rate)
    complete = bool(
        remaining_quote <= 1e-7
        and btc_fill.complete
        and eth_fill.complete
        and abs(btc_fill.quote + eth_fill.quote - total_quote_usd) <= 1e-6
        and abs(btc_fill.quantity - eth_fill.quantity) <= 1e-9
    )
    btc_fill.complete = complete
    eth_fill.complete = complete
    return btc_fill, eth_fill


class PairMatchRegistry:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pair_orders (
                order_id TEXT PRIMARY KEY,
                order_number INTEGER,
                interval_key TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                direction TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                opened_at TEXT NOT NULL,
                btc_leg_json TEXT NOT NULL,
                eth_leg_json TEXT NOT NULL,
                spread_cents REAL NOT NULL,
                total_cost_usd REAL NOT NULL,
                scenario_pnl_json TEXT NOT NULL,
                status TEXT NOT NULL,
                btc_outcome TEXT,
                eth_outcome TEXT,
                payout_usd REAL,
                realized_pnl REAL,
                settled_at TEXT,
                UNIQUE(interval_key, fingerprint)
            )
            """
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS pair_orders_status_idx ON pair_orders(status, end_time)"
        )
        self._ensure_order_numbers()
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pair_match_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        self.connection.commit()

    def _ensure_order_numbers(self) -> None:
        columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(pair_orders)").fetchall()
        }
        if "order_number" not in columns:
            self.connection.execute("ALTER TABLE pair_orders ADD COLUMN order_number INTEGER")
        maximum = self.connection.execute(
            "SELECT COALESCE(MAX(order_number), 0) AS value FROM pair_orders"
        ).fetchone()
        next_number = int(maximum["value"] if maximum else 0) + 1
        missing = self.connection.execute(
            "SELECT order_id FROM pair_orders WHERE order_number IS NULL ORDER BY opened_at, order_id"
        ).fetchall()
        for row in missing:
            self.connection.execute(
                "UPDATE pair_orders SET order_number = ? WHERE order_id = ?",
                (next_number, row["order_id"]),
            )
            next_number += 1
        self.connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS pair_orders_order_number_idx ON pair_orders(order_number)"
        )

    def close(self) -> None:
        self.connection.close()

    def count(self, interval_key: str) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS count FROM pair_orders WHERE interval_key = ?", (interval_key,)
        ).fetchone()
        return int(row["count"] if row else 0)

    def last_direction(self, interval_key: str) -> PairDirection | None:
        row = self.connection.execute(
            "SELECT direction FROM pair_orders WHERE interval_key = ? ORDER BY order_number DESC LIMIT 1",
            (interval_key,),
        ).fetchone()
        return PairDirection(row["direction"]) if row else None

    def continuous_next_direction(self) -> PairDirection | None:
        row = self.connection.execute(
            "SELECT value FROM pair_match_state WHERE key = 'continuous_next_direction'"
        ).fetchone()
        if not row:
            return None
        try:
            return PairDirection(row["value"])
        except ValueError:
            return None

    def set_continuous_next_direction(self, direction: PairDirection) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO pair_match_state(key, value) VALUES ('continuous_next_direction', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (direction.value,),
            )

    def record(
        self,
        order: PairOrder,
        max_pairs: int,
        continuous_next_direction: PairDirection | None = None,
    ) -> bool:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                "SELECT COUNT(*) AS count FROM pair_orders WHERE interval_key = ?", (order.interval_key,)
            ).fetchone()
            if int(row["count"] if row else 0) >= max_pairs:
                self.connection.rollback()
                return False
            sequence = self.connection.execute(
                "SELECT COALESCE(MAX(order_number), 0) + 1 AS value FROM pair_orders"
            ).fetchone()
            order.order_number = int(sequence["value"] if sequence else 1)
            payload = order.model_dump(mode="json")
            self.connection.execute(
                """
                INSERT INTO pair_orders (
                    order_id, order_number, interval_key, start_time, end_time, direction, fingerprint, opened_at,
                    btc_leg_json, eth_leg_json, spread_cents, total_cost_usd, scenario_pnl_json,
                    status, btc_outcome, eth_outcome, payout_usd, realized_pnl, settled_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["order_id"], payload["order_number"], payload["interval_key"],
                    payload["start_time"], payload["end_time"],
                    payload["direction"], payload["fingerprint"], payload["opened_at"],
                    json.dumps(payload["btc_leg"], separators=(",", ":")),
                    json.dumps(payload["eth_leg"], separators=(",", ":")),
                    payload["spread_cents"], payload["total_cost_usd"],
                    json.dumps(payload["scenario_pnl"], separators=(",", ":")), payload["status"],
                    payload["btc_outcome"], payload["eth_outcome"], payload["payout_usd"],
                    payload["realized_pnl"], payload["settled_at"],
                ),
            )
            if continuous_next_direction is not None:
                self.connection.execute(
                    """
                    INSERT INTO pair_match_state(key, value) VALUES ('continuous_next_direction', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (continuous_next_direction.value,),
                )
            self.connection.commit()
            return True
        except sqlite3.IntegrityError:
            self.connection.rollback()
            return False
        except Exception:
            self.connection.rollback()
            raise

    def _row_to_order(self, row: sqlite3.Row) -> PairOrder:
        return PairOrder(
            order_id=row["order_id"],
            order_number=row["order_number"],
            interval_key=row["interval_key"],
            start_time=row["start_time"],
            end_time=row["end_time"],
            direction=row["direction"],
            fingerprint=row["fingerprint"],
            opened_at=row["opened_at"],
            btc_leg=json.loads(row["btc_leg_json"]),
            eth_leg=json.loads(row["eth_leg_json"]),
            spread_cents=row["spread_cents"],
            total_cost_usd=row["total_cost_usd"],
            scenario_pnl=json.loads(row["scenario_pnl_json"]),
            status=row["status"],
            btc_outcome=row["btc_outcome"],
            eth_outcome=row["eth_outcome"],
            payout_usd=row["payout_usd"],
            realized_pnl=row["realized_pnl"],
            settled_at=row["settled_at"],
        )

    def recent_orders(self, limit: int = 100) -> list[PairOrder]:
        rows = self.connection.execute(
            "SELECT * FROM pair_orders ORDER BY order_number DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row_to_order(row) for row in rows]

    def pending_market_pairs(self) -> list[tuple[str, str]]:
        rows = self.connection.execute(
            """
            SELECT DISTINCT json_extract(btc_leg_json, '$.market_slug') AS btc_slug,
                            json_extract(eth_leg_json, '$.market_slug') AS eth_slug
            FROM pair_orders WHERE status = 'PENDING' AND end_time <= ? ORDER BY end_time
            """,
            (datetime.now(timezone.utc).isoformat(),),
        ).fetchall()
        return [(str(row["btc_slug"]), str(row["eth_slug"])) for row in rows]

    def settle(
        self,
        btc_slug: str,
        eth_slug: str,
        btc_outcome: Direction,
        eth_outcome: Direction,
        settled_at: datetime,
    ) -> list[PairOrder]:
        rows = self.connection.execute(
            """
            SELECT * FROM pair_orders
            WHERE status = 'PENDING'
              AND json_extract(btc_leg_json, '$.market_slug') = ?
              AND json_extract(eth_leg_json, '$.market_slug') = ?
            """,
            (btc_slug, eth_slug),
        ).fetchall()
        settled: list[PairOrder] = []
        with self.connection:
            for row in rows:
                order = self._row_to_order(row)
                payout = 0.0
                if order.btc_leg.direction == btc_outcome:
                    payout += order.btc_leg.quantity
                if order.eth_leg.direction == eth_outcome:
                    payout += order.eth_leg.quantity
                pnl = payout - order.total_cost_usd
                self.connection.execute(
                    """
                    UPDATE pair_orders SET status = 'SETTLED', btc_outcome = ?, eth_outcome = ?,
                        payout_usd = ?, realized_pnl = ?, settled_at = ?
                    WHERE order_id = ? AND status = 'PENDING'
                    """,
                    (btc_outcome.value, eth_outcome.value, payout, pnl, settled_at.isoformat(), order.order_id),
                )
                order.status = "SETTLED"
                order.btc_outcome = btc_outcome
                order.eth_outcome = eth_outcome
                order.payout_usd = payout
                order.realized_pnl = pnl
                order.settled_at = settled_at
                settled.append(order)
        return settled

    def summary(self) -> dict[str, float | int]:
        row = self.connection.execute(
            """
            SELECT COUNT(*) AS orders,
                   SUM(CASE WHEN status = 'SETTLED' THEN 1 ELSE 0 END) AS settled,
                   SUM(CASE WHEN status = 'PENDING' THEN 1 ELSE 0 END) AS pending,
                   COALESCE(SUM(realized_pnl), 0) AS pnl,
                   COALESCE(SUM(CASE WHEN status = 'SETTLED' AND realized_pnl > 0 THEN 1 ELSE 0 END), 0) AS wins,
                   COALESCE(SUM(CASE WHEN direction = 'BTC_UP_ETH_DOWN' THEN realized_pnl ELSE 0 END), 0) AS a_pnl,
                   COALESCE(SUM(CASE WHEN direction = 'BTC_DOWN_ETH_UP' THEN realized_pnl ELSE 0 END), 0) AS b_pnl
            FROM pair_orders
            """
        ).fetchone()
        settled = int(row["settled"] or 0)
        wins = int(row["wins"] or 0)
        return {
            "orders": int(row["orders"] or 0),
            "settled_orders": settled,
            "pending_orders": int(row["pending"] or 0),
            "realized_pnl": float(row["pnl"] or 0.0),
            "winning_orders": wins,
            "win_rate": wins / settled if settled else 0.0,
            "btc_up_eth_down_pnl": float(row["a_pnl"] or 0.0),
            "btc_down_eth_up_pnl": float(row["b_pnl"] or 0.0),
        }

    def recent_markets(self, limit: int = 20) -> list[dict[str, Any]]:
        intervals = self.connection.execute(
            "SELECT interval_key, MAX(order_number) AS latest FROM pair_orders GROUP BY interval_key ORDER BY latest DESC LIMIT ?",
            (limit,),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for interval in intervals:
            orders = [
                self._row_to_order(row)
                for row in self.connection.execute(
                    "SELECT * FROM pair_orders WHERE interval_key = ? ORDER BY order_number",
                    (interval["interval_key"],),
                ).fetchall()
            ]
            if not orders:
                continue
            settled = [order for order in orders if order.status == "SETTLED"]
            result.append(
                {
                    "interval_key": interval["interval_key"],
                    "start_time": orders[0].start_time.isoformat(),
                    "end_time": orders[0].end_time.isoformat(),
                    "btc_slug": orders[0].btc_leg.market_slug,
                    "eth_slug": orders[0].eth_leg.market_slug,
                    "btc_outcome": settled[0].btc_outcome.value if settled and settled[0].btc_outcome else None,
                    "eth_outcome": settled[0].eth_outcome.value if settled and settled[0].eth_outcome else None,
                    "orders": len(orders),
                    "btc_up_eth_down_orders": sum(order.direction == PairDirection.BTC_UP_ETH_DOWN for order in orders),
                    "btc_down_eth_up_orders": sum(order.direction == PairDirection.BTC_DOWN_ETH_UP for order in orders),
                    "total_cost_usd": sum(order.total_cost_usd for order in orders),
                    "fees_usd": sum(order.btc_leg.fee_usd + order.eth_leg.fee_usd for order in orders),
                    "payout_usd": sum(order.payout_usd or 0.0 for order in settled),
                    "realized_pnl": sum(order.realized_pnl or 0.0 for order in settled),
                    "status": "SETTLED" if len(settled) == len(orders) else "PENDING",
                }
            )
        return result


class PairMatchEngine:
    def __init__(self, config: AppConfig, registry: PairMatchRegistry):
        self.config = config
        self.registry = registry
        self.status = "disabled" if not config.pair_match.enabled else "waiting_for_markets"
        self.last_reason = self.status
        self.current_interval_key: str | None = None
        self.current_count = 0
        self.next_direction: PairDirection | None = None
        self.candidates: dict[PairDirection, PairCandidate] = {}
        self.last_evaluated_fingerprint: dict[str, str] = {}
        self.refresh_history()

    def refresh_history(self) -> None:
        self._recent_orders = self.registry.recent_orders(100)
        self._recent_markets = self.registry.recent_markets(20)
        self._summary = self.registry.summary()

    def _aligned_markets(self, engines: dict[str, "PaperEngine"]) -> tuple[MarketState, MarketState] | None:
        btc = engines.get("BTC")
        eth = engines.get("ETH")
        if not btc or not eth or not btc.market or not eth.market:
            return None
        btc_market, eth_market = btc.market, eth.market
        if (
            btc_market.start_time is None
            or eth_market.start_time is None
            or btc_market.start_time != eth_market.start_time
            or btc_market.end_time != eth_market.end_time
        ):
            return None
        return btc_market, eth_market

    def _book_fingerprint(self, books: list[OrderBookSnapshot]) -> str:
        payload = [
            {
                "token_id": book.token_id,
                "asks": sorted((level.price, level.size) for level in book.asks),
            }
            for book in books
        ]
        return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode("utf-8")).hexdigest()

    def _candidate(
        self,
        direction: PairDirection,
        btc_market: MarketState,
        eth_market: MarketState,
        btc_book: OrderBookSnapshot,
        eth_book: OrderBookSnapshot,
        now: datetime,
        min_spread_cents: float,
        min_leg_price_gap_cents: float | None,
    ) -> PairCandidate:
        max_age_seconds = self.config.risk.max_data_age_ms / 1000
        for book in (btc_book, eth_book):
            age = (now - book.received_at).total_seconds()
            if age < 0 or age > max_age_seconds:
                return PairCandidate(direction=direction, available=False, reason="book_stale")
            if not book.depth_trusted:
                return PairCandidate(direction=direction, available=False, reason="book_depth_untrusted")
        btc_fill, eth_fill = simulate_equal_quantity_buys(
            btc_book,
            eth_book,
            self.config.pair_match.leg_quote_usd * 2.0,
            self.config.strategy.taker_fee_rate,
        )
        if not btc_fill.complete or not eth_fill.complete:
            return PairCandidate(direction=direction, available=False, reason="insufficient_depth")
        if btc_fill.quantity < btc_market.min_order_size or eth_fill.quantity < eth_market.min_order_size:
            return PairCandidate(direction=direction, available=False, reason="below_min_order_size")
        spread = spread_cents(btc_fill, eth_fill)
        if spread is None:
            return PairCandidate(direction=direction, available=False, reason="invalid_execution")
        btc_direction = Direction.UP if direction == PairDirection.BTC_UP_ETH_DOWN else Direction.DOWN
        eth_direction = Direction.DOWN if direction == PairDirection.BTC_UP_ETH_DOWN else Direction.UP
        btc_leg = PairLeg(
            asset="BTC", market_id=btc_market.condition_id, market_slug=btc_market.slug,
            token_id=btc_book.token_id, direction=btc_direction, avg_price=btc_fill.avg_price,
            quantity=btc_fill.quantity, quote=btc_fill.quote, fee_usd=btc_fill.fee_usd,
            slippage=btc_fill.slippage, levels_used=btc_fill.levels_used,
        )
        eth_leg = PairLeg(
            asset="ETH", market_id=eth_market.condition_id, market_slug=eth_market.slug,
            token_id=eth_book.token_id, direction=eth_direction, avg_price=eth_fill.avg_price,
            quantity=eth_fill.quantity, quote=eth_fill.quote, fee_usd=eth_fill.fee_usd,
            slippage=eth_fill.slippage, levels_used=eth_fill.levels_used,
        )
        scenarios = pair_scenario_pnl(btc_leg, eth_leg)
        total_cost = btc_leg.quote + btc_leg.fee_usd + eth_leg.quote + eth_leg.fee_usd
        leg_price_gap = 100.0 * abs(btc_fill.avg_price - eth_fill.avg_price)
        meets_spread = spread >= min_spread_cents
        meets_leg_price_gap = (
            min_leg_price_gap_cents is None or leg_price_gap >= min_leg_price_gap_cents
        )
        if not meets_spread:
            reason = "spread_below_threshold"
        elif not meets_leg_price_gap:
            reason = "leg_price_gap_below_threshold"
        else:
            reason = "eligible"
        return PairCandidate(
            direction=direction,
            available=True,
            meets_spread=meets_spread,
            meets_leg_price_gap=meets_leg_price_gap,
            reason=reason,
            spread_cents=spread,
            leg_price_gap_cents=leg_price_gap,
            btc_leg=btc_leg,
            eth_leg=eth_leg,
            total_cost_usd=total_cost,
            scenario_pnl=scenarios,
        )

    def _alternation_target(self, interval_key: str) -> PairDirection | None:
        if not self.config.pair_match.alternate_directions:
            return None
        mode = self.config.pair_match.alternation_mode
        if mode == "always_a":
            return PairDirection.BTC_UP_ETH_DOWN
        if mode == "always_b":
            return PairDirection.BTC_DOWN_ETH_UP
        if mode in {"per_market_ab", "per_market_ba"}:
            last_direction = self.registry.last_direction(interval_key)
            if last_direction is not None:
                return last_direction.opposite
            if mode == "per_market_ab":
                return PairDirection.BTC_UP_ETH_DOWN
            return PairDirection.BTC_DOWN_ETH_UP
        if mode == "continuous_abab":
            target = self.registry.continuous_next_direction()
            if target is None:
                target = secrets.choice(list(PairDirection))
                self.registry.set_continuous_next_direction(target)
            return target
        last_direction = self.registry.last_direction(interval_key)
        return last_direction.opposite if last_direction else None

    def _fingerprint_for_stage(self, fingerprint: str, second_order_stage: bool) -> str:
        if self.config.pair_match.alternation_mode != "per_market_two_stage":
            return fingerprint
        stage = "second" if second_order_stage else "first"
        return hashlib.sha256(f"two-stage:{stage}:{fingerprint}".encode()).hexdigest()

    def evaluate(self, engines: dict[str, "PaperEngine"], now: datetime | None = None) -> PairOrder | None:
        now = now or datetime.now(timezone.utc)
        aligned = self._aligned_markets(engines)
        if aligned is None:
            self.status = "waiting_for_aligned_markets"
            self.last_reason = self.status
            return None
        btc_market, eth_market = aligned
        interval_key = pair_interval_key(btc_market, eth_market)
        self.current_interval_key = interval_key
        self.current_count = self.registry.count(interval_key)
        self.next_direction = self._alternation_target(interval_key)
        second_order_stage = (
            self.config.pair_match.alternation_mode == "per_market_two_stage"
            and self.current_count >= 1
        )
        effective_min_spread = (
            self.config.pair_match.second_order_min_spread_cents
            if second_order_stage
            else self.config.pair_match.min_spread_cents
        )
        effective_min_leg_price_gap = (
            None
            if second_order_stage
            else self.config.pair_match.min_leg_price_gap_cents
        )
        btc_engine, eth_engine = engines["BTC"], engines["ETH"]
        required = [
            btc_engine.books.get(Direction.UP), btc_engine.books.get(Direction.DOWN),
            eth_engine.books.get(Direction.UP), eth_engine.books.get(Direction.DOWN),
        ]
        if any(book is None for book in required):
            self.candidates = {}
            self.status = "books_unavailable"
            self.last_reason = self.status
            return None
        books = [book for book in required if book is not None]
        raw_fingerprint = self._book_fingerprint(books)
        fingerprint = self._fingerprint_for_stage(raw_fingerprint, second_order_stage)
        self.candidates = {
            PairDirection.BTC_UP_ETH_DOWN: self._candidate(
                PairDirection.BTC_UP_ETH_DOWN, btc_market, eth_market,
                btc_engine.books[Direction.UP], eth_engine.books[Direction.DOWN], now,
                effective_min_spread, effective_min_leg_price_gap,
            ),
            PairDirection.BTC_DOWN_ETH_UP: self._candidate(
                PairDirection.BTC_DOWN_ETH_UP, btc_market, eth_market,
                btc_engine.books[Direction.DOWN], eth_engine.books[Direction.UP], now,
                effective_min_spread, effective_min_leg_price_gap,
            ),
        }
        if not self.config.pair_match.enabled:
            self.status = "disabled"
            self.last_reason = self.status
            return None
        if not all((btc_market.accepting_orders, eth_market.accepting_orders)):
            self.status = "market_not_accepting_orders"
            self.last_reason = self.status
            return None
        if not market_is_active(btc_market, now) or not market_is_active(eth_market, now):
            self.status = "markets_not_active"
            self.last_reason = self.status
            return None
        elapsed = (now - btc_market.start_time).total_seconds() if btc_market.start_time else -1
        if not (
            self.config.pair_match.start_seconds_after_open
            <= elapsed
            < self.config.pair_match.end_seconds_after_open
        ):
            self.status = "outside_entry_window"
            self.last_reason = self.status
            return None
        if self.current_count >= self.config.pair_match.max_pairs_per_market:
            self.status = "market_pair_limit"
            self.last_reason = self.status
            return None
        if not all(candidate.available for candidate in self.candidates.values()):
            self.status = "all_four_books_must_be_executable"
            self.last_reason = self.status
            return None
        if self.last_evaluated_fingerprint.get(interval_key) == fingerprint:
            self.status = "duplicate_quote_snapshot"
            self.last_reason = self.status
            return None
        self.last_evaluated_fingerprint[interval_key] = fingerprint
        eligible = [
            candidate
            for candidate in self.candidates.values()
            if candidate.available
            and candidate.meets_spread
            and candidate.meets_leg_price_gap
        ]
        if not eligible:
            self.status = "no_eligible_pair"
            self.last_reason = self.status
            return None
        selected: PairCandidate | None
        if self.config.pair_match.alternate_directions and self.next_direction is not None:
            selected = self.candidates.get(self.next_direction)
            if (
                selected is None
                or not selected.available
                or not selected.meets_spread
                or not selected.meets_leg_price_gap
            ):
                self.status = "waiting_for_alternating_direction"
                self.last_reason = self.status
                return None
        else:
            selected = max(
                eligible,
                key=lambda candidate: (
                    candidate.spread_cents if candidate.spread_cents is not None else float("-inf")
                ),
            )
        assert selected.btc_leg and selected.eth_leg and selected.spread_cents is not None
        assert btc_market.start_time is not None
        order = PairOrder(
            order_id=uuid.uuid4().hex,
            interval_key=interval_key,
            start_time=btc_market.start_time,
            end_time=btc_market.end_time,
            direction=selected.direction,
            fingerprint=fingerprint,
            opened_at=now,
            btc_leg=selected.btc_leg,
            eth_leg=selected.eth_leg,
            spread_cents=selected.spread_cents,
            total_cost_usd=selected.total_cost_usd or 0.0,
            scenario_pnl=selected.scenario_pnl,
        )
        continuous_next = (
            selected.direction.opposite
            if self.config.pair_match.alternate_directions
            and self.config.pair_match.alternation_mode == "continuous_abab"
            else None
        )
        if not self.registry.record(
            order,
            self.config.pair_match.max_pairs_per_market,
            continuous_next_direction=continuous_next,
        ):
            self.status = "market_pair_limit_or_duplicate"
            self.last_reason = self.status
            self.current_count = self.registry.count(interval_key)
            return None
        self.status = "pair_opened"
        self.last_reason = self.status
        self.current_count += 1
        self.next_direction = self._alternation_target(interval_key)
        self.refresh_history()
        return order

    def pending_market_pairs(self) -> list[tuple[str, str]]:
        return self.registry.pending_market_pairs()

    def settle(
        self,
        btc_slug: str,
        eth_slug: str,
        btc_outcome: Direction,
        eth_outcome: Direction,
        now: datetime | None = None,
    ) -> list[PairOrder]:
        settled = self.registry.settle(
            btc_slug, eth_slug, btc_outcome, eth_outcome, now or datetime.now(timezone.utc)
        )
        if settled:
            self.status = "pair_settled"
            self.last_reason = self.status
            self.refresh_history()
        return settled

    def dashboard_state(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "last_reason": self.last_reason,
            "current_interval_key": self.current_interval_key,
            "current_count": self.current_count,
            "next_direction": self.next_direction.value if self.next_direction else None,
            "config": self.config.pair_match.model_dump(mode="json"),
            "candidates": {
                direction.value: candidate.model_dump(mode="json")
                for direction, candidate in self.candidates.items()
            },
            "summary": self._summary,
            "recent_orders": [order.model_dump(mode="json") for order in self._recent_orders],
            "recent_markets": self._recent_markets,
        }
