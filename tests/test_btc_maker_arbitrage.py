from datetime import datetime, timedelta, timezone

import pytest

from polybtc.btc_maker_arbitrage import (
    BtcMakerArbitrageEngine,
    BtcMakerArbitrageRegistry,
    MakerGroup,
    MakerQuote,
    MakerTradePrint,
)
from polybtc.config import AppConfig
from polybtc.models import BookLevel, Direction, MarketState, OrderBookSnapshot


NOW = datetime(2026, 8, 13, 8, 0, tzinfo=timezone.utc)


def market(remaining: float = 180) -> MarketState:
    return MarketState(
        asset="BTC", condition_id="m1", slug="btc-updown-5m-test", question="BTC?",
        threshold_price=100_000, start_time=NOW - timedelta(seconds=120),
        end_time=NOW + timedelta(seconds=remaining), up_token_id="up", down_token_id="down",
    )


def book(direction: Direction, bid: float, ask: float, *, bid_size: float = 10, ask_size: float = 10) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        token_id="up" if direction == Direction.UP else "down", market_id="m1",
        timestamp=NOW, received_at=NOW, depth_trusted=True, tick_size=0.01,
        bids=[BookLevel(price=bid, size=bid_size)], asks=[BookLevel(price=ask, size=ask_size)],
    )


@pytest.fixture
def subject(tmp_path):
    config = AppConfig(btc_maker_arbitrage={"enabled": True})
    registry = BtcMakerArbitrageRegistry(tmp_path / "maker.sqlite3")
    engine = BtcMakerArbitrageEngine(config, registry)
    engine.set_market(market())
    yield engine
    registry.close()


def ready(subject, up=(0.48, 0.51), down=(0.48, 0.51)):
    subject.add_book(Direction.UP, book(Direction.UP, *up), NOW)
    subject.add_book(Direction.DOWN, book(Direction.DOWN, *down), NOW)


def print_at(direction: Direction, price: float, size: float, side: str, timestamp=NOW) -> MakerTradePrint:
    return MakerTradePrint(
        token_id="up" if direction == Direction.UP else "down", market_id="m1",
        price=price, size=size, side=side, timestamp=timestamp, received_at=timestamp,
    )


def test_dynamic_quotes_are_post_only_and_keep_two_cent_profit(subject) -> None:
    ready(subject)
    subject.evaluate(NOW)
    quotes = subject._open_quotes()
    assert len(quotes) == 2
    by_direction = {quote.direction: quote for quote in quotes}
    assert by_direction[Direction.UP].price < 0.51
    assert by_direction[Direction.DOWN].price < 0.51
    assert sum(quote.price for quote in quotes) <= 0.98 + 1e-9
    assert subject.dashboard_state(NOW)["opportunity"]["expected_profit_cents"] >= 2


def test_budget_sizing_uses_equal_leg_quantity_and_total_pair_budget(tmp_path) -> None:
    config = AppConfig(btc_maker_arbitrage={
        "enabled": True, "sizing_mode": "budget", "pair_budget_usd": 5.0,
    })
    registry = BtcMakerArbitrageRegistry(tmp_path / "maker.sqlite3")
    engine = BtcMakerArbitrageEngine(config, registry)
    engine.set_market(market())
    ready(engine, up=(0.38, 0.41), down=(0.56, 0.59))

    engine.evaluate(NOW)

    quotes = engine._open_quotes()
    assert len(quotes) == 2
    assert quotes[0].quantity == pytest.approx(quotes[1].quantity)
    assert sum(quote.price * quote.quantity for quote in quotes) == pytest.approx(5.0)
    assert engine._active_group().target_quantity == pytest.approx(quotes[0].quantity)
    registry.close()


def test_book_size_reduction_consumes_queue_without_premature_fill(subject) -> None:
    ready(subject, up=(0.48, 0.49), down=(0.48, 0.51))
    subject.evaluate(NOW)
    quote = next(item for item in subject._open_quotes() if item.direction == Direction.UP)
    subject.add_book(Direction.UP, book(Direction.UP, 0.48, 0.51, bid_size=1), NOW + timedelta(milliseconds=10))
    assert quote.queue_ahead == 1
    assert quote.filled_quantity == 0


def test_book_level_swept_below_quote_fills_shadow_buy(subject) -> None:
    ready(subject, up=(0.48, 0.49), down=(0.48, 0.51))
    subject.evaluate(NOW)
    quote = next(item for item in subject._open_quotes() if item.direction == Direction.UP)
    update = book(Direction.UP, 0.47, 0.49, bid_size=4)
    update.timestamp = update.received_at = NOW + timedelta(milliseconds=10)

    subject.add_book(Direction.UP, update, update.received_at)

    assert quote.status == "FILLED"
    assert quote.filled_quantity == quote.quantity
    assert quote.reason == "shadow_depth_swept"


def test_ask_crossing_resting_shadow_buy_fills_from_depth(subject) -> None:
    ready(subject)
    subject.evaluate(NOW)
    quote = next(item for item in subject._open_quotes() if item.direction == Direction.UP)
    update = book(Direction.UP, 0.47, quote.price, bid_size=4)
    update.timestamp = update.received_at = NOW + timedelta(milliseconds=10)

    subject.add_book(Direction.UP, update, update.received_at)

    assert quote.status == "FILLED"
    assert quote.reason == "shadow_depth_swept"


def test_rest_depth_change_never_infers_shadow_fill(subject) -> None:
    ready(subject)
    subject.evaluate(NOW)
    quote = next(item for item in subject._open_quotes() if item.direction == Direction.UP)
    update = book(Direction.UP, 0.47, 0.50, bid_size=4)
    update.timestamp = update.received_at = NOW + timedelta(milliseconds=10)
    update.raw = {"_transport": "rest"}

    subject.add_book(Direction.UP, update, update.received_at)

    assert quote.filled_quantity == 0
    assert quote.status == "OPEN"


def test_rest_reconciliation_does_not_interrupt_healthy_websocket_source(subject) -> None:
    ready(subject)
    rest = book(Direction.UP, 0.48, 0.51)
    rest.raw = {"_transport": "rest"}
    reconciled_at = NOW + timedelta(milliseconds=100)
    rest.received_at = reconciled_at

    subject.add_book(Direction.UP, rest, reconciled_at)

    assert subject.book_sources[Direction.UP] == "websocket"
    assert subject.unmeasurable_until is None


def test_periodic_rest_fallback_does_not_interrupt_websocket_source(subject) -> None:
    ready(subject)
    rest = book(Direction.UP, 0.48, 0.51)
    rest.raw = {"_transport": "rest"}
    fallback_at = NOW + timedelta(seconds=2)
    rest.received_at = fallback_at

    subject.add_book(Direction.UP, rest, fallback_at)

    assert subject.book_sources[Direction.UP] == "websocket"
    assert subject.unmeasurable_until is None


def test_explicit_stream_interruption_still_pauses_measurement(subject) -> None:
    subject.mark_unmeasurable("clob_stream_interrupted", NOW)

    assert subject.last_reason == "clob_stream_interrupted"
    assert subject.unmeasurable_until == NOW + timedelta(seconds=1)


def test_market_change_resets_trade_stream_watermark(subject) -> None:
    ready(subject)
    subject.on_trade_print(
        Direction.UP,
        print_at(Direction.UP, 0.50, 1, "BUY", NOW + timedelta(milliseconds=10)),
    )
    assert subject.last_trade_print_at

    next_market = market()
    next_market.condition_id = "m2"
    next_market.slug = "btc-updown-5m-next"
    next_market.up_token_id = "up-next"
    next_market.down_token_id = "down-next"
    subject.set_market(next_market)

    assert subject.last_trade_print_at == {}
    assert subject.book_sources == {}
    assert subject.unmeasurable_until is None


def test_same_price_trade_consumes_queue_before_fill(subject) -> None:
    ready(subject, up=(0.48, 0.49), down=(0.48, 0.51))
    subject.evaluate(NOW)
    quote = next(item for item in subject._open_quotes() if item.direction == Direction.UP)
    assert quote.queue_ahead == 10
    subject.on_trade_print(Direction.UP, print_at(Direction.UP, quote.price, 8, "SELL", NOW + timedelta(milliseconds=10)))
    assert quote.queue_ahead == 2
    assert quote.filled_quantity == 0
    subject.on_trade_print(Direction.UP, print_at(Direction.UP, quote.price, 7, "SELL", NOW + timedelta(milliseconds=20)))
    assert quote.queue_ahead == 0
    assert quote.filled_quantity == 5


def test_trade_through_price_fills_without_using_book_shrink(subject) -> None:
    ready(subject)
    subject.evaluate(NOW)
    quote = next(item for item in subject._open_quotes() if item.direction == Direction.UP)
    subject.on_trade_print(Direction.UP, print_at(Direction.UP, quote.price - 0.01, 5, "SELL", NOW + timedelta(milliseconds=10)))
    assert quote.status == "FILLED"


def test_live_numeric_string_trade_timestamp_reaches_shadow_queue(subject) -> None:
    ready(subject)
    subject.evaluate(NOW)
    quote = next(item for item in subject._open_quotes() if item.direction == Direction.UP)
    received_at = NOW + timedelta(milliseconds=25)
    trade_book = subject.books[Direction.UP].model_copy(deep=True)
    trade_book.received_at = received_at
    trade_book.raw = {
        "_last_trade": {
            "asset_id": "up",
            "market": "m1",
            "timestamp": str(int(received_at.timestamp() * 1000)),
            "price": str(quote.price - 0.01),
            "size": "0.1",
            "side": "SELL",
            "transaction_hash": "0x-live",
        }
    }

    subject.add_book(Direction.UP, trade_book, received_at)

    assert subject.last_trade_print_at[Direction.UP] == received_at
    assert quote.status == "FILLED"
    assert quote.filled_quantity == quote.quantity


def test_single_leg_fill_cancels_excess_and_quotes_profitable_hedge(subject) -> None:
    ready(subject)
    subject.evaluate(NOW)
    up_quote = next(item for item in subject._open_quotes() if item.direction == Direction.UP)
    subject.on_trade_print(Direction.UP, print_at(Direction.UP, up_quote.price - 0.01, 5, "SELL", NOW + timedelta(milliseconds=10)))
    subject.evaluate(NOW + timedelta(milliseconds=20))
    group = subject._active_group()
    assert group is not None and group.unhedged_direction == Direction.UP
    hedge = next(item for item in subject._open_quotes() if item.purpose == "HEDGE")
    assert hedge.direction == Direction.DOWN
    assert up_quote.price + hedge.price <= 0.98 + 1e-9


def test_both_legs_fill_locks_pair_profit(subject) -> None:
    ready(subject)
    subject.evaluate(NOW)
    quotes = list(subject._open_quotes())
    for index, quote in enumerate(quotes, start=1):
        subject.on_trade_print(
            quote.direction,
            print_at(quote.direction, quote.price - 0.01, 5, "SELL", NOW + timedelta(milliseconds=index * 10)),
        )
    group = subject.groups[quotes[0].group_id]
    assert group.status == "LOCKED"
    assert group.locked_quantity == 5
    assert group.realized_pnl_usd >= 0.10 - 1e-8


def test_quote_ttl_and_reprice_cooldown(subject) -> None:
    ready(subject)
    subject.evaluate(NOW)
    subject.evaluate(NOW + timedelta(seconds=2))
    assert not subject._open_quotes()
    ready(subject)
    subject.books[Direction.UP].received_at = NOW + timedelta(seconds=2, milliseconds=499)
    subject.books[Direction.DOWN].received_at = NOW + timedelta(seconds=2, milliseconds=499)
    subject.evaluate(NOW + timedelta(seconds=2, milliseconds=499))
    assert subject.last_reason == "reprice_cooldown"
    subject.evaluate(NOW + timedelta(seconds=2, milliseconds=500))
    assert len(subject._open_quotes()) == 2


def test_open_pair_is_not_closed_or_duplicated_before_ttl(subject) -> None:
    ready(subject)
    subject.evaluate(NOW)
    group_id = subject._active_group().group_id

    subject.evaluate(NOW + timedelta(seconds=1))

    assert subject._active_group().group_id == group_id
    assert len(subject._open_quotes(group_id)) == 2
    assert len(subject.groups) == 1


def test_equal_partial_fills_requote_only_the_remaining_quantity(subject) -> None:
    ready(subject)
    subject.evaluate(NOW)
    up_quote = next(item for item in subject._open_quotes() if item.direction == Direction.UP)
    down_quote = next(item for item in subject._open_quotes() if item.direction == Direction.DOWN)
    subject.on_trade_print(
        Direction.UP,
        print_at(Direction.UP, up_quote.price, up_quote.queue_ahead + 2, "SELL", NOW + timedelta(milliseconds=10)),
    )
    subject.on_trade_print(
        Direction.DOWN,
        print_at(Direction.DOWN, down_quote.price, down_quote.queue_ahead + 2, "SELL", NOW + timedelta(milliseconds=20)),
    )

    group = subject.groups[up_quote.group_id]
    assert group.status == "PARTIAL"
    assert group.locked_quantity == 2
    assert not subject._open_quotes(group.group_id)

    refresh_at = NOW + timedelta(milliseconds=600)
    up_book = book(Direction.UP, 0.48, 0.51)
    down_book = book(Direction.DOWN, 0.48, 0.51)
    up_book.timestamp = up_book.received_at = refresh_at
    down_book.timestamp = down_book.received_at = refresh_at
    subject.add_book(Direction.UP, up_book, refresh_at)
    subject.add_book(Direction.DOWN, down_book, refresh_at)
    subject.evaluate(refresh_at)
    remaining = subject._open_quotes(group.group_id)
    assert len(remaining) == 2
    assert {quote.quantity for quote in remaining} == {3}


def test_trade_strictly_through_fills_full_quote_even_with_small_print(subject) -> None:
    ready(subject)
    subject.evaluate(NOW)
    quote = next(item for item in subject._open_quotes() if item.direction == Direction.UP)

    subject.on_trade_print(
        Direction.UP,
        print_at(Direction.UP, quote.price - 0.01, 0.1, "SELL", NOW + timedelta(milliseconds=10)),
    )

    assert quote.status == "FILLED"
    assert quote.filled_quantity == quote.quantity


def test_unhedged_timeout_places_maker_exit(subject) -> None:
    ready(subject)
    subject.evaluate(NOW)
    quote = next(item for item in subject._open_quotes() if item.direction == Direction.UP)
    subject.on_trade_print(Direction.UP, print_at(Direction.UP, quote.price - 0.01, 5, "SELL", NOW + timedelta(milliseconds=10)))
    subject.evaluate(NOW + timedelta(seconds=5, milliseconds=20))
    exits = [item for item in subject._open_quotes() if item.purpose == "EXIT"]
    assert len(exits) == 1
    assert exits[0].side == "SELL"


def test_unhedged_exit_stays_open_until_its_ttl(subject) -> None:
    ready(subject)
    subject.evaluate(NOW)
    quote = next(item for item in subject._open_quotes() if item.direction == Direction.UP)
    subject.on_trade_print(Direction.UP, print_at(Direction.UP, quote.price - 0.01, 5, "SELL", NOW + timedelta(milliseconds=10)))

    exit_at = NOW + timedelta(seconds=5, milliseconds=20)
    subject.evaluate(exit_at)
    first_exit = next(item for item in subject._open_quotes() if item.purpose == "EXIT")

    subject.evaluate(exit_at + timedelta(seconds=1))

    exits = [item for item in subject._open_quotes() if item.purpose == "EXIT"]
    assert exits == [first_exit]
    assert first_exit.status == "OPEN"


def test_single_leg_loss_limit_uses_immediate_taker_exit(subject) -> None:
    ready(subject)
    subject.evaluate(NOW)
    quote = next(item for item in subject._open_quotes() if item.direction == Direction.UP)
    subject.on_trade_print(Direction.UP, print_at(Direction.UP, quote.price - 0.01, 5, "SELL", NOW + timedelta(milliseconds=10)))
    loss_book = book(Direction.UP, 0.20, 0.21, bid_size=10)
    loss_book.timestamp = loss_book.received_at = NOW + timedelta(milliseconds=20)
    subject.add_book(Direction.UP, loss_book, loss_book.received_at)

    subject.evaluate(NOW + timedelta(milliseconds=20))

    group = subject.groups[quote.group_id]
    assert group.status == "CLOSED"
    assert not subject._open_quotes(group.group_id)
    fills = subject.registry.recent_fills()
    assert any(fill.purpose == "STOP_LOSS_EXIT" for fill in fills)
    assert subject.last_reason == "single_leg_stop_loss_taker"


def test_emergency_exit_uses_taker_depth_and_fee(subject) -> None:
    ready(subject)
    subject.evaluate(NOW)
    quote = next(item for item in subject._open_quotes() if item.direction == Direction.UP)
    subject.on_trade_print(Direction.UP, print_at(Direction.UP, quote.price - 0.01, 5, "SELL", NOW + timedelta(milliseconds=10)))
    subject.market.end_time = NOW + timedelta(seconds=25)
    subject.evaluate(NOW + timedelta(milliseconds=20))
    group = subject.groups[quote.group_id]
    assert group.status == "CLOSED"
    assert any(fill.purpose == "EMERGENCY_EXIT" for fill in subject.registry.recent_fills())
    assert group.fees_usd > 0


def test_stop_window_and_market_limit(subject) -> None:
    subject.market.end_time = NOW + timedelta(seconds=60)
    ready(subject)
    subject.evaluate(NOW)
    assert subject.last_reason == "stop_new_quotes_window"
    assert not subject._open_quotes()


def test_dashboard_does_not_count_unfilled_expired_group_as_completed_or_single_leg(subject) -> None:
    ready(subject)
    subject.evaluate(NOW)
    subject.evaluate(NOW + timedelta(seconds=2))

    summary = subject.dashboard_state(NOW + timedelta(seconds=2))["summary"]
    group = next(iter(subject.groups.values()))

    assert group.status == "UNFILLED"
    assert group.completion_reason == "no_shadow_fill"
    assert summary["completed_groups"] == 0
    assert summary["unfilled_groups"] == 1
    assert summary["single_leg_groups"] == 0
    assert summary["single_leg_rate"] == 0.0


def test_registry_relabels_legacy_zero_fill_closed_group(tmp_path) -> None:
    path = tmp_path / "maker.sqlite3"
    registry = BtcMakerArbitrageRegistry(path)
    legacy = MakerGroup(market_id="m1", slug="s", target_quantity=5, status="CLOSED")
    registry.save_group(legacy)
    registry.close()

    reopened = BtcMakerArbitrageRegistry(path)
    repaired = reopened.recent_groups()[0]

    assert repaired.status == "UNFILLED"
    assert repaired.completion_reason == "no_shadow_fill"
    reopened.close()


def test_restart_cancels_open_quotes_but_restores_group(tmp_path) -> None:
    path = tmp_path / "maker.sqlite3"
    registry = BtcMakerArbitrageRegistry(path)
    group = MakerGroup(market_id="m1", slug="s", target_quantity=5, up_bought_quantity=2, up_buy_cost_usd=0.96)
    registry.save_group(group)
    quote = MakerQuote(group_id=group.group_id, market_id="m1", slug="s", direction=Direction.DOWN,
                       side="BUY", purpose="HEDGE", price=0.48, quantity=2)
    registry.save_quote(quote)
    registry.close()
    reopened = BtcMakerArbitrageRegistry(path)
    assert reopened.recent_quotes()[0].status == "CANCELLED"
    restored = BtcMakerArbitrageEngine(AppConfig(), reopened)
    assert group.group_id in restored.groups
    reopened.close()


def test_mode_cannot_be_changed_from_shadow_only() -> None:
    with pytest.raises(ValueError):
        AppConfig(btc_maker_arbitrage={"mode": "REAL"})
