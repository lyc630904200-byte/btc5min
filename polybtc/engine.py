from __future__ import annotations

from datetime import datetime, timezone

from .config import AppConfig
from .models import Direction, ExitReason, MarketState, OrderBookSnapshot, Position, PriceTick
from .strategy import StrategyState, evaluate_entry, evaluate_exit, position_from_entry, settle_position


class PaperEngine:
    def __init__(self, config: AppConfig):
        self.config = config
        self.market: MarketState | None = None
        self.tick: PriceTick | None = None
        self.polymarket_tick: PriceTick | None = None
        self.books: dict[Direction, OrderBookSnapshot] = {}
        self.open_position: Position | None = None
        self.positions: list[Position] = []
        self.signals = []
        self.fills = []
        self.exit_events = []
        self.rejections: list[dict[str, str]] = []
        self.market_exposure_usd = 0.0

    def set_market(self, market: MarketState) -> None:
        is_new_market = self.market is None or self.market.condition_id != market.condition_id
        if self.market and is_new_market and self.open_position:
            self._settle_if_expired(datetime.now(timezone.utc), force=True)
        self.market = market
        if is_new_market:
            self.books = {}
        self.market_exposure_usd = sum(pos.entry_quote for pos in self.positions if pos.market_id == market.condition_id)

    def book_matches_current_market(self, direction: Direction, book: OrderBookSnapshot) -> bool:
        if not self.market:
            return False
        expected_token = self.market.up_token_id if direction == Direction.UP else self.market.down_token_id
        if book.token_id != expected_token:
            return False
        if book.market_id and book.market_id != self.market.condition_id:
            return False
        return True

    def set_tick(self, tick: PriceTick) -> None:
        self.tick = tick
        self.capture_dynamic_threshold(tick)
        self.evaluate(tick.received_at)

    def set_polymarket_tick(self, tick: PriceTick) -> None:
        self.polymarket_tick = tick
        self.evaluate(tick.received_at)

    def edge_correction_usd(self) -> float:
        if self.tick and self.polymarket_tick:
            return self.polymarket_tick.price - self.tick.price
        return self.config.strategy.edge_correction_usd

    def edge_correction_source(self) -> str:
        if self.tick and self.polymarket_tick:
            return "polymarket_minus_binance"
        return "configured_fallback"

    def capture_dynamic_threshold(self, tick: PriceTick) -> bool:
        if not self.market or self.market.threshold_price is not None:
            return False
        if self.market.threshold_source != "dynamic_start_price" or not self.market.start_time:
            return False
        if self.market.observe_only or not self.market.settlement_verified:
            return False
        source_time = tick.exchange_timestamp or tick.received_at
        lag_ms = (source_time - self.market.start_time).total_seconds() * 1000
        if lag_ms < 0 or lag_ms > self.config.sources.max_start_price_lag_ms:
            return False
        self.market.threshold_price = tick.price
        self.market.threshold_observed_at = source_time
        self.market.threshold_source = f"{tick.source}_first_tick_after_start"
        return True

    def apply_threshold(self, price: float, source: str, observed_at: datetime) -> bool:
        if not self.market or self.market.threshold_price is not None:
            return False
        if self.market.observe_only or not self.market.settlement_verified:
            return False
        self.market.threshold_price = price
        self.market.threshold_source = source
        self.market.threshold_observed_at = observed_at
        return True

    def set_book(self, direction: Direction, book: OrderBookSnapshot) -> None:
        if not self.book_matches_current_market(direction, book):
            self.rejections.append({"created_at": datetime.now(timezone.utc).isoformat(), "reason": "stale_book_market"})
            return
        existing = self.books.get(direction)
        if existing and book.timestamp <= existing.timestamp:
            self.rejections.append({"created_at": datetime.now(timezone.utc).isoformat(), "reason": "stale_book_timestamp"})
            return
        self.books[direction] = book
        self.evaluate(book.timestamp)

    def ready(self) -> bool:
        return self.market is not None and self.tick is not None and Direction.UP in self.books and Direction.DOWN in self.books

    def evaluate(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        if not self.ready():
            return
        assert self.market and self.tick
        state = StrategyState(
            market=self.market,
            price_tick=self.tick,
            up_book=self.books[Direction.UP],
            down_book=self.books[Direction.DOWN],
            now=now,
            market_exposure_usd=self.market_exposure_usd,
            edge_correction_usd=self.edge_correction_usd(),
        )
        if self.open_position:
            exit_decision = evaluate_exit(self.open_position, state, self.config.strategy, self.config.risk)
            if exit_decision.should_exit and exit_decision.fill and exit_decision.event:
                self.fills.append(exit_decision.fill)
                self.exit_events.append(exit_decision.event)
                self._apply_exit(exit_decision.event.reason, exit_decision.fill.avg_price, exit_decision.fill.quote, exit_decision.event.pnl, now)
            else:
                self._settle_if_expired(now)
            return

        entry = evaluate_entry(state, self.config.strategy, self.config.risk)
        if entry.accepted and entry.signal and entry.fill:
            self.signals.append(entry.signal)
            self.fills.append(entry.fill)
            edge = entry.signal.edge_usd
            self.open_position = position_from_entry(entry.fill, edge=edge, opened_at=now)
            self.positions.append(self.open_position)
            self.market_exposure_usd += entry.fill.quote
        else:
            self.rejections.append({"created_at": now.isoformat(), "reason": entry.reason})

    def _apply_exit(self, reason: ExitReason, price: float, quote: float, pnl: float, now: datetime) -> None:
        if not self.open_position:
            return
        position = self.open_position
        position.exit_price = price
        position.exit_quote += quote
        position.realized_pnl += pnl
        position.exit_reason = reason
        position.closed_at = now
        position.status = "CLOSED"
        self.open_position = None

    def _settle_if_expired(self, now: datetime, force: bool = False) -> None:
        if not self.open_position or not self.market or not self.tick:
            return
        if not force and now < self.market.end_time:
            return
        event = settle_position(self.open_position, self.market, self.tick.price, now=now)
        self.exit_events.append(event)
        self._apply_exit(event.reason, event.price or 0.0, event.quantity * (event.price or 0.0), event.pnl, now)

    def summary(self) -> dict[str, float | int]:
        realized = sum(position.realized_pnl for position in self.positions)
        take_profit = sum(
            position.realized_pnl for position in self.positions if position.exit_reason == ExitReason.TAKE_PROFIT
        )
        risk_exit = sum(
            position.realized_pnl
            for position in self.positions
            if position.exit_reason in {ExitReason.EDGE_FADED, ExitReason.REVERSE_BREAK, ExitReason.MAX_HOLD_SECONDS, ExitReason.FORCE_EXIT}
        )
        settlement = sum(
            position.realized_pnl
            for position in self.positions
            if position.exit_reason in {ExitReason.SETTLEMENT_WIN, ExitReason.SETTLEMENT_LOSS}
        )
        return {
            "total_positions": len(self.positions),
            "closed_positions": len([pos for pos in self.positions if pos.status == "CLOSED"]),
            "open_positions": len([pos for pos in self.positions if pos.status == "OPEN"]),
            "fills": len(self.fills),
            "signals": len(self.signals),
            "rejections": len(self.rejections),
            "realized_pnl": realized,
            "take_profit_pnl": take_profit,
            "risk_exit_pnl": risk_exit,
            "settlement_pnl": settlement,
            "total_quote": sum(pos.entry_quote for pos in self.positions),
        }
