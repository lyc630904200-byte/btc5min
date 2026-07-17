from datetime import datetime, timedelta, timezone

from polybtc.config import RiskConfig, StrategyConfig
from polybtc.engine import MAX_RECENT_REJECTIONS, PaperEngine
from polybtc.config import AppConfig
from polybtc.models import BookLevel, Direction, ExitReason, MarketState, OrderBookSnapshot, Position, PriceTick
from polybtc.strategy import StrategyState, evaluate_entry, evaluate_exit, position_from_entry


def raw_edge_strategy() -> StrategyConfig:
    return StrategyConfig(min_entry_edge_usd=10)


def market(now: datetime) -> MarketState:
    return MarketState(
        condition_id="m1",
        slug="bitcoin-up-or-down",
        question="Bitcoin Up or Down above 118000",
        threshold_price=118000,
        threshold_verified=True,
        end_time=now + timedelta(seconds=120),
        up_token_id="up",
        down_token_id="down",
        min_order_size=5,
    )


def book(token_id: str, bid: float, ask: float, now: datetime) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        token_id=token_id,
        market_id="m1",
        timestamp=now,
        received_at=now,
        bids=[BookLevel(price=bid, size=100)],
        asks=[BookLevel(price=ask, size=100)],
        depth_trusted=True,
    )


def test_entry_accepts_up_edge_with_depth() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    state = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=118070, received_at=now),
        up_book=book("up", 0.58, 0.60, now),
        down_book=book("down", 0.38, 0.40, now),
        now=now,
    )

    decision = evaluate_entry(state, raw_edge_strategy(), RiskConfig())

    assert decision.accepted is True
    assert decision.signal is not None
    assert decision.signal.direction == Direction.UP
    assert decision.fill is not None
    assert decision.fill.avg_price == 0.60


def test_entry_rejects_unverified_threshold() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    unverified_market = market(now)
    unverified_market.threshold_verified = False
    state = StrategyState(
        market=unverified_market,
        price_tick=PriceTick(price=118070, received_at=now),
        up_book=book("up", 0.58, 0.60, now),
        down_book=book("down", 0.38, 0.40, now),
        now=now,
    )

    decision = evaluate_entry(state, raw_edge_strategy(), RiskConfig())

    assert decision.accepted is False
    assert decision.reason == "threshold_unverified"


def test_entry_rejects_stale_polymarket_price() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    state = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=118070, received_at=now),
        up_book=book("up", 0.58, 0.60, now),
        down_book=book("down", 0.38, 0.40, now),
        now=now,
        polymarket_price_fresh=False,
    )

    decision = evaluate_entry(state, raw_edge_strategy(), RiskConfig())

    assert decision.accepted is False
    assert decision.reason == "polymarket_price_stale"


def test_entry_rejects_stale_forged_verified_threshold_state() -> None:
    start = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    checked_at = start + timedelta(seconds=70)
    forged_market = MarketState(
        condition_id="m1",
        slug=f"btc-updown-5m-{int(start.timestamp())}",
        question="Bitcoin Up or Down",
        threshold_price=118000,
        threshold_source="polymarket_page_verified_open_price",
        threshold_observed_at=start,
        threshold_verified=True,
        threshold_fetched_at=start - timedelta(seconds=1),
        start_time=start,
        end_time=start + timedelta(minutes=5),
        up_token_id="up",
        down_token_id="down",
    )
    state = StrategyState(
        market=forged_market,
        price_tick=PriceTick(price=118070, received_at=checked_at),
        up_book=book("up", 0.58, 0.60, checked_at),
        down_book=book("down", 0.38, 0.40, checked_at),
        now=checked_at,
    )

    decision = evaluate_entry(state, raw_edge_strategy(), RiskConfig())

    assert decision.accepted is False
    assert decision.reason == "threshold_unverified"


def test_entry_rejects_fast_verified_source_without_rtds_candidate_evidence() -> None:
    start = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    checked_at = start + timedelta(seconds=70)
    forged_market = MarketState(
        condition_id="m1",
        slug=f"btc-updown-5m-{int(start.timestamp())}",
        question="Bitcoin Up or Down",
        threshold_price=118000,
        threshold_source="polymarket_page_rtds_verified_open_price",
        threshold_observed_at=start,
        threshold_verified=True,
        threshold_fetched_at=start + timedelta(seconds=30),
        start_time=start,
        end_time=start + timedelta(minutes=5),
        up_token_id="up",
        down_token_id="down",
    )
    state = StrategyState(
        market=forged_market,
        price_tick=PriceTick(price=118070, received_at=checked_at),
        up_book=book("up", 0.58, 0.60, checked_at),
        down_book=book("down", 0.38, 0.40, checked_at),
        now=checked_at,
    )

    decision = evaluate_entry(state, raw_edge_strategy(), RiskConfig())

    assert decision.accepted is False
    assert decision.reason == "threshold_unverified"


def test_entry_rejects_page_verified_threshold_when_late_rtds_candidate_disagrees() -> None:
    start = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    checked_at = start + timedelta(seconds=70)
    conflicting_market = MarketState(
        condition_id="m1",
        slug=f"btc-updown-5m-{int(start.timestamp())}",
        question="Bitcoin Up or Down",
        threshold_price=118000,
        threshold_source="polymarket_page_verified_open_price",
        threshold_observed_at=start,
        threshold_verified=True,
        threshold_fetched_at=start + timedelta(seconds=30),
        threshold_candidate_price=118010,
        threshold_candidate_source="polymarket_rtds_start_tick",
        threshold_candidate_observed_at=start,
        threshold_candidate_received_at=start,
        start_time=start,
        end_time=start + timedelta(minutes=5),
        up_token_id="up",
        down_token_id="down",
    )
    state = StrategyState(
        market=conflicting_market,
        price_tick=PriceTick(price=118070, received_at=checked_at),
        up_book=book("up", 0.58, 0.60, checked_at),
        down_book=book("down", 0.38, 0.40, checked_at),
        now=checked_at,
    )

    decision = evaluate_entry(state, raw_edge_strategy(), RiskConfig())

    assert decision.accepted is False
    assert decision.reason == "threshold_unverified"


def test_entry_rejects_when_orderbook_direction_conflicts_with_price_edge() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    state = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=118070, received_at=now),
        up_book=book("up", 0.38, 0.40, now),
        down_book=book("down", 0.58, 0.60, now),
        now=now,
    )

    decision = evaluate_entry(state, raw_edge_strategy(), RiskConfig())

    assert decision.accepted is False
    assert decision.reason == "book_direction_conflicts_with_edge"


def test_entry_rejects_after_market_trade_limit_is_reached() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    state = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=118070, received_at=now),
        up_book=book("up", 0.58, 0.60, now),
        down_book=book("down", 0.38, 0.40, now),
        now=now,
        market_trade_count=1,
    )

    decision = evaluate_entry(state, raw_edge_strategy(), RiskConfig(max_trades_per_market=1))

    assert decision.accepted is False
    assert decision.reason == "market_trade_limit"


def test_entry_rejects_expensive_ask() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    state = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=118070, received_at=now),
        up_book=book("up", 0.80, 0.81, now),
        down_book=book("down", 0.18, 0.20, now),
        now=now,
    )

    decision = evaluate_entry(state, raw_edge_strategy(), RiskConfig())

    assert decision.accepted is False
    assert decision.reason == "ask_too_expensive"


def test_entry_accepts_ask_at_minimum_buy_price() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    state = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=118070, received_at=now),
        up_book=book("up", 0.09, 0.10, now),
        down_book=book("down", 0.03, 0.04, now),
        now=now,
    )

    decision = evaluate_entry(state, raw_edge_strategy(), RiskConfig())

    assert decision.accepted is True


def test_entry_rejects_ask_below_minimum_buy_price() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    state = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=118070, received_at=now),
        up_book=book("up", 0.08, 0.09, now),
        down_book=book("down", 0.03, 0.04, now),
        now=now,
    )

    decision = evaluate_entry(state, raw_edge_strategy(), RiskConfig())

    assert decision.accepted is False
    assert decision.reason == "ask_too_cheap"


def test_entry_uses_book_received_time_for_freshness() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    up_book = book("up", 0.58, 0.60, now)
    down_book = book("down", 0.38, 0.40, now)
    # The exchange sequence time may lag, but the data was just received.
    up_book.timestamp = now - timedelta(seconds=5)
    down_book.timestamp = now - timedelta(seconds=5)
    state = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=118070, received_at=now),
        up_book=up_book,
        down_book=down_book,
        now=now,
    )

    decision = evaluate_entry(state, raw_edge_strategy(), RiskConfig())

    assert decision.accepted is True


def test_entry_rejects_edge_equal_to_threshold() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    state = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=118010, received_at=now),
        up_book=book("up", 0.58, 0.60, now),
        down_book=book("down", 0.38, 0.40, now),
        now=now,
    )

    decision = evaluate_entry(state, raw_edge_strategy(), RiskConfig())

    assert decision.accepted is False
    assert decision.reason == "edge_too_small"


def test_entry_accepts_ask_equal_to_max_buy_price() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    state = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=118070, received_at=now),
        up_book=book("up", 0.74, 0.75, now),
        down_book=book("down", 0.18, 0.20, now),
        now=now,
    )

    decision = evaluate_entry(state, raw_edge_strategy(), RiskConfig())

    assert decision.accepted is True


def test_entry_rejects_market_before_start() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    future_market = market(now)
    future_market.start_time = now + timedelta(seconds=30)
    state = StrategyState(
        market=future_market,
        price_tick=PriceTick(price=118070, received_at=now),
        up_book=book("up", 0.58, 0.60, now),
        down_book=book("down", 0.38, 0.40, now),
        now=now,
    )

    decision = evaluate_entry(state, raw_edge_strategy(), RiskConfig())

    assert decision.accepted is False
    assert decision.reason == "market_not_started"


def test_entry_rejects_outside_configured_entry_window() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    state = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=118070, received_at=now),
        up_book=book("up", 0.58, 0.60, now),
        down_book=book("down", 0.38, 0.40, now),
        now=now,
    )
    strategy = raw_edge_strategy()

    state.market.end_time = now + timedelta(seconds=241)
    assert evaluate_entry(state, strategy, RiskConfig()).reason == "too_early_to_entry"

    state.market.end_time = now + timedelta(seconds=9)
    assert evaluate_entry(state, strategy, RiskConfig()).reason == "too_close_to_expiry"

    state.market.end_time = now + timedelta(seconds=240)
    assert evaluate_entry(state, strategy, RiskConfig()).accepted is True


def test_exit_take_profit() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    state = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=118070, received_at=now),
        up_book=book("up", 0.72, 0.73, now),
        down_book=book("down", 0.25, 0.28, now),
        now=now + timedelta(seconds=10),
    )
    entry_state = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=118070, received_at=now),
        up_book=book("up", 0.58, 0.60, now),
        down_book=book("down", 0.38, 0.40, now),
        now=now,
    )
    strategy = raw_edge_strategy()
    entry = evaluate_entry(entry_state, strategy, RiskConfig())
    assert entry.fill is not None
    position = position_from_entry(entry.fill, edge=70, opened_at=now)

    decision = evaluate_exit(position, state, strategy, RiskConfig())

    assert decision.should_exit is True
    assert decision.reason is not None
    assert decision.reason.value == "take_profit"
    assert entry.fill is not None
    assert round(entry.fill.fee_usd, 6) == 0.28
    assert decision.fill is not None
    assert round(decision.fill.fee_usd, 6) == 0.2352
    assert decision.event is not None
    assert round(decision.event.pnl, 6) == 1.4848


def test_exit_hard_stop_uses_complete_net_liquidation_pnl() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    strategy = raw_edge_strategy()
    entry_state = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=118070, received_at=now),
        up_book=book("up", 0.58, 0.60, now),
        down_book=book("down", 0.38, 0.40, now),
        now=now,
    )
    entry = evaluate_entry(entry_state, strategy, RiskConfig())
    assert entry.fill is not None
    position = position_from_entry(entry.fill, edge=70, opened_at=now, taker_fee_rate=strategy.taker_fee_rate)
    loss_market = market(now)
    loss_market.threshold_price = None
    loss_state = StrategyState(
        market=loss_market,
        price_tick=PriceTick(price=118070, received_at=now + timedelta(seconds=1)),
        up_book=book("up", 0.48, 0.49, now + timedelta(seconds=1)),
        down_book=book("down", 0.30, 0.31, now + timedelta(seconds=1)),
        now=now + timedelta(seconds=1),
    )
    expected_pnl = 8.0 - 0.2912 - 10.28

    decision = evaluate_exit(
        position,
        loss_state,
        strategy,
        RiskConfig(max_loss_usd=abs(expected_pnl)),
    )

    assert decision.should_exit is True
    assert decision.reason == ExitReason.MAX_LOSS_USD
    assert decision.fill is not None
    assert decision.event is not None
    assert round(decision.fill.avg_price, 6) == 0.48
    assert round(decision.fill.fee_usd, 6) == 0.2912
    assert round(decision.event.pnl, 6) == round(expected_pnl, 6)
    assert evaluate_exit(
        position,
        loss_state,
        strategy,
        RiskConfig(max_loss_usd=abs(expected_pnl) + 0.001),
    ).should_exit is False


def test_exit_hard_stop_uses_multiple_bid_levels_not_only_best_bid() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    strategy = raw_edge_strategy()
    entry_state = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=118070, received_at=now),
        up_book=book("up", 0.58, 0.60, now),
        down_book=book("down", 0.38, 0.40, now),
        now=now,
    )
    entry = evaluate_entry(entry_state, strategy, RiskConfig())
    assert entry.fill is not None
    position = position_from_entry(entry.fill, edge=70, opened_at=now, taker_fee_rate=strategy.taker_fee_rate)
    liquidation_book = OrderBookSnapshot(
        token_id="up",
        market_id="m1",
        timestamp=now + timedelta(seconds=1),
        received_at=now + timedelta(seconds=1),
        bids=[BookLevel(price=0.50, size=8), BookLevel(price=0.45, size=100)],
        asks=[BookLevel(price=0.51, size=100)],
        depth_trusted=True,
    )
    loss_market = market(now)
    loss_market.threshold_price = None
    loss_state = StrategyState(
        market=loss_market,
        price_tick=PriceTick(price=118070, received_at=now + timedelta(seconds=1)),
        up_book=liquidation_book,
        down_book=book("down", 0.30, 0.31, now + timedelta(seconds=1)),
        now=now + timedelta(seconds=1),
    )
    top_only_net = (
        position.quantity * 0.50
        - position.quantity * strategy.taker_fee_rate * 0.50 * (1.0 - 0.50)
        - position.entry_quote
    )

    decision = evaluate_exit(position, loss_state, strategy, RiskConfig(max_loss_usd=2.5))

    assert top_only_net > -2.5
    assert decision.should_exit is True
    assert decision.reason == ExitReason.MAX_LOSS_USD
    assert decision.fill is not None
    assert decision.fill.avg_price < 0.50
    assert decision.event is not None
    assert decision.event.pnl <= -2.5


def test_hard_stop_requires_trusted_depth() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    strategy = raw_edge_strategy()
    entry_state = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=118070, received_at=now),
        up_book=book("up", 0.58, 0.60, now),
        down_book=book("down", 0.38, 0.40, now),
        now=now,
    )
    entry = evaluate_entry(entry_state, strategy, RiskConfig())
    assert entry.fill is not None
    position = position_from_entry(entry.fill, edge=70, opened_at=now, taker_fee_rate=strategy.taker_fee_rate)
    untrusted_book = book("up", 0.10, 0.11, now + timedelta(seconds=1))
    untrusted_book.depth_trusted = False
    loss_market = market(now)
    loss_market.threshold_price = None
    loss_state = StrategyState(
        market=loss_market,
        price_tick=PriceTick(price=118070, received_at=now + timedelta(seconds=1)),
        up_book=untrusted_book,
        down_book=book("down", 0.03, 0.04, now + timedelta(seconds=1)),
        now=now + timedelta(seconds=1),
    )

    decision = evaluate_exit(position, loss_state, strategy, RiskConfig(max_loss_usd=0.01))

    assert decision.should_exit is False


def test_hard_stop_does_not_close_partial_liquidation() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    strategy = raw_edge_strategy()
    entry_state = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=118070, received_at=now),
        up_book=book("up", 0.58, 0.60, now),
        down_book=book("down", 0.38, 0.40, now),
        now=now,
    )
    entry = evaluate_entry(entry_state, strategy, RiskConfig())
    assert entry.fill is not None
    position = position_from_entry(entry.fill, edge=70, opened_at=now, taker_fee_rate=strategy.taker_fee_rate)
    thin_book = OrderBookSnapshot(
        token_id="up",
        market_id="m1",
        timestamp=now + timedelta(seconds=1),
        received_at=now + timedelta(seconds=1),
        bids=[BookLevel(price=0.10, size=1)],
        asks=[BookLevel(price=0.60, size=100)],
        depth_trusted=True,
    )
    state = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=118070, received_at=now + timedelta(seconds=1)),
        up_book=thin_book,
        down_book=book("down", 0.04, 0.05, now + timedelta(seconds=1)),
        now=now + timedelta(seconds=1),
    )

    decision = evaluate_exit(position, state, strategy, RiskConfig(max_loss_usd=0.01))

    assert decision.should_exit is False


def test_position_waits_ten_seconds_before_orderbook_conflict_exit() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    strategy = raw_edge_strategy()
    entry_state = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=118070, received_at=now),
        up_book=book("up", 0.58, 0.60, now),
        down_book=book("down", 0.38, 0.40, now),
        now=now,
    )
    entry = evaluate_entry(entry_state, strategy, RiskConfig())
    assert entry.fill is not None
    position = position_from_entry(entry.fill, edge=70, opened_at=now)
    early_state = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=118070, received_at=now + timedelta(seconds=1)),
        up_book=book("up", 0.38, 0.40, now + timedelta(seconds=1)),
        down_book=book("down", 0.58, 0.60, now + timedelta(seconds=1)),
        now=now + timedelta(seconds=1),
    )
    delayed_state = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=118070, received_at=now + timedelta(seconds=10)),
        up_book=book("up", 0.38, 0.40, now + timedelta(seconds=10)),
        down_book=book("down", 0.58, 0.60, now + timedelta(seconds=10)),
        now=now + timedelta(seconds=10),
    )

    conflict_only_risk = RiskConfig(max_loss_usd=100)
    early_decision = evaluate_exit(position, early_state, strategy, conflict_only_risk)
    delayed_decision = evaluate_exit(position, delayed_state, strategy, conflict_only_risk)

    assert early_decision.should_exit is False
    assert delayed_decision.should_exit is True
    assert delayed_decision.reason == ExitReason.BOOK_DIRECTION_CONFLICT
    assert delayed_decision.fill is not None
    assert delayed_decision.fill.side.value == "SELL"


def test_orderbook_conflict_takes_priority_over_hard_stop() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    strategy = raw_edge_strategy()
    entry_state = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=118070, received_at=now),
        up_book=book("up", 0.58, 0.60, now),
        down_book=book("down", 0.38, 0.40, now),
        now=now,
    )
    entry = evaluate_entry(entry_state, strategy, RiskConfig())
    assert entry.fill is not None
    position = position_from_entry(entry.fill, edge=70, opened_at=now, taker_fee_rate=strategy.taker_fee_rate)
    conflict_state = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=118070, received_at=now + timedelta(seconds=10)),
        up_book=book("up", 0.38, 0.40, now + timedelta(seconds=10)),
        down_book=book("down", 0.58, 0.60, now + timedelta(seconds=10)),
        now=now + timedelta(seconds=10),
    )

    decision = evaluate_exit(position, conflict_state, strategy, RiskConfig(max_loss_usd=2.5))

    assert decision.should_exit is True
    assert decision.reason == ExitReason.BOOK_DIRECTION_CONFLICT
    assert decision.event is not None
    assert decision.event.pnl <= -2.5


def test_down_position_does_not_exit_while_edge_still_beyond_stop_threshold() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    strategy = raw_edge_strategy()
    entry_state = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=117980, received_at=now),
        up_book=book("up", 0.38, 0.40, now),
        down_book=book("down", 0.44, 0.46, now),
        now=now,
    )
    entry = evaluate_entry(entry_state, strategy, RiskConfig())
    assert entry.fill is not None
    position = position_from_entry(entry.fill, edge=-20, opened_at=now)
    exit_state = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=117988, received_at=now + timedelta(seconds=1)),
        up_book=book("up", 0.48, 0.50, now + timedelta(seconds=1)),
        down_book=book("down", 0.50, 0.52, now + timedelta(seconds=1)),
        now=now + timedelta(seconds=1),
    )

    decision = evaluate_exit(position, exit_state, strategy, RiskConfig())

    assert decision.should_exit is False


def test_down_position_exits_when_edge_reaches_stop_threshold() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    strategy = raw_edge_strategy()
    entry_state = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=117980, received_at=now),
        up_book=book("up", 0.38, 0.40, now),
        down_book=book("down", 0.44, 0.46, now),
        now=now,
    )
    entry = evaluate_entry(entry_state, strategy, RiskConfig())
    assert entry.fill is not None
    position = position_from_entry(entry.fill, edge=-20, opened_at=now)
    exit_state = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=117990, received_at=now + timedelta(seconds=1)),
        up_book=book("up", 0.48, 0.50, now + timedelta(seconds=1)),
        down_book=book("down", 0.50, 0.52, now + timedelta(seconds=1)),
        now=now + timedelta(seconds=1),
    )

    decision = evaluate_exit(position, exit_state, strategy, RiskConfig())

    assert decision.should_exit is True
    assert decision.reason is not None
    assert decision.reason.value == "edge_faded"


def test_up_position_exits_when_edge_reaches_stop_threshold() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    strategy = raw_edge_strategy()
    entry_state = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=118020, received_at=now),
        up_book=book("up", 0.58, 0.60, now),
        down_book=book("down", 0.38, 0.40, now),
        now=now,
    )
    entry = evaluate_entry(entry_state, strategy, RiskConfig())
    assert entry.fill is not None
    position = position_from_entry(entry.fill, edge=20, opened_at=now)
    exit_state = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=118010, received_at=now + timedelta(seconds=1)),
        up_book=book("up", 0.58, 0.60, now + timedelta(seconds=1)),
        down_book=book("down", 0.38, 0.40, now + timedelta(seconds=1)),
        now=now + timedelta(seconds=1),
    )

    decision = evaluate_exit(position, exit_state, strategy, RiskConfig())

    assert decision.should_exit is True
    assert decision.reason is not None
    assert decision.reason.value == "edge_faded"


def test_up_position_uses_stop_edge_instead_of_entry_edge_for_exit() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    strategy = StrategyConfig(min_entry_edge_usd=20, stop_edge_usd=15)
    entry_state = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=118030, received_at=now),
        up_book=book("up", 0.58, 0.60, now),
        down_book=book("down", 0.38, 0.40, now),
        now=now,
    )
    entry = evaluate_entry(entry_state, strategy, RiskConfig())
    assert entry.fill is not None
    position = position_from_entry(entry.fill, edge=30, opened_at=now)

    above_stop = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=118017, received_at=now + timedelta(seconds=1)),
        up_book=book("up", 0.58, 0.60, now + timedelta(seconds=1)),
        down_book=book("down", 0.38, 0.40, now + timedelta(seconds=1)),
        now=now + timedelta(seconds=1),
    )
    at_stop = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=118015, received_at=now + timedelta(seconds=2)),
        up_book=book("up", 0.58, 0.60, now + timedelta(seconds=2)),
        down_book=book("down", 0.38, 0.40, now + timedelta(seconds=2)),
        now=now + timedelta(seconds=2),
    )

    assert evaluate_exit(position, above_stop, strategy, RiskConfig()).should_exit is False
    decision = evaluate_exit(position, at_stop, strategy, RiskConfig())
    assert decision.should_exit is True
    assert decision.reason == ExitReason.EDGE_FADED


def test_entry_uses_binance_minus_threshold() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    state = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=118070, received_at=now),
        up_book=book("up", 0.58, 0.60, now),
        down_book=book("down", 0.38, 0.40, now),
        now=now,
    )

    decision = evaluate_entry(state, StrategyConfig(), RiskConfig())

    assert decision.accepted is True
    assert decision.signal is not None
    assert decision.signal.edge_usd == 70


def test_entry_records_binance_minus_threshold_edge() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    state = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=118100, received_at=now),
        up_book=book("up", 0.58, 0.60, now),
        down_book=book("down", 0.38, 0.40, now),
        now=now,
    )

    decision = evaluate_entry(state, StrategyConfig(), RiskConfig())

    assert decision.accepted is True
    assert decision.signal is not None
    assert decision.signal.direction == Direction.UP
    assert decision.signal.edge_usd == 100


def test_entry_subtracts_dynamic_edge_correction() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    state = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=118070, received_at=now),
        up_book=book("up", 0.58, 0.60, now),
        down_book=book("down", 0.38, 0.40, now),
        now=now,
        edge_correction_usd=20,
    )

    decision = evaluate_entry(state, raw_edge_strategy(), RiskConfig())

    assert decision.accepted is True
    assert decision.signal is not None
    assert decision.signal.edge_usd == 50


def test_engine_uses_binance_minus_polymarket_as_dynamic_correction() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    engine = PaperEngine(AppConfig())
    engine.set_market(market(now))
    engine.set_tick(PriceTick(price=118070, received_at=now))
    engine.set_polymarket_tick(PriceTick(source="polymarket_rtds", symbol="BTC/USD", price=118040, received_at=now))

    engine.set_book(Direction.UP, book("up", 0.58, 0.60, now))
    engine.set_book(Direction.DOWN, book("down", 0.38, 0.40, now))

    assert engine.edge_correction_usd() == 30
    assert engine.edge_correction_source() == "binance_minus_polymarket"
    assert engine.signals == []

    engine.set_polymarket_tick(
        PriceTick(source="polymarket_rtds", symbol="BTC/USD", price=118040, received_at=now + timedelta(milliseconds=500))
    )
    assert engine.signals == []
    engine.set_polymarket_tick(
        PriceTick(source="polymarket_rtds", symbol="BTC/USD", price=118040, received_at=now + timedelta(seconds=1))
    )

    assert engine.signals[-1].edge_usd == 40


def test_engine_blocks_second_entry_in_same_market_and_resets_for_new_market() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    engine = PaperEngine(AppConfig(risk={"max_data_age_ms": 10000, "max_trades_per_market": 1}))
    engine.set_market(market(now))
    engine.set_tick(PriceTick(price=118070, received_at=now))
    engine.set_polymarket_tick(PriceTick(source="polymarket_rtds", symbol="BTC/USD", price=118040, received_at=now))
    engine.set_book(Direction.UP, book("up", 0.58, 0.60, now))
    engine.set_book(Direction.DOWN, book("down", 0.38, 0.40, now))
    for milliseconds in (500, 1000):
        engine.set_polymarket_tick(
            PriceTick(
                source="polymarket_rtds",
                symbol="BTC/USD",
                price=118040,
                received_at=now + timedelta(milliseconds=milliseconds),
            )
        )

    assert len(engine.positions) == 1
    assert engine.market_trade_count == 1
    assert engine.open_position is not None
    engine.open_position.status = "CLOSED"
    engine.open_position.closed_at = now + timedelta(seconds=1)
    engine.open_position = None
    engine.set_polymarket_tick(
        PriceTick(
            source="polymarket_rtds",
            symbol="BTC/USD",
            price=118040,
            received_at=now + timedelta(milliseconds=1500),
        )
    )

    assert len(engine.positions) == 1
    assert engine.rejections[-1]["reason"] == "market_trade_limit"

    next_market = market(now)
    next_market.condition_id = "m2"
    next_market.up_token_id = "up2"
    next_market.down_token_id = "down2"
    engine.set_market(next_market)

    assert engine.market_trade_count == 0

    engine.set_market(market(now))

    assert engine.market_trade_count == 1


def test_engine_does_not_count_duplicate_book_events_as_entry_confirmations() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    engine = PaperEngine(AppConfig())
    engine.set_market(market(now))
    engine.set_tick(PriceTick(price=118070, received_at=now))
    engine.set_polymarket_tick(PriceTick(source="polymarket_rtds", symbol="BTC/USD", price=118040, received_at=now))
    engine.set_book(Direction.UP, book("up", 0.58, 0.60, now))
    engine.set_book(Direction.DOWN, book("down", 0.38, 0.40, now))

    for offset in (100, 200, 300):
        engine.set_book(Direction.UP, book("up", 0.58, 0.60, now + timedelta(milliseconds=offset)))

    assert engine.entry_confirmation_updates == 1
    assert engine.signals == []


def test_engine_can_disable_entry_confirmation() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    engine = PaperEngine(
        AppConfig(
            strategy={"entry_confirmation_enabled": False},
            risk={"max_data_age_ms": 10000},
        )
    )
    engine.set_market(market(now))
    engine.set_tick(PriceTick(price=118070, received_at=now))
    engine.set_polymarket_tick(PriceTick(source="polymarket_rtds", symbol="BTC/USD", price=118040, received_at=now))
    engine.set_book(Direction.UP, book("up", 0.58, 0.60, now))
    engine.set_book(Direction.DOWN, book("down", 0.38, 0.40, now))

    assert len(engine.signals) == 1
    assert engine.entry_confirmation_updates == 0


def test_engine_resets_entry_confirmation_when_signal_breaks() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    engine = PaperEngine(AppConfig(risk={"max_data_age_ms": 10000}))
    engine.set_market(market(now))
    engine.set_tick(PriceTick(price=118070, received_at=now))
    engine.set_polymarket_tick(PriceTick(source="polymarket_rtds", symbol="BTC/USD", price=118040, received_at=now))
    engine.set_book(Direction.UP, book("up", 0.58, 0.60, now))
    engine.set_book(Direction.DOWN, book("down", 0.38, 0.40, now))
    engine.set_polymarket_tick(
        PriceTick(source="polymarket_rtds", symbol="BTC/USD", price=118040, received_at=now + timedelta(milliseconds=500))
    )
    assert engine.entry_confirmation_updates == 2

    engine.set_polymarket_tick(
        PriceTick(source="polymarket_rtds", symbol="BTC/USD", price=118000, received_at=now + timedelta(milliseconds=750))
    )
    assert engine.entry_confirmation_updates == 0

    for milliseconds in (1000, 1500, 2000):
        engine.set_polymarket_tick(
            PriceTick(
                source="polymarket_rtds",
                symbol="BTC/USD",
                price=118040,
                received_at=now + timedelta(milliseconds=milliseconds),
            )
        )

    assert len(engine.signals) == 1


def test_engine_captures_dynamic_threshold_only_near_start() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    dynamic_market = market(now)
    dynamic_market.threshold_price = None
    dynamic_market.threshold_source = "dynamic_start_price"
    dynamic_market.start_time = now

    engine = PaperEngine(AppConfig())
    engine.set_market(dynamic_market)

    assert engine.capture_dynamic_threshold(PriceTick(price=118001, exchange_timestamp=now - timedelta(seconds=1), received_at=now)) is False
    assert engine.market is not None
    assert engine.market.threshold_price is None

    source_time = now + timedelta(seconds=1)
    received_at = now + timedelta(seconds=3)
    assert engine.capture_dynamic_threshold(PriceTick(price=118010, exchange_timestamp=source_time, received_at=received_at)) is True
    assert engine.market.threshold_price == 118010
    assert engine.market.threshold_observed_at == source_time
    assert engine.market.threshold_source == "binance_first_tick_after_start"
    assert engine.market.threshold_verified is False


def test_engine_caches_exact_rtds_start_tick_before_market_switch() -> None:
    start = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    engine = PaperEngine(AppConfig())
    start_tick = PriceTick(
        source="polymarket_rtds",
        symbol="BTC/USD",
        price=118005.25,
        exchange_timestamp=start,
        received_at=start - timedelta(milliseconds=500),
    )

    engine.set_polymarket_tick(start_tick)
    next_market = market(start)
    next_market.start_time = start
    engine.set_market(next_market)

    assert engine.market is not None
    assert engine.market.threshold_candidate_price == 118005.25
    assert engine.market.threshold_candidate_source == "polymarket_rtds_start_tick"
    assert engine.market.threshold_candidate_observed_at == start
    assert engine.market.threshold_candidate_received_at == start - timedelta(milliseconds=500)
    assert engine.market.threshold_candidate_conflicted is False


def test_engine_marks_conflicting_duplicate_rtds_start_ticks() -> None:
    start = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    engine = PaperEngine(AppConfig())
    for price in (118005.25, 118006.25):
        engine.set_polymarket_tick(
            PriceTick(
                source="polymarket_rtds",
                symbol="BTC/USD",
                price=price,
                exchange_timestamp=start,
                received_at=start,
            )
        )
    next_market = market(start)
    next_market.start_time = start
    engine.set_market(next_market)

    assert engine.market is not None
    assert engine.market.threshold_candidate_price is None
    assert engine.market.threshold_candidate_source == "polymarket_rtds_conflict"
    assert engine.market.threshold_candidate_conflicted is True


def test_engine_rejects_rtds_start_tick_received_outside_boundary_window() -> None:
    start = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    engine = PaperEngine(AppConfig())
    engine.set_polymarket_tick(
        PriceTick(
            source="polymarket_rtds",
            symbol="BTC/USD",
            price=118005.25,
            exchange_timestamp=start,
            received_at=start - timedelta(seconds=5),
        )
    )
    next_market = market(start)
    next_market.start_time = start
    engine.set_market(next_market)

    assert engine.market is not None
    assert engine.market.threshold_candidate_price is None


def test_engine_ignores_non_btc_rtds_boundary_tick() -> None:
    start = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    engine = PaperEngine(AppConfig())
    engine.set_polymarket_tick(
        PriceTick(
            source="polymarket_rtds",
            symbol="ETH/USD",
            price=3500,
            exchange_timestamp=start,
            received_at=start,
        )
    )
    next_market = market(start)
    next_market.start_time = start
    engine.set_market(next_market)

    assert engine.market is not None
    assert engine.market.threshold_candidate_price is None


def test_engine_does_not_capture_stale_dynamic_threshold() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    dynamic_market = market(now)
    dynamic_market.threshold_price = None
    dynamic_market.threshold_source = "dynamic_start_price"
    dynamic_market.start_time = now

    engine = PaperEngine(AppConfig())
    engine.set_market(dynamic_market)

    captured = engine.capture_dynamic_threshold(
        PriceTick(price=118010, exchange_timestamp=now + timedelta(seconds=5), received_at=now + timedelta(seconds=5))
    )

    assert captured is False
    assert engine.market is not None
    assert engine.market.threshold_price is None


def test_engine_rejects_book_from_wrong_market() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    engine = PaperEngine(AppConfig())
    engine.set_market(market(now))
    engine.set_tick(PriceTick(price=118070, received_at=now))

    stale_book = OrderBookSnapshot(
        token_id="old-up",
        market_id="old-market",
        timestamp=now,
        bids=[BookLevel(price=0.99, size=1000)],
        asks=[BookLevel(price=0.01, size=1000)],
    )

    engine.set_book(Direction.UP, stale_book)

    assert Direction.UP not in engine.books
    assert engine.rejections[-1]["reason"] == "stale_book_market"
    assert engine.positions == []

    engine.set_book(Direction.UP, book("up", 0.58, 0.60, now))

    assert engine.books[Direction.UP].token_id == "up"


def test_engine_ignores_older_book_for_same_market() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    engine = PaperEngine(AppConfig())
    engine.set_market(market(now))
    newer = book("up", 0.60, 0.61, now + timedelta(seconds=2))
    older = book("up", 0.40, 0.41, now)

    engine.set_book(Direction.UP, newer)
    engine.set_book(Direction.UP, older)

    assert engine.books[Direction.UP].best_bid == 0.60
    assert engine.books[Direction.UP].best_ask == 0.61
    assert engine.rejections == []


def test_engine_uses_fresh_rest_fallback_when_websocket_book_is_stale() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    engine = PaperEngine(AppConfig())
    engine.set_market(market(now))
    websocket_book = book("up", 0.60, 0.61, now + timedelta(seconds=2))
    rest_book = book("up", 0.40, 0.41, now + timedelta(seconds=1))
    rest_book.received_at = now + timedelta(seconds=3)
    rest_book.raw = {"_transport": "rest", "_request_started_at": (now + timedelta(seconds=2, milliseconds=500)).isoformat()}

    engine.set_book(Direction.UP, websocket_book)
    engine.set_book(Direction.UP, rest_book)

    active = engine.books[Direction.UP]
    assert active.best_bid == 0.40
    assert active.best_ask == 0.41
    assert active.timestamp == websocket_book.timestamp
    assert active.received_at == now + timedelta(seconds=3)


def test_engine_drops_rest_snapshot_when_websocket_advanced_during_request() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    engine = PaperEngine(AppConfig())
    engine.set_market(market(now))
    websocket_book = book("up", 0.60, 0.61, now + timedelta(seconds=2))
    websocket_book.received_at = now + timedelta(seconds=2)
    rest_book = book("up", 0.40, 0.41, now + timedelta(seconds=1))
    rest_book.received_at = now + timedelta(seconds=3)
    rest_book.raw = {"_transport": "rest", "_request_started_at": (now + timedelta(seconds=1)).isoformat()}

    engine.set_book(Direction.UP, websocket_book)
    engine.set_book(Direction.UP, rest_book)

    active = engine.books[Direction.UP]
    assert active.best_bid == 0.60
    assert active.best_ask == 0.61


def test_engine_uses_later_snapshot_with_equal_source_timestamp() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    engine = PaperEngine(AppConfig())
    engine.set_market(market(now))
    partial = book("up", 0.40, 0.45, now)
    final = book("up", 0.42, 0.43, now)
    final.received_at = now + timedelta(microseconds=1)

    engine.set_book(Direction.UP, partial)
    engine.set_book(Direction.UP, final)

    assert engine.books[Direction.UP].best_bid == 0.42
    assert engine.books[Direction.UP].best_ask == 0.43


def test_engine_caps_retained_rejections_but_keeps_total_count() -> None:
    engine = PaperEngine(AppConfig())

    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    for index in range(MAX_RECENT_REJECTIONS + 10):
        engine.record_rejection("edge_too_small", now + timedelta(seconds=index))

    assert len(engine.rejections) == MAX_RECENT_REJECTIONS
    assert engine.summary()["rejections"] == MAX_RECENT_REJECTIONS + 10


def test_engine_rate_limits_duplicate_rejection_reasons() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    engine = PaperEngine(AppConfig())

    engine.record_rejection("edge_too_small", now)
    engine.record_rejection("edge_too_small", now + timedelta(milliseconds=500))
    engine.record_rejection("ask_too_expensive", now + timedelta(milliseconds=500))
    engine.record_rejection("edge_too_small", now + timedelta(seconds=1))

    assert engine.summary()["rejections"] == 3
    assert [item["reason"] for item in engine.rejections] == ["edge_too_small", "ask_too_expensive", "edge_too_small"]


def test_reverse_entry_keeps_up_signal_but_buys_down_at_actual_price() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    state = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=118070, received_at=now),
        up_book=book("up", 0.58, 0.60, now),
        # The actual reverse price is intentionally above max_buy_price.
        down_book=book("down", 0.08, 0.90, now),
        now=now,
    )

    decision = evaluate_entry(
        state,
        StrategyConfig(min_entry_edge_usd=10, max_buy_price=0.75, reverse_entry_enabled=True),
        RiskConfig(),
    )

    assert decision.accepted is True
    assert decision.signal is not None
    assert decision.signal.direction == Direction.UP
    assert decision.signal.execution_direction == Direction.DOWN
    assert decision.signal.ask_price == 0.60
    assert decision.signal.reverse_entry is True
    assert decision.fill is not None
    assert decision.fill.direction == Direction.DOWN
    assert decision.fill.token_id == "down"
    assert decision.fill.avg_price == 0.90
    assert decision.fill.strategy_direction == Direction.UP
    assert decision.fill.reverse_entry is True
    assert decision.fill.reason == "reverse_entry_edge"
    assert decision.strategy_execution is not None
    assert decision.strategy_execution.avg_price == 0.60


def test_reverse_entry_keeps_down_signal_but_buys_up() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    state = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=117930, received_at=now),
        up_book=book("up", 0.08, 0.90, now),
        down_book=book("down", 0.58, 0.60, now),
        now=now,
    )

    decision = evaluate_entry(
        state,
        StrategyConfig(min_entry_edge_usd=10, reverse_entry_enabled=True),
        RiskConfig(),
    )

    assert decision.accepted is True
    assert decision.signal is not None and decision.signal.direction == Direction.DOWN
    assert decision.fill is not None and decision.fill.direction == Direction.UP
    assert decision.fill.token_id == "up"
    assert decision.fill.avg_price == 0.90


def test_reverse_entry_rejects_unavailable_or_insufficient_actual_book() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    down_book = book("down", 0.38, 0.40, now)
    down_book.asks = []
    state = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=118070, received_at=now),
        up_book=book("up", 0.58, 0.60, now),
        down_book=down_book,
        now=now,
    )
    strategy = StrategyConfig(min_entry_edge_usd=10, reverse_entry_enabled=True)

    assert evaluate_entry(state, strategy, RiskConfig()).reason == "reverse_ask_unavailable"

    down_book.asks = [BookLevel(price=0.40, size=1)]
    decision = evaluate_entry(state, strategy, RiskConfig())
    assert decision.accepted is False
    assert decision.reason == "reverse_depth_insufficient"


def test_reverse_entry_checks_actual_minimum_order_size() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    reverse_market = market(now)
    reverse_market.min_order_size = 15
    state = StrategyState(
        market=reverse_market,
        price_tick=PriceTick(price=118070, received_at=now),
        up_book=book("up", 0.49, 0.50, now),
        down_book=book("down", 0.10, 0.80, now),
        now=now,
    )

    decision = evaluate_entry(
        state,
        StrategyConfig(min_entry_edge_usd=10, reverse_entry_enabled=True),
        RiskConfig(),
    )

    assert decision.accepted is False
    assert decision.reason == "reverse_below_min_order_size"


def test_reverse_exit_uses_signal_book_for_take_profit_and_actual_book_for_sale() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    strategy = StrategyConfig(
        min_entry_edge_usd=10,
        take_profit_ticks=0.15,
        reverse_entry_enabled=True,
    )
    entry_state = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=118070, received_at=now),
        up_book=book("up", 0.58, 0.60, now),
        down_book=book("down", 0.38, 0.40, now),
        now=now,
    )
    entry = evaluate_entry(entry_state, strategy, RiskConfig())
    assert entry.fill is not None
    position = position_from_entry(
        entry.fill,
        edge=70,
        opened_at=now,
        taker_fee_rate=strategy.taker_fee_rate,
        strategy_execution=entry.strategy_execution,
    )
    exit_state = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=118070, received_at=now),
        up_book=book("up", 0.75, 0.76, now),
        down_book=book("down", 0.20, 0.21, now),
        now=now,
    )

    decision = evaluate_exit(position, exit_state, strategy, RiskConfig(max_loss_usd=100))

    assert decision.should_exit is True
    assert decision.reason == ExitReason.TAKE_PROFIT
    assert decision.fill is not None
    assert decision.fill.direction == Direction.DOWN
    assert decision.fill.avg_price == 0.20
    assert decision.fill.reverse_entry is True
    assert decision.event is not None
    assert decision.event.pnl < 0


def test_reverse_exit_uses_signal_position_for_max_loss() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    strategy = StrategyConfig(min_entry_edge_usd=10, reverse_entry_enabled=True)
    entry_state = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=118070, received_at=now),
        up_book=book("up", 0.58, 0.60, now),
        down_book=book("down", 0.38, 0.40, now),
        now=now,
    )
    entry = evaluate_entry(entry_state, strategy, RiskConfig())
    assert entry.fill is not None
    position = position_from_entry(
        entry.fill,
        edge=70,
        opened_at=now,
        taker_fee_rate=strategy.taker_fee_rate,
        strategy_execution=entry.strategy_execution,
    )
    exit_state = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=118070, received_at=now),
        up_book=book("up", 0.40, 0.41, now),
        down_book=book("down", 0.35, 0.36, now),
        now=now,
    )

    decision = evaluate_exit(position, exit_state, strategy, RiskConfig(max_loss_usd=2.5))

    assert decision.should_exit is True
    assert decision.reason == ExitReason.MAX_LOSS_USD
    assert decision.fill is not None and decision.fill.direction == Direction.DOWN


def test_engine_summary_splits_normal_and_reverse_realized_pnl() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    engine = PaperEngine(AppConfig())
    engine.positions = [
        Position(
            position_id="normal",
            market_id="m1",
            token_id="up",
            direction=Direction.UP,
            entry_price=0.50,
            quantity=20,
            entry_quote=10,
            opened_at=now,
            entry_edge_usd=20,
            status="CLOSED",
            realized_pnl=1.25,
        ),
        Position(
            position_id="reverse",
            market_id="m2",
            token_id="down",
            direction=Direction.DOWN,
            entry_price=0.40,
            quantity=25,
            entry_quote=10,
            opened_at=now,
            entry_edge_usd=20,
            reverse_entry=True,
            status="CLOSED",
            realized_pnl=-0.75,
        ),
    ]

    summary = engine.summary()

    assert summary["normal_realized_pnl"] == 1.25
    assert summary["reverse_realized_pnl"] == -0.75
    assert summary["realized_pnl"] == 0.50
