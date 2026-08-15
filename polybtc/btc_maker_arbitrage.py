from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .config import AppConfig
from .models import Direction, MarketState, OrderBookSnapshot
from .orderbook import simulate_sell, taker_fee_usd


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MakerTradePrint(BaseModel):
    token_id: str
    market_id: str | None = None
    price: float
    size: float
    side: str
    timestamp: datetime
    received_at: datetime = Field(default_factory=utc_now)


class MakerQuote(BaseModel):
    quote_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    group_id: str
    market_id: str
    slug: str
    direction: Direction
    side: Literal["BUY", "SELL"]
    purpose: Literal["PAIR", "HEDGE", "EXIT"]
    price: float
    quantity: float
    filled_quantity: float = 0.0
    queue_ahead: float = 0.0
    initial_queue_ahead: float = 0.0
    status: Literal["OPEN", "FILLED", "CANCELLED", "UNMEASURABLE"] = "OPEN"
    reason: str = "shadow_post_only_open"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    closed_at: datetime | None = None

    @property
    def remaining_quantity(self) -> float:
        return max(0.0, self.quantity - self.filled_quantity)


class MakerFill(BaseModel):
    fill_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    group_id: str
    quote_id: str
    market_id: str
    direction: Direction
    side: Literal["BUY", "SELL"]
    purpose: Literal["PAIR", "HEDGE", "EXIT", "EMERGENCY_EXIT", "STOP_LOSS_EXIT", "SETTLEMENT"]
    price: float
    quantity: float
    quote_usd: float
    fee_usd: float = 0.0
    created_at: datetime = Field(default_factory=utc_now)


class MakerGroup(BaseModel):
    group_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    market_id: str
    slug: str
    status: str = "QUOTING"
    target_quantity: float
    up_bought_quantity: float = 0.0
    down_bought_quantity: float = 0.0
    up_buy_cost_usd: float = 0.0
    down_buy_cost_usd: float = 0.0
    up_sold_quantity: float = 0.0
    down_sold_quantity: float = 0.0
    up_sell_proceeds_usd: float = 0.0
    down_sell_proceeds_usd: float = 0.0
    fees_usd: float = 0.0
    locked_quantity: float = 0.0
    unhedged_direction: Direction | None = None
    unhedged_since: datetime | None = None
    realized_pnl_usd: float = 0.0
    completion_reason: str | None = None
    completed_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    def net_quantity(self, direction: Direction) -> float:
        if direction == Direction.UP:
            return max(0.0, self.up_bought_quantity - self.up_sold_quantity)
        return max(0.0, self.down_bought_quantity - self.down_sold_quantity)

    def buy_average(self, direction: Direction) -> float | None:
        quantity = self.up_bought_quantity if direction == Direction.UP else self.down_bought_quantity
        cost = self.up_buy_cost_usd if direction == Direction.UP else self.down_buy_cost_usd
        return cost / quantity if quantity > 1e-12 else None

    @property
    def has_execution(self) -> bool:
        return any((
            self.up_bought_quantity > 1e-9,
            self.down_bought_quantity > 1e-9,
            self.up_sold_quantity > 1e-9,
            self.down_sold_quantity > 1e-9,
        ))


class BtcMakerArbitrageRegistry:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS btc_maker_quotes (
                quote_id TEXT PRIMARY KEY, group_id TEXT NOT NULL, market_id TEXT NOT NULL,
                status TEXT NOT NULL, updated_at TEXT NOT NULL, payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_btc_maker_quotes_group ON btc_maker_quotes(group_id, updated_at);
            CREATE TABLE IF NOT EXISTS btc_maker_fills (
                fill_id TEXT PRIMARY KEY, group_id TEXT NOT NULL, market_id TEXT NOT NULL,
                created_at TEXT NOT NULL, payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_btc_maker_fills_group ON btc_maker_fills(group_id, created_at);
            CREATE TABLE IF NOT EXISTS btc_maker_groups (
                group_id TEXT PRIMARY KEY, market_id TEXT NOT NULL, slug TEXT NOT NULL,
                status TEXT NOT NULL, updated_at TEXT NOT NULL, payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_btc_maker_groups_status ON btc_maker_groups(status, updated_at);
            CREATE TABLE IF NOT EXISTS btc_maker_round_stats (
                market_id TEXT PRIMARY KEY, slug TEXT NOT NULL, payload_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        self.connection.commit()
        self._cancel_interrupted_quotes()
        self._repair_unfilled_history()

    @staticmethod
    def _json(item: BaseModel | dict[str, Any]) -> str:
        payload = item.model_dump(mode="json") if isinstance(item, BaseModel) else item
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def save_quote(self, quote: MakerQuote) -> None:
        self.connection.execute(
            """INSERT INTO btc_maker_quotes(quote_id,group_id,market_id,status,updated_at,payload_json)
               VALUES(?,?,?,?,?,?) ON CONFLICT(quote_id) DO UPDATE SET
               status=excluded.status,updated_at=excluded.updated_at,payload_json=excluded.payload_json""",
            (quote.quote_id, quote.group_id, quote.market_id, quote.status,
             quote.updated_at.isoformat(), self._json(quote)),
        )
        self.connection.commit()

    def save_fill(self, fill: MakerFill) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO btc_maker_fills VALUES(?,?,?,?,?)",
            (fill.fill_id, fill.group_id, fill.market_id, fill.created_at.isoformat(), self._json(fill)),
        )
        self.connection.commit()

    def save_group(self, group: MakerGroup) -> None:
        self.connection.execute(
            """INSERT INTO btc_maker_groups(group_id,market_id,slug,status,updated_at,payload_json)
               VALUES(?,?,?,?,?,?) ON CONFLICT(group_id) DO UPDATE SET
               status=excluded.status,updated_at=excluded.updated_at,payload_json=excluded.payload_json""",
            (group.group_id, group.market_id, group.slug, group.status,
             group.updated_at.isoformat(), self._json(group)),
        )
        self.connection.commit()

    def save_round_stats(self, market_id: str, slug: str, payload: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO btc_maker_round_stats VALUES(?,?,?,?)",
            (market_id, slug, self._json(payload), utc_now().isoformat()),
        )
        self.connection.commit()

    def recent_quotes(self, limit: int = 200) -> list[MakerQuote]:
        rows = self.connection.execute(
            "SELECT payload_json FROM btc_maker_quotes ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [MakerQuote.model_validate_json(row["payload_json"]) for row in rows]

    def recent_fills(self, limit: int = 200) -> list[MakerFill]:
        rows = self.connection.execute(
            "SELECT payload_json FROM btc_maker_fills ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [MakerFill.model_validate_json(row["payload_json"]) for row in rows]

    def recent_groups(self, limit: int = 100) -> list[MakerGroup]:
        rows = self.connection.execute(
            "SELECT payload_json FROM btc_maker_groups ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [MakerGroup.model_validate_json(row["payload_json"]) for row in rows]

    def active_groups(self) -> list[MakerGroup]:
        rows = self.connection.execute(
            "SELECT payload_json FROM btc_maker_groups WHERE status NOT IN ('LOCKED','CLOSED','SETTLED','UNFILLED')"
        ).fetchall()
        return [MakerGroup.model_validate_json(row["payload_json"]) for row in rows]

    def _cancel_interrupted_quotes(self) -> None:
        rows = self.connection.execute(
            "SELECT payload_json FROM btc_maker_quotes WHERE status='OPEN'"
        ).fetchall()
        for row in rows:
            quote = MakerQuote.model_validate_json(row["payload_json"])
            quote.status = "CANCELLED"
            quote.reason = "process_restart_cancelled"
            quote.updated_at = quote.closed_at = utc_now()
            self.save_quote(quote)

    def _repair_unfilled_history(self) -> None:
        """Reclassify old zero-fill terminal groups without changing actual fills or PnL."""
        rows = self.connection.execute(
            "SELECT payload_json FROM btc_maker_groups WHERE status IN ('CLOSED','SETTLED')"
        ).fetchall()
        for row in rows:
            group = MakerGroup.model_validate_json(row["payload_json"])
            if group.has_execution:
                continue
            group.status = "UNFILLED"
            group.completion_reason = group.completion_reason or "no_shadow_fill"
            group.realized_pnl_usd = 0.0
            group.completed_at = group.completed_at or group.updated_at
            self.save_group(group)

    def close(self) -> None:
        self.connection.close()


class BtcMakerArbitrageEngine:
    def __init__(self, config: AppConfig, registry: BtcMakerArbitrageRegistry):
        self.config = config
        self.registry = registry
        self.market: MarketState | None = None
        self.books: dict[Direction, OrderBookSnapshot] = {}
        self.quotes: dict[str, MakerQuote] = {}
        self.groups: dict[str, MakerGroup] = {item.group_id: item for item in registry.active_groups()}
        self.events: list[tuple[str, Any]] = []
        self.status = "starting"
        self.last_reason = "starting"
        self.paused = False
        self.last_trade_print_at: dict[Direction, datetime] = {}
        self.book_sources: dict[Direction, str] = {}
        self.last_quote_closed_at: datetime | None = None
        self.unmeasurable_until: datetime | None = None
        self.round_quote_count = 0
        self.round_single_leg_count = 0
        self.round_hedge_success_count = 0

    @property
    def settings(self):
        return self.config.btc_maker_arbitrage

    def set_market(self, market: MarketState) -> None:
        if self.market and self.market.condition_id == market.condition_id:
            self.market = market
            return
        now = utc_now()
        self.cancel_quotes("market_changed", now)
        self.market = market
        self.books.clear()
        self.book_sources.clear()
        self.last_trade_print_at.clear()
        self.unmeasurable_until = None
        self.round_quote_count = 0
        self.round_single_leg_count = 0
        self.round_hedge_success_count = 0
        self.status = "waiting_books"
        self.last_reason = self.status

    def add_book(self, direction: Direction, book: OrderBookSnapshot, now: datetime | None = None) -> None:
        now = now or utc_now()
        if not self.market or book.token_id != (
            self.market.up_token_id if direction == Direction.UP else self.market.down_token_id
        ):
            return
        if book.market_id and book.market_id != self.market.condition_id:
            return
        raw_trade = book.raw.get("_last_trade") if isinstance(book.raw, dict) else None
        if isinstance(raw_trade, dict):
            trade = self.parse_trade_print(raw_trade, book, now)
            if trade is not None:
                self.on_trade_print(direction, trade)
            return
        source = "rest" if (
            isinstance(book.raw, dict) and book.raw.get("_transport") == "rest"
        ) else "websocket"
        if source == "websocket":
            # A WebSocket snapshot promotes the direction back to its live
            # source.  Any real stream interruption has already been reported
            # by the runner and marked unmeasurable there.
            self.book_sources[direction] = source
        elif direction not in self.book_sources:
            # REST snapshots are periodically used to reconcile depth while
            # the WebSocket remains healthy.  A REST calibration is not a
            # source switch, even when the market is quiet for several seconds;
            # otherwise each periodic reconcile would repeatedly pause quotes.
            self.book_sources[direction] = source
        current = self.books.get(direction)
        if current and book.timestamp < current.timestamp and not (
            isinstance(book.raw, dict) and book.raw.get("_transport") == "rest"
        ):
            self.unmeasurable_until = now + timedelta(seconds=self.settings.trade_print_max_age_seconds)
            self.last_reason = "out_of_order_book"
            return
        if source == "websocket" and current is not None:
            self._apply_depth_assisted_fills(direction, current, book, now)
        # Keep an engine-owned copy.  Accepted trade prints adjust this local
        # depth watermark so a following price_change cannot count the same
        # queue consumption twice.
        self.books[direction] = book.model_copy(deep=True)

    def _apply_depth_assisted_fills(
        self,
        direction: Direction,
        previous: OrderBookSnapshot,
        current: OrderBookSnapshot,
        now: datetime,
    ) -> None:
        if (
            not previous.depth_trusted
            or not current.depth_trusted
            or (self.unmeasurable_until and now < self.unmeasurable_until)
        ):
            return
        for quote in sorted(
            [item for item in self._open_quotes() if item.direction == direction],
            key=lambda item: item.created_at,
        ):
            if quote.side == "BUY":
                crossed = current.best_ask is not None and current.best_ask <= quote.price + 1e-9
                swept = (
                    self._level_size(previous, "BUY", quote.price) > 1e-9
                    and self._level_size(current, "BUY", quote.price) <= 1e-9
                    and (current.best_bid is None or current.best_bid < quote.price - 1e-9)
                )
                previous_size = self._level_size(previous, "BUY", quote.price)
                current_size = self._level_size(current, "BUY", quote.price)
            else:
                crossed = current.best_bid is not None and current.best_bid >= quote.price - 1e-9
                swept = (
                    self._level_size(previous, "SELL", quote.price) > 1e-9
                    and self._level_size(current, "SELL", quote.price) <= 1e-9
                    and (current.best_ask is None or current.best_ask > quote.price + 1e-9)
                )
                previous_size = self._level_size(previous, "SELL", quote.price)
                current_size = self._level_size(current, "SELL", quote.price)
            if crossed or swept:
                self._record_fill(
                    quote,
                    quote.remaining_quantity,
                    quote.price,
                    now,
                    fill_reason="shadow_depth_swept",
                )
                continue
            reduction = max(0.0, previous_size - current_size)
            if reduction <= 1e-12:
                continue
            queue_consumed = min(quote.queue_ahead, reduction)
            quote.queue_ahead -= queue_consumed
            reduction -= queue_consumed
            quote.updated_at = now
            self.registry.save_quote(quote)
            if reduction > 1e-12 and quote.remaining_quantity > 1e-12:
                self._record_fill(
                    quote,
                    min(quote.remaining_quantity, reduction),
                    quote.price,
                    now,
                    fill_reason="shadow_depth_reduction",
                )

    def mark_unmeasurable(self, reason: str, now: datetime | None = None) -> None:
        now = now or utc_now()
        self.unmeasurable_until = now + timedelta(
            seconds=self.settings.trade_print_max_age_seconds
        )
        self.last_reason = reason

    @staticmethod
    def parse_trade_print(
        payload: dict[str, Any], book: OrderBookSnapshot, received_at: datetime
    ) -> MakerTradePrint | None:
        try:
            timestamp_value = payload.get("timestamp")
            # CLOB sends epoch milliseconds as a JSON string in live
            # last_trade_price frames.  Tests and replays can also provide a
            # numeric value or an ISO timestamp, so accept all three forms.
            try:
                epoch_value = float(timestamp_value)
            except (TypeError, ValueError):
                timestamp = datetime.fromisoformat(
                    str(timestamp_value).replace("Z", "+00:00")
                )
            else:
                # Epoch values above 10^11 are milliseconds; retaining second
                # precision support keeps hand-authored replay events useful.
                if abs(epoch_value) >= 100_000_000_000:
                    epoch_value /= 1000.0
                timestamp = datetime.fromtimestamp(epoch_value, tz=timezone.utc)
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            return MakerTradePrint(
                token_id=str(payload.get("asset_id") or payload.get("token_id") or book.token_id),
                market_id=str(payload.get("market") or book.market_id or "") or None,
                price=float(payload["price"]),
                size=float(payload.get("size") or 0),
                side=str(payload.get("side") or "").upper(),
                timestamp=timestamp,
                received_at=received_at,
            )
        except (KeyError, TypeError, ValueError):
            return None

    def _active_group(self) -> MakerGroup | None:
        active = [group for group in self.groups.values() if group.status not in {"LOCKED", "CLOSED", "SETTLED"}]
        return max(active, key=lambda item: item.updated_at, default=None)

    def _open_quotes(self, group_id: str | None = None) -> list[MakerQuote]:
        return [
            quote for quote in self.quotes.values()
            if quote.status == "OPEN" and (group_id is None or quote.group_id == group_id)
        ]

    @staticmethod
    def _level_size(book: OrderBookSnapshot, side: str, price: float) -> float:
        levels = book.bids if side == "BUY" else book.asks
        return sum(level.size for level in levels if abs(level.price - price) <= 1e-9)

    def _quote_pair(self) -> tuple[float, float, dict[str, float]] | None:
        up = self.books.get(Direction.UP)
        down = self.books.get(Direction.DOWN)
        if not up or not down or up.best_bid is None or down.best_bid is None or up.best_ask is None or down.best_ask is None:
            return None
        tick = max(up.tick_size, down.tick_size, 0.01)
        max_sum = (100.0 - self.settings.min_locked_profit_cents - self.settings.maker_fee_reserve_cents) / 100.0
        if up.best_bid + down.best_bid > max_sum + 1e-9:
            return None
        candidates: list[tuple[float, float, float, float, float]] = []
        up_max_ticks = int((up.best_ask - tick + 1e-9) / tick)
        down_max_ticks = int((down.best_ask - tick + 1e-9) / tick)
        for up_ticks in range(int((up.best_bid + 1e-9) / tick), up_max_ticks + 1):
            up_price = round(up_ticks * tick, 8)
            if up_price + 1e-9 < up.best_bid or up_price >= up.best_ask - 1e-9:
                continue
            for down_ticks in range(int((down.best_bid + 1e-9) / tick), down_max_ticks + 1):
                down_price = round(down_ticks * tick, 8)
                total = up_price + down_price
                if total > max_sum + 1e-9 or down_price >= down.best_ask - 1e-9:
                    continue
                up_queue = self._level_size(up, "BUY", up_price)
                down_queue = self._level_size(down, "BUY", down_price)
                candidates.append((total, -(up_queue + down_queue), -abs(up_queue - down_queue), up_price, down_price))
        if not candidates:
            return None
        _, _, _, up_price, down_price = max(candidates)
        return up_price, down_price, {
            "max_total_cost_cents": max_sum * 100.0,
            "expected_profit_cents": (1.0 - up_price - down_price) * 100.0 - self.settings.maker_fee_reserve_cents,
        }

    def _pair_quantity(self, up_price: float, down_price: float) -> float:
        """Return equal share quantity for both legs under the configured sizing mode."""
        if self.settings.sizing_mode == "budget":
            total_price = up_price + down_price
            if total_price <= 1e-12:
                return 0.0
            return round(self.settings.pair_budget_usd / total_price, 8)
        return self.settings.quantity_per_leg

    def _create_quote(
        self, group: MakerGroup, direction: Direction, side: Literal["BUY", "SELL"],
        purpose: Literal["PAIR", "HEDGE", "EXIT"], price: float, quantity: float, now: datetime
    ) -> MakerQuote:
        book = self.books[direction]
        queue_ahead = self._level_size(book, side, price)
        quote = MakerQuote(
            group_id=group.group_id, market_id=group.market_id, slug=group.slug,
            direction=direction, side=side, purpose=purpose, price=price,
            quantity=quantity, queue_ahead=queue_ahead, initial_queue_ahead=queue_ahead,
            created_at=now, updated_at=now,
        )
        self.quotes[quote.quote_id] = quote
        self.registry.save_quote(quote)
        self.events.append(("btc_maker_quote", quote.model_dump(mode="json")))
        self.round_quote_count += 1
        self._save_round_stats()
        return quote

    def _save_round_stats(self) -> None:
        if not self.market:
            return
        market_groups = [
            group for group in self.groups.values()
            if group.market_id == self.market.condition_id
        ]
        self.registry.save_round_stats(
            self.market.condition_id,
            self.market.slug,
            {
                "quote_count": self.round_quote_count,
                "single_leg_count": self.round_single_leg_count,
                "hedge_success_count": self.round_hedge_success_count,
                "locked_groups": sum(group.status == "LOCKED" for group in market_groups),
                "closed_groups": sum(group.status in {"CLOSED", "SETTLED"} for group in market_groups),
                "unfilled_groups": sum(group.status == "UNFILLED" for group in market_groups),
                "realized_pnl_usd": sum(group.realized_pnl_usd for group in market_groups),
            },
        )

    def cancel_quotes(self, reason: str, now: datetime | None = None, group_id: str | None = None) -> None:
        now = now or utc_now()
        changed = False
        for quote in self._open_quotes(group_id):
            quote.status = "CANCELLED"
            quote.reason = reason
            quote.updated_at = quote.closed_at = now
            self.registry.save_quote(quote)
            self.events.append(("btc_maker_quote", quote.model_dump(mode="json")))
            changed = True
        if changed:
            self.last_quote_closed_at = now

    def pause(self) -> None:
        self.paused = True
        self.cancel_quotes("manual_pause")
        self.status = self.last_reason = "paused"

    def resume(self) -> None:
        self.paused = False
        self.status = self.last_reason = "resumed"

    def _record_fill(
        self, quote: MakerQuote, quantity: float, price: float, now: datetime,
        purpose: Literal["PAIR", "HEDGE", "EXIT", "EMERGENCY_EXIT", "STOP_LOSS_EXIT", "SETTLEMENT"] | None = None,
        fee_usd: float = 0.0,
        fill_reason: str = "shadow_queue_filled",
    ) -> MakerFill:
        group = self.groups[quote.group_id]
        fill = MakerFill(
            group_id=group.group_id, quote_id=quote.quote_id, market_id=group.market_id,
            direction=quote.direction, side=quote.side, purpose=purpose or quote.purpose,
            price=price, quantity=quantity, quote_usd=price * quantity,
            fee_usd=fee_usd, created_at=now,
        )
        if quote.side == "BUY":
            if quote.direction == Direction.UP:
                group.up_bought_quantity += quantity
                group.up_buy_cost_usd += price * quantity
            else:
                group.down_bought_quantity += quantity
                group.down_buy_cost_usd += price * quantity
        else:
            if quote.direction == Direction.UP:
                group.up_sold_quantity += quantity
                group.up_sell_proceeds_usd += price * quantity
            else:
                group.down_sold_quantity += quantity
                group.down_sell_proceeds_usd += price * quantity
        group.fees_usd += fee_usd
        group.updated_at = now
        quote.filled_quantity += quantity
        quote.updated_at = now
        if quote.remaining_quantity <= 1e-9:
            quote.status = "FILLED"
            quote.reason = fill_reason
            quote.closed_at = now
            self.last_quote_closed_at = now
        self.registry.save_quote(quote)
        self.registry.save_fill(fill)
        self.events.append(("btc_maker_fill", fill.model_dump(mode="json")))
        self._refresh_group(group, now)
        return fill

    def _refresh_group(self, group: MakerGroup, now: datetime) -> None:
        up_net = group.net_quantity(Direction.UP)
        down_net = group.net_quantity(Direction.DOWN)
        group.locked_quantity = min(up_net, down_net)
        difference = up_net - down_net
        previous_unhedged = group.unhedged_direction
        fully_locked = group.locked_quantity >= group.target_quantity - 1e-9
        if abs(difference) <= 1e-9 and fully_locked:
            group.status = "LOCKED"
            group.unhedged_direction = None
            group.unhedged_since = None
            group.completed_at = now
            group.realized_pnl_usd = (
                group.locked_quantity + group.up_sell_proceeds_usd + group.down_sell_proceeds_usd
                - group.up_buy_cost_usd - group.down_buy_cost_usd - group.fees_usd
            )
            self.cancel_quotes("pair_locked", now, group.group_id)
            if previous_unhedged is not None:
                self.round_hedge_success_count += 1
            self.status = self.last_reason = "pair_locked"
        elif (
            abs(difference) <= 1e-9
            and up_net <= 1e-9
            and down_net <= 1e-9
            and not self._open_quotes(group.group_id)
        ):
            group.status = "CLOSED" if group.has_execution else "UNFILLED"
            group.completed_at = now
            group.completion_reason = "position_closed" if group.has_execution else "no_shadow_fill"
            group.realized_pnl_usd = (
                group.up_sell_proceeds_usd + group.down_sell_proceeds_usd
                - group.up_buy_cost_usd - group.down_buy_cost_usd - group.fees_usd
            ) if group.has_execution else 0.0
            group.unhedged_direction = None
            group.unhedged_since = None
            self.status = self.last_reason = (
                "position_closed" if group.has_execution else "quotes_unfilled_closed"
            )
        elif abs(difference) > 1e-9:
            direction = Direction.UP if difference > 0 else Direction.DOWN
            group.status = "UNHEDGED" if group.status != "EXITING" else group.status
            group.unhedged_direction = direction
            if group.unhedged_since is None:
                group.unhedged_since = now
                self.round_single_leg_count += 1
            self._cancel_excess_buy_quotes(group, direction, now)
        elif group.locked_quantity > 1e-9:
            group.unhedged_direction = None
            group.unhedged_since = None
            open_buys = [
                quote
                for quote in self._open_quotes(group.group_id)
                if quote.side == "BUY"
            ]
            up_remaining = sum(
                quote.remaining_quantity
                for quote in open_buys
                if quote.direction == Direction.UP
            )
            down_remaining = sum(
                quote.remaining_quantity
                for quote in open_buys
                if quote.direction == Direction.DOWN
            )
            if abs(up_remaining - down_remaining) > 1e-9:
                self.cancel_quotes("partial_pair_rebalanced", now, group.group_id)
            group.status = "PARTIAL"
        self.registry.save_group(group)
        self._save_round_stats()
        self.events.append(("btc_maker_group", group.model_dump(mode="json")))

    def _cancel_excess_buy_quotes(self, group: MakerGroup, direction: Direction, now: datetime) -> None:
        for quote in self._open_quotes(group.group_id):
            if quote.side == "BUY" and quote.direction == direction:
                quote.status = "CANCELLED"
                quote.reason = "excess_leg_cancelled"
                quote.updated_at = quote.closed_at = now
                self.registry.save_quote(quote)

    def on_trade_print(self, direction: Direction, trade: MakerTradePrint) -> None:
        if not self.market or trade.market_id not in {None, "", self.market.condition_id}:
            return
        now = trade.received_at
        last_print = self.last_trade_print_at.get(direction)
        if last_print and trade.timestamp <= last_print:
            self.unmeasurable_until = now + timedelta(seconds=self.settings.trade_print_max_age_seconds)
            self.last_reason = "duplicate_or_out_of_order_trade_print"
            return
        if (now - trade.timestamp).total_seconds() > self.settings.trade_print_max_age_seconds:
            self.unmeasurable_until = now + timedelta(seconds=self.settings.trade_print_max_age_seconds)
            self.last_reason = "stale_trade_print"
            return
        self.last_trade_print_at[direction] = trade.timestamp
        if self.unmeasurable_until and now < self.unmeasurable_until:
            return
        remaining_trade = max(0.0, trade.size)
        candidates = sorted(
            [q for q in self._open_quotes() if q.direction == direction],
            key=lambda q: q.created_at,
        )
        for quote in candidates:
            crosses = (
                quote.side == "BUY" and trade.side == "SELL" and trade.price <= quote.price + 1e-9
            ) or (
                quote.side == "SELL" and trade.side == "BUY" and trade.price >= quote.price - 1e-9
            )
            if not crosses or remaining_trade <= 1e-12:
                continue
            strictly_through = (
                quote.side == "BUY" and trade.price < quote.price - 1e-9
            ) or (
                quote.side == "SELL" and trade.price > quote.price + 1e-9
            )
            if strictly_through:
                self._record_fill(quote, quote.remaining_quantity, quote.price, now)
                continue
            if abs(trade.price - quote.price) <= 1e-9 and quote.queue_ahead > 0:
                consumed = min(quote.queue_ahead, remaining_trade)
                quote.queue_ahead -= consumed
                remaining_trade -= consumed
                quote.updated_at = now
                self.registry.save_quote(quote)
            if remaining_trade <= 1e-12:
                continue
            quantity = min(quote.remaining_quantity, remaining_trade)
            self._record_fill(quote, quantity, quote.price, now)
            remaining_trade -= quantity
        self._consume_local_depth_for_trade(direction, trade)

    def _consume_local_depth_for_trade(
        self, direction: Direction, trade: MakerTradePrint
    ) -> None:
        book = self.books.get(direction)
        if book is None or trade.size <= 1e-12:
            return
        updated = book.model_copy(deep=True)
        levels = updated.bids if trade.side == "SELL" else updated.asks
        remaining = trade.size
        kept = []
        for level in levels:
            if abs(level.price - trade.price) > 1e-9 or remaining <= 1e-12:
                kept.append(level)
                continue
            consumed = min(level.size, remaining)
            remaining -= consumed
            if level.size - consumed > 1e-12:
                level.size -= consumed
                kept.append(level)
        if trade.side == "SELL":
            updated.bids = kept
        else:
            updated.asks = kept
        self.books[direction] = updated

    def _place_remaining_pair(self, group: MakerGroup, now: datetime) -> bool:
        quantity = group.target_quantity - group.locked_quantity
        if quantity <= 1e-9:
            return False
        pair = self._quote_pair()
        if not pair:
            self.status = self.last_reason = "waiting_partial_pair_opportunity"
            return False
        up_price, down_price, _ = pair
        self._create_quote(group, Direction.UP, "BUY", "PAIR", up_price, quantity, now)
        self._create_quote(group, Direction.DOWN, "BUY", "PAIR", down_price, quantity, now)
        group.status = "QUOTING"
        group.updated_at = now
        self.registry.save_group(group)
        self.status = self.last_reason = "partial_pair_requoted"
        return True

    def _finalize_partial_lock(self, group: MakerGroup, now: datetime) -> None:
        self.cancel_quotes("partial_pair_stop_window", now, group.group_id)
        group.status = "LOCKED"
        group.completed_at = group.updated_at = now
        group.unhedged_direction = None
        group.unhedged_since = None
        group.realized_pnl_usd = (
            group.locked_quantity
            + group.up_sell_proceeds_usd
            + group.down_sell_proceeds_usd
            - group.up_buy_cost_usd
            - group.down_buy_cost_usd
            - group.fees_usd
        )
        self.registry.save_group(group)
        self._save_round_stats()
        self.events.append(("btc_maker_group", group.model_dump(mode="json")))
        self.status = self.last_reason = "partial_pair_locked"

    def _place_hedge(self, group: MakerGroup, now: datetime) -> None:
        up_net = group.net_quantity(Direction.UP)
        down_net = group.net_quantity(Direction.DOWN)
        if abs(up_net - down_net) <= 1e-9:
            return
        missing = Direction.DOWN if up_net > down_net else Direction.UP
        excess = Direction.UP if missing == Direction.DOWN else Direction.DOWN
        quantity = abs(up_net - down_net)
        for quote in list(self._open_quotes(group.group_id)):
            if quote.side == "BUY" and quote.direction == missing:
                if quote.remaining_quantity <= quantity + 1e-9:
                    quote.purpose = "HEDGE"
                    quote.reason = "converted_to_hedge"
                    quote.updated_at = now
                    self.registry.save_quote(quote)
                    group.status = "HEDGING"
                    group.updated_at = now
                    self.registry.save_group(group)
                    self.status = self.last_reason = "hedge_quoted"
                    return
                quote.status = "CANCELLED"
                quote.reason = "hedge_resize"
                quote.updated_at = quote.closed_at = now
                self.registry.save_quote(quote)
        if self._open_quotes(group.group_id):
            return
        excess_average = group.buy_average(excess)
        book = self.books.get(missing)
        if excess_average is None or not book or book.best_bid is None or book.best_ask is None:
            self.last_reason = "hedge_book_unavailable"
            return
        tick = max(book.tick_size, 0.01)
        max_price = 1.0 - excess_average - (
            self.settings.min_locked_profit_cents + self.settings.maker_fee_reserve_cents
        ) / 100.0
        price = min(book.best_ask - tick, max_price)
        price = int((price + 1e-9) / tick) * tick
        if price + 1e-9 < book.best_bid or price >= book.best_ask - 1e-9:
            self.last_reason = "no_profitable_hedge_quote"
            return
        self._create_quote(group, missing, "BUY", "HEDGE", round(price, 8), quantity, now)
        group.status = "HEDGING"
        group.updated_at = now
        self.registry.save_group(group)
        self.status = self.last_reason = "hedge_quoted"

    def _place_exit(self, group: MakerGroup, now: datetime) -> None:
        if self._open_quotes(group.group_id):
            return
        up_net = group.net_quantity(Direction.UP)
        down_net = group.net_quantity(Direction.DOWN)
        if abs(up_net - down_net) <= 1e-9:
            return
        direction = Direction.UP if up_net > down_net else Direction.DOWN
        quantity = abs(up_net - down_net)
        book = self.books.get(direction)
        if not book or book.best_bid is None or book.best_ask is None:
            self.last_reason = "exit_book_unavailable"
            return
        tick = max(book.tick_size, 0.01)
        price = max(book.best_bid + tick, book.best_ask)
        if price <= book.best_bid + 1e-9:
            self.last_reason = "no_post_only_exit_price"
            return
        self._create_quote(group, direction, "SELL", "EXIT", round(price, 8), quantity, now)
        group.status = "EXITING"
        group.updated_at = now
        self.registry.save_group(group)
        self.status = self.last_reason = "exit_quoted"

    def _taker_exit(
        self,
        group: MakerGroup,
        now: datetime,
        *,
        cancel_reason: str,
        fill_purpose: Literal["EMERGENCY_EXIT", "STOP_LOSS_EXIT"],
        quote_reason: str,
    ) -> bool:
        self.cancel_quotes(cancel_reason, now, group.group_id)
        exited = False
        for direction in (Direction.UP, Direction.DOWN):
            quantity = group.net_quantity(direction) - group.locked_quantity
            if quantity <= 1e-9:
                continue
            book = self.books.get(direction)
            if not book:
                self.last_reason = f"{fill_purpose.lower()}_book_unavailable"
                continue
            execution = simulate_sell(book.model_copy(deep=True), quantity, self.config.strategy.taker_fee_rate)
            if execution.quantity <= 1e-9:
                self.last_reason = f"{fill_purpose.lower()}_depth_unavailable"
                continue
            dummy = MakerQuote(
                group_id=group.group_id, market_id=group.market_id, slug=group.slug,
                direction=direction, side="SELL", purpose="EXIT", price=execution.avg_price,
                quantity=execution.quantity, status="FILLED", reason=quote_reason,
                created_at=now, updated_at=now, closed_at=now,
            )
            self.quotes[dummy.quote_id] = dummy
            self.registry.save_quote(dummy)
            self._record_fill(
                dummy, execution.quantity, execution.avg_price, now,
                purpose=fill_purpose, fee_usd=execution.fee_usd,
            )
            exited = True
        return exited

    def _emergency_exit(self, group: MakerGroup, now: datetime) -> bool:
        return self._taker_exit(
            group,
            now,
            cancel_reason="emergency_exit",
            fill_purpose="EMERGENCY_EXIT",
            quote_reason="shadow_emergency_taker",
        )

    def evaluate(self, now: datetime | None = None) -> None:
        now = now or utc_now()
        settings = self.settings
        if not settings.enabled:
            self.cancel_quotes("strategy_disabled", now)
            self.status = self.last_reason = "disabled"
            return
        if self.paused:
            self.status = self.last_reason = "paused"
            return
        market = self.market
        if not market:
            self.status = self.last_reason = "market_unavailable"
            return
        if not market.accepting_orders or market.observe_only:
            self.cancel_quotes("market_not_accepting", now)
            self.status = self.last_reason = "market_not_accepting"
            return
        remaining = (market.end_time - now).total_seconds()
        group = self._active_group()
        for quote in list(self._open_quotes()):
            if (now - quote.created_at).total_seconds() >= settings.quote_ttl_seconds:
                quote.status = "CANCELLED"
                quote.reason = "quote_ttl_expired"
                quote.updated_at = quote.closed_at = now
                self.registry.save_quote(quote)
                self.last_quote_closed_at = now
        if group:
            self._refresh_group(group, now)
            if group.status in {"LOCKED", "CLOSED", "SETTLED", "UNFILLED"}:
                group = None
            elif group.unhedged_since is not None:
                unhedged_seconds = (now - group.unhedged_since).total_seconds()
                exit_quote_open = any(
                    quote.side == "SELL" and quote.purpose == "EXIT"
                    for quote in self._open_quotes(group.group_id)
                )
                excess = group.unhedged_direction
                excess_quantity = (
                    abs(group.net_quantity(Direction.UP) - group.net_quantity(Direction.DOWN))
                    if excess is not None else 0.0
                )
                excess_book = self.books.get(excess) if excess is not None else None
                excess_average = group.buy_average(excess) if excess is not None else None
                marked_loss = (
                    excess_quantity * (excess_book.best_bid - excess_average)
                    - taker_fee_usd(
                        excess_quantity,
                        excess_book.best_bid,
                        self.config.strategy.taker_fee_rate,
                    )
                    if excess_book and excess_book.best_bid is not None and excess_average is not None
                    else 0.0
                )
                if remaining <= settings.emergency_exit_remaining_seconds:
                    self._emergency_exit(group, now)
                elif marked_loss <= -settings.max_single_leg_loss_usd:
                    if self._taker_exit(
                        group,
                        now,
                        cancel_reason="single_leg_loss_limit",
                        fill_purpose="STOP_LOSS_EXIT",
                        quote_reason="shadow_stop_loss_taker",
                    ):
                        self.status = self.last_reason = "single_leg_stop_loss_taker"
                elif unhedged_seconds >= settings.max_unhedged_seconds:
                    if exit_quote_open:
                        self.status = self.last_reason = "exit_quoted"
                    else:
                        self.cancel_quotes("unhedged_timeout", now, group.group_id)
                        self._place_exit(group, now)
                else:
                    self._place_hedge(group, now)
                return
            elif group.status == "PARTIAL":
                if remaining <= settings.stop_new_quotes_remaining_seconds:
                    self._finalize_partial_lock(group, now)
                elif not self._open_quotes(group.group_id):
                    self._place_remaining_pair(group, now)
                else:
                    self.status = self.last_reason = "partial_pair_quoting"
                return
        if group:
            if not self._open_quotes(group.group_id):
                group.status = "CLOSED"
                group.completed_at = group.updated_at = now
                self.registry.save_group(group)
            else:
                self.status = self.last_reason = "quotes_open"
                return
        if remaining <= settings.stop_new_quotes_remaining_seconds:
            self.status = self.last_reason = "stop_new_quotes_window"
            return
        completed = sum(
            1 for item in self.registry.recent_groups(500)
            if item.market_id == market.condition_id and item.status == "LOCKED"
        )
        if completed >= settings.max_pairs_per_market:
            self.status = self.last_reason = "market_pair_limit"
            return
        today = now.date()
        daily_pnl = sum(
            item.realized_pnl_usd for item in self.registry.recent_groups(2000)
            if item.completed_at and item.completed_at.date() == today
            and item.status in {"LOCKED", "CLOSED", "SETTLED"}
        )
        if daily_pnl <= -settings.daily_shadow_loss_limit_usd:
            self.status = self.last_reason = "daily_shadow_loss_limit"
            return
        if any(
            direction not in self.books
            or not self.books[direction].depth_trusted
            or (now - self.books[direction].received_at).total_seconds() > settings.book_max_age_seconds
            for direction in (Direction.UP, Direction.DOWN)
        ):
            self.status = self.last_reason = "book_unavailable_or_stale"
            return
        if self.unmeasurable_until and now < self.unmeasurable_until:
            self.status = self.last_reason = "trade_stream_unmeasurable"
            return
        if self.last_quote_closed_at and (
            now - self.last_quote_closed_at
        ).total_seconds() * 1000 < settings.reprice_cooldown_ms:
            self.status = self.last_reason = "reprice_cooldown"
            return
        pair = self._quote_pair()
        if not pair:
            self.status = self.last_reason = "no_profitable_post_only_pair"
            return
        up_price, down_price, _ = pair
        quantity = self._pair_quantity(up_price, down_price)
        if quantity <= 1e-9:
            self.status = self.last_reason = "invalid_pair_quantity"
            return
        group = MakerGroup(
            market_id=market.condition_id, slug=market.slug,
            target_quantity=quantity, created_at=now, updated_at=now,
        )
        self.groups[group.group_id] = group
        self.registry.save_group(group)
        self._create_quote(group, Direction.UP, "BUY", "PAIR", up_price, quantity, now)
        self._create_quote(group, Direction.DOWN, "BUY", "PAIR", down_price, quantity, now)
        self.status = self.last_reason = "pair_quoted"

    def settle(self, slug: str, outcome: Direction | str) -> list[MakerGroup]:
        winning = outcome if isinstance(outcome, Direction) else Direction(str(outcome).upper())
        now = utc_now()
        settled: list[MakerGroup] = []
        for group in self.groups.values():
            if group.slug != slug or group.status in {"SETTLED", "UNFILLED"}:
                continue
            self.cancel_quotes("market_settled", now, group.group_id)
            if not group.has_execution:
                group.status = "UNFILLED"
                group.completion_reason = "market_settled_no_shadow_fill"
                group.realized_pnl_usd = 0.0
                group.completed_at = group.updated_at = now
                self.registry.save_group(group)
                continue
            up_net = group.net_quantity(Direction.UP)
            down_net = group.net_quantity(Direction.DOWN)
            payout = up_net if winning == Direction.UP else down_net
            group.realized_pnl_usd = (
                payout + group.up_sell_proceeds_usd + group.down_sell_proceeds_usd
                - group.up_buy_cost_usd - group.down_buy_cost_usd - group.fees_usd
            )
            group.status = "SETTLED"
            group.completed_at = group.updated_at = now
            self.registry.save_group(group)
            settled.append(group)
        self._save_round_stats()
        return settled

    def unresolved_slugs(self) -> set[str]:
        return {
            group.slug for group in self.groups.values()
            if group.status not in {"SETTLED", "UNFILLED"} and (group.up_bought_quantity > 0 or group.down_bought_quantity > 0)
        }

    def drain_events(self) -> list[tuple[str, Any]]:
        events, self.events = self.events, []
        return events

    def dashboard_state(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or utc_now()
        market = self.market
        opportunity = self._quote_pair()
        group = self._active_group()
        groups = self.registry.recent_groups(100)
        quotes = self.registry.recent_quotes(200)
        fills = self.registry.recent_fills(200)
        groups_with_fills = [
            item for item in groups
            if item.up_bought_quantity > 1e-9 or item.down_bought_quantity > 1e-9
        ]
        completed = [
            item for item in groups_with_fills
            if item.status in {"LOCKED", "CLOSED", "SETTLED"}
        ]
        unfilled = [item for item in groups if item.status == "UNFILLED"]
        locked = [item for item in groups if item.status == "LOCKED"]
        single_leg = [
            item for item in groups_with_fills
            if item.unhedged_since is not None or item.status in {"EXITING", "CLOSED"}
        ]
        pnl = sum(item.realized_pnl_usd for item in completed)
        maker_profit = sum(max(0.0, item.realized_pnl_usd) for item in locked)
        exit_loss = sum(min(0.0, item.realized_pnl_usd) for item in completed if item.status != "LOCKED")
        quote_waits = [
            (quote.closed_at - quote.created_at).total_seconds()
            for quote in quotes if quote.status == "FILLED" and quote.closed_at
        ]
        books = {}
        for direction in (Direction.UP, Direction.DOWN):
            book = self.books.get(direction)
            books[direction.value] = {
                "best_bid": book.best_bid if book else None,
                "best_ask": book.best_ask if book else None,
                "age_seconds": (now - book.received_at).total_seconds() if book else None,
                "depth_trusted": bool(book and book.depth_trusted),
            }
        latest_print = max(self.last_trade_print_at.values(), default=None)
        return {
            "mode": "SHADOW_ONLY", "enabled": self.settings.enabled,
            "status": self.status, "last_reason": self.last_reason, "paused": self.paused,
            "config": self.settings.model_dump(mode="json"),
            "market": market.model_dump(mode="json") if market else None,
            "time_left_seconds": max(0.0, (market.end_time - now).total_seconds()) if market else None,
            "books": books,
            "trade_stream": {
                "last_print_at": latest_print.isoformat() if latest_print else None,
                "age_seconds": (now - latest_print).total_seconds() if latest_print else None,
                "measurable": not self.unmeasurable_until or now >= self.unmeasurable_until,
            },
            "opportunity": {
                "up_quote": opportunity[0] if opportunity else None,
                "down_quote": opportunity[1] if opportunity else None,
                **(opportunity[2] if opportunity else {
                    "max_total_cost_cents": 100 - self.settings.min_locked_profit_cents - self.settings.maker_fee_reserve_cents,
                    "expected_profit_cents": None,
                }),
            },
            "current_quotes": [item.model_dump(mode="json") for item in self._open_quotes()],
            "current_group": group.model_dump(mode="json") if group else None,
            "recent_groups": [item.model_dump(mode="json") for item in groups[:50]],
            "recent_quotes": [item.model_dump(mode="json") for item in quotes[:100]],
            "recent_fills": [item.model_dump(mode="json") for item in fills[:100]],
            "summary": {
                "quotes": len(quotes), "completed_groups": len(completed),
                "unfilled_groups": len(unfilled),
                "locked_groups": len(locked), "single_leg_groups": len(single_leg),
                "dual_fill_rate": len(locked) / len(completed) if completed else 0.0,
                "single_leg_rate": (
                    len(single_leg) / len(groups_with_fills) if groups_with_fills else 0.0
                ),
                "hedge_success_rate": self.round_hedge_success_count / self.round_single_leg_count if self.round_single_leg_count else 0.0,
                "average_queue_seconds": sum(quote_waits) / len(quote_waits) if quote_waits else 0.0,
                "maker_profit_usd": maker_profit, "exit_loss_usd": exit_loss,
                "net_pnl_usd": pnl,
            },
        }
