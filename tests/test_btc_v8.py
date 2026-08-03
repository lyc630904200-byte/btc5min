from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from polybtc.btc_v8 import (
    BtcV8Engine,
    BtcV8Registry,
    V8Model,
    V8Position,
    V8Snapshot,
    normalized_remaining_time,
    v8_auto_exit_decision,
    v8_decision_policy,
    v8_orderbook_chase_exit_decision,
    v8_orderbook_chase_signal,
)
from polybtc.config import AppConfig
from polybtc.models import BookLevel, Direction, MarketState, OrderBookSnapshot, PriceTick
from polybtc.signal_sources import SignalEvent


def market(start: datetime, market_id: str) -> MarketState:
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


def poly_books(
    current: MarketState,
    now: datetime,
    *,
    up_bid: float = 0.29,
    up_ask: float = 0.30,
    down_bid: float = 0.74,
    down_ask: float = 0.75,
) -> dict[Direction, OrderBookSnapshot]:
    return {
        Direction.UP: OrderBookSnapshot(
            token_id=current.up_token_id,
            market_id=current.condition_id,
            timestamp=now,
            received_at=now,
            bids=[BookLevel(price=up_bid, size=100)],
            asks=[BookLevel(price=up_ask, size=100)],
            depth_trusted=True,
            min_order_size=5,
            tick_size=0.01,
        ),
        Direction.DOWN: OrderBookSnapshot(
            token_id=current.down_token_id,
            market_id=current.condition_id,
            timestamp=now,
            received_at=now,
            bids=[BookLevel(price=down_bid, size=100)],
            asks=[BookLevel(price=down_ask, size=100)],
            depth_trusted=True,
            min_order_size=5,
            tick_size=0.01,
        ),
    }


def signal(
    source: str,
    kind: str,
    now: datetime,
    *,
    price: float = 100_000.0,
    side: str = "buy",
) -> SignalEvent:
    market_type = "futures" if source == "binance_futures" else "spot"
    payload = {
        "source": source,
        "market_type": market_type,
        "kind": kind,
        "symbol": "BTCUSDT",
        "exchange_timestamp": now,
        "received_at": now,
    }
    if kind in {"trade", "liquidation"}:
        payload.update(price=price, quantity=1.0, taker_side=side)
    if kind == "book":
        payload.update(
            bids=[BookLevel(price=price - 1, size=5)],
            asks=[BookLevel(price=price + 1, size=5)],
        )
    return SignalEvent.model_validate(payload)


def make_engine(tmp_path, **overrides):
    config = AppConfig(
        data_dir=tmp_path,
        btc_v8={
            "enabled": True,
            "buy_confirmation_seconds": 0.1,
            "buy_confirmation_updates": 2,
            "sell_confirmation_seconds": 0.1,
            "sell_confirmation_updates": 2,
            **overrides,
        },
        risk={"max_data_age_ms": 5_000},
    )
    registry = BtcV8Registry(tmp_path / "btc-v8.sqlite3")
    return BtcV8Engine(config, registry), registry


def feed_inputs(
    engine: BtcV8Engine,
    current: MarketState,
    now: datetime,
    books: dict[Direction, OrderBookSnapshot],
    sources: tuple[str, ...] = ("binance", "coinbase", "kraken"),
) -> None:
    engine.add_chainlink_tick(
        PriceTick(
            source="polymarket_rtds",
            symbol="BTCUSD",
            price=100_000.0,
            exchange_timestamp=now,
            received_at=now,
        )
    )
    for source in sources:
        engine.add_signal(signal(source, "trade", now))
        engine.add_signal(signal(source, "book", now))
    for direction, book in books.items():
        engine.add_polymarket_book(direction, book)


def feed_chainlink_and_poly(
    engine: BtcV8Engine,
    now: datetime,
    books: dict[Direction, OrderBookSnapshot],
) -> None:
    engine.add_chainlink_tick(
        PriceTick(
            source="polymarket_rtds",
            symbol="BTCUSD",
            price=100_000.0,
            exchange_timestamp=now,
            received_at=now,
        )
    )
    for direction, book in books.items():
        engine.add_polymarket_book(direction, book)


def test_remaining_time_uses_full_market_and_clamps() -> None:
    start = datetime(2026, 8, 3, tzinfo=timezone.utc)
    end = start + timedelta(seconds=420)
    assert normalized_remaining_time(start, end, start) == pytest.approx(1.0)
    assert normalized_remaining_time(start, end, start + timedelta(seconds=210)) == pytest.approx(0.0)
    assert normalized_remaining_time(start, end, end) == pytest.approx(-1.0)
    assert normalized_remaining_time(start, end, start - timedelta(seconds=10)) == 1.0
    assert normalized_remaining_time(start, end, end + timedelta(seconds=10)) == -1.0


def test_requires_two_fresh_spot_sources(tmp_path) -> None:
    start = datetime(2026, 8, 3, tzinfo=timezone.utc)
    current = market(start, "fresh-gate")
    engine, registry = make_engine(tmp_path)
    now = start + timedelta(seconds=10)
    books = poly_books(current, now)
    feed_inputs(engine, current, now, books, sources=("binance",))
    engine.evaluate(current, books, now, force=True)

    assert engine.position is None
    assert engine.last_reason == "insufficient_fresh_spot_exchanges"
    assert engine.diagnostics["fresh_spot_count"] == 1
    registry.close()


def test_cross_exchange_price_outlier_is_removed_from_consensus(tmp_path) -> None:
    start = datetime(2026, 8, 3, tzinfo=timezone.utc)
    current = market(start, "outlier")
    engine, registry = make_engine(tmp_path)
    now = start + timedelta(seconds=10)
    books = poly_books(current, now)
    engine.add_chainlink_tick(
        PriceTick(
            source="polymarket_rtds",
            symbol="BTCUSD",
            price=100_000,
            exchange_timestamp=now,
            received_at=now,
        )
    )
    for source, price in (("binance", 100_000), ("coinbase", 100_010), ("kraken", 120_000)):
        engine.add_signal(signal(source, "trade", now, price=price))
        engine.add_signal(signal(source, "book", now, price=price))
    engine.evaluate(current, books, now, force=True)

    assert engine.diagnostics["fresh_spot_exchanges"] == ["binance", "coinbase"]
    assert engine.diagnostics["anomalous_spot_exchanges"] == ["kraken"]
    assert engine.diagnostics["features"]["kraken_anomaly"] == 1.0
    assert engine.diagnostics["features"]["kraken_missing"] == 1.0
    registry.close()


def test_fixed_five_dollar_buy_and_value_sell_charge_both_fees(tmp_path) -> None:
    start = datetime(2026, 8, 3, tzinfo=timezone.utc)
    current = market(start, "buy-sell")
    engine, registry = make_engine(tmp_path, max_entries_per_market=1)
    engine.set_market(current, start)

    first = start + timedelta(seconds=10)
    first_books = poly_books(current, first)
    feed_inputs(engine, current, first, first_books)
    engine.evaluate(current, first_books, first, force=True)
    second = first + timedelta(seconds=0.11)
    second_books = poly_books(current, second)
    feed_inputs(engine, current, second, second_books)
    engine.evaluate(current, second_books, second, force=True)

    assert engine.position is not None
    assert engine.position.entry_quote == pytest.approx(5.0)
    assert engine.position.entry_fee_usd > 0
    bought_position_id = engine.position.position_id

    sell_first = first + timedelta(seconds=1)
    sell_books = poly_books(current, sell_first, up_bid=0.80, up_ask=0.81)
    feed_inputs(engine, current, sell_first, sell_books)
    engine.evaluate(current, sell_books, sell_first, force=True)
    sell_second = sell_first + timedelta(seconds=0.11)
    sell_books = poly_books(current, sell_second, up_bid=0.80, up_ask=0.81)
    feed_inputs(engine, current, sell_second, sell_books)
    engine.evaluate(current, sell_books, sell_second, force=True)

    assert engine.position is None
    closed = registry.get_position(bought_position_id)
    assert closed is not None
    assert closed.exit_fee_usd > 0
    assert closed.realized_pnl is not None and closed.realized_pnl > 0
    assert [trade["action"] for trade in registry.recent_trades()] == ["SELL", "BUY"]

    after_cooldown = sell_second + timedelta(seconds=4)
    new_books = poly_books(current, after_cooldown)
    feed_inputs(engine, current, after_cooldown, new_books)
    engine.evaluate(current, new_books, after_cooldown, force=True)
    assert engine.last_reason == "market_entry_limit"
    registry.close()


def test_auto_decision_mode_controls_buy_sell_and_uses_internal_risk(tmp_path) -> None:
    start = datetime(2026, 8, 3, tzinfo=timezone.utc)
    current = market(start, "auto-decision")
    engine, registry = make_engine(
        tmp_path,
        auto_decision_mode=True,
        buy_edge_cents=99,
        sell_edge_cents=99,
        max_loss_usd=0.01,
        max_entries_per_market=1,
    )
    policy = v8_decision_policy(engine.config.btc_v8)
    assert policy["buy_edge_cents"] == 0
    assert policy["sell_edge_cents"] == 0
    assert policy["use_fixed_max_loss"] is False
    engine.set_market(current, start)

    first = start + timedelta(seconds=10)
    books = poly_books(current, first)
    feed_inputs(engine, current, first, books)
    engine.evaluate(current, books, first, force=True)
    second = first + timedelta(seconds=2.01)
    books = poly_books(current, second)
    feed_inputs(engine, current, second, books)
    engine.evaluate(current, books, second, force=True)
    assert engine.position is not None

    adverse_first = second + timedelta(seconds=1)
    adverse_books = poly_books(current, adverse_first, up_bid=0.28, up_ask=0.29)
    feed_inputs(engine, current, adverse_first, adverse_books)
    engine.evaluate(current, adverse_books, adverse_first, force=True)
    adverse_second = adverse_first + timedelta(seconds=1.01)
    adverse_books = poly_books(current, adverse_second, up_bid=0.28, up_ask=0.29)
    feed_inputs(engine, current, adverse_second, adverse_books)
    engine.evaluate(current, adverse_books, adverse_second, force=True)
    assert engine.candidates["SELL"]["pnl"] < -0.01
    assert engine.candidates["SELL"]["reason"] == "auto_hold"
    assert engine.candidates["SELL"]["risk_score"] < engine.candidates["SELL"]["risk_threshold"]
    assert engine.position is not None

    exit_first = adverse_second + timedelta(seconds=1)
    exit_books = poly_books(current, exit_first, up_bid=0.80, up_ask=0.81)
    feed_inputs(engine, current, exit_first, exit_books)
    engine.evaluate(current, exit_books, exit_first, force=True)
    exit_second = exit_first + timedelta(seconds=1.01)
    exit_books = poly_books(current, exit_second, up_bid=0.80, up_ask=0.81)
    feed_inputs(engine, current, exit_second, exit_books)
    engine.evaluate(current, exit_books, exit_second, force=True)
    assert engine.position is None
    assert registry.recent_trades()[0]["reason"] == "auto_model_sell"
    registry.close()


def test_auto_mode_rejects_buy_that_cannot_exit_inside_loss_budget(tmp_path) -> None:
    start = datetime(2026, 8, 3, tzinfo=timezone.utc)
    current = market(start, "auto-wide-spread")
    current.tick_size = 0.001
    engine, registry = make_engine(tmp_path, auto_decision_mode=True)
    engine.set_market(current, start)

    for offset in (10.0, 12.01):
        now = start + timedelta(seconds=offset)
        books = poly_books(
            current,
            now,
            up_bid=0.002,
            up_ask=0.009,
            down_bid=0.99,
            down_ask=0.998,
        )
        books[Direction.UP].tick_size = 0.001
        books[Direction.UP].bids[0].size = 10_000
        books[Direction.UP].asks[0].size = 10_000
        feed_inputs(engine, current, now, books)
        engine.evaluate(current, books, now, force=True)

    assert engine.position is None
    assert engine.candidates["UP"]["reason"] == "auto_liquidation_risk"
    assert engine.candidates["UP"]["immediate_liquidation_pnl"] < -2.5
    registry.close()


def test_orderbook_chase_policy_is_short_horizon_and_mutually_exclusive() -> None:
    config = AppConfig(
        btc_v8={"enabled": True, "orderbook_chase_mode": True}
    )
    policy = v8_decision_policy(config.btc_v8)

    assert policy["orderbook_chase_mode"] is True
    assert policy["automated_mode"] is True
    assert policy["buy_confirmation_seconds"] == pytest.approx(0.25)
    assert policy["buy_confirmation_updates"] == 2
    assert policy["buy_edge_cents"] == pytest.approx(1.0)
    with pytest.raises(ValueError, match="mutually exclusive"):
        AppConfig(
            btc_v8={
                "auto_decision_mode": True,
                "orderbook_chase_mode": True,
            }
        )
    with pytest.raises(ValueError, match="drawdown fraction"):
        AppConfig(btc_v8={"chase_take_profit_drawdown_fraction": 1.01})
    with pytest.raises(ValueError, match="must be positive"):
        AppConfig(btc_v8={"chase_take_profit_arm_usd": 0})


def test_orderbook_chase_signal_uses_received_spot_lead_and_consensus() -> None:
    diagnostics = {
        "sigma": 0.001,
        "remaining_seconds": 100,
        "chainlink_open_price": 100_000,
        "chainlink_current_price": 100_000,
        "spot_chainlink_lead_return_1s": 0.001,
        "spot_positive_return_sources_1s": 2,
        "spot_negative_return_sources_1s": 0,
        "required_fresh_spot_count": 2,
    }

    signal_result = v8_orderbook_chase_signal(0.50, diagnostics)

    assert signal_result["eligible"] is True
    assert signal_result["direction"] == "UP"
    assert signal_result["supporting_sources"] == 2
    assert signal_result["target_probability"] > 0.50
    diagnostics["spot_positive_return_sources_1s"] = 1
    weak_consensus = v8_orderbook_chase_signal(0.50, diagnostics)
    assert weak_consensus["eligible"] is False
    assert weak_consensus["reason"] == "chase_consensus_insufficient"


def test_orderbook_chase_exit_takes_catchup_and_enforces_timeout() -> None:
    opened = datetime(2026, 8, 3, tzinfo=timezone.utc)
    position = auto_position(
        strategy_mode="orderbook_chase",
        entry_target_probability=0.58,
        entry_signal_strength=1.0,
        opened_at=opened,
    )
    diagnostics = {
        "orderbook_chase": {
            "eligible": True,
            "direction": "UP",
            "signal_strength": 0.8,
        }
    }

    caught_up = v8_orderbook_chase_exit_decision(
        position, 0.58, 0.03, diagnostics, opened + timedelta(seconds=1)
    )
    assert caught_up["reason"] == "chase_caught_up"

    timed_out = v8_orderbook_chase_exit_decision(
        position, 0.50, -0.20, diagnostics, opened + timedelta(seconds=5)
    )
    assert timed_out["reason"] == "chase_timeout"


def test_orderbook_chase_does_not_sell_only_because_lead_signal_decays() -> None:
    opened = datetime(2026, 8, 3, tzinfo=timezone.utc)
    position = auto_position(
        strategy_mode="orderbook_chase",
        entry_target_probability=0.58,
        entry_signal_strength=2.0,
        opened_at=opened,
    )
    diagnostics = {
        "orderbook_chase": {
            "eligible": False,
            "reason": "chase_signal_weak",
            "direction": "UP",
            "signal_strength": 0.1,
        }
    }

    decision = v8_orderbook_chase_exit_decision(
        position, 0.50, -0.20, diagnostics, opened + timedelta(seconds=2)
    )

    assert decision["eligible"] is False
    assert decision["reason"] == "chase_holding"
    assert decision["signal_decayed"] is True


def test_orderbook_chase_arms_and_executes_trailing_profit_exit() -> None:
    opened = datetime(2026, 8, 3, tzinfo=timezone.utc)
    diagnostics = {
        "orderbook_chase": {
            "eligible": True,
            "direction": "UP",
            "signal_strength": 1.0,
        }
    }
    position = auto_position(
        strategy_mode="orderbook_chase",
        entry_target_probability=0.90,
        entry_signal_strength=1.0,
        peak_unrealized_pnl=0.80,
        opened_at=opened,
    )

    decision = v8_orderbook_chase_exit_decision(
        position,
        0.50,
        0.40,
        diagnostics,
        opened + timedelta(seconds=2),
    )

    assert decision["eligible"] is True
    assert decision["reason"] == "chase_profit_trailing"
    assert decision["take_profit_armed"] is True
    assert decision["peak_unrealized_pnl"] == pytest.approx(0.80)
    assert decision["profit_drawdown_usd"] == pytest.approx(0.40)
    assert decision["required_profit_drawdown_usd"] == pytest.approx(0.28)


def test_orderbook_chase_trailing_profit_waits_for_arm_and_drawdown() -> None:
    opened = datetime(2026, 8, 3, tzinfo=timezone.utc)
    diagnostics = {
        "orderbook_chase": {
            "eligible": True,
            "direction": "UP",
            "signal_strength": 1.0,
        }
    }
    position = auto_position(
        strategy_mode="orderbook_chase",
        entry_target_probability=0.90,
        entry_signal_strength=1.0,
        peak_unrealized_pnl=0.40,
        opened_at=opened,
    )

    decision = v8_orderbook_chase_exit_decision(
        position,
        0.50,
        0.30,
        diagnostics,
        opened + timedelta(seconds=2),
    )

    assert decision["eligible"] is False
    assert decision["take_profit_armed"] is True
    assert decision["profit_drawdown_usd"] == pytest.approx(0.10)
    assert decision["required_profit_drawdown_usd"] == pytest.approx(0.15)


def test_orderbook_chase_opens_from_spot_lead_and_sells_into_catchup(tmp_path) -> None:
    start = datetime(2026, 8, 3, tzinfo=timezone.utc)
    current = market(start, "chase-market")
    engine, registry = make_engine(
        tmp_path,
        orderbook_chase_mode=True,
        slippage_reserve_cents=0,
        max_entries_per_market=1,
    )
    engine.set_market(current, start)

    history_at = start + timedelta(seconds=9)
    engine.add_chainlink_tick(
        PriceTick(
            source="polymarket_rtds",
            symbol="BTCUSD",
            price=100_000,
            exchange_timestamp=history_at,
            received_at=history_at,
        )
    )
    for source in ("binance", "coinbase"):
        engine.add_signal(signal(source, "trade", history_at, price=100_000))
        engine.add_signal(signal(source, "book", history_at, price=100_000))

    for offset in (10.0, 10.6):
        now = start + timedelta(seconds=offset)
        books = poly_books(
            current, now, up_bid=0.45, up_ask=0.46, down_bid=0.53, down_ask=0.54
        )
        engine.add_chainlink_tick(
            PriceTick(
                source="polymarket_rtds",
                symbol="BTCUSD",
                price=100_000,
                exchange_timestamp=now,
                received_at=now,
            )
        )
        for source in ("binance", "coinbase"):
            engine.add_signal(signal(source, "trade", now, price=100_100))
            engine.add_signal(signal(source, "book", now, price=100_100))
        for direction, book in books.items():
            engine.add_polymarket_book(direction, book)
        engine.evaluate(current, books, now, force=True)

    assert engine.diagnostics["timestamp_basis"] == "received_at"
    assert engine.diagnostics["orderbook_chase"]["direction"] == "UP"
    assert engine.position is not None
    assert engine.position.strategy_mode == "orderbook_chase"
    position_id = engine.position.position_id

    for offset in (11.0, 11.3):
        now = start + timedelta(seconds=offset)
        books = poly_books(
            current, now, up_bid=0.60, up_ask=0.61, down_bid=0.38, down_ask=0.39
        )
        engine.add_chainlink_tick(
            PriceTick(
                source="polymarket_rtds",
                symbol="BTCUSD",
                price=100_100,
                exchange_timestamp=now,
                received_at=now,
            )
        )
        for source in ("binance", "coinbase"):
            engine.add_signal(signal(source, "trade", now, price=100_100))
            engine.add_signal(signal(source, "book", now, price=100_100))
        for direction, book in books.items():
            engine.add_polymarket_book(direction, book)
        engine.evaluate(current, books, now, force=True)

    assert engine.position is None
    closed = registry.get_position(position_id)
    assert closed is not None and closed.realized_pnl is not None
    assert closed.realized_pnl > 0
    assert closed.exit_reason == "chase_caught_up"
    trades = registry.recent_trades()
    assert trades[0]["strategy_mode"] == "orderbook_chase"
    assert trades[0]["reason"] == "chase_caught_up"
    assert trades[1]["reason"] == "chase_buy"
    registry.close()


def auto_position(**overrides) -> V8Position:
    now = datetime(2026, 8, 3, tzinfo=timezone.utc)
    values = {
        "market_id": "exit-market",
        "market_slug": "btc-updown-5m-exit",
        "direction": Direction.UP,
        "quantity": 10,
        "entry_price": 0.50,
        "entry_quote": 5.0,
        "entry_fee_usd": 0.10,
        "entry_model_probability": 0.70,
        "entry_formula_probability": 0.70,
        "peak_unrealized_pnl": 0.0,
        "opened_at": now,
    }
    values.update(overrides)
    return V8Position(**values)


def auto_exit(
    position: V8Position,
    *,
    probability: float = 0.70,
    formula_probability: float = 0.70,
    pnl: float = -0.25,
    elapsed: float = 120.0,
    support: float = 0.0,
    emergency_loss_enabled: bool = False,
) -> dict:
    features = {}
    for seconds in (1, 3, 5, 10):
        features[f"cross_median_return_{seconds}s"] = support
        features[f"cross_direction_agreement_{seconds}s"] = support
        features[f"cross_cvd_consensus_{seconds}s"] = support
    features.update(
        {
            "futures_return_1s": support,
            "futures_return_5s": support,
            "futures_return_30s": support,
            "futures_cvd_5s": support,
            "futures_book_imbalance": support,
            "futures_ofi_5s": support,
            "poly_up_depth_imbalance": support,
            "poly_up_microprice": support,
            "poly_up_ofi_5s": support,
            "poly_up_trade_flow_5s": support,
        }
    )
    return v8_auto_exit_decision(
        position=position,
        probability=probability,
        formula_probability=formula_probability,
        net_value=0.20,
        pnl=pnl,
        features=features,
        diagnostics={
            "z_score": support * 2,
            "fresh_spot_count": 2,
            "required_fresh_spot_count": 2,
        },
        elapsed_seconds=elapsed,
        sell_end_seconds=298,
        trained_markets=100,
        emergency_loss_enabled=emergency_loss_enabled,
    )


def test_auto_exit_has_immediate_emergency_loss_protection() -> None:
    decision = auto_exit(
        auto_position(), pnl=-2.50, emergency_loss_enabled=True
    )

    assert decision["eligible"] is True
    assert decision["reason"] == "auto_emergency_loss"
    assert decision["loss_limit_usd"] == pytest.approx(2.50)


def test_auto_emergency_loss_executes_without_waiting_for_confirmation(tmp_path) -> None:
    start = datetime(2026, 8, 3, tzinfo=timezone.utc)
    current = market(start, "auto-emergency")
    engine, registry = make_engine(
        tmp_path,
        auto_decision_mode=True,
        auto_emergency_loss_enabled=True,
        max_entries_per_market=1,
    )
    engine.set_market(current, start)

    first = start + timedelta(seconds=10)
    books = poly_books(current, first)
    feed_inputs(engine, current, first, books)
    engine.evaluate(current, books, first, force=True)
    second = first + timedelta(seconds=2.01)
    books = poly_books(current, second)
    feed_inputs(engine, current, second, books)
    engine.evaluate(current, books, second, force=True)
    assert engine.position is not None
    position_id = engine.position.position_id

    adverse = second + timedelta(seconds=0.25)
    books = poly_books(current, adverse, up_bid=0.10, up_ask=0.11)
    feed_inputs(engine, current, adverse, books)
    engine.evaluate(current, books, adverse, force=True)

    assert engine.position is None
    closed = registry.get_position(position_id)
    assert closed is not None
    assert closed.exit_reason == "auto_emergency_loss"
    sell = registry.recent_trades()[0]
    assert sell["reason"] == "auto_emergency_loss"
    assert sell["decision_details"]["risk_score"] > 0
    registry.close()


def test_auto_emergency_loss_can_be_disabled() -> None:
    decision = auto_exit(auto_position(), pnl=-2.50)

    assert decision["reason"] != "auto_emergency_loss"
    assert decision["emergency_loss_enabled"] is False


def test_auto_exit_uses_probability_decay_and_adverse_market_signals() -> None:
    decision = auto_exit(
        auto_position(),
        probability=0.30,
        formula_probability=0.35,
        pnl=-1.0,
        support=-1.0,
    )

    assert decision["eligible"] is True
    assert decision["reason"] == "auto_thesis_exit"
    assert decision["probability_decay"] > 0.50
    assert decision["signal_support"] < 0


def test_auto_exit_protects_peak_profit_drawdown() -> None:
    decision = auto_exit(
        auto_position(peak_unrealized_pnl=2.0),
        probability=0.65,
        formula_probability=0.65,
        pnl=0.50,
    )

    assert decision["eligible"] is True
    assert decision["reason"] == "auto_trailing_exit"
    assert decision["drawdown_usd"] == pytest.approx(1.50)


def test_auto_exit_reduces_losing_settlement_tail() -> None:
    decision = auto_exit(
        auto_position(),
        probability=0.45,
        formula_probability=0.45,
        pnl=-1.0,
        elapsed=289.0,
    )

    assert decision["eligible"] is True
    assert decision["reason"] == "auto_terminal_exit"


def test_legacy_position_json_loads_without_auto_exit_state() -> None:
    position = V8Position.model_validate(
        {
            "market_id": "legacy",
            "market_slug": "btc-updown-5m-legacy",
            "direction": "UP",
            "quantity": 10,
            "entry_price": 0.50,
            "entry_quote": 5.0,
            "entry_fee_usd": 0.1,
            "opened_at": "2026-08-03T00:00:00Z",
        }
    )

    assert position.entry_model_probability is None
    assert position.entry_formula_probability is None
    assert position.peak_unrealized_pnl is None


def test_open_position_can_sell_when_all_spot_sources_are_stale(tmp_path) -> None:
    start = datetime(2026, 8, 3, tzinfo=timezone.utc)
    current = market(start, "sell-with-stale-spots")
    engine, registry = make_engine(tmp_path)
    engine.set_market(current, start)

    first = start + timedelta(seconds=10)
    books = poly_books(current, first)
    feed_inputs(engine, current, first, books)
    engine.evaluate(current, books, first, force=True)
    second = first + timedelta(seconds=0.11)
    books = poly_books(current, second)
    feed_inputs(engine, current, second, books)
    engine.evaluate(current, books, second, force=True)
    assert engine.position is not None

    sell_first = first + timedelta(seconds=3)
    sell_books = poly_books(current, sell_first, up_bid=0.80, up_ask=0.81)
    feed_chainlink_and_poly(engine, sell_first, sell_books)
    engine.evaluate(current, sell_books, sell_first, force=True)
    sell_second = sell_first + timedelta(seconds=0.11)
    sell_books = poly_books(current, sell_second, up_bid=0.80, up_ask=0.81)
    feed_chainlink_and_poly(engine, sell_second, sell_books)
    engine.evaluate(current, sell_books, sell_second, force=True)

    assert engine.diagnostics["fresh_spot_count"] == 0
    assert engine.position is None
    assert engine.last_reason == "sold"
    registry.close()


def test_restart_restores_round_position_and_pending_model(tmp_path) -> None:
    start = datetime(2026, 8, 3, tzinfo=timezone.utc)
    current = market(start, "restart")
    engine, registry = make_engine(tmp_path)
    engine.set_market(current, start)
    first = start + timedelta(seconds=10)
    books = poly_books(current, first)
    feed_inputs(engine, current, first, books)
    engine.evaluate(current, books, first, force=True)
    second = first + timedelta(seconds=0.11)
    books = poly_books(current, second)
    feed_inputs(engine, current, second, books)
    engine.evaluate(current, books, second, force=True)
    position_id = engine.position.position_id

    candidate = V8Model(model_key="v8_candidate", updated_at=second)
    registry.save_model(candidate)
    registry.schedule_model(candidate.model_key, second)
    registry.close()

    config = engine.config
    restarted_registry = BtcV8Registry(tmp_path / "btc-v8.sqlite3")
    restarted = BtcV8Engine(config, restarted_registry)
    restarted.set_market(current, second + timedelta(seconds=1))

    assert restarted.current_round is not None
    assert restarted.current_round.entry_count == 1
    assert restarted.position is not None
    assert restarted.position.position_id == position_id
    assert restarted_registry.pending_model()[0] == "v8_candidate"
    restarted_registry.close()


def test_liquidations_do_not_pollute_futures_trade_return_or_cvd(tmp_path) -> None:
    engine, registry = make_engine(tmp_path)
    now = datetime(2026, 8, 3, tzinfo=timezone.utc)
    engine.add_signal(signal("binance_futures", "trade", now, price=100.0, side="buy"))
    engine.add_signal(signal("binance_futures", "liquidation", now, price=500.0, side="sell"))

    assert engine._trade_prices("binance_futures") == [(now, 100.0)]
    assert engine._cvd("binance_futures", now, 5) == pytest.approx(1.0)
    registry.close()


def test_polymarket_last_trade_becomes_deduplicated_flow_feature(tmp_path) -> None:
    engine, registry = make_engine(tmp_path)
    now = datetime(2026, 8, 3, tzinfo=timezone.utc)
    book = OrderBookSnapshot(
        token_id="up",
        market_id="m1",
        timestamp=now,
        received_at=now,
        bids=[BookLevel(price=0.50, size=10)],
        asks=[BookLevel(price=0.51, size=10)],
        depth_trusted=True,
        raw={
            "_last_trade": {
                "timestamp": int(now.timestamp() * 1000),
                "price": "0.51",
                "size": "3",
                "side": "BUY",
            }
        },
    )
    engine.add_polymarket_book(Direction.UP, book)
    engine.add_polymarket_book(Direction.UP, book)

    assert engine._polymarket_trade_flow(Direction.UP, now) == 1.0
    assert len(engine.polymarket_trades[Direction.UP]) == 1
    registry.flush_raw_events(force=True)
    trade_count = registry.connection.execute(
        "SELECT COUNT(*) AS value FROM btc_v8_raw_events WHERE source='polymarket' AND kind='trade'"
    ).fetchone()["value"]
    assert trade_count == 1
    registry.close()


def test_raw_books_coalesce_to_one_event_per_source_symbol_and_250ms_bucket(tmp_path) -> None:
    engine, registry = make_engine(tmp_path)
    now = datetime(2026, 8, 3, tzinfo=timezone.utc)
    for offset_ms in (0, 50, 249, 250):
        received_at = now + timedelta(milliseconds=offset_ms)
        book = OrderBookSnapshot(
            token_id="up",
            market_id="m1",
            timestamp=received_at,
            received_at=received_at,
            bids=[BookLevel(price=0.50, size=10 + offset_ms)],
            asks=[BookLevel(price=0.51, size=10)],
            depth_trusted=True,
        )
        engine.add_polymarket_book(Direction.UP, book)
    registry.flush_raw_events(force=True)

    rows = registry.connection.execute(
        "SELECT payload_json FROM btc_v8_raw_events WHERE source='polymarket' AND kind='book' ORDER BY received_at"
    ).fetchall()
    assert len(rows) == 2
    assert isinstance(rows[0]["payload_json"], bytes)
    assert registry.decode_raw_payload(rows[0]["payload_json"])["received_at"] == (
        "2026-08-03T00:00:00.249000+00:00"
    )
    assert registry.decode_raw_payload(rows[1]["payload_json"])["received_at"] == (
        "2026-08-03T00:00:00.250000+00:00"
    )
    assert registry.decode_raw_payload('{"legacy":true}') == {"legacy": True}
    registry.close()


def test_settlement_model_update_waits_for_a_later_market(tmp_path) -> None:
    start = datetime(2026, 8, 3, tzinfo=timezone.utc)
    first_market = market(start, "m1")
    second_market = market(start + timedelta(minutes=5), "m2")
    third_market = market(start + timedelta(minutes=10), "m3")
    engine, registry = make_engine(tmp_path)
    engine.set_market(first_market, start)
    registry.save_snapshot(
        V8Snapshot(
            market_id=first_market.condition_id,
            snapshot_second=10,
            formula_probability=0.5,
            model_probability=0.5,
            features={"remaining_time": 0.5},
            fresh_spot_exchanges=["binance", "coinbase"],
            created_at=start + timedelta(seconds=10),
        )
    )

    engine.set_market(second_market, second_market.start_time)
    assert engine.current_round.model_version == 1
    settled_at = second_market.start_time + timedelta(seconds=10)
    engine.settle(first_market.slug, Direction.UP, settled_at)
    assert registry.load_model("v8").version == 2
    assert engine.current_round.market_id == second_market.condition_id
    assert engine.model.version == 1

    engine.set_market(third_market, third_market.start_time)
    assert engine.current_round.model_version == 2
    assert engine.model.version == 2
    engine.settle(first_market.slug, Direction.UP, settled_at + timedelta(seconds=1))
    assert registry.load_model("v8").version == 2
    registry.close()


def test_strict_retrain_recalculates_probabilities_and_schedules_candidate(tmp_path) -> None:
    engine, registry = make_engine(tmp_path)
    start = datetime(2026, 8, 3, tzinfo=timezone.utc)
    markets = [market(start, "early"), market(start + timedelta(minutes=5), "late")]
    for current, outcome in reversed(list(zip(markets, (Direction.UP, Direction.DOWN)))):
        engine.set_market(current, current.start_time)
        round_ = engine.current_round
        round_.official_outcome = outcome
        round_.closed_at = current.end_time
        round_.trained = True
        registry.save_round(round_)
        registry.save_snapshot(
            V8Snapshot(
                market_id=current.condition_id,
                snapshot_second=30,
                formula_probability=0.5,
                model_probability=0.99,
                features={"remaining_time": 0.8 if outcome == Direction.UP else -0.8},
                fresh_spot_exchanges=["binance", "coinbase"],
                created_at=current.start_time + timedelta(seconds=30),
            )
        )
    original = registry.connection.execute(
        "SELECT payload_json FROM btc_v8_models WHERE model_key='v8' AND version=1"
    ).fetchone()["payload_json"]
    completed_at = start + timedelta(minutes=20)
    result = registry.retrain_candidate(completed_at, "v8_test_candidate")

    assert result["trained_markets"] == 2
    assert result["snapshots"] == 2
    assert result["market_order"] == [markets[0].slug, markets[1].slug]
    assert result["walk_forward_brier"] < 0.4
    summary = registry.summary()
    assert summary["by_source_availability"]["binance"]["available"]["count"] == 2
    assert summary["by_source_availability"]["kraken"]["missing"]["count"] == 2
    assert registry.active_model_key() == "v8"
    assert registry.pending_model()[0] == "v8_test_candidate"
    unchanged = registry.connection.execute(
        "SELECT payload_json FROM btc_v8_models WHERE model_key='v8' AND version=1"
    ).fetchone()["payload_json"]
    assert unchanged == original
    assert registry.activate_pending_for_market(completed_at) == "v8"
    assert (
        registry.activate_pending_for_market(completed_at + timedelta(seconds=1))
        == "v8_test_candidate"
    )
    registry.close()
