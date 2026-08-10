from __future__ import annotations

import math
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
    v8_buy_candidate,
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
            "low_price_penalty_weight": 0.0,
            "polymarket_trend_min_span_seconds": 0.1,
            "min_hold_seconds": 0.1,
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
    for seconds_ago in (30, 10, 0):
        observed_at = now - timedelta(seconds=seconds_ago)
        engine.add_chainlink_tick(
            PriceTick(
                source="polymarket_rtds",
                symbol="BTCUSD",
                price=100_000.0,
                exchange_timestamp=observed_at,
                received_at=observed_at,
            )
        )
    for source in sources:
        engine.add_signal(signal(source, "trade", now - timedelta(seconds=30), price=99_700))
        engine.add_signal(signal(source, "trade", now - timedelta(seconds=10), price=99_800))
        engine.add_signal(signal(source, "trade", now))
        engine.add_signal(signal(source, "book", now))
    seed_twap_direction_average(engine, sources, now, 0.003)
    for direction, book in books.items():
        prior = book.model_copy(deep=True)
        prior.timestamp = now - timedelta(seconds=0.2)
        prior.received_at = prior.timestamp
        price_delta = -0.01 if direction == Direction.UP else 0.01
        prior.bids[0].price += price_delta
        prior.asks[0].price += price_delta
        engine.add_polymarket_book(direction, prior)
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


def seed_twap_direction_average(
    engine: BtcV8Engine,
    sources: tuple[str, ...],
    end_at: datetime,
    signal_return: float,
) -> None:
    for source in sources:
        for seconds_ago in range(20, -1, -1):
            engine._record_twap_direction_signal(
                source,
                end_at - timedelta(seconds=seconds_ago),
                signal_return,
            )


def test_remaining_time_uses_full_market_and_clamps() -> None:
    start = datetime(2026, 8, 3, tzinfo=timezone.utc)
    end = start + timedelta(seconds=420)
    assert normalized_remaining_time(start, end, start) == pytest.approx(1.0)
    assert normalized_remaining_time(start, end, start + timedelta(seconds=210)) == pytest.approx(0.0)
    assert normalized_remaining_time(start, end, end) == pytest.approx(-1.0)
    assert normalized_remaining_time(start, end, start - timedelta(seconds=10)) == 1.0
    assert normalized_remaining_time(start, end, end + timedelta(seconds=10)) == -1.0


def test_accepts_one_fresh_spot_source_and_rejects_zero(tmp_path) -> None:
    start = datetime(2026, 8, 3, tzinfo=timezone.utc)
    current = market(start, "fresh-gate")
    engine, registry = make_engine(tmp_path)
    now = start + timedelta(seconds=10)
    books = poly_books(current, now)
    feed_inputs(engine, current, now, books, sources=("binance",))
    engine.evaluate(current, books, now, force=True)

    assert engine.position is None
    assert engine.last_reason == "confirming_buy"
    assert engine.diagnostics["fresh_spot_count"] == 1
    assert engine.diagnostics["required_fresh_spot_count"] == 1
    registry.close()

    no_spot_engine, no_spot_registry = make_engine(tmp_path)
    no_spot_engine.set_market(current, start)
    feed_chainlink_and_poly(no_spot_engine, now, books)
    no_spot_engine.evaluate(current, books, now, force=True)

    assert no_spot_engine.position is None
    assert no_spot_engine.last_reason == "insufficient_fresh_spot_exchanges"
    assert no_spot_engine.diagnostics["fresh_spot_count"] == 0
    no_spot_registry.close()


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
    assert engine.diagnostics["source_health"]["kraken"]["last_reason"] == "price_outlier"
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


def test_removed_v8_parameters_are_ignored_and_not_serialized() -> None:
    settings = AppConfig(
        btc_v8={
            "enabled": True,
            "auto_decision_mode": True,
            "buy_edge_cents": 99,
            "max_loss_usd": 0.01,
        }
    ).btc_v8

    dumped = settings.model_dump()
    assert "auto_decision_mode" not in dumped
    assert "buy_edge_cents" not in dumped
    assert "max_loss_usd" not in dumped
    assert dumped["min_effective_edge_cents"] == pytest.approx(0.5)
    assert dumped["exit_fee_reserve_fraction"] == pytest.approx(0.0)
    assert v8_decision_policy(settings)["orderbook_chase_mode"] is True


def test_direction_mode_rejects_buy_outside_price_range(tmp_path) -> None:
    start = datetime(2026, 8, 3, tzinfo=timezone.utc)
    current = market(start, "auto-wide-spread")
    current.tick_size = 0.001
    engine, registry = make_engine(tmp_path)
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
    assert engine.candidates["UP"]["reason"] == "chase_price_out_of_range"
    assert engine.candidates["UP"]["avg_price"] < 0.15
    registry.close()


def test_v8_policy_is_always_twap_directional_and_validates_new_parameters() -> None:
    config = AppConfig(btc_v8={"enabled": True})
    policy = v8_decision_policy(config.btc_v8)

    assert policy["orderbook_chase_mode"] is True
    assert policy["automated_mode"] is True
    assert policy["buy_confirmation_seconds"] == pytest.approx(1.0)
    assert policy["buy_confirmation_updates"] == 2
    assert policy["sell_confirmation_seconds"] == pytest.approx(0.50)
    assert policy["sell_confirmation_updates"] == 1
    assert policy["buy_edge_cents"] == pytest.approx(0.5)
    assert config.btc_v8.min_fresh_spot_exchanges == 1
    assert config.btc_v8.min_buy_price_cents == pytest.approx(15)
    assert config.btc_v8.max_buy_price_cents == pytest.approx(90)
    late_entry = AppConfig(
        btc_v8={
            "min_entry_remaining_seconds": 10,
            "min_hold_seconds": 30,
            "max_hold_seconds": 90,
        }
    ).btc_v8
    assert late_entry.min_entry_remaining_seconds < late_entry.min_hold_seconds
    assert late_entry.min_entry_remaining_seconds < late_entry.max_hold_seconds
    with pytest.raises(ValueError, match="buy price range"):
        AppConfig(btc_v8={"min_buy_price_cents": 91, "max_buy_price_cents": 90})
    with pytest.raises(ValueError, match="at most 100 points"):
        AppConfig(btc_v8={"min_probability_move_points": 101})
    with pytest.raises(ValueError, match="supporting source minimum"):
        AppConfig(btc_v8={"spot_exchanges": ["binance"], "min_supporting_sources": 2})
    with pytest.raises(ValueError, match="reserve fraction"):
        AppConfig(btc_v8={"exit_fee_reserve_fraction": 1.01})
    assert AppConfig(
        btc_v8={
            "min_profit_usd": 0,
            "basis_exclusion_seconds": 0,
            "basis_min_clip_bps": 0,
        }
    ).btc_v8.min_profit_usd == 0
    with pytest.raises(ValueError, match="probabilities"):
        AppConfig(btc_v8={"chase_take_profit_drawdown_fraction": 1.01})
    with pytest.raises(ValueError, match="must be positive"):
        AppConfig(btc_v8={"chase_take_profit_arm_usd": 0})


def test_orderbook_chase_signal_uses_received_spot_lead_and_consensus() -> None:
    diagnostics = {
        "sigma": 0.001,
        "remaining_seconds": 100,
        "chainlink_open_price": 100_000,
        "chainlink_current_price": 100_250,
        "chainlink_age_seconds": 0.1,
        "twap_direction_signal_return": 0.003,
        "twap_direction_signal_returns": {
            "binance": 0.003,
            "coinbase": 0.0025,
        },
        "required_fresh_spot_count": 2,
    }

    signal_result = v8_orderbook_chase_signal(0.61, diagnostics)

    assert signal_result["eligible"] is True
    assert signal_result["direction"] == "UP"
    assert signal_result["supporting_sources"] == 2
    assert signal_result["target_probability"] > 0.50
    diagnostics["twap_direction_signal_return"] = 0.0000009
    diagnostics["twap_direction_signal_returns"] = {
        "binance": 0.0000009,
        "coinbase": 0.0000009,
    }
    weak_bps = v8_orderbook_chase_signal(0.61, diagnostics)
    assert weak_bps["eligible"] is False
    assert weak_bps["reason"] == "chase_direction_signal_too_small"
    assert weak_bps["min_direction_signal_bps"] == pytest.approx(0.03)
    assert weak_bps["min_signal_sigma"] == pytest.approx(0.05)
    assert weak_bps["min_probability_move"] == pytest.approx(0.002)

    diagnostics["sigma"] = 0.00001
    diagnostics["twap_direction_signal_return"] = 0.000006
    diagnostics["twap_direction_signal_returns"] = {
        "binance": 0.000006,
        "coinbase": 0.000006,
    }
    relaxed_bps = v8_orderbook_chase_signal(0.61, diagnostics)
    assert relaxed_bps["eligible"] is True
    assert relaxed_bps["direction_signal_abs_bps"] == pytest.approx(0.06)

    diagnostics["sigma"] = 0.001
    diagnostics["twap_direction_signal_return"] = 0.003
    diagnostics["twap_direction_signal_returns"] = {}
    weak_consensus = v8_orderbook_chase_signal(0.50, diagnostics)
    assert weak_consensus["eligible"] is False
    assert weak_consensus["reason"] == "chase_consensus_insufficient"

    diagnostics["twap_direction_signal_returns"] = {
        "binance": 0.000019,
        "coinbase": 0.000019,
    }
    diagnostics["twap_direction_signal_return"] = 0.000019
    weak_signal = v8_orderbook_chase_signal(0.50, diagnostics)
    assert weak_signal["eligible"] is False
    assert weak_signal["reason"] == "chase_signal_weak"

    diagnostics["twap_direction_signal_returns"] = {
        "binance": 0.0024,
        "coinbase": 0.0024,
    }
    diagnostics["twap_direction_signal_return"] = 0.0024
    diagnostics["remaining_seconds"] = 302
    small_probability_move = v8_orderbook_chase_signal(0.61, diagnostics)
    assert small_probability_move["signal_strength"] == pytest.approx(2.4)
    assert small_probability_move["probability_move"] < 0.001
    assert small_probability_move["eligible"] is False
    assert small_probability_move["reason"] == "chase_probability_move_small"

    diagnostics["remaining_seconds"] = 100
    diagnostics["chainlink_current_price"] = 99_980
    diagnostics["twap_direction_signal_returns"] = {
        "binance": 0.003,
        "coinbase": 0.0025,
    }
    diagnostics["twap_direction_signal_return"] = 0.003
    absolute_mismatch = v8_orderbook_chase_signal(0.49, diagnostics)
    assert absolute_mismatch["eligible"] is True
    assert absolute_mismatch["reason"] == "chase_signal"
    assert absolute_mismatch["absolute_direction_aligned"] is False
    assert absolute_mismatch["absolute_direction_filter_enabled"] is False

    diagnostics["chainlink_current_price"] = 100_001
    neutral_open_band = v8_orderbook_chase_signal(0.49, diagnostics)
    assert neutral_open_band["eligible"] is True
    assert neutral_open_band["absolute_direction_neutral"] is True
    assert neutral_open_band["absolute_direction_aligned"] is True

    diagnostics["chainlink_current_price"] = 98_000
    low_target_probability = v8_orderbook_chase_signal(0.01, diagnostics)
    assert low_target_probability["eligible"] is False
    assert low_target_probability["target_probability"] < 0.40
    assert low_target_probability["reason"] == "chase_target_probability_too_low"

    diagnostics["chainlink_current_price"] = 100_250
    diagnostics["remaining_seconds"] = 100
    diagnostics["chainlink_age_seconds"] = 5.01
    stale_chainlink = v8_orderbook_chase_signal(0.61, diagnostics)
    assert stale_chainlink["eligible"] is False
    assert stale_chainlink["reason"] == "chase_chainlink_stale"


def test_orderbook_chase_low_price_is_risk_sized_then_rejected() -> None:
    now = datetime(2026, 8, 3, tzinfo=timezone.utc)
    current = market(now, "chase-low-price")
    book = poly_books(
        current,
        now,
        up_bid=0.04,
        up_ask=0.05,
    )[Direction.UP]
    result = v8_buy_candidate(
        market=current,
        direction=Direction.UP,
        probability=0.50,
        formula_probability=0.50,
        settings=AppConfig(
            btc_v8={"enabled": True, "orderbook_chase_mode": True}
        ).btc_v8,
        book=book,
        book_reason=None,
        taker_fee_rate=0.07,
        valuation_probability=0.50,
        strategy_mode="orderbook_chase",
    )

    assert result["eligible"] is False
    assert result["reason"] == "chase_price_out_of_range"
    assert result["risk_sized"] is True
    assert result["max_quantity_by_tick"] == pytest.approx(50.0)
    assert result["quantity"] == pytest.approx(50.0)
    assert result["quote"] == pytest.approx(2.50)


def test_orderbook_chase_low_price_penalty_blocks_cheap_edge() -> None:
    now = datetime(2026, 8, 3, tzinfo=timezone.utc)
    current = market(now, "chase-cheap-edge")
    book = poly_books(
        current,
        now,
        down_bid=0.25,
        down_ask=0.26,
    )[Direction.DOWN]
    result = v8_buy_candidate(
        market=current,
        direction=Direction.DOWN,
        probability=0.4845808942958585,
        formula_probability=0.4845808942958585,
        settings=AppConfig(
            btc_v8={"enabled": True, "orderbook_chase_mode": True}
        ).btc_v8,
        book=book,
        book_reason=None,
        taker_fee_rate=0.07,
        valuation_probability=0.4987797480274653,
        strategy_mode="orderbook_chase",
    )

    assert result["unpenalized_edge_per_share"] > 0.03
    assert result["low_price_penalty_per_share"] == pytest.approx((0.45 - result["avg_price"]) * 1.5)
    assert result["edge_per_share"] == pytest.approx(result["effective_edge_per_share"])
    assert result["edge_per_share"] < 0.01
    assert result["eligible"] is False
    assert result["reason"] == "actual_edge_below_threshold"


def test_relaxed_entry_fee_reserve_and_limit_gap_are_reported() -> None:
    now = datetime(2026, 8, 3, tzinfo=timezone.utc)
    current = market(now, "relaxed-limit")
    book = poly_books(current, now, up_bid=0.69, up_ask=0.70)[Direction.UP]
    settings = AppConfig(btc_v8={"enabled": True}).btc_v8

    result = v8_buy_candidate(
        market=current,
        direction=Direction.UP,
        probability=0.56,
        formula_probability=0.56,
        settings=settings,
        book=book,
        book_reason=None,
        taker_fee_rate=0.07,
        valuation_probability=0.56,
        strategy_mode="orderbook_chase",
    )

    assert result["full_estimated_exit_fee_per_share"] > 0
    assert result["estimated_exit_fee_per_share"] == pytest.approx(0.0)
    assert result["exit_fee_reserve_fraction"] == pytest.approx(0.0)
    assert result["reason"] == "best_ask_above_limit"
    assert result["book_best_ask"] == pytest.approx(0.70)
    assert result["limit_gap_cents"] == pytest.approx((0.70 - result["limit"]) * 100)


def test_buy_score_prefers_higher_price_over_raw_edge() -> None:
    now = datetime(2026, 8, 3, tzinfo=timezone.utc)
    current = market(now, "high-price-priority")
    settings = AppConfig(
        btc_v8={
            "enabled": True,
            "buy_edge_cents": 1.0,
            "slippage_reserve_cents": 0.0,
        }
    ).btc_v8
    low_price = v8_buy_candidate(
        market=current,
        direction=Direction.UP,
        probability=0.45,
        formula_probability=0.45,
        settings=settings,
        book=poly_books(current, now, up_bid=0.15, up_ask=0.16)[Direction.UP],
        book_reason=None,
        taker_fee_rate=0.07,
    )
    high_price = v8_buy_candidate(
        market=current,
        direction=Direction.DOWN,
        probability=0.55,
        formula_probability=0.55,
        settings=settings,
        book=poly_books(current, now, down_bid=0.49, down_ask=0.50)[Direction.DOWN],
        book_reason=None,
        taker_fee_rate=0.07,
    )

    assert low_price["eligible"] is True
    assert high_price["eligible"] is True
    assert low_price["edge_per_share"] > high_price["edge_per_share"]
    assert high_price["avg_price"] > low_price["avg_price"]
    assert high_price["buy_score"] > low_price["buy_score"]
    assert high_price["buy_score_formula"] == "avg_price*1+edge_per_share*0.3"


def test_engine_only_buys_the_confirmed_direction(tmp_path, monkeypatch) -> None:
    start = datetime(2026, 8, 3, tzinfo=timezone.utc)
    now = start + timedelta(seconds=10)
    current = market(start, "high-price-choice")
    engine, registry = make_engine(
        tmp_path,
        buy_edge_cents=1.0,
        slippage_reserve_cents=0.0,
        max_entries_per_market=1,
    )
    engine.set_market(current, start)
    books = poly_books(
        current,
        now,
        up_bid=0.15,
        up_ask=0.16,
        down_bid=0.49,
        down_ask=0.50,
    )

    def probabilities(*args, **kwargs):
        return (
            0.45,
            0.45,
            {},
            {
                "sigma": 0.001,
                "chainlink_open_price": 100_000,
                "chainlink_current_price": 100_000,
                "chainlink_age_seconds": 0.0,
                "twap_direction_signal_return": -0.003,
                "twap_direction_signal_returns": {
                    "binance": -0.003,
                    "coinbase": -0.0025,
                },
                "required_fresh_spot_count": 1,
                "fresh_spot_count": 2,
                "fresh_spot_exchanges": ["binance", "coinbase"],
                "source_health": {},
                "remaining_seconds": 290.0,
            },
        )

    monkeypatch.setattr(engine, "_probabilities", probabilities)
    monkeypatch.setattr(engine, "_confirmation", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        engine,
        "_chase_polymarket_trend",
        lambda *args, **kwargs: {"eligible": True, "reason": "chase_polymarket_trend_ok"},
    )
    engine.evaluate(current, books, now, force=True)

    assert engine.position is not None
    assert engine.position.direction == Direction.DOWN
    assert engine.candidates["UP"]["reason"] == "chase_opposite_direction"
    assert engine.candidates["DOWN"]["buy_score_formula"] == "avg_price*1+edge_per_share*0.3"
    registry.close()


def test_spot_twap_basis_calibration_waits_then_removes_fixed_basis(tmp_path) -> None:
    engine, registry = make_engine(tmp_path, orderbook_chase_mode=True)
    now = datetime(2026, 8, 3, 0, 5, tzinfo=timezone.utc)
    baseline = 0.0004

    warming = engine._spot_twap_basis_calibration("binance", now, baseline + 0.00005)
    assert warming["ready"] is False
    assert warming["calibrated_gap_return"] == 0.0

    for seconds_ago in range(70, 9, -1):
        engine._record_spot_twap_basis(
            "binance",
            now - timedelta(seconds=seconds_ago),
            baseline,
        )
    calibrated = engine._spot_twap_basis_calibration(
        "binance",
        now,
        baseline + 0.00005,
    )

    assert calibrated["ready"] is True
    assert calibrated["sample_count"] == 61
    assert calibrated["sample_span_seconds"] == pytest.approx(60.0)
    assert calibrated["baseline_return"] == pytest.approx(baseline)
    assert calibrated["calibrated_gap_return"] == pytest.approx(0.00005)
    assert calibrated["clipped"] is False
    registry.close()


def test_spot_twap_basis_calibration_clips_large_outlier(tmp_path) -> None:
    engine, registry = make_engine(tmp_path, orderbook_chase_mode=True)
    now = datetime(2026, 8, 3, 0, 5, tzinfo=timezone.utc)
    for seconds_ago in range(70, 9, -1):
        value = 0.0004 + (0.000001 if seconds_ago % 2 else -0.000001)
        engine._record_spot_twap_basis(
            "binance",
            now - timedelta(seconds=seconds_ago),
            value,
        )

    calibrated = engine._spot_twap_basis_calibration("binance", now, 0.01)

    assert calibrated["ready"] is True
    assert calibrated["clipped"] is True
    assert calibrated["clip_limit_return"] == pytest.approx(0.0002)
    assert calibrated["calibrated_gap_return"] == pytest.approx(0.0002)
    registry.close()


def test_twap_direction_signal_uses_time_weighted_average_and_rewarms_after_gap(
    tmp_path,
) -> None:
    engine, registry = make_engine(tmp_path, orderbook_chase_mode=True)
    now = datetime(2026, 8, 3, 0, 5, tzinfo=timezone.utc)
    for seconds in range(21):
        engine._record_twap_direction_signal(
            "binance",
            now - timedelta(seconds=20 - seconds),
            seconds * 0.0001,
        )

    averaged = engine._twap_direction_signal_average("binance", now)
    assert averaged["ready"] is True
    assert averaged["sample_count"] == 21
    assert averaged["sample_span_seconds"] == pytest.approx(20.0)
    assert averaged["average_return"] == pytest.approx(0.001)

    after_gap = now + timedelta(seconds=4)
    engine._record_twap_direction_signal("binance", after_gap, 0.002)
    rewarmed = engine._twap_direction_signal_average("binance", after_gap)
    assert rewarmed["ready"] is False
    assert rewarmed["sample_count"] == 1
    assert rewarmed["average_return"] is None
    registry.close()


def test_orderbook_chase_lead_signal_uses_calibrated_spot_twap_gap_when_momentum_stalls(
    tmp_path,
) -> None:
    start = datetime(2026, 8, 3, tzinfo=timezone.utc)
    current = market(start, "chase-gap-lead")
    engine, registry = make_engine(
        tmp_path,
        orderbook_chase_mode=True,
        slippage_reserve_cents=0,
        max_entries_per_market=1,
    )
    engine.set_market(current, start)
    now = start + timedelta(seconds=40)
    baseline_basis = 0.001
    for source in ("binance", "coinbase"):
        for seconds_ago in range(70, 9, -1):
            engine._record_spot_twap_basis(
                source,
                now - timedelta(seconds=seconds_ago),
                baseline_basis,
            )

    history_at = start + timedelta(seconds=10)
    engine.add_chainlink_tick(
        PriceTick(
            source="polymarket_rtds_twap_30s",
            symbol="BTC/USD",
            price=100_000,
            exchange_timestamp=history_at,
            received_at=history_at,
        )
    )
    for source in ("binance", "coinbase"):
        engine.add_signal(signal(source, "trade", history_at, price=100_200))
        engine.add_signal(signal(source, "book", history_at, price=100_200))

    mid_at = start + timedelta(seconds=30)
    engine.add_chainlink_tick(
        PriceTick(
            source="polymarket_rtds_twap_30s",
            symbol="BTC/USD",
            price=100_000,
            exchange_timestamp=mid_at,
            received_at=mid_at,
        )
    )
    for source in ("binance", "coinbase"):
        engine.add_signal(signal(source, "trade", mid_at, price=100_200))
        engine.add_signal(signal(source, "book", mid_at, price=100_200))

    engine.add_chainlink_tick(
        PriceTick(
            source="polymarket_rtds_twap_30s",
            symbol="BTC/USD",
            price=100_000,
            exchange_timestamp=now,
            received_at=now,
        )
    )
    for source in ("binance", "coinbase"):
        engine.add_signal(signal(source, "trade", now, price=100_200))
        engine.add_signal(signal(source, "book", now, price=100_200))
    books = poly_books(current, now, up_bid=0.45, up_ask=0.46, down_bid=0.53, down_ask=0.54)
    for direction, book in books.items():
        engine.add_polymarket_book(direction, book)

    expected_raw_gap = math.log(100_200 / 100_000)
    expected_gap = min(expected_raw_gap - baseline_basis, 0.0002)
    seed_twap_direction_average(
        engine,
        ("binance", "coinbase"),
        now,
        0.35 * expected_gap,
    )
    engine.evaluate(current, books, now, force=True)

    binance_components = engine.diagnostics["twap_direction_components"]["binance"]
    assert engine.diagnostics["spot_returns_1s"]["binance"] == pytest.approx(0.0)
    assert binance_components["spot_momentum_10s"] == pytest.approx(0.0)
    assert binance_components["spot_momentum_30s"] == pytest.approx(0.0)
    assert binance_components["raw_spot_twap_gap"] == pytest.approx(expected_raw_gap)
    assert binance_components["spot_twap_basis_baseline"] == pytest.approx(
        baseline_basis
    )
    assert binance_components["spot_twap_gap"] == pytest.approx(expected_gap)
    assert binance_components["spot_twap_calibration_ready"] is True
    assert binance_components["twap_drift_10s"] == pytest.approx(0.0)
    assert binance_components["twap_drift_30s"] == pytest.approx(0.0)
    assert binance_components["direction_signal_return"] == pytest.approx(
        0.35 * expected_gap
    )
    assert binance_components["direction_signal_average_ready"] is True
    assert engine.diagnostics["spot_twap_calibration_ready_sources"] == [
        "binance",
        "coinbase",
    ]
    assert engine.diagnostics["spot_twap_basis_clipped_sources"] == [
        "binance",
        "coinbase",
    ]
    assert engine.diagnostics["twap_direction_positive_sources"] == 2
    assert engine.diagnostics["spot_positive_return_sources_1s"] == 0
    chase = engine.diagnostics["orderbook_chase"]
    assert chase["direction"] == "UP"
    assert chase["supporting_sources"] == 2
    assert chase["supporting_lead_sources"] == ["binance", "coinbase"]
    assert chase["signal_mode"] == "twap_direction"
    assert chase["lead_signal_formula"] == (
        "time_weighted_average_30s(spot_momentum_10s "
        "+ 0.5*spot_momentum_30s + 0.35*calibrated_spot_twap_gap "
        "- twap_drift_10s - 0.5*twap_drift_30s)"
    )
    assert chase["confirmation_key"] == (
        "twap_direction_average_30s:UP:binance,coinbase"
    )
    registry.close()


def test_orderbook_chase_stale_chainlink_cancels_buy_confirmation(tmp_path) -> None:
    start = datetime(2026, 8, 3, tzinfo=timezone.utc)
    current = market(start, "chase-stale-chainlink")
    engine, _ = make_engine(
        tmp_path,
        orderbook_chase_mode=True,
        slippage_reserve_cents=0,
        max_entries_per_market=1,
    )
    engine.set_market(current, start)

    history_at = start + timedelta(seconds=170)
    engine.add_chainlink_tick(
        PriceTick(
            source="polymarket_rtds_twap_30s",
            symbol="BTC/USD",
            price=100_000,
            exchange_timestamp=history_at,
            received_at=history_at,
        )
    )
    for source in ("binance", "coinbase"):
        engine.add_signal(signal(source, "trade", history_at, price=100_000))
        engine.add_signal(signal(source, "book", history_at, price=100_000))

    mid_at = start + timedelta(seconds=190)
    engine.add_chainlink_tick(
        PriceTick(
            source="polymarket_rtds_twap_30s",
            symbol="BTC/USD",
            price=100_010,
            exchange_timestamp=mid_at,
            received_at=mid_at,
        )
    )
    for source in ("binance", "coinbase"):
        engine.add_signal(signal(source, "trade", mid_at, price=100_150))
        engine.add_signal(signal(source, "book", mid_at, price=100_150))

    trend_at = start + timedelta(seconds=198)
    trend_books = poly_books(
        current, trend_at, up_bid=0.45, up_ask=0.46, down_bid=0.53, down_ask=0.54
    )
    engine.add_chainlink_tick(
        PriceTick(
            source="polymarket_rtds_twap_30s",
            symbol="BTC/USD",
            price=100_020,
            exchange_timestamp=trend_at,
            received_at=trend_at,
        )
    )
    for source in ("binance", "coinbase"):
        engine.add_signal(signal(source, "trade", trend_at, price=100_300))
        engine.add_signal(signal(source, "book", trend_at, price=100_300))
    for direction, book in trend_books.items():
        engine.add_polymarket_book(direction, book)

    first = start + timedelta(seconds=200)
    books = poly_books(current, first, up_bid=0.46, up_ask=0.47, down_bid=0.52, down_ask=0.53)
    engine.add_chainlink_tick(
        PriceTick(
            source="polymarket_rtds_twap_30s",
            symbol="BTC/USD",
            price=100_080,
            exchange_timestamp=first,
            received_at=first,
        )
    )
    for source in ("binance", "coinbase"):
        engine.add_signal(signal(source, "trade", first, price=100_700))
        engine.add_signal(signal(source, "book", first, price=100_700))
    for direction, book in books.items():
        engine.add_polymarket_book(direction, book)
    seed_twap_direction_average(
        engine,
        ("binance", "coinbase"),
        first,
        0.003,
    )
    engine.evaluate(current, books, first, force=True)

    assert engine.status == "confirming_buy"
    assert engine.confirmations["buy"]["candidate"] == (
        engine.diagnostics["orderbook_chase"]["confirmation_key"]
    )

    stale_at = start + timedelta(seconds=205, milliseconds=200)
    books = poly_books(
        current, stale_at, up_bid=0.46, up_ask=0.47, down_bid=0.52, down_ask=0.53
    )
    for source in ("binance", "coinbase"):
        engine.add_signal(signal(source, "trade", stale_at, price=100_700))
        engine.add_signal(signal(source, "book", stale_at, price=100_700))
    for direction, book in books.items():
        engine.add_polymarket_book(direction, book)
    engine.evaluate(current, books, stale_at, force=True)

    assert engine.position is None
    assert engine.last_reason == "chase_chainlink_stale"
    assert engine.diagnostics["chainlink_age_seconds"] == pytest.approx(5.2)
    assert "buy" not in engine.confirmations


def test_orderbook_chase_rejects_falling_polymarket_direction(tmp_path) -> None:
    start = datetime(2026, 8, 3, tzinfo=timezone.utc)
    current = market(start, "chase-falling-book")
    engine, _ = make_engine(
        tmp_path,
        orderbook_chase_mode=True,
        slippage_reserve_cents=0,
        max_entries_per_market=1,
    )
    engine.set_market(current, start)

    for offset, twap, spot, up_bid, up_ask in (
        (170.0, 100_000, 100_000, 0.48, 0.49),
        (190.0, 100_010, 100_150, 0.48, 0.49),
        (198.0, 100_020, 100_300, 0.48, 0.49),
        (200.0, 100_080, 100_700, 0.46, 0.47),
    ):
        now = start + timedelta(seconds=offset)
        engine.add_chainlink_tick(
            PriceTick(
                source="polymarket_rtds_twap_30s",
                symbol="BTC/USD",
                price=twap,
                exchange_timestamp=now,
                received_at=now,
            )
        )
        for source in ("binance", "coinbase"):
            engine.add_signal(signal(source, "trade", now, price=spot))
            engine.add_signal(signal(source, "book", now, price=spot))
        books = poly_books(
            current,
            now,
            up_bid=up_bid,
            up_ask=up_ask,
            down_bid=1.0 - up_ask,
            down_ask=1.0 - up_bid,
        )
        for direction, book in books.items():
            engine.add_polymarket_book(direction, book)

    seed_twap_direction_average(
        engine,
        ("binance", "coinbase"),
        now,
        0.003,
    )
    engine.evaluate(current, books, now, force=True)

    assert engine.diagnostics["orderbook_chase"]["eligible"] is True
    assert engine.candidates["UP"]["eligible"] is False
    assert engine.candidates["UP"]["reason"] == "chase_polymarket_falling"
    assert engine.position is None
    assert "buy" not in engine.confirmations


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

    too_early = v8_orderbook_chase_exit_decision(
        position, 0.58, 0.06, diagnostics, opened + timedelta(seconds=1)
    )
    assert too_early["reason"] == "chase_holding"
    assert too_early["min_hold_satisfied"] is False

    caught_up = v8_orderbook_chase_exit_decision(
        position, 0.58, 0.06, diagnostics, opened + timedelta(seconds=30)
    )
    assert caught_up["reason"] == "chase_caught_up"
    assert caught_up["min_hold_satisfied"] is True

    caught_up_at_timeout = v8_orderbook_chase_exit_decision(
        position, 0.58, 0.06, diagnostics, opened + timedelta(seconds=90)
    )
    assert caught_up_at_timeout["reason"] == "chase_caught_up"

    protected_early_loss = v8_orderbook_chase_exit_decision(
        position, 0.40, -1.51, diagnostics, opened + timedelta(seconds=3)
    )
    assert protected_early_loss["reason"] == "chase_holding"
    assert protected_early_loss["hard_stop"] is False

    emergency_stop = v8_orderbook_chase_exit_decision(
        position, 0.30, -2.01, diagnostics, opened + timedelta(seconds=3)
    )
    assert emergency_stop["reason"] == "chase_emergency_stop"
    assert emergency_stop["emergency_stop"] is True

    hard_stop = v8_orderbook_chase_exit_decision(
        position, 0.40, -1.51, diagnostics, opened + timedelta(seconds=30)
    )
    assert hard_stop["reason"] == "chase_hard_stop"
    assert hard_stop["hard_stop"] is True

    timed_out = v8_orderbook_chase_exit_decision(
        position, 0.50, -0.20, diagnostics, opened + timedelta(seconds=90)
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


def test_orderbook_chase_reversal_accepts_one_supporting_source() -> None:
    opened = datetime(2026, 8, 3, tzinfo=timezone.utc)
    position = auto_position(
        strategy_mode="orderbook_chase",
        entry_target_probability=0.80,
        entry_signal_strength=1.0,
        opened_at=opened,
    )
    diagnostics = {
        "orderbook_chase": {
            "eligible": True,
            "direction": "DOWN",
            "signal_strength": 1.0,
            "supporting_sources": 0,
        }
    }

    no_support = v8_orderbook_chase_exit_decision(
        position,
        0.50,
        0.0,
        diagnostics,
        opened + timedelta(seconds=31),
    )
    assert no_support["signal_reversed"] is False
    assert no_support["reason"] == "chase_holding"

    diagnostics["orderbook_chase"]["supporting_sources"] = 1
    one_source = v8_orderbook_chase_exit_decision(
        position,
        0.50,
        0.0,
        diagnostics,
        opened + timedelta(seconds=31),
    )
    assert one_source["signal_reversed"] is True
    assert one_source["reason"] == "chase_signal_reversed"
    assert one_source["required_reversal_sources"] == 1


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
        opened + timedelta(seconds=30),
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
        opened + timedelta(seconds=30),
    )

    assert decision["eligible"] is False
    assert decision["take_profit_armed"] is True
    assert decision["profit_drawdown_usd"] == pytest.approx(0.10)
    assert decision["required_profit_drawdown_usd"] == pytest.approx(0.15)


def test_orderbook_chase_opens_from_spot_lead_and_sells_into_catchup(
    tmp_path, monkeypatch
) -> None:
    start = datetime(2026, 8, 3, tzinfo=timezone.utc)
    current = market(start, "chase-market")
    engine, registry = make_engine(
        tmp_path,
        orderbook_chase_mode=True,
        slippage_reserve_cents=0,
        max_entries_per_market=1,
    )
    engine.set_market(current, start)
    original_summary = registry.summary

    def fail_if_full_v8_feature_path_runs(*args, **kwargs):
        raise AssertionError("orderbook chase must use the lightweight feature path")

    monkeypatch.setattr(engine, "_futures_features", fail_if_full_v8_feature_path_runs)
    monkeypatch.setattr(engine, "_cvd", fail_if_full_v8_feature_path_runs)
    monkeypatch.setattr(engine, "_polymarket_ofi", fail_if_full_v8_feature_path_runs)

    def fail_if_trade_recomputes_full_summary():
        raise AssertionError("trade path must not recompute the full V8 history summary")

    monkeypatch.setattr(registry, "summary", fail_if_trade_recomputes_full_summary)

    seed_twap_direction_average(
        engine,
        ("binance", "coinbase"),
        start + timedelta(seconds=200),
        0.003,
    )

    history_at = start + timedelta(seconds=170)
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

    mid_at = start + timedelta(seconds=190)
    engine.add_chainlink_tick(
        PriceTick(
            source="polymarket_rtds",
            symbol="BTCUSD",
            price=100_010,
            exchange_timestamp=mid_at,
            received_at=mid_at,
        )
    )
    for source in ("binance", "coinbase"):
        engine.add_signal(signal(source, "trade", mid_at, price=100_150))
        engine.add_signal(signal(source, "book", mid_at, price=100_150))

    trend_at = start + timedelta(seconds=198)
    trend_books = poly_books(
        current, trend_at, up_bid=0.45, up_ask=0.46, down_bid=0.53, down_ask=0.54
    )
    engine.add_chainlink_tick(
        PriceTick(
            source="polymarket_rtds",
            symbol="BTCUSD",
            price=100_020,
            exchange_timestamp=trend_at,
            received_at=trend_at,
        )
    )
    for source in ("binance", "coinbase"):
        engine.add_signal(signal(source, "trade", trend_at, price=100_300))
        engine.add_signal(signal(source, "book", trend_at, price=100_300))
    for direction, book in trend_books.items():
        engine.add_polymarket_book(direction, book)

    for offset in (200.0, 202.2):
        now = start + timedelta(seconds=offset)
        books = poly_books(
            current, now, up_bid=0.46, up_ask=0.47, down_bid=0.52, down_ask=0.53
        )
        engine.add_chainlink_tick(
            PriceTick(
                source="polymarket_rtds",
                symbol="BTCUSD",
                price=100_080,
                exchange_timestamp=now,
                received_at=now,
            )
        )
        for source in ("binance", "coinbase"):
            engine.add_signal(signal(source, "trade", now, price=100_700))
            engine.add_signal(signal(source, "book", now, price=100_700))
        for direction, book in books.items():
            engine.add_polymarket_book(direction, book)
        engine.evaluate(current, books, now, force=True)

    assert engine.diagnostics["timestamp_basis"] == "received_at"
    assert engine.diagnostics["orderbook_chase"]["direction"] == "UP"
    assert engine.diagnostics["runtime_profile"] == "orderbook_chase_lightweight"
    assert engine.diagnostics["raw_event_archive_enabled"] is False
    assert engine.diagnostics["futures_features_enabled"] is False
    assert engine.diagnostics["model_metrics_enabled"] is False
    assert engine.diagnostics["residual_model_enabled"] is False
    assert engine.diagnostics["features"] == {}
    assert "model" not in engine.diagnostics
    assert "feature_contributions" not in engine.diagnostics
    assert engine.diagnostics["model_probability_up"] == pytest.approx(
        engine.diagnostics["formula_probability_up"]
    )
    assert registry.snapshots(current.condition_id) == []
    engine.add_signal(signal("binance_futures", "trade", now, price=100_100))
    assert engine.trades["binance_futures"] == []
    registry.flush_raw_events(force=True)
    assert registry.connection.execute(
        "SELECT COUNT(*) AS value FROM btc_v8_raw_events"
    ).fetchone()["value"] == 0
    assert registry.connection.execute(
        "SELECT COUNT(*) AS value FROM btc_v8_models"
    ).fetchone()["value"] == 0
    assert engine.dashboard_state()["model"] is None
    assert engine.position is not None
    assert engine.position.strategy_mode == "orderbook_chase"
    position_id = engine.position.position_id

    for offset in (233.0, 233.6):
        now = start + timedelta(seconds=offset)
        books = poly_books(
            current, now, up_bid=0.90, up_ask=0.91, down_bid=0.08, down_ask=0.09
        )
        engine.add_chainlink_tick(
            PriceTick(
                source="polymarket_rtds",
                symbol="BTCUSD",
                price=100_250,
                exchange_timestamp=now,
                received_at=now,
            )
        )
        for source in ("binance", "coinbase"):
            engine.add_signal(signal(source, "trade", now, price=100_300))
            engine.add_signal(signal(source, "book", now, price=100_300))
        for direction, book in books.items():
            engine.add_polymarket_book(direction, book)
        engine.evaluate(current, books, now, force=True)

    assert engine.position is None
    closed = registry.get_position(position_id)
    assert closed is not None and closed.realized_pnl is not None
    assert closed.realized_pnl > 0
    assert closed.exit_reason == "chase_caught_up"
    assert engine.dashboard_state()["summary"]["positions"] == 1
    assert engine.dashboard_state()["summary"]["completed_positions"] == 1
    assert engine.dashboard_state()["summary"]["wins"] == 1
    trades = registry.recent_trades()
    assert trades[0]["strategy_mode"] == "orderbook_chase"
    assert trades[0]["reason"] == "chase_caught_up"
    assert trades[1]["reason"] == "chase_buy"
    model_version = registry.load_model("v8").version
    monkeypatch.setattr(registry, "summary", original_summary)
    engine.settle(current.slug, Direction.UP, start + timedelta(minutes=5))
    assert registry.load_model("v8").version == model_version
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


def test_direction_emergency_stop_uses_new_configured_limit() -> None:
    position = auto_position(strategy_mode="orderbook_chase")
    settings = AppConfig(
        btc_v8={"emergency_stop_loss_usd": 1.0, "hard_stop_loss_usd": 0.75}
    ).btc_v8

    decision = v8_orderbook_chase_exit_decision(
        position=position,
        net_value=0.2,
        pnl=-1.01,
        diagnostics={"orderbook_chase": {}},
        now=position.opened_at + timedelta(seconds=0.1),
        settings=settings,
    )

    assert decision["eligible"] is True
    assert decision["reason"] == "chase_emergency_stop"
    assert decision["emergency_stop_loss_usd"] == pytest.approx(1.0)


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


def test_restart_restores_direction_round_and_position(tmp_path) -> None:
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

    registry.close()

    config = engine.config
    restarted_registry = BtcV8Registry(tmp_path / "btc-v8.sqlite3")
    restarted = BtcV8Engine(config, restarted_registry)
    restarted.set_market(current, second + timedelta(seconds=1))

    assert restarted.current_round is not None
    assert restarted.current_round.entry_count == 1
    assert restarted.position is not None
    assert restarted.position.position_id == position_id
    assert restarted.current_round.settings.min_buy_price_cents == pytest.approx(15)
    assert restarted.current_round.settings.max_buy_price_cents == pytest.approx(90)
    restarted_registry.close()


def test_direction_mode_does_not_collect_futures_features(tmp_path) -> None:
    engine, registry = make_engine(tmp_path)
    now = datetime(2026, 8, 3, tzinfo=timezone.utc)
    engine.add_signal(signal("binance_futures", "trade", now, price=100.0, side="buy"))
    engine.add_signal(signal("binance_futures", "liquidation", now, price=500.0, side="sell"))

    assert engine._trade_prices("binance_futures") == []
    assert engine._cvd("binance_futures", now, 5) is None
    registry.close()


def test_direction_mode_uses_polymarket_price_trend_without_trade_archive(tmp_path) -> None:
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

    assert engine._polymarket_trade_flow(Direction.UP, now) == 0.0
    assert len(engine.polymarket_price_history[Direction.UP]) == 2
    registry.flush_raw_events(force=True)
    trade_count = registry.connection.execute(
        "SELECT COUNT(*) AS value FROM btc_v8_raw_events WHERE source='polymarket' AND kind='trade'"
    ).fetchone()["value"]
    assert trade_count == 0
    registry.close()


def test_direction_mode_does_not_archive_raw_books(tmp_path) -> None:
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
    assert rows == []
    assert registry.decode_raw_payload('{"legacy":true}') == {"legacy": True}
    registry.close()


def test_snapshot_cleanup_removes_expired_snapshots_even_from_current_round(tmp_path) -> None:
    now = datetime(2026, 8, 7, tzinfo=timezone.utc)
    start = now - timedelta(days=2)
    current = market(start, "still-current")
    engine, registry = make_engine(tmp_path)
    engine.set_market(current, start)
    for snapshot_second, created_at in (
        (10, start + timedelta(seconds=10)),
        (20, start + timedelta(seconds=20)),
        (30, now - timedelta(hours=1)),
    ):
        registry.save_snapshot(
            V8Snapshot(
                market_id=current.condition_id,
                snapshot_second=snapshot_second,
                formula_probability=0.5,
                model_probability=0.5,
                features={"remaining_time": 0.5},
                fresh_spot_exchanges=["binance", "coinbase"],
                created_at=created_at,
            )
        )

    assert engine.current_round is not None
    assert engine.current_round.market_id == current.condition_id
    assert registry.cleanup_expired_snapshots(24, now, batch_size=1) == 1
    assert registry.cleanup_expired_snapshots(24, now, batch_size=1) == 1
    assert registry.cleanup_expired_snapshots(24, now, batch_size=1) == 0
    assert [snapshot.snapshot_second for snapshot in registry.snapshots(current.condition_id)] == [30]
    registry.close()


def test_settlement_does_not_train_or_activate_a_residual_model(tmp_path) -> None:
    start = datetime(2026, 8, 3, tzinfo=timezone.utc)
    first_market = market(start, "m1")
    second_market = market(start + timedelta(minutes=5), "m2")
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
    assert registry.load_model("v8").version == 1
    assert engine.current_round.market_id == second_market.condition_id
    assert engine.model.version == 1

    engine.settle(first_market.slug, Direction.UP, settled_at + timedelta(seconds=1))
    assert registry.load_model("v8").version == 1
    registry.close()


def test_direction_engine_does_not_create_training_model(tmp_path) -> None:
    engine, registry = make_engine(tmp_path)
    start = datetime(2026, 8, 3, tzinfo=timezone.utc)
    engine.set_market(market(start, "direction-only"), start)

    count = registry.connection.execute(
        "SELECT COUNT(*) AS value FROM btc_v8_models"
    ).fetchone()["value"]
    assert count == 0
    assert engine.diagnostics.get("model") is None
    registry.close()
