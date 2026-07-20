import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from polybtc.clients import PolymarketClient
from polybtc.config import AppConfig, PairMatchConfig, SourceConfig
from polybtc.engine import PaperEngine
from polybtc.models import BookLevel, Direction, MarketState, OrderBookSnapshot
from polybtc.pair_match import (
    PairDirection,
    PairMatchEngine,
    PairMatchRegistry,
    simulate_equal_quantity_buys,
    spread_cents,
)


def market(asset: str, start: datetime) -> MarketState:
    return MarketState(
        asset=asset,
        condition_id=f"{asset}-market",
        slug=f"{asset.lower()}-updown-5m-{int(start.timestamp())}",
        question=f"{asset} Up or Down",
        threshold_price=None,
        threshold_verified=False,
        start_time=start,
        end_time=start + timedelta(minutes=5),
        up_token_id=f"{asset}-up",
        down_token_id=f"{asset}-down",
    )


def book(token_id: str, market_id: str, ask: float, now: datetime, size: float = 100) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        token_id=token_id,
        market_id=market_id,
        timestamp=now,
        received_at=now,
        bids=[BookLevel(price=max(0.01, ask - 0.01), size=size)],
        asks=[BookLevel(price=ask, size=size)],
        depth_trusted=True,
        min_order_size=5,
    )


def engines(config: AppConfig, start: datetime, now: datetime) -> dict[str, PaperEngine]:
    result = {asset: PaperEngine(config, asset=asset) for asset in ("BTC", "ETH")}
    for asset, engine in result.items():
        current = market(asset, start)
        engine.set_market(current)
    result["BTC"].books = {
        Direction.UP: book("BTC-up", "BTC-market", 0.40, now),
        Direction.DOWN: book("BTC-down", "BTC-market", 0.55, now),
    }
    result["ETH"].books = {
        Direction.UP: book("ETH-up", "ETH-market", 0.35, now),
        Direction.DOWN: book("ETH-down", "ETH-market", 0.50, now),
    }
    return result


def test_pair_spread_uses_equal_shares_multilevel_execution_and_per_share_fees() -> None:
    now = datetime(2026, 7, 20, 9, 0, 30, tzinfo=timezone.utc)
    btc_book = OrderBookSnapshot(
        token_id="btc", timestamp=now, received_at=now, depth_trusted=True,
        asks=[BookLevel(price=0.40, size=10), BookLevel(price=0.50, size=20)],
    )
    eth_book = OrderBookSnapshot(
        token_id="eth", timestamp=now, received_at=now, depth_trusted=True,
        asks=[BookLevel(price=0.30, size=10), BookLevel(price=0.35, size=30)],
    )

    btc_fill, eth_fill = simulate_equal_quantity_buys(
        btc_book, eth_book, total_quote_usd=20, fee_rate=0.07
    )
    value = spread_cents(btc_fill, eth_fill)

    expected = 100 * (
        1
        - btc_fill.avg_price
        - eth_fill.avg_price
        - btc_fill.fee_usd / btc_fill.quantity
        - eth_fill.fee_usd / eth_fill.quantity
    )
    assert value == expected
    assert btc_fill.complete is True
    assert eth_fill.complete is True
    assert btc_fill.quantity == pytest.approx(eth_fill.quantity)
    assert btc_fill.quote + eth_fill.quote == pytest.approx(20)
    assert btc_fill.quote != pytest.approx(eth_fill.quote)
    assert btc_fill.levels_used == 2
    assert eth_fill.levels_used == 2


def test_equal_share_execution_rejects_insufficient_combined_depth() -> None:
    now = datetime(2026, 7, 20, 9, 0, 30, tzinfo=timezone.utc)
    btc_book = book("btc", "btc-market", 0.40, now, size=5)
    eth_book = book("eth", "eth-market", 0.50, now, size=5)

    btc_fill, eth_fill = simulate_equal_quantity_buys(
        btc_book, eth_book, total_quote_usd=20, fee_rate=0.07
    )

    assert btc_fill.complete is False
    assert eth_fill.complete is False
    assert btc_fill.quantity == pytest.approx(eth_fill.quantity)
    assert btc_fill.quote + eth_fill.quote < 20


def test_pair_engine_opens_once_per_quote_and_strictly_alternates(tmp_path) -> None:
    start = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
    now = start + timedelta(seconds=30)
    config = AppConfig(
        pair_match=PairMatchConfig(
            enabled=True,
            leg_quote_usd=10,
            min_spread_cents=-100,
            max_pairs_per_market=2,
            alternate_directions=True,
        )
    )
    registry = PairMatchRegistry(tmp_path / "pairs.sqlite3")
    matcher = PairMatchEngine(config, registry)
    states = engines(config, start, now)

    first = matcher.evaluate(states, now)
    assert first is not None
    assert first.btc_leg.quantity == pytest.approx(first.eth_leg.quantity)
    assert first.btc_leg.quote + first.eth_leg.quote == pytest.approx(20)
    assert first.scenario_pnl["btc_only_wins"] == pytest.approx(first.scenario_pnl["eth_only_wins"])
    assert matcher.evaluate(states, now) is None

    states["ETH"].books[Direction.UP].asks[0].size += 1
    second = matcher.evaluate(states, now + timedelta(milliseconds=100))

    assert second is not None
    assert second.direction == first.direction.opposite
    assert registry.count(first.interval_key) == 2
    states["BTC"].books[Direction.UP].asks[0].size += 1
    assert matcher.evaluate(states, now + timedelta(milliseconds=200)) is None
    assert matcher.status == "market_pair_limit"
    registry.close()


@pytest.mark.parametrize(
    ("btc_outcome", "eth_outcome"),
    [
        (Direction.UP, Direction.UP),
        (Direction.UP, Direction.DOWN),
        (Direction.DOWN, Direction.UP),
        (Direction.DOWN, Direction.DOWN),
    ],
)
def test_pair_entry_does_not_require_verified_threshold_and_persists_settlement(
    tmp_path, btc_outcome: Direction, eth_outcome: Direction
) -> None:
    start = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
    now = start + timedelta(seconds=30)
    config = AppConfig(
        pair_match={
            "enabled": True,
            "min_spread_cents": -100,
            "max_pairs_per_market": 1,
            "alternate_directions": False,
        }
    )
    path = tmp_path / "pairs.sqlite3"
    registry = PairMatchRegistry(path)
    matcher = PairMatchEngine(config, registry)

    order = matcher.evaluate(engines(config, start, now), now)

    assert order is not None
    assert order.btc_leg.market_slug.startswith("btc-updown")
    settled = matcher.settle(
        order.btc_leg.market_slug,
        order.eth_leg.market_slug,
        btc_outcome,
        eth_outcome,
        now=start + timedelta(minutes=6),
    )
    assert len(settled) == 1
    expected_payout = 0.0
    if order.btc_leg.direction == btc_outcome:
        expected_payout += order.btc_leg.quantity
    if order.eth_leg.direction == eth_outcome:
        expected_payout += order.eth_leg.quantity
    assert settled[0].payout_usd == expected_payout
    assert settled[0].realized_pnl == expected_payout - order.total_cost_usd
    registry.close()

    reopened = PairMatchRegistry(path)
    restored = reopened.recent_orders()
    assert restored[0].status == "SETTLED"
    assert restored[0].realized_pnl == settled[0].realized_pnl
    assert reopened.summary()["settled_orders"] == 1
    assert reopened.settle(
        order.btc_leg.market_slug,
        order.eth_leg.market_slug,
        btc_outcome,
        eth_outcome,
        start + timedelta(minutes=7),
    ) == []
    reopened.close()


def test_pair_engine_enforces_open_second_window(tmp_path) -> None:
    start = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
    config = AppConfig(pair_match={"enabled": True, "min_spread_cents": -100})
    registry = PairMatchRegistry(tmp_path / "pairs.sqlite3")
    matcher = PairMatchEngine(config, registry)

    assert matcher.evaluate(engines(config, start, start + timedelta(seconds=19)), start + timedelta(seconds=19)) is None
    assert matcher.status == "outside_entry_window"
    assert matcher.evaluate(engines(config, start, start + timedelta(seconds=20)), start + timedelta(seconds=20)) is not None
    registry.close()


def test_pair_engine_excludes_entry_window_end_and_misaligned_markets(tmp_path) -> None:
    start = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
    config = AppConfig(pair_match={"enabled": True, "min_spread_cents": -100})
    registry = PairMatchRegistry(tmp_path / "pairs.sqlite3")
    matcher = PairMatchEngine(config, registry)
    at_end = start + timedelta(seconds=280)
    assert matcher.evaluate(engines(config, start, at_end), at_end) is None
    assert matcher.status == "outside_entry_window"

    now = start + timedelta(seconds=30)
    states = engines(config, start, now)
    states["ETH"].market.start_time = start + timedelta(minutes=5)
    states["ETH"].market.end_time = start + timedelta(minutes=10)
    assert matcher.evaluate(states, now) is None
    assert matcher.status == "waiting_for_aligned_markets"
    registry.close()


def test_pair_engine_requires_all_four_books_to_be_fresh_and_executable(tmp_path) -> None:
    start = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
    now = start + timedelta(seconds=30)
    config = AppConfig(pair_match={"enabled": True, "min_spread_cents": -100})
    registry = PairMatchRegistry(tmp_path / "pairs.sqlite3")
    matcher = PairMatchEngine(config, registry)
    states = engines(config, start, now)
    states["BTC"].books[Direction.DOWN].received_at = now - timedelta(seconds=2)

    assert matcher.evaluate(states, now) is None
    assert matcher.status == "all_four_books_must_be_executable"
    assert matcher.candidates[PairDirection.BTC_UP_ETH_DOWN].available is True
    assert matcher.candidates[PairDirection.BTC_DOWN_ETH_UP].reason == "book_stale"

    states["BTC"].books[Direction.DOWN].received_at = now
    assert matcher.evaluate(states, now) is not None
    registry.close()


def test_bid_only_or_timestamp_change_is_not_a_new_pair_quote(tmp_path) -> None:
    start = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
    now = start + timedelta(seconds=30)
    config = AppConfig(
        pair_match={"enabled": True, "min_spread_cents": -100, "max_pairs_per_market": 2}
    )
    registry = PairMatchRegistry(tmp_path / "pairs.sqlite3")
    matcher = PairMatchEngine(config, registry)
    states = engines(config, start, now)
    assert matcher.evaluate(states, now) is not None

    unchanged_asks = states["BTC"].books[Direction.UP]
    unchanged_asks.timestamp = now + timedelta(milliseconds=100)
    unchanged_asks.bids[0].price -= 0.01
    assert matcher.evaluate(states, now + timedelta(milliseconds=100)) is None
    assert matcher.status == "duplicate_quote_snapshot"
    assert registry.count(matcher.current_interval_key or "") == 1
    registry.close()


def test_pair_limit_and_alternating_direction_survive_restart(tmp_path) -> None:
    start = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
    now = start + timedelta(seconds=30)
    config = AppConfig(
        pair_match={"enabled": True, "min_spread_cents": -100, "max_pairs_per_market": 2}
    )
    path = tmp_path / "pairs.sqlite3"
    first_registry = PairMatchRegistry(path)
    first_matcher = PairMatchEngine(config, first_registry)
    states = engines(config, start, now)
    first = first_matcher.evaluate(states, now)
    assert first is not None
    first_registry.close()

    second_registry = PairMatchRegistry(path)
    second_matcher = PairMatchEngine(config, second_registry)
    states["ETH"].books[Direction.UP].asks[0].size += 1
    second = second_matcher.evaluate(states, now + timedelta(milliseconds=100))
    assert second is not None
    assert second.direction == first.direction.opposite
    states["BTC"].books[Direction.UP].asks[0].size += 1
    assert second_matcher.evaluate(states, now + timedelta(milliseconds=200)) is None
    assert second_matcher.status == "market_pair_limit"
    second_registry.close()


def test_resolved_outcome_requires_strict_gamma_resolution(monkeypatch) -> None:
    class Response:
        def json(self):
            return [
                {
                    "slug": "eth-updown-5m-1",
                    "closed": True,
                    "umaResolutionStatus": "resolved",
                    "outcomes": '["Up", "Down"]',
                    "outcomePrices": '["0", "1"]',
                }
            ]

    async def fake_get(*args, **kwargs):
        now = datetime.now(timezone.utc)
        return Response(), now, now, False

    monkeypatch.setattr("polybtc.clients.get_direct_first", fake_get)
    client = PolymarketClient(SourceConfig(proxy_url=None), "ETH")

    assert asyncio.run(client.resolved_outcome("eth-updown-5m-1")) == Direction.DOWN


def test_resolved_outcome_rejects_unresolved_and_non_binary_gamma_prices(monkeypatch) -> None:
    payload = {
        "slug": "btc-updown-5m-1",
        "closed": True,
        "umaResolutionStatus": "proposed",
        "outcomes": '["Up", "Down"]',
        "outcomePrices": '["0", "1"]',
    }

    class Response:
        def json(self):
            return [payload]

    async def fake_get(*args, **kwargs):
        now = datetime.now(timezone.utc)
        return Response(), now, now, False

    monkeypatch.setattr("polybtc.clients.get_direct_first", fake_get)
    client = PolymarketClient(SourceConfig(proxy_url=None), "BTC")
    assert asyncio.run(client.resolved_outcome("btc-updown-5m-1")) is None

    payload["umaResolutionStatus"] = "resolved"
    payload["outcomePrices"] = '["0.01", "0.99"]'
    assert asyncio.run(client.resolved_outcome("btc-updown-5m-1")) is None
