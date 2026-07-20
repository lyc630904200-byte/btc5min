from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from .config import AppConfig
from .entry_registry import InMemoryMarketEntryRegistry, MarketEntryRegistry
from .models import Direction, ExitReason, MarketState, OrderBookSnapshot, Position, PriceTick, rest_request_started_at
from .strategy import StrategyState, evaluate_entry, evaluate_exit, position_from_entry, settle_position


MAX_RECENT_REJECTIONS = 500
REJECTION_RECORD_INTERVAL = timedelta(seconds=1)
RTDS_THRESHOLD_EARLY_TOLERANCE = timedelta(seconds=1)
RTDS_THRESHOLD_LATE_TOLERANCE = timedelta(seconds=2)
RTDS_DUPLICATE_PRICE_TOLERANCE = 1e-9


class PaperEngine:
    def __init__(
        self,
        config: AppConfig,
        entry_registry: MarketEntryRegistry | None = None,
        run_id: str = "memory",
        asset: str = "BTC",
    ):
        self.config = config
        self.asset = asset.upper()
        self.entry_registry = entry_registry or InMemoryMarketEntryRegistry()
        self.run_id = run_id
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
        self.market_trade_count = 0
        self.entry_enabled = True
        self.entry_confirmation_direction: Direction | None = None
        self.entry_confirmation_started_at: datetime | None = None
        self.entry_confirmation_last_at: datetime | None = None
        self.entry_confirmation_updates = 0
        self.polymarket_ticks_by_exchange_time: dict[datetime, PriceTick] = {}
        self.polymarket_tick_conflicts: set[datetime] = set()

    def reset_entry_confirmation(self) -> None:
        self.entry_confirmation_direction = None
        self.entry_confirmation_started_at = None
        self.entry_confirmation_last_at = None
        self.entry_confirmation_updates = 0

    def entry_is_confirmed(self, direction: Direction, now: datetime, observed_at: datetime) -> bool:
        strategy = self.config.strategy
        if not strategy.entry_confirmation_enabled:
            self.reset_entry_confirmation()
            return True
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
        self.apply_polymarket_start_threshold_candidate()
        if is_new_market:
            self.books = {}
            self.reset_entry_confirmation()
        self.market_exposure_usd = sum(pos.entry_quote for pos in self.positions if pos.market_id == market.condition_id)
        try:
            self.market_trade_count = self.entry_registry.count(market.condition_id)
        except Exception:
            self.market_trade_count = self.config.risk.max_trades_per_market
            self.record_rejection("market_entry_registry_error")

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
        self.evaluate_after_market_data(tick.received_at)

    def set_polymarket_tick(self, tick: PriceTick) -> None:
        if tick.symbol.upper() != f"{self.asset}/USD":
            return
        self.remember_polymarket_tick(tick)
        self.polymarket_tick = tick
        self.apply_polymarket_start_threshold_candidate()
        self.evaluate_after_market_data(tick.received_at)

    def remember_polymarket_tick(self, tick: PriceTick) -> None:
        if (
            tick.source != "polymarket_rtds"
            or tick.symbol.upper() != f"{self.asset}/USD"
            or tick.exchange_timestamp is None
            or not math.isfinite(tick.price)
        ):
            return
        exchange_time = tick.exchange_timestamp
        if exchange_time.tzinfo is None:
            exchange_time = exchange_time.replace(tzinfo=timezone.utc)
        else:
            exchange_time = exchange_time.astimezone(timezone.utc)
        existing = self.polymarket_ticks_by_exchange_time.get(exchange_time)
        if existing is not None and abs(existing.price - tick.price) > RTDS_DUPLICATE_PRICE_TOLERANCE:
            self.polymarket_tick_conflicts.add(exchange_time)
        else:
            self.polymarket_ticks_by_exchange_time[exchange_time] = tick
        cutoff = tick.received_at - timedelta(minutes=10)
        for timestamp in list(self.polymarket_ticks_by_exchange_time):
            if timestamp < cutoff:
                self.polymarket_ticks_by_exchange_time.pop(timestamp, None)
                self.polymarket_tick_conflicts.discard(timestamp)

    def apply_polymarket_start_threshold_candidate(self) -> bool:
        market = self.market
        if market is None or market.start_time is None:
            return False
        start_time = market.start_time.astimezone(timezone.utc)
        if start_time in self.polymarket_tick_conflicts:
            changed = not market.threshold_candidate_conflicted or market.threshold_candidate_price is not None
            market.threshold_candidate_price = None
            market.threshold_candidate_source = "polymarket_rtds_conflict"
            market.threshold_candidate_observed_at = start_time
            market.threshold_candidate_received_at = None
            market.threshold_candidate_conflicted = True
            return changed
        tick = self.polymarket_ticks_by_exchange_time.get(start_time)
        if (
            tick is None
            or tick.received_at < start_time - RTDS_THRESHOLD_EARLY_TOLERANCE
            or tick.received_at > start_time + RTDS_THRESHOLD_LATE_TOLERANCE
        ):
            return False
        changed = (
            market.threshold_candidate_price != tick.price
            or market.threshold_candidate_observed_at != start_time
            or market.threshold_candidate_conflicted
        )
        market.threshold_candidate_price = tick.price
        market.threshold_candidate_source = "polymarket_rtds_start_tick"
        market.threshold_candidate_observed_at = start_time
        market.threshold_candidate_received_at = tick.received_at
        market.threshold_candidate_conflicted = False
        return changed

    def polymarket_price_is_fresh(self, now: datetime | None = None) -> bool:
        if self.polymarket_tick is None:
            return False
        reference_time = now or (self.tick.received_at if self.tick else self.polymarket_tick.received_at)
        age_seconds = (reference_time - self.polymarket_tick.received_at).total_seconds()
        return 0 <= age_seconds <= self.config.sources.rtds_stale_seconds

    def edge_correction_usd(self, now: datetime | None = None) -> float | None:
        if self.tick and self.polymarket_tick and self.polymarket_price_is_fresh(now):
            return self.tick.price - self.polymarket_tick.price
        return None

    def edge_correction_source(self, now: datetime | None = None) -> str:
        if self.edge_correction_usd(now) is not None:
            return "binance_minus_polymarket"
        return "polymarket_price_stale" if self.polymarket_tick else "polymarket_price_unavailable"

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
        self.market.threshold_verified = False
        self.market.threshold_fetched_at = tick.received_at
        return True

    def apply_threshold(self, price: float, source: str, observed_at: datetime) -> bool:
        if not self.market or self.market.threshold_price is not None:
            return False
        if self.market.observe_only or not self.market.settlement_verified:
            return False
        self.market.threshold_price = price
        self.market.threshold_source = source
        self.market.threshold_observed_at = observed_at
        self.market.threshold_verified = False
        self.market.threshold_fetched_at = datetime.now(timezone.utc)
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
        self.evaluate_after_market_data(book.received_at)

    def evaluate_after_market_data(self, now: datetime) -> None:
        if not self.entry_enabled and self.open_position is None:
            if self.ready():
                self.reset_entry_confirmation()
                self.record_rejection("pair_match_replaces_single_strategy", now)
            return
        self.evaluate(now)

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
            market_trade_count=self.market_trade_count,
            edge_correction_usd=self.edge_correction_usd(now),
            # Historical replay files from before RTDS support have no
            # Polymarket ticks.  They retain their original raw-edge replay
            # behavior; once RTDS has produced a tick, it must stay fresh for
            # any subsequent live entry.
            polymarket_price_fresh=(
                self.polymarket_price_is_fresh(now) if self.polymarket_tick is not None else True
            ),
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

        if not self.entry_enabled:
            self.reset_entry_confirmation()
            self.record_rejection("pair_match_replaces_single_strategy", now)
            return

        entry = evaluate_entry(state, self.config.strategy, self.config.risk)
        if entry.accepted and entry.signal and entry.fill:
            observed_at = self.polymarket_tick.received_at if self.polymarket_tick else self.tick.received_at
            if not self.entry_is_confirmed(entry.signal.direction, now, observed_at):
                return
            self.reset_entry_confirmation()
            position_id = entry.fill.position_id or ""
            try:
                claimed = self.entry_registry.claim(
                    self.market.condition_id,
                    position_id,
                    now,
                    self.run_id,
                    self.config.risk.max_trades_per_market,
                )
            except Exception:
                self.record_rejection("market_entry_registry_error", now)
                return
            if not claimed:
                self.market_trade_count = self.config.risk.max_trades_per_market
                self.record_rejection("market_trade_limit", now)
                return
            self.market_trade_count += 1
            self.signals.append(entry.signal)
            self.fills.append(entry.fill)
            edge = entry.signal.edge_usd
            self.open_position = position_from_entry(
                entry.fill,
                edge=edge,
                opened_at=now,
                taker_fee_rate=self.config.strategy.taker_fee_rate,
                strategy_execution=entry.strategy_execution,
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
        normal_realized = sum(position.realized_pnl for position in self.positions if not position.reverse_entry)
        reverse_realized = sum(position.realized_pnl for position in self.positions if position.reverse_entry)
        realized = normal_realized + reverse_realized
        take_profit = sum(
            position.realized_pnl for position in self.positions if position.exit_reason == ExitReason.TAKE_PROFIT
        )
        risk_exit = sum(
            position.realized_pnl
            for position in self.positions
            if position.exit_reason in {
                ExitReason.BOOK_DIRECTION_CONFLICT,
                ExitReason.EDGE_FADED,
                ExitReason.MAX_LOSS_USD,
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
            "normal_realized_pnl": normal_realized,
            "reverse_realized_pnl": reverse_realized,
            "take_profit_pnl": take_profit,
            "risk_exit_pnl": risk_exit,
            "settlement_pnl": settlement,
            "total_quote": sum(pos.entry_quote for pos in self.positions),
            "entry_confirmation_updates": self.entry_confirmation_updates,
            "current_market_trade_count": self.market_trade_count,
            "max_trades_per_market": self.config.risk.max_trades_per_market,
            "max_loss_usd": self.config.risk.max_loss_usd,
            "max_loss_exit_count": len(
                [position for position in self.positions if position.exit_reason == ExitReason.MAX_LOSS_USD]
            ),
            "fees_paid_usd": sum(fill.fee_usd for fill in self.fills),
        }
