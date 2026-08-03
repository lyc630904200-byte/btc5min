from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import polybtc.btc_dynamic as btc_dynamic_module
from polybtc.btc_dynamic import (
    BtcDynamicEngine,
    BtcDynamicRegistry,
    DynamicModel,
    DynamicOrder,
    DynamicRound,
    DynamicSnapshot,
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


def test_dynamic_sizing_defaults_preserve_quantity_mode() -> None:
    settings = BtcDynamicConfig.model_validate({"quantity": 12})

    assert settings.sizing_mode == "quantity"
    assert settings.quantity == 12
    assert settings.quote_amount_usd == 5
    assert settings.loss_streak_limit == 5
    assert settings.loss_cooldown_minutes == 30


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


def test_unverified_rtds_open_candidate_updates_chainlink_diagnostics_only(tmp_path) -> None:
    start = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)
    strategy, registry, current = engine(tmp_path, start)
    current.threshold_price = None
    current.threshold_source = "threshold_verification_failed"
    current.threshold_verified = False
    current.threshold_fetched_at = start + timedelta(seconds=3)
    current.threshold_candidate_price = 100_000.0
    current.threshold_candidate_source = "polymarket_rtds_start_tick"
    current.threshold_candidate_observed_at = start
    current.threshold_candidate_received_at = start + timedelta(seconds=1)

    now = start + timedelta(seconds=270)
    feed_prices(strategy, start, now)
    strategy.evaluate(current, books(current, now), now)

    assert strategy.status == "chainlink_open_unverified"
    assert strategy.diagnostics["chainlink_open_price"] == 100_000.0
    assert strategy.diagnostics["chainlink_open_verified"] is False
    assert strategy.diagnostics["chainlink_current_price"] == 100_250.0
    assert strategy.current_round is not None
    assert strategy.current_round.online_order is None
    assert strategy.candidates == {}
    registry.close()


def test_first_after_start_chainlink_tick_is_diagnostic_open_fallback(tmp_path) -> None:
    start = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)
    strategy, registry, current = engine(tmp_path, start)
    current.threshold_price = None
    current.threshold_source = "threshold_verification_failed"
    current.threshold_verified = False

    strategy.add_chainlink_tick(
        PriceTick(
            source="polymarket_rtds",
            symbol="BTC/USD",
            price=99_990.0,
            exchange_timestamp=start - timedelta(seconds=1),
            received_at=start + timedelta(milliseconds=200),
        )
    )
    strategy.add_chainlink_tick(
        PriceTick(
            source="polymarket_rtds",
            symbol="BTC/USD",
            price=100_005.0,
            exchange_timestamp=start + timedelta(seconds=1),
            received_at=start + timedelta(seconds=2),
        )
    )
    now = start + timedelta(seconds=3)
    strategy.evaluate(current, books(current, now), now)

    assert strategy.status == "chainlink_open_unverified"
    assert strategy.diagnostics["chainlink_open_price"] == 100_005.0
    assert (
        strategy.diagnostics["chainlink_open_source"]
        == "polymarket_rtds_first_tick_after_start_unverified"
    )
    assert strategy.diagnostics["chainlink_open_verified"] is False
    assert strategy.current_round is not None
    assert strategy.current_round.online_order is None
    assert strategy.candidates == {}
    registry.close()


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


def test_fixed_quote_mode_spends_requested_principal_and_records_actual_quantity(
    tmp_path,
) -> None:
    start = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)
    strategy, registry, current = engine(
        tmp_path,
        start,
        sizing_mode="quote",
        quote_amount_usd=5,
    )
    now = start + timedelta(seconds=270)
    feed_prices(strategy, start, now)
    strategy.evaluate(current, books(current, now), now)

    assert strategy.current_round is not None
    order = strategy.current_round.online_order
    assert order is not None
    assert order.sizing_mode == "quote"
    assert order.requested_quantity is None
    assert order.requested_quote_usd == 5
    assert order.quote == pytest.approx(5)
    assert order.quantity == pytest.approx(5 / order.avg_price)
    assert order.quantity != pytest.approx(strategy.current_round.settings.quantity)
    assert strategy.candidates["UP"]["quantity"] == pytest.approx(order.quantity)
    registry.close()


def test_fixed_quote_mode_rejects_result_below_market_minimum(tmp_path) -> None:
    start = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)
    strategy, registry, current = engine(
        tmp_path,
        start,
        sizing_mode="quote",
        quote_amount_usd=2,
    )
    now = start + timedelta(seconds=270)
    feed_prices(strategy, start, now)
    strategy.evaluate(current, books(current, now), now)

    assert strategy.current_round is not None
    assert strategy.current_round.online_order is None
    assert strategy.candidates["UP"]["reason"] == "quantity_below_market_minimum"
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


def test_dashboard_state_exposes_model_before_market_diagnostics(tmp_path) -> None:
    start = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)
    strategy, registry, _ = engine(tmp_path, start)
    strategy.model = DynamicModel(version=7, trained_markets=6)
    strategy.diagnostics = {}

    state = strategy.dashboard_state()

    assert state["diagnostics"] == {}
    assert state["model"]["version"] == 7
    assert state["model"]["trained_markets"] == 6
    registry.close()


def test_frozen_model_activates_only_for_next_market_and_survives_restart(
    tmp_path,
) -> None:
    start = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
    strategy, registry, current = engine(tmp_path, start)
    frozen = DynamicModel(
        model_key="v3",
        frozen=True,
        source_model_key="online",
        source_version=413,
        source_as_of=start,
        version=413,
        trained_markets=412,
        bias=-0.024,
    )
    registry.save_model(frozen, "v3")
    requested_at = start + timedelta(seconds=30)
    registry.schedule_model_activation("v3", requested_at)

    strategy.set_market(current, start + timedelta(seconds=60))
    assert strategy.current_round is not None
    assert strategy.current_round.model_key == "online"
    assert strategy.model.model_key == "online"

    next_market = market(start + timedelta(minutes=5), "btc-v3")
    strategy.set_market(next_market, next_market.start_time)
    assert strategy.current_round.model_key == "v3"
    assert strategy.model.model_key == "v3"
    assert strategy.model.frozen is True
    assert registry.active_model_key() == "v3"
    assert registry.pending_model_key() is None
    registry.close()

    reopened = BtcDynamicRegistry(tmp_path / "dynamic.sqlite3")
    restored = BtcDynamicEngine(strategy.config, reopened)
    restored.set_market(next_market, next_market.start_time + timedelta(seconds=1))
    assert restored.current_round is not None
    assert restored.current_round.model_key == "v3"
    assert restored.model.version == 413
    assert restored.model.frozen is True
    reopened.close()


def test_frozen_model_settlement_does_not_train_or_get_replaced_by_delayed_v1(
    tmp_path,
) -> None:
    start = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
    config = AppConfig(data_dir=tmp_path, btc_dynamic={"enabled": True})
    registry = BtcDynamicRegistry(tmp_path / "dynamic.sqlite3")
    online = DynamicModel(model_key="online", version=8, trained_markets=7)
    frozen = DynamicModel(
        model_key="v3",
        frozen=True,
        source_model_key="online",
        source_version=413,
        source_as_of=start,
        version=413,
        trained_markets=412,
        bias=-0.024,
    )
    registry.save_model(online, "online")
    registry.save_model(frozen, "v3")
    registry.set_control("active_model_key", "v3", start)

    delayed_market = market(start - timedelta(minutes=5), "btc-delayed-v1")
    delayed_round = DynamicRound(
        market_id=delayed_market.condition_id,
        market_slug=delayed_market.slug,
        model_key="online",
        start_time=delayed_market.start_time,
        end_time=delayed_market.end_time,
        settings=config.btc_dynamic,
        created_at=delayed_market.start_time,
        updated_at=delayed_market.start_time,
    )
    registry.save_round(delayed_round)
    registry.save_snapshot(
        DynamicSnapshot(
            market_id=delayed_market.condition_id,
            model_key="online",
            snapshot_second=270,
            formula_probability=0.6,
            online_probability=0.6,
            features={name: 0.1 for name in online.weights},
            created_at=delayed_market.start_time + timedelta(seconds=270),
        )
    )

    current = market(start, "btc-current-v3")
    strategy = BtcDynamicEngine(config, registry)
    strategy.set_market(current, start)
    registry.save_snapshot(
        DynamicSnapshot(
            market_id=current.condition_id,
            model_key="v3",
            snapshot_second=270,
            formula_probability=0.6,
            online_probability=0.6,
            features={name: 0.1 for name in frozen.weights},
            created_at=start + timedelta(seconds=270),
        )
    )

    strategy.settle(delayed_market.slug, Direction.UP, start + timedelta(seconds=1))
    assert registry.load_model("online").version == 9
    assert strategy.model.model_key == "v3"
    assert strategy.model.version == 413

    strategy.settle(current.slug, Direction.UP, start + timedelta(minutes=5))
    persisted = registry.load_model("v3")
    assert persisted.version == 413
    assert persisted.trained_markets == 412
    assert persisted.bias == pytest.approx(-0.024)
    assert strategy.current_round is not None
    assert strategy.current_round.trained is True
    registry.close()


def test_online_loss_streak_cooldown_survives_restart_and_pauses_all_orders(
    tmp_path,
) -> None:
    start = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)
    strategy, registry, current = engine(
        tmp_path,
        start,
        loss_streak_limit=2,
        loss_cooldown_minutes=30,
    )

    def place_and_lose(current_market: MarketState) -> datetime:
        decision_at = current_market.start_time + timedelta(seconds=270)
        feed_prices(strategy, current_market.start_time, decision_at)
        strategy.evaluate(
            current_market,
            books(current_market, decision_at),
            decision_at,
        )
        assert strategy.current_round is not None
        order = strategy.current_round.online_order
        assert order is not None
        losing_outcome = (
            Direction.DOWN if order.direction == Direction.UP else Direction.UP
        )
        settled_at = current_market.end_time + timedelta(seconds=1)
        strategy.settle(current_market.slug, losing_outcome, settled_at)
        return settled_at

    first_settled_at = place_and_lose(current)
    state = strategy.dashboard_state(first_settled_at)["loss_cooldown"]
    assert state["active"] is False
    assert state["consecutive_losses"] == 1

    second = market(start + timedelta(minutes=5), "btc-second")
    strategy.set_market(second, second.start_time)
    second_settled_at = place_and_lose(second)
    state = strategy.dashboard_state(second_settled_at)["loss_cooldown"]
    assert state["active"] is True
    assert state["consecutive_losses"] == 2
    cooldown_until = datetime.fromisoformat(state["cooldown_until"])
    assert cooldown_until == second_settled_at + timedelta(minutes=30)

    strategy.settle(
        second.slug,
        Direction.DOWN,
        second_settled_at + timedelta(seconds=1),
    )
    assert strategy.dashboard_state(second_settled_at + timedelta(seconds=1))[
        "loss_cooldown"
    ]["consecutive_losses"] == 2
    registry.close()

    reopened_registry = BtcDynamicRegistry(tmp_path / "dynamic.sqlite3")
    strategy = BtcDynamicEngine(strategy.config, reopened_registry)
    paused = market(start + timedelta(minutes=10), "btc-paused")
    strategy.set_market(paused, paused.start_time)
    paused_at = paused.start_time + timedelta(seconds=270)
    feed_prices(strategy, paused.start_time, paused_at)
    strategy.evaluate(paused, books(paused, paused_at), paused_at)

    assert strategy.current_round is not None
    assert strategy.current_round.online_order is None
    assert strategy.current_round.formula_order is None
    assert strategy.status == "loss_streak_cooldown"
    assert strategy.diagnostics["online_probability_up"] is not None
    assert strategy.candidates["UP"]["reason"] == "loss_streak_cooldown"
    assert strategy.candidates["formula_UP"]["reason"] == "loss_streak_cooldown"

    resumed = market(cooldown_until + timedelta(minutes=1), "btc-resumed")
    strategy.set_market(resumed, resumed.start_time)
    resumed_at = resumed.start_time + timedelta(seconds=270)
    feed_prices(strategy, resumed.start_time, resumed_at)
    strategy.evaluate(resumed, books(resumed, resumed_at), resumed_at)

    assert strategy.current_round is not None
    assert strategy.current_round.online_order is not None
    assert strategy.dashboard_state(resumed_at)["loss_cooldown"] == {
        "active": False,
        "consecutive_losses": 0,
        "cooldown_until": None,
        "remaining_seconds": 0.0,
    }
    reopened_registry.close()


def test_daily_summary_uses_computer_local_calendar_date(tmp_path, monkeypatch) -> None:
    local_timezone = timezone(timedelta(hours=8))
    monkeypatch.setattr(
        btc_dynamic_module,
        "local_calendar_date",
        lambda value: value.astimezone(local_timezone).date().isoformat(),
    )
    registry = BtcDynamicRegistry(tmp_path / "dynamic.sqlite3")

    def save_order(
        market_id: str,
        variant: str,
        created_at: datetime,
        realized_pnl: float,
    ) -> None:
        registry.save_order(
            DynamicOrder(
                market_id=market_id,
                market_slug=market_id,
                variant=variant,
                direction=Direction.UP,
                model_probability=0.6,
                formula_probability=0.6,
                max_price=0.5,
                avg_price=0.5,
                quantity=10,
                quote=5,
                fee_usd=0.1,
                net_edge_per_share=0.05,
                created_at=created_at,
                realized_pnl=realized_pnl,
            )
        )

    save_order("before-local-midnight", "online", datetime(2026, 7, 31, 15, 59, tzinfo=timezone.utc), 2)
    save_order("after-local-midnight", "online", datetime(2026, 7, 31, 16, 1, tzinfo=timezone.utc), 3)
    save_order("formula-after-midnight", "formula", datetime(2026, 7, 31, 17, 0, tzinfo=timezone.utc), -1)

    daily = registry.summary()["daily"]

    assert daily == [
        {
            "date": "2026-08-01",
            "online_orders": 1,
            "online_pnl": pytest.approx(3),
            "formula_orders": 1,
            "formula_pnl": pytest.approx(-1),
        },
        {
            "date": "2026-07-31",
            "online_orders": 1,
            "online_pnl": pytest.approx(2),
            "formula_orders": 0,
            "formula_pnl": pytest.approx(0),
        },
    ]
    registry.close()
