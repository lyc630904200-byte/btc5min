from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Direction(StrEnum):
    UP = "UP"
    DOWN = "DOWN"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class ExitReason(StrEnum):
    BOOK_DIRECTION_CONFLICT = "book_direction_conflict"
    REVERSE_BREAK = "reverse_break"
    EDGE_FADED = "edge_faded"
    MAX_LOSS_USD = "max_loss_usd"
    TAKE_PROFIT = "take_profit"
    MAX_HOLD_SECONDS = "max_hold_seconds"
    FORCE_EXIT = "force_exit"
    HELD_TO_EXPIRY = "held_to_expiry"
    SETTLEMENT_WIN = "settlement_win"
    SETTLEMENT_LOSS = "settlement_loss"
    DATA_STALE = "data_stale"


class BookLevel(BaseModel):
    price: float
    size: float


class OrderBookSnapshot(BaseModel):
    token_id: str
    market_id: str | None = None
    # Exchange sequence time, used to reject an out-of-order order-book update.
    timestamp: datetime = Field(default_factory=utc_now)
    # Local arrival time, used for the trading freshness guard.  The CLOB's
    # timestamp can lag the local clock even when the WebSocket is healthy.
    received_at: datetime = Field(default_factory=utc_now)
    bids: list[BookLevel] = Field(default_factory=list)
    asks: list[BookLevel] = Field(default_factory=list)
    depth_trusted: bool = False
    min_order_size: float = 5.0
    tick_size: float = 0.01
    raw: dict[str, Any] | None = None

    @property
    def best_bid(self) -> float | None:
        return max((level.price for level in self.bids), default=None)

    @property
    def best_ask(self) -> float | None:
        return min((level.price for level in self.asks), default=None)


def rest_request_started_at(book: OrderBookSnapshot) -> datetime | None:
    if not isinstance(book.raw, dict) or book.raw.get("_transport") != "rest":
        return None
    value = book.raw.get("_request_started_at")
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class MarketState(BaseModel):
    condition_id: str
    slug: str
    question: str
    threshold_price: float | None
    threshold_source: str | None = None
    threshold_observed_at: datetime | None = None
    threshold_verified: bool = False
    threshold_fetched_at: datetime | None = None
    threshold_candidate_price: float | None = None
    threshold_candidate_source: str | None = None
    threshold_candidate_observed_at: datetime | None = None
    threshold_candidate_received_at: datetime | None = None
    threshold_candidate_conflicted: bool = False
    start_time: datetime | None = None
    end_time: datetime
    up_token_id: str
    down_token_id: str
    min_order_size: float = 5.0
    tick_size: float = 0.01
    accepting_orders: bool = True
    settlement_verified: bool = True
    observe_only: bool = False
    raw: dict[str, Any] | None = None


class PriceTick(BaseModel):
    source: str = "binance"
    symbol: str = "BTCUSDT"
    price: float
    exchange_timestamp: datetime | None = None
    received_at: datetime = Field(default_factory=utc_now)


class Fill(BaseModel):
    fill_id: str
    position_id: str | None = None
    market_id: str
    token_id: str
    direction: Direction
    side: OrderSide
    avg_price: float
    quantity: float
    quote: float
    slippage: float
    fee_usd: float = 0.0
    strategy_direction: Direction | None = None
    reverse_entry: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    reason: str


class Position(BaseModel):
    position_id: str
    market_id: str
    token_id: str
    direction: Direction
    entry_price: float
    quantity: float
    entry_quote: float
    entry_fee_usd: float = 0.0
    exit_fee_usd: float = 0.0
    taker_fee_rate: float = 0.0
    opened_at: datetime
    entry_edge_usd: float
    strategy_direction: Direction | None = None
    reverse_entry: bool = False
    strategy_entry_price: float | None = None
    strategy_quantity: float | None = None
    strategy_entry_quote: float | None = None
    strategy_entry_fee_usd: float = 0.0
    status: str = "OPEN"
    exit_price: float | None = None
    exit_quote: float = 0.0
    realized_pnl: float = 0.0
    exit_reason: ExitReason | None = None
    closed_at: datetime | None = None

    def unrealized_pnl(self, current_bid: float | None) -> float:
        if current_bid is None or self.status != "OPEN":
            return 0.0
        exit_fee = self.quantity * self.taker_fee_rate * current_bid * (1.0 - current_bid)
        return self.quantity * current_bid - exit_fee - self.entry_quote


class Signal(BaseModel):
    signal_id: str
    market_id: str
    direction: Direction
    binance_price: float
    threshold_price: float
    edge_usd: float
    ask_price: float
    execution_direction: Direction | None = None
    reverse_entry: bool = False
    reason: str
    created_at: datetime = Field(default_factory=utc_now)


class ExitEvent(BaseModel):
    position_id: str
    market_id: str
    direction: Direction
    reason: ExitReason
    edge_usd: float | None = None
    price: float | None = None
    quantity: float
    pnl: float
    fee_usd: float = 0.0
    strategy_direction: Direction | None = None
    reverse_entry: bool = False
    created_at: datetime = Field(default_factory=utc_now)


class PnLReport(BaseModel):
    total_positions: int = 0
    closed_positions: int = 0
    open_positions: int = 0
    realized_pnl: float = 0.0
    normal_realized_pnl: float = 0.0
    reverse_realized_pnl: float = 0.0
    settlement_pnl: float = 0.0
    take_profit_pnl: float = 0.0
    risk_exit_pnl: float = 0.0
    total_quote: float = 0.0
