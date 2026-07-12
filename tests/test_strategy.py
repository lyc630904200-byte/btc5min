from datetime import datetime, timedelta, timezone

from polybtc.config import RiskConfig, StrategyConfig
from polybtc.engine import PaperEngine
from polybtc.config import AppConfig
from polybtc.models import BookLevel, Direction, MarketState, OrderBookSnapshot, PriceTick
from polybtc.strategy import StrategyState, evaluate_entry, evaluate_exit, position_from_entry


def market(now: datetime) -> MarketState:
    return MarketState(
        condition_id="m1",
        slug="bitcoin-up-or-down",
        question="Bitcoin Up or Down above 118000",
        threshold_price=118000,
        end_time=now + timedelta(minutes=5),
        up_token_id="up",
        down_token_id="down",
        min_order_size=5,
    )


def book(token_id: str, bid: float, ask: float, now: datetime) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        token_id=token_id,
        market_id="m1",
        timestamp=now,
        bids=[BookLevel(price=bid, size=100)],
        asks=[BookLevel(price=ask, size=100)],
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

    decision = evaluate_entry(state, StrategyConfig(), RiskConfig())

    assert decision.accepted is True
    assert decision.signal is not None
    assert decision.signal.direction == Direction.UP
    assert decision.fill is not None
    assert decision.fill.avg_price == 0.60


def test_entry_rejects_expensive_ask() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    state = StrategyState(
        market=market(now),
        price_tick=PriceTick(price=118070, received_at=now),
        up_book=book("up", 0.80, 0.81, now),
        down_book=book("down", 0.18, 0.20, now),
        now=now,
    )

    decision = evaluate_entry(state, StrategyConfig(), RiskConfig())

    assert decision.accepted is False
    assert decision.reason == "ask_too_expensive"


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

    decision = evaluate_entry(state, StrategyConfig(), RiskConfig())

    assert decision.accepted is False
    assert decision.reason == "market_not_started"


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
    entry = evaluate_entry(entry_state, StrategyConfig(), RiskConfig())
    assert entry.fill is not None
    position = position_from_entry(entry.fill, edge=70, opened_at=now)

    decision = evaluate_exit(position, state, StrategyConfig(), RiskConfig())

    assert decision.should_exit is True
    assert decision.reason is not None
    assert decision.reason.value == "take_profit"


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
    assert engine.rejections[-1]["reason"] == "stale_book_timestamp"
