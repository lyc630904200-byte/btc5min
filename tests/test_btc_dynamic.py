from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from polybtc.btc_dynamic import (
    BtcDynamicEngine,
    BtcDynamicRegistry,
    DynamicModel,
    dynamic_max_price,
    training_snapshot_seconds,
)
from polybtc.config import AppConfig, BtcDynamicConfig
from polybtc.models import (
    BookLevel,
    Direction,
    MarketState,
    OrderBookSnapshot,
    PriceTick,
)


def market(start: datetime, market_id: str = "btc-dynamic") -> MarketState:
    return MarketState(
        asset="BTC",
        condition_id=market_id,
        slug=f"btc-updown-5m-{int(start.timestamp())}",
        question="BTC Up or Down",
        threshold_price=100_000.0,
        threshold_source="polymarket_rtds",
        threshold_verified=True,
        start_time=start,
        end_time=start + timedelta(minutes=5),
        up_token_id=f"{market_id}-up",
        down_token_id=f"{market_id}-down",
        min_order_size=5,
        tick_size=0.01,
    )


def books(
    current: MarketState,
    now: datetime,
    *,
    up_ask: float = 0.80,
    down_ask: float = 0.20,
    size: float = 100.0,
) -> dict[Direction, OrderBookSnapshot]:
    return {
        Direction.UP: OrderBookSnapshot(
            token_id=current.up_token_id,
            market_id=current.condition_id,
            timestamp=now,
            received_at=now,
            bids=[BookLevel(price=up_ask - 0.01, size=size)],
            asks=[BookLevel(price=up_ask, size=size)],
            depth_trusted=True,
            min_order_size=5,
            tick_size=0.01,
        ),
        Direction.DOWN: OrderBookSnapshot(
            token_id=current.down_token_id,
            market_id=current.condition_id,
            timestamp=now,
            received_at=now,
            bids=[BookLevel(price=max(0.01, down_ask - 0.01), size=size)],
            asks=[BookLevel(price=down_ask, size=size)],
            depth_trusted=True,
            min_order_size=5,
            tick_size=0.01,
        ),
    }


def tick(price: float, now: datetime, source: str) -> PriceTick:
    return PriceTick(
        source=source,
        symbol="BTCUSD" if source == "polymarket_rtds" else "BTCUSDT",
        price=price,
        exchange_timestamp=now,
        received_at=now,
    )


def engine(tmp_path, start: datetime, **overrides):
    config = AppConfig(
        data_dir=tmp_path,
        btc_dynamic={
            "enabled": True,
            "confirmation_seconds": 0,
            "confirmation_updates": 1,
            **overrides,
        },
        risk={"max_data_age_ms": 5000},
    )
    registry = BtcDynamicRegistry(tmp_path / "dynamic.sqlite3")
    strategy = BtcDynamicEngine(config, registry)
    current = market(start)
    strategy.set_market(current, start)
    return strategy, registry, current


def feed_prices(
    strategy: BtcDynamicEngine,
    start: datetime,
    now: datetime,
    *,
    chainlink: float = 100_250.0,
    binance: float = 71_500.0,
) -> None:
    strategy.add_chainlink_tick(tick(100_000.0, start, "polymarket_rtds"))
    strategy.add_chainlink_tick(tick(chainlink, now, "polymarket_rtds"))
    strategy.add_binance_tick(tick(71_000.0, start, "binance"))
    strategy.add_binance_tick(tick(binance, now, "binance"))


def test_dynamic_limit_is_probability_driven_and_floored() -> None:
    high = dynamic_max_price(0.99, 0.07, 0.0135, 0.03, 0.01)
    low = dynamic_max_price(0.70, 0.07, 0.0135, 0.03, 0.01)
    assert high is not None and high > 0.91
    assert low is not None and high > low
    assert high * 100 == pytest.approx(round(high * 100))
    margin = 0.99 - high - 0.07 * high * (1 - high) - 0.0135
    assert margin >= 0.03


def test_training_snapshots_follow_entry_window(tmp_path) -> None:
    settings = BtcDynamicConfig(
        entry_seconds_after_open=240,
        exit_seconds_after_open=270,
    )
    assert training_snapshot_seconds(settings) == (240.0, 247.5, 255.0, 262.5)

    start = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)
    strategy, registry, current = engine(
        tmp_path,
        start,
        entry_seconds_after_open=240,
        exit_seconds_after_open=270,
    )
    now = start + timedelta(seconds=240)
    feed_prices(strategy, start, now)
    strategy.evaluate(current, books(current, now), now)
    assert [item.snapshot_second for item in registry.snapshots(current.condition_id)] == [
        240.0
    ]
    assert strategy.diagnostics["training_snapshot_seconds"] == (
        240.0,
        247.5,
        255.0,
        262.5,
    )
    registry.close()


def test_zero_model_matches_formula_and_correction_is_limited() -> None:
    features = {name: 1.0 for name in DynamicModel().weights}
    zero = DynamicModel()
    assert zero.probability(0.63, features, 0.10) == pytest.approx(0.63)
    strong = DynamicModel(bias=3.0, weights={name: 3.0 for name in features})
    assert strong.probability(0.63, features, 0.10) == pytest.approx(0.73)


def test_formula_uses_chainlink_relative_move_not_binance_absolute_price(tmp_path) -> None:
    start = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)
    strategy, registry, current = engine(tmp_path, start)
    now = start + timedelta(seconds=270)
    feed_prices(strategy, start, now, binance=30_000.0)
    strategy.evaluate(current, books(current, now), now)
    first = strategy.diagnostics["formula_probability_up"]

    other_registry = BtcDynamicRegistry(tmp_path / "other.sqlite3")
    other = BtcDynamicEngine(strategy.config, other_registry)
    other.set_market(current, start)
    feed_prices(other, start, now, binance=200_000.0)
    other.evaluate(current, books(current, now), now)
    assert other.diagnostics["formula_probability_up"] == pytest.approx(first)
    assert all(-1 <= value <= 1 for value in other.diagnostics["features"].values())
    registry.close()
    other_registry.close()


def test_places_online_and_formula_orders_and_holds_for_settlement(tmp_path) -> None:
    start = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)
    strategy, registry, current = engine(tmp_path, start)
    now = start + timedelta(seconds=270)
    feed_prices(strategy, start, now)
    strategy.evaluate(current, books(current, now), now)

    assert strategy.current_round is not None
    assert strategy.current_round.online_order is not None
    assert strategy.current_round.formula_order is not None
    assert strategy.current_round.online_order.direction == Direction.UP
    assert strategy.status == "order_held_for_settlement"
    assert len(registry.snapshots(current.condition_id)) == 1
    registry.close()


def test_depth_and_actual_edge_are_hard_guards(tmp_path) -> None:
    start = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)
    strategy, registry, current = engine(tmp_path, start)
    now = start + timedelta(seconds=270)
    feed_prices(strategy, start, now)
    shallow = books(current, now, size=3)
    strategy.evaluate(current, shallow, now)
    assert strategy.current_round is not None
    assert strategy.current_round.online_order is None
    assert strategy.candidates["UP"]["reason"] == "depth_below_dynamic_limit"
    registry.close()


def test_settlement_trains_once_and_persists_model(tmp_path) -> None:
    start = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)
    strategy, registry, current = engine(tmp_path, start)
    now = start + timedelta(seconds=270)
    feed_prices(strategy, start, now)
    strategy.evaluate(current, books(current, now), now)
    assert strategy.model.trained_markets == 0

    settled = strategy.settle(current.slug, Direction.UP, now + timedelta(seconds=31))
    assert settled is not None
    assert strategy.model.trained_markets == 1
    assert settled.online_order is not None
    assert settled.online_order.realized_pnl is not None
    strategy.settle(current.slug, Direction.UP, now + timedelta(seconds=32))
    assert strategy.model.trained_markets == 1

    persisted = registry.load_model()
    assert persisted.trained_markets == 1
    registry.close()


def test_restart_restores_round_ticks_and_prevents_duplicate_order(tmp_path) -> None:
    start = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)
    strategy, registry, current = engine(tmp_path, start)
    now = start + timedelta(seconds=270)
    feed_prices(strategy, start, now)
    strategy.evaluate(current, books(current, now), now)
    original_id = strategy.current_round.online_order.order_id
    registry.close()

    reopened_registry = BtcDynamicRegistry(tmp_path / "dynamic.sqlite3")
    restored = BtcDynamicEngine(strategy.config, reopened_registry)
    restored.set_market(current, now)
    restored.evaluate(current, books(current, now), now)
    assert restored.current_round.online_order.order_id == original_id
    assert len(reopened_registry.recent_orders()) == 2
    reopened_registry.close()


def test_model_reset_is_deferred_until_next_market(tmp_path) -> None:
    start = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)
    strategy, registry, current = engine(tmp_path, start)
    strategy.model = DynamicModel(bias=1.0, trained_markets=4)
    registry.save_model(strategy.model)
    strategy.request_model_reset(start + timedelta(seconds=10))
    assert strategy.model.trained_markets == 4

    next_market = market(start + timedelta(minutes=5), "btc-next")
    strategy.set_market(next_market, next_market.start_time)
    assert strategy.model.trained_markets == 0
    assert strategy.model.bias == 0
    registry.close()
