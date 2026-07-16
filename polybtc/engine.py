from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .config import AppConfig
from .models import Direction, ExitReason, MarketState, OrderBookSnapshot, Position, PriceTick, rest_request_started_at
from .strategy import StrategyState, evaluate_entry, evaluate_exit, position_from_entry, settle_position


MAX_RECENT_REJECTIONS = 500
REJECTION_RECORD_INTERVAL = timedelta(seconds=1)


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
        self.rejection_count = 0
        self.last_rejection_at_by_reason: dict[str, datetime] = {}
        self.market_exposure_usd = 0.0
        self.entry_confirmation_direction: Direction | None = None
        self.entry_confirmation_started_at: datetime | None = None
        self.entry_confirmation_last_at: datetime | None = None
        self.entry_confirmation_updates = 0

    def reset_entry_confirmation(self) -> None:
        self.entry_confirmation_direction = None
        self.entry_confirmation_started_at = None
        self.entry_confirmation_last_at = None
        self.entry_confirmation_updates = 0

    def entry_is_confirmed(self, direction: Direction, now: datetime, observed_at: datetime) -> bool:
        strategy = self.config.strategy
        max_gap_seconds = max(strategy.entry_confirmation_seconds, self.config.risk.max_data_age_ms / 1000)
        gap_too_large = (
            self.entry_confirmation_last_at is not None
            and (observed_at - self.entry_confirmation_last_at).total_seconds() > max_gap_seconds
        )
        if self.entry_confirmation_direction != direction or gap_too_large:
            self.entry_confirmation_direction = direction
            self.entry_confirmation_started_at = now
            self.entry_confirmation_last_at = observed_at
            self.entry_confirmation_updates = 1
            return False
        if self.entry_confirmation_last_at is not None and observed_at <= self.entry_confirmation_last_at:
            return False
        self.entry_confirmation_updates += 1
        self.entry_confirmation_last_at = observed_at
        started_at = self.entry_confirmation_started_at or now
        elapsed = max(0.0, (now - started_at).total_seconds())
        return elapsed >= strategy.entry_confirmation_seconds and self.entry_confirmation_updates >= strategy.entry_confirmation_updates

    def record_rejection(self, reason: str, now: datetime | None = None) -> None:
        recorded_at = now or datetime.now(timezone.utc)
        previous = self.last_rejection_at_by_reason.get(reason)
        if previous:
            elapsed = recorded_at - previous
            if timedelta(0) <= elapsed < REJECTION_RECORD_INTERVAL:
                return
        self.last_rejection_at_by_reason[reason] = recorded_at
        self.rejection_count += 1
        self.rejections.append({"created_at": recorded_at.isoformat(), "reason": reason})
        if len(self.rejections) > MAX_RECENT_REJECTIONS:
            del self.rejections[:-MAX_RECENT_REJECTIONS]

    def set_market(self, market: MarketState) -> None:
        is_new_market = self.market is None or self.market.condition_id != market.condition_id
        if self.market and is_new_market and self.open_position:
            self._settle_if_expired(datetime.now(timezone.utc), force=True)
        self.market = market
        if is_new_market:
            self.books = {}
            self.reset_entry_confirmation()
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

    def edge_correction_usd(self) -> float | None:
        if self.tick and self.polymarket_tick:
            return self.tick.price - self.polymarket_tick.price
        return None

    def edge_correction_source(self) -> str:
        return "binance_minus_polymarket" if self.edge_correction_usd() is not None else "unavailable_zero"

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
            self.record_rejection("stale_book_market")
            return
        existing = self.books.get(direction)
        request_started_at = rest_request_started_at(book)
        if (
            existing
            and request_started_at is not None
            and book.timestamp <= existing.timestamp
            and existing.received_at > request_started_at
        ):
            # The WebSocket advanced while this REST request was in flight.
            # Its late response is no longer a safe reconciliation snapshot.
            return
        if existing and book.timestamp < existing.timestamp:
            # A REST fallback is only requested after the locally received
            # WebSocket book is stale.  CLOB REST timestamps can lag a silent
            # WebSocket frame, so accept that fresh local snapshot while
            # retaining the WebSocket timestamp as the sequence watermark.
            is_fresh_rest_fallback = (
                isinstance(book.raw, dict)
                and book.raw.get("_transport") == "rest"
                and book.received_at > existing.received_at
            )
            if is_fresh_rest_fallback:
                book.timestamp = existing.timestamp
            else:
                # Duplicate and out-of-order CLOB frames are expected on a busy
                # WebSocket.  They are not trading rejections, so drop them
                # silently instead of adding logging and dashboard pressure.
                return
        elif existing and book.timestamp == existing.timestamp and book.received_at < existing.received_at:
            return
        self.books[direction] = book
        self.evaluate(book.received_at)

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
            self.reset_entry_confirmation()
            exit_decision = evaluate_exit(self.open_position, state, self.config.strategy, self.config.risk)
            if exit_decision.should_exit and exit_decision.fill and exit_decision.event:
                self.fills.append(exit_decision.fill)
                self.exit_events.append(exit_decision.event)
                self._apply_exit(
                    exit_decision.event.reason,
                    exit_decision.fill.avg_price,
                    exit_decision.fill.quote - exit_decision.fill.fee_usd,
                    exit_decision.event.pnl,
                    now,
                    fee_usd=exit_decision.fill.fee_usd,
                )
            else:
                self._settle_if_expired(now)
            return

        entry = evaluate_entry(state, self.config.strategy, self.config.risk)
        if entry.accepted and entry.signal and entry.fill:
            observed_at = self.polymarket_tick.received_at if self.polymarket_tick else self.tick.received_at
            if not self.entry_is_confirmed(entry.signal.direction, now, observed_at):
                return
            self.reset_entry_confirmation()
            self.signals.append(entry.signal)
            self.fills.append(entry.fill)
            edge = entry.signal.edge_usd
            self.open_position = position_from_entry(
                entry.fill,
                edge=edge,
                opened_at=now,
                taker_fee_rate=self.config.strategy.taker_fee_rate,
            )
            self.positions.append(self.open_position)
            self.market_exposure_usd += self.open_position.entry_quote
        else:
            self.reset_entry_confirmation()
            self.record_rejection(entry.reason, now)

    def _apply_exit(
        self,
        reason: ExitReason,
        price: float,
        quote: float,
        pnl: float,
        now: datetime,
        fee_usd: float = 0.0,
    ) -> None:
        if not self.open_position:
            return
        position = self.open_position
        position.exit_price = price
        position.exit_quote += quote
        position.exit_fee_usd += fee_usd
        position.realized_pnl += pnl
        position.exit_reason = reason
        position.closed_at = now
        position.status = "CLOSED"
        self.open_position = None
        self.reset_entry_confirmation()

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
            if position.exit_reason in {
                ExitReason.BOOK_DIRECTION_CONFLICT,
                ExitReason.EDGE_FADED,
                ExitReason.REVERSE_BREAK,
                ExitReason.MAX_HOLD_SECONDS,
                ExitReason.FORCE_EXIT,
            }
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
            "rejections": self.rejection_count,
            "realized_pnl": realized,
            "take_profit_pnl": take_profit,
            "risk_exit_pnl": risk_exit,
            "settlement_pnl": settlement,
            "total_quote": sum(pos.entry_quote for pos in self.positions),
            "entry_confirmation_updates": self.entry_confirmation_updates,
            "fees_paid_usd": sum(fill.fee_usd for fill in self.fills),
        }
