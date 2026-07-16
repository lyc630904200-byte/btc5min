from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from .config import RiskConfig, StrategyConfig
from .models import Direction, ExitEvent, ExitReason, Fill, MarketState, OrderBookSnapshot, OrderSide, Position, PriceTick, Signal
from .orderbook import ExecutionResult, simulate_buy, simulate_sell


@dataclass(frozen=True)
class StrategyState:
    market: MarketState
    price_tick: PriceTick
    up_book: OrderBookSnapshot
    down_book: OrderBookSnapshot
    now: datetime
    market_exposure_usd: float = 0.0
    edge_correction_usd: float | None = None


@dataclass(frozen=True)
class EntryDecision:
    accepted: bool
    reason: str
    signal: Signal | None = None
    fill: Fill | None = None
    execution: ExecutionResult | None = None


@dataclass(frozen=True)
class ExitDecision:
    should_exit: bool
    reason: ExitReason | None = None
    fill: Fill | None = None
    event: ExitEvent | None = None


def age_ms(ts: datetime, now: datetime) -> float:
    return max(0.0, (now - ts).total_seconds() * 1000)


def seconds_to_expiry(market: MarketState, now: datetime) -> float:
    return (market.end_time - now).total_seconds()


def edge_usd(market: MarketState, tick: PriceTick) -> float | None:
    if market.threshold_price is None:
        return None
    return tick.price - market.threshold_price


def corrected_edge_usd(
    market: MarketState,
    tick: PriceTick,
    edge_correction_usd: float | None = None,
) -> float | None:
    edge = edge_usd(market, tick)
    if edge is None:
        return None
    return edge - (edge_correction_usd or 0.0)


def orderbook_direction(up_book: OrderBookSnapshot, down_book: OrderBookSnapshot) -> Direction | None:
    """Return the outcome favored by the two-sided order book, if decisive."""
    up_bid, up_ask = up_book.best_bid, up_book.best_ask
    down_bid, down_ask = down_book.best_bid, down_book.best_ask
    if None in {up_bid, up_ask, down_bid, down_ask}:
        return None
    up_mid = (up_bid + up_ask) / 2
    down_mid = (down_bid + down_ask) / 2
    if up_mid > down_mid:
        return Direction.UP
    if down_mid > up_mid:
        return Direction.DOWN
    return None


def edge_direction(edge: float | None) -> Direction | None:
    if edge is None or edge == 0:
        return None
    return Direction.UP if edge > 0 else Direction.DOWN


def validate_common(state: StrategyState, strategy: StrategyConfig, risk: RiskConfig) -> str | None:
    market = state.market
    if market.observe_only:
        return "observe_only_market"
    if market.threshold_price is None:
        return "threshold_unavailable"
    if not market.settlement_verified:
        return "settlement_unverified"
    if not market.accepting_orders:
        return "market_not_accepting_orders"
    if market.start_time is not None and state.now < market.start_time:
        return "market_not_started"
    remaining_seconds = seconds_to_expiry(market, state.now)
    if remaining_seconds > strategy.max_seconds_to_entry:
        return "too_early_to_entry"
    if remaining_seconds < strategy.min_seconds_to_entry:
        return "too_close_to_expiry"
    if age_ms(state.price_tick.received_at, state.now) > risk.max_data_age_ms:
        return "binance_data_stale"
    if age_ms(state.up_book.received_at, state.now) > risk.max_data_age_ms:
        return "up_book_stale"
    if age_ms(state.down_book.received_at, state.now) > risk.max_data_age_ms:
        return "down_book_stale"
    if state.market_exposure_usd + risk.max_order_usd > risk.max_market_usd:
        return "market_exposure_limit"
    return None


def evaluate_entry(
    state: StrategyState,
    strategy: StrategyConfig,
    risk: RiskConfig,
    has_open_position: bool = False,
) -> EntryDecision:
    common_error = validate_common(state, strategy, risk)
    if common_error:
        return EntryDecision(False, common_error)
    if has_open_position:
        return EntryDecision(False, "open_position_exists")

    edge = corrected_edge_usd(state.market, state.price_tick, state.edge_correction_usd)
    if edge is None:
        return EntryDecision(False, "threshold_unavailable")

    if edge > strategy.min_entry_edge_usd:
        direction = Direction.UP
        book = state.up_book
        token_id = state.market.up_token_id
    elif edge < -strategy.min_entry_edge_usd:
        direction = Direction.DOWN
        book = state.down_book
        token_id = state.market.down_token_id
    else:
        return EntryDecision(False, "edge_too_small")

    ask = book.best_ask
    if ask is None:
        return EntryDecision(False, "ask_unavailable")
    if ask < strategy.min_buy_price:
        return EntryDecision(False, "ask_too_cheap")
    if ask > strategy.max_buy_price:
        return EntryDecision(False, "ask_too_expensive")

    book_direction = orderbook_direction(state.up_book, state.down_book)
    if book_direction is not None and book_direction != direction:
        return EntryDecision(False, "book_direction_conflicts_with_edge")

    execution = simulate_buy(book, risk.max_order_usd, strategy.taker_fee_rate)
    if not execution.complete:
        return EntryDecision(False, "depth_insufficient", execution=execution)
    if execution.quantity < state.market.min_order_size:
        return EntryDecision(False, "below_min_order_size", execution=execution)
    fee_per_share = execution.fee_usd / execution.quantity if execution.quantity else 0.0
    if 1.0 - execution.avg_price - fee_per_share < strategy.min_profit_after_slippage:
        return EntryDecision(False, "profit_after_slippage_too_low", execution=execution)

    signal_id = str(uuid4())
    position_id = str(uuid4())
    signal = Signal(
        signal_id=signal_id,
        market_id=state.market.condition_id,
        direction=direction,
        binance_price=state.price_tick.price,
        threshold_price=state.market.threshold_price,
        edge_usd=edge,
        ask_price=ask,
        reason="entry_edge",
        created_at=state.now,
    )
    fill = Fill(
        fill_id=str(uuid4()),
        position_id=position_id,
        market_id=state.market.condition_id,
        token_id=token_id,
        direction=direction,
        side=OrderSide.BUY,
        avg_price=execution.avg_price,
        quantity=execution.quantity,
        quote=execution.quote,
        slippage=execution.slippage,
        fee_usd=execution.fee_usd,
        created_at=state.now,
        reason="entry_edge",
    )
    return EntryDecision(True, "accepted", signal=signal, fill=fill, execution=execution)


def position_from_entry(
    fill: Fill,
    edge: float,
    opened_at: datetime | None = None,
    taker_fee_rate: float = 0.0,
) -> Position:
    return Position(
        position_id=fill.position_id or str(uuid4()),
        market_id=fill.market_id,
        token_id=fill.token_id,
        direction=fill.direction,
        entry_price=fill.avg_price,
        quantity=fill.quantity,
        entry_quote=fill.quote + fill.fee_usd,
        entry_fee_usd=fill.fee_usd,
        taker_fee_rate=taker_fee_rate,
        opened_at=opened_at or fill.created_at,
        entry_edge_usd=edge,
    )


def choose_exit_reason(
    position: Position,
    state: StrategyState,
    strategy: StrategyConfig,
    risk: RiskConfig,
) -> ExitReason | None:
    edge = corrected_edge_usd(state.market, state.price_tick, state.edge_correction_usd)
    if edge is None:
        return None
    held_seconds = (state.now - position.opened_at).total_seconds()
    book_direction = orderbook_direction(state.up_book, state.down_book)
    if (
        held_seconds >= strategy.book_direction_exit_delay_seconds
        and book_direction is not None
        and book_direction != edge_direction(edge)
    ):
        return ExitReason.BOOK_DIRECTION_CONFLICT
    book = state.up_book if position.direction == Direction.UP else state.down_book
    best_bid = book.best_bid
    expiry_seconds = seconds_to_expiry(state.market, state.now)

    if position.direction == Direction.UP and edge <= -strategy.stop_edge_usd:
        return ExitReason.REVERSE_BREAK
    if position.direction == Direction.DOWN and edge >= strategy.stop_edge_usd:
        return ExitReason.REVERSE_BREAK
    if position.direction == Direction.UP and edge <= strategy.min_entry_edge_usd:
        return ExitReason.EDGE_FADED
    if position.direction == Direction.DOWN and edge >= -strategy.min_entry_edge_usd:
        return ExitReason.EDGE_FADED
    if best_bid is not None and best_bid >= position.entry_price + strategy.take_profit_ticks:
        return ExitReason.TAKE_PROFIT
    if held_seconds >= risk.max_hold_seconds:
        return ExitReason.MAX_HOLD_SECONDS
    if expiry_seconds <= strategy.force_exit_seconds:
        return ExitReason.FORCE_EXIT
    return None


def evaluate_exit(
    position: Position,
    state: StrategyState,
    strategy: StrategyConfig,
    risk: RiskConfig,
) -> ExitDecision:
    reason = choose_exit_reason(position, state, strategy, risk)
    if reason is None:
        return ExitDecision(False)

    book = state.up_book if position.direction == Direction.UP else state.down_book
    execution = simulate_sell(book, position.quantity, strategy.taker_fee_rate)
    if not execution.complete:
        return ExitDecision(False, reason=reason)

    pnl = execution.quote - execution.fee_usd - position.entry_quote
    fill = Fill(
        fill_id=str(uuid4()),
        position_id=position.position_id,
        market_id=position.market_id,
        token_id=position.token_id,
        direction=position.direction,
        side=OrderSide.SELL,
        avg_price=execution.avg_price,
        quantity=execution.quantity,
        quote=execution.quote,
        slippage=execution.slippage,
        fee_usd=execution.fee_usd,
        created_at=state.now,
        reason=reason.value,
    )
    event = ExitEvent(
        position_id=position.position_id,
        market_id=position.market_id,
        direction=position.direction,
        reason=reason,
        edge_usd=corrected_edge_usd(state.market, state.price_tick, state.edge_correction_usd),
        price=execution.avg_price,
        quantity=execution.quantity,
        pnl=pnl,
        fee_usd=execution.fee_usd,
        created_at=state.now,
    )
    return ExitDecision(True, reason=reason, fill=fill, event=event)


def settle_position(position: Position, market: MarketState, settlement_price: float, now: datetime | None = None) -> ExitEvent:
    if market.threshold_price is None:
        payout = 0.0
    elif position.direction == Direction.UP:
        payout = 1.0 if settlement_price > market.threshold_price else 0.0
    else:
        payout = 1.0 if settlement_price <= market.threshold_price else 0.0
    pnl = position.quantity * payout - position.entry_quote
    reason = ExitReason.SETTLEMENT_WIN if payout == 1.0 else ExitReason.SETTLEMENT_LOSS
    return ExitEvent(
        position_id=position.position_id,
        market_id=position.market_id,
        direction=position.direction,
        reason=reason,
        edge_usd=settlement_price - market.threshold_price if market.threshold_price is not None else None,
        price=payout,
        quantity=position.quantity,
        pnl=pnl,
        created_at=now or datetime.now(timezone.utc),
    )
