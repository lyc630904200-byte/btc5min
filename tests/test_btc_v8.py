from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from polybtc.btc_v8 import (
    BtcV8Engine,
    V8Model,
    BtcV8Registry,
    V8Snapshot,
    normalized_remaining_time,
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
