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
    REVERSE_BREAK = "reverse_break"
    EDGE_FADED = "edge_faded"
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
    timestamp: datetime = Field(default_factory=utc_now)
    bids: list[BookLevel] = Field(default_factory=list)
    asks: list[BookLevel] = Field(default_factory=list)
    min_order_size: float = 5.0
    tick_size: float = 0.01
    raw: dict[str, Any] | None = None

    @property
    def best_bid(self) -> float | None:
        return max((level.price for level in self.bids), default=None)

    @property
    def best_ask(self) -> float | None:
        return min((level.price for level in self.asks), default=None)


class MarketState(BaseModel):
    condition_id: str
    slug: str
    question: str
    threshold_price: float | None
    threshold_source: str | None = None
    threshold_observed_at: datetime | None = None
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
    opened_at: datetime
    entry_edge_usd: float
    status: str = "OPEN"
    exit_price: float | None = None
    exit_quote: float = 0.0
    realized_pnl: float = 0.0
    exit_reason: ExitReason | None = None
    closed_at: datetime | None = None

    def unrealized_pnl(self, current_bid: float | None) -> float:
        if current_bid is None or self.status != "OPEN":
            return 0.0
        return self.quantity * (current_bid - self.entry_price)


class Signal(BaseModel):
    signal_id: str
    market_id: str
    direction: Direction
    binance_price: float
    threshold_price: float
    edge_usd: float
    ask_price: float
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
    created_at: datetime = Field(default_factory=utc_now)


class PnLReport(BaseModel):
    total_positions: int = 0
    closed_positions: int = 0
    open_positions: int = 0
    realized_pnl: float = 0.0
    settlement_pnl: float = 0.0
    take_profit_pnl: float = 0.0
    risk_exit_pnl: float = 0.0
    total_quote: float = 0.0
