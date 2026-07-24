import asyncio
import json
import sqlite3
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


def two_stage_engines(
    config: AppConfig, start: datetime, now: datetime
) -> dict[str, PaperEngine]:
    result = engines(config, start, now)
    result["BTC"].books[Direction.UP].asks[0].price = 0.10
    result["ETH"].books[Direction.DOWN].asks[0].price = 0.50
    result["BTC"].books[Direction.DOWN].asks[0].price = 0.40
    result["ETH"].books[Direction.UP].asks[0].price = 0.50
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


def test_pair_leg_price_gap_uses_multilevel_execution_averages(tmp_path) -> None:
    start = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
    now = start + timedelta(seconds=30)
    config = AppConfig(
        pair_match={
            "enabled": True,
            "min_spread_cents": -100,
            "min_leg_price_gap_cents": 0,
            "alternate_directions": True,
            "alternation_mode": "always_a",
        }
    )
    states = engines(config, start, now)
    states["BTC"].books[Direction.UP].asks = [
        BookLevel(price=0.20, size=5),
        BookLevel(price=0.40, size=100),
    ]
    states["ETH"].books[Direction.DOWN].asks = [BookLevel(price=0.50, size=100)]
    registry = PairMatchRegistry(tmp_path / "pairs.sqlite3")
    matcher = PairMatchEngine(config, registry)

    assert matcher.evaluate(states, now) is not None
    candidate = matcher.candidates[PairDirection.BTC_UP_ETH_DOWN]

    assert candidate.btc_leg is not None
    assert candidate.eth_leg is not None
    assert candidate.btc_leg.levels_used == 2
    assert candidate.leg_price_gap_cents == pytest.approx(
        100 * abs(candidate.btc_leg.avg_price - candidate.eth_leg.avg_price)
    )
    assert candidate.leg_price_gap_cents != pytest.approx(30.0)
    registry.close()


@pytest.mark.parametrize(
    ("direction", "mode"),
    [
        (PairDirection.BTC_UP_ETH_DOWN, "always_a"),
        (PairDirection.BTC_DOWN_ETH_UP, "always_b"),
    ],
)
@pytest.mark.parametrize(
    ("minimum_gap", "opens"),
    [(24.99, True), (25.0, True), (25.01, False)],
)
def test_pair_leg_price_gap_boundary_is_symmetric(
    tmp_path,
    direction: PairDirection,
    mode: str,
    minimum_gap: float,
    opens: bool,
) -> None:
    start = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
    now = start + timedelta(seconds=30)
    config = AppConfig(
        pair_match={
            "enabled": True,
            "min_spread_cents": -100,
            "min_leg_price_gap_cents": minimum_gap,
            "alternate_directions": True,
            "alternation_mode": mode,
        }
    )
    states = engines(config, start, now)
    if direction == PairDirection.BTC_UP_ETH_DOWN:
        states["BTC"].books[Direction.UP].asks[0].price = 0.25
        states["ETH"].books[Direction.DOWN].asks[0].price = 0.50
    else:
        states["BTC"].books[Direction.DOWN].asks[0].price = 0.25
        states["ETH"].books[Direction.UP].asks[0].price = 0.50
    registry = PairMatchRegistry(tmp_path / f"{mode}-{minimum_gap}.sqlite3")
    matcher = PairMatchEngine(config, registry)

    opened = matcher.evaluate(states, now)
    candidate = matcher.candidates[direction]

    assert candidate.leg_price_gap_cents == pytest.approx(25.0)
    assert candidate.meets_leg_price_gap is opens
    assert (opened is not None) is opens
    if opens:
        assert opened is not None
        assert opened.direction == direction
        assert candidate.reason == "eligible"
    else:
        assert candidate.reason == "leg_price_gap_below_threshold"
        assert matcher.status == "no_eligible_pair"
    registry.close()


@pytest.mark.parametrize(
    ("strict", "expected_direction", "expected_status"),
    [
        (True, None, "waiting_for_alternating_direction"),
        (False, PairDirection.BTC_DOWN_ETH_UP, "pair_opened"),
    ],
)
def test_pair_leg_price_gap_respects_strict_direction_control(
    tmp_path,
    strict: bool,
    expected_direction: PairDirection | None,
    expected_status: str,
) -> None:
    start = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
    now = start + timedelta(seconds=30)
    config = AppConfig(
        pair_match={
            "enabled": True,
            "min_spread_cents": -100,
            "min_leg_price_gap_cents": 15,
            "alternate_directions": strict,
            "alternation_mode": "always_a",
        }
    )
    registry = PairMatchRegistry(tmp_path / f"strict-{strict}.sqlite3")
    matcher = PairMatchEngine(config, registry)

    opened = matcher.evaluate(engines(config, start, now), now)

    assert matcher.candidates[PairDirection.BTC_UP_ETH_DOWN].reason == (
        "leg_price_gap_below_threshold"
    )
    assert matcher.candidates[PairDirection.BTC_DOWN_ETH_UP].reason == "eligible"
    assert matcher.status == expected_status
    assert (opened.direction if opened else None) == expected_direction
    registry.close()


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


def test_sequence_modes_default_to_two_without_changing_existing_mode_defaults() -> None:
    assert PairMatchConfig().min_leg_price_gap_cents == 0
    assert PairMatchConfig().second_order_min_spread_cents == 0
    with pytest.raises(ValueError, match="min_leg_price_gap_cents"):
        PairMatchConfig(min_leg_price_gap_cents=-0.01)
    with pytest.raises(ValueError, match="min_leg_price_gap_cents"):
        PairMatchConfig(min_leg_price_gap_cents=100.01)
    with pytest.raises(ValueError, match="second_order_min_spread_cents"):
        PairMatchConfig(second_order_min_spread_cents=-100.01)
    with pytest.raises(ValueError, match="second_order_min_spread_cents"):
        PairMatchConfig(second_order_min_spread_cents=100.01)

    for mode in ("per_market", "continuous_abab", "always_a", "always_b"):
        assert PairMatchConfig(alternation_mode=mode).max_pairs_per_market == 1

    for mode in ("per_market_ab", "per_market_ba"):
        assert PairMatchConfig(alternation_mode=mode).max_pairs_per_market == 2
        assert PairMatchConfig(
            alternation_mode=mode, max_pairs_per_market=4
        ).max_pairs_per_market == 4

    two_stage = PairMatchConfig(
        alternation_mode="per_market_two_stage",
        max_pairs_per_market=4,
        alternate_directions=False,
    )
    assert two_stage.max_pairs_per_market == 2
    assert two_stage.alternate_directions is True


@pytest.mark.parametrize(
    ("mode", "first_direction"),
    [
        ("per_market_ab", PairDirection.BTC_UP_ETH_DOWN),
        ("per_market_ba", PairDirection.BTC_DOWN_ETH_UP),
    ],
)
def test_per_market_sequence_cycles_restores_and_resets(
    tmp_path, mode: str, first_direction: PairDirection
) -> None:
    config = AppConfig(
        pair_match={
            "enabled": True,
            "min_spread_cents": -100,
            "max_pairs_per_market": 3,
            "alternate_directions": True,
            "alternation_mode": mode,
        }
    )
    path = tmp_path / "pairs.sqlite3"
    start = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
    now = start + timedelta(seconds=30)
    states = engines(config, start, now)
    registry = PairMatchRegistry(path)
    matcher = PairMatchEngine(config, registry)

    first = matcher.evaluate(states, now)
    assert first is not None
    assert first.direction == first_direction
    assert matcher.next_direction == first_direction.opposite

    states["BTC"].books[Direction.UP].asks[0].size += 1
    second = matcher.evaluate(states, now + timedelta(milliseconds=100))
    assert second is not None
    assert second.direction == first_direction.opposite
    assert matcher.next_direction == first_direction
    registry.close()

    states["ETH"].books[Direction.DOWN].asks[0].size += 1
    reopened = PairMatchRegistry(path)
    restored = PairMatchEngine(config, reopened)
    third = restored.evaluate(states, now + timedelta(milliseconds=200))
    assert third is not None
    assert third.direction == first_direction
    assert restored.next_direction == first_direction.opposite
    states["BTC"].books[Direction.DOWN].asks[0].size += 1
    assert restored.evaluate(states, now + timedelta(milliseconds=300)) is None
    assert restored.status == "market_pair_limit"

    next_start = start + timedelta(minutes=5)
    next_now = next_start + timedelta(seconds=30)
    next_order = restored.evaluate(engines(config, next_start, next_now), next_now)
    assert next_order is not None
    assert next_order.direction == first_direction
    reopened.close()


@pytest.mark.parametrize(
    ("mode", "target"),
    [
        ("per_market_ab", PairDirection.BTC_UP_ETH_DOWN),
        ("per_market_ba", PairDirection.BTC_DOWN_ETH_UP),
    ],
)
def test_per_market_sequence_waits_for_its_first_direction(
    tmp_path, mode: str, target: PairDirection
) -> None:
    start = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
    now = start + timedelta(seconds=30)
    config = AppConfig(
        pair_match={
            "enabled": True,
            "min_spread_cents": 10,
            "alternate_directions": True,
            "alternation_mode": mode,
        }
    )
    states = engines(config, start, now)
    if target == PairDirection.BTC_UP_ETH_DOWN:
        states["BTC"].books[Direction.UP].asks[0].price = 0.60
        states["ETH"].books[Direction.DOWN].asks[0].price = 0.60
        states["BTC"].books[Direction.DOWN].asks[0].price = 0.20
        states["ETH"].books[Direction.UP].asks[0].price = 0.20
    else:
        states["BTC"].books[Direction.DOWN].asks[0].price = 0.60
        states["ETH"].books[Direction.UP].asks[0].price = 0.60
        states["BTC"].books[Direction.UP].asks[0].price = 0.20
        states["ETH"].books[Direction.DOWN].asks[0].price = 0.20
    registry = PairMatchRegistry(tmp_path / "pairs.sqlite3")
    matcher = PairMatchEngine(config, registry)

    assert matcher.evaluate(states, now) is None
    assert matcher.status == "waiting_for_alternating_direction"
    assert matcher.next_direction == target
    registry.close()


def test_continuous_abab_starts_randomly_then_crosses_markets_and_restart(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "polybtc.pair_match.secrets.choice",
        lambda choices: PairDirection.BTC_UP_ETH_DOWN,
    )
    config = AppConfig(
        pair_match={
            "enabled": True,
            "min_spread_cents": -100,
            "max_pairs_per_market": 1,
            "alternate_directions": True,
            "alternation_mode": "continuous_abab",
        }
    )
    path = tmp_path / "pairs.sqlite3"
    start = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
    registry = PairMatchRegistry(path)
    matcher = PairMatchEngine(config, registry)

    first_now = start + timedelta(seconds=30)
    first = matcher.evaluate(engines(config, start, first_now), first_now)
    assert first is not None
    assert first.direction == PairDirection.BTC_UP_ETH_DOWN
    assert matcher.next_direction == PairDirection.BTC_DOWN_ETH_UP

    second_start = start + timedelta(minutes=5)
    second_now = second_start + timedelta(seconds=30)
    second = matcher.evaluate(engines(config, second_start, second_now), second_now)
    assert second is not None
    assert second.direction == PairDirection.BTC_DOWN_ETH_UP
    assert matcher.next_direction == PairDirection.BTC_UP_ETH_DOWN
    registry.close()

    reopened = PairMatchRegistry(path)
    restored_matcher = PairMatchEngine(config, reopened)
    third_start = start + timedelta(minutes=10)
    third_now = third_start + timedelta(seconds=30)
    third = restored_matcher.evaluate(engines(config, third_start, third_now), third_now)
    assert third is not None
    assert third.direction == PairDirection.BTC_UP_ETH_DOWN
    assert restored_matcher.next_direction == PairDirection.BTC_DOWN_ETH_UP
    reopened.close()


def test_continuous_abab_waits_for_randomly_chosen_first_direction(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "polybtc.pair_match.secrets.choice",
        lambda choices: PairDirection.BTC_UP_ETH_DOWN,
    )
    start = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
    now = start + timedelta(seconds=30)
    config = AppConfig(
        pair_match={
            "enabled": True,
            "min_spread_cents": 0,
            "alternate_directions": True,
            "alternation_mode": "continuous_abab",
        }
    )
    registry = PairMatchRegistry(tmp_path / "pairs.sqlite3")
    matcher = PairMatchEngine(config, registry)
    states = engines(config, start, now)
    states["BTC"].books[Direction.UP].asks[0].price = 0.70

    assert matcher.evaluate(states, now) is None
    assert matcher.status == "waiting_for_alternating_direction"
    assert matcher.next_direction == PairDirection.BTC_UP_ETH_DOWN

    states["BTC"].books[Direction.UP].asks[0].price = 0.40
    opened = matcher.evaluate(states, now + timedelta(milliseconds=100))
    assert opened is not None
    assert opened.direction == PairDirection.BTC_UP_ETH_DOWN
    registry.close()


@pytest.mark.parametrize(
    ("mode", "target"),
    [
        ("always_a", PairDirection.BTC_UP_ETH_DOWN),
        ("always_b", PairDirection.BTC_DOWN_ETH_UP),
    ],
)
def test_fixed_direction_ignores_better_opposite_spread_and_survives_restart(
    tmp_path, mode: str, target: PairDirection
) -> None:
    start = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
    now = start + timedelta(seconds=30)
    config = AppConfig(
        pair_match={
            "enabled": True,
            "min_spread_cents": 10,
            "max_pairs_per_market": 2,
            "alternate_directions": True,
            "alternation_mode": mode,
        }
    )
    states = engines(config, start, now)
    if target == PairDirection.BTC_UP_ETH_DOWN:
        states["BTC"].books[Direction.UP].asks[0].price = 0.45
        states["ETH"].books[Direction.DOWN].asks[0].price = 0.40
        states["BTC"].books[Direction.DOWN].asks[0].price = 0.20
        states["ETH"].books[Direction.UP].asks[0].price = 0.20
        target_book = states["ETH"].books[Direction.DOWN]
    else:
        states["BTC"].books[Direction.DOWN].asks[0].price = 0.45
        states["ETH"].books[Direction.UP].asks[0].price = 0.40
        states["BTC"].books[Direction.UP].asks[0].price = 0.20
        states["ETH"].books[Direction.DOWN].asks[0].price = 0.20
        target_book = states["ETH"].books[Direction.UP]

    path = tmp_path / "pairs.sqlite3"
    registry = PairMatchRegistry(path)
    matcher = PairMatchEngine(config, registry)
    first = matcher.evaluate(states, now)

    assert first is not None
    assert first.direction == target
    assert matcher.next_direction == target
    assert matcher.evaluate(states, now + timedelta(milliseconds=50)) is None
    assert matcher.status == "duplicate_quote_snapshot"
    registry.close()

    target_book.asks[0].size += 1
    reopened = PairMatchRegistry(path)
    restored = PairMatchEngine(config, reopened)
    second = restored.evaluate(states, now + timedelta(milliseconds=100))

    assert second is not None
    assert second.direction == target
    assert restored.next_direction == target
    target_book.asks[0].size += 1
    assert restored.evaluate(states, now + timedelta(milliseconds=200)) is None
    assert restored.status == "market_pair_limit"
    reopened.close()


@pytest.mark.parametrize(
    ("mode", "target"),
    [
        ("always_a", PairDirection.BTC_UP_ETH_DOWN),
        ("always_b", PairDirection.BTC_DOWN_ETH_UP),
    ],
)
def test_fixed_direction_waits_when_only_opposite_direction_is_eligible(
    tmp_path, mode: str, target: PairDirection
) -> None:
    start = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
    now = start + timedelta(seconds=30)
    config = AppConfig(
        pair_match={
            "enabled": True,
            "min_spread_cents": 10,
            "alternate_directions": True,
            "alternation_mode": mode,
        }
    )
    states = engines(config, start, now)
    if target == PairDirection.BTC_UP_ETH_DOWN:
        states["BTC"].books[Direction.UP].asks[0].price = 0.60
        states["ETH"].books[Direction.DOWN].asks[0].price = 0.60
        states["BTC"].books[Direction.DOWN].asks[0].price = 0.20
        states["ETH"].books[Direction.UP].asks[0].price = 0.20
    else:
        states["BTC"].books[Direction.DOWN].asks[0].price = 0.60
        states["ETH"].books[Direction.UP].asks[0].price = 0.60
        states["BTC"].books[Direction.UP].asks[0].price = 0.20
        states["ETH"].books[Direction.DOWN].asks[0].price = 0.20

    registry = PairMatchRegistry(tmp_path / "pairs.sqlite3")
    matcher = PairMatchEngine(config, registry)

    assert matcher.evaluate(states, now) is None
    assert matcher.status == "waiting_for_alternating_direction"
    assert matcher.next_direction == target
    registry.close()


@pytest.mark.parametrize("mode", ["always_a", "per_market_ab"])
def test_fixed_mode_is_ignored_when_strict_direction_control_is_disabled(
    tmp_path, mode: str
) -> None:
    start = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
    now = start + timedelta(seconds=30)
    config = AppConfig(
        pair_match={
            "enabled": True,
            "min_spread_cents": 10,
            "alternate_directions": False,
            "alternation_mode": mode,
        }
    )
    states = engines(config, start, now)
    states["BTC"].books[Direction.UP].asks[0].price = 0.60
    states["ETH"].books[Direction.DOWN].asks[0].price = 0.60
    states["BTC"].books[Direction.DOWN].asks[0].price = 0.20
    states["ETH"].books[Direction.UP].asks[0].price = 0.20
    registry = PairMatchRegistry(tmp_path / "pairs.sqlite3")
    matcher = PairMatchEngine(config, registry)

    opened = matcher.evaluate(states, now)

    assert opened is not None
    assert opened.direction == PairDirection.BTC_DOWN_ETH_UP
    assert matcher.next_direction is None
    registry.close()


def two_stage_config(second_min_spread: float = 0) -> AppConfig:
    return AppConfig(
        pair_match={
            "enabled": True,
            "min_spread_cents": 20,
            "second_order_min_spread_cents": second_min_spread,
            "min_leg_price_gap_cents": 30,
            "max_pairs_per_market": 9,
            "alternate_directions": False,
            "alternation_mode": "per_market_two_stage",
        }
    )


def test_two_stage_mode_switches_thresholds_and_reuses_the_same_snapshot(tmp_path) -> None:
    start = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
    now = start + timedelta(seconds=30)
    config = two_stage_config()
    states = two_stage_engines(config, start, now)
    registry = PairMatchRegistry(tmp_path / "pairs.sqlite3")
    matcher = PairMatchEngine(config, registry)

    first = matcher.evaluate(states, now)

    assert first is not None
    assert first.direction == PairDirection.BTC_UP_ETH_DOWN
    first_stage_b = matcher.candidates[PairDirection.BTC_DOWN_ETH_UP]
    assert first_stage_b.meets_spread is False
    assert first_stage_b.meets_leg_price_gap is False
    assert matcher.next_direction == PairDirection.BTC_DOWN_ETH_UP

    second = matcher.evaluate(states, now + timedelta(milliseconds=1))

    assert second is not None
    assert second.direction == PairDirection.BTC_DOWN_ETH_UP
    second_stage_b = matcher.candidates[PairDirection.BTC_DOWN_ETH_UP]
    assert second_stage_b.meets_spread is True
    assert second_stage_b.meets_leg_price_gap is True
    assert second_stage_b.reason == "eligible"
    assert first.fingerprint != second.fingerprint
    assert registry.count(first.interval_key) == 2

    assert matcher.evaluate(states, now + timedelta(milliseconds=2)) is None
    assert matcher.status == "market_pair_limit"
    registry.close()


@pytest.mark.parametrize(
    ("threshold_offset", "opens"),
    [(-0.001, True), (0.0, True), (0.001, False)],
)
def test_two_stage_second_order_spread_boundary(
    tmp_path, threshold_offset: float, opens: bool
) -> None:
    start = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
    now = start + timedelta(seconds=30)
    config = two_stage_config()
    states = two_stage_engines(config, start, now)
    registry = PairMatchRegistry(tmp_path / f"boundary-{threshold_offset}.sqlite3")
    matcher = PairMatchEngine(config, registry)

    assert matcher.evaluate(states, now) is not None
    second_direction = PairDirection.BTC_DOWN_ETH_UP
    second_spread = matcher.candidates[second_direction].spread_cents
    assert second_spread is not None
    config.pair_match.second_order_min_spread_cents = second_spread + threshold_offset

    second = matcher.evaluate(states, now + timedelta(milliseconds=1))

    assert (second is not None) is opens
    assert matcher.candidates[second_direction].meets_spread is opens
    assert matcher.candidates[second_direction].meets_leg_price_gap is True
    if opens:
        assert second is not None
        assert second.direction == second_direction
    else:
        assert matcher.status == "waiting_for_alternating_direction"
    registry.close()


def test_two_stage_second_order_restores_after_restart_with_same_snapshot(tmp_path) -> None:
    start = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
    now = start + timedelta(seconds=30)
    config = two_stage_config()
    states = two_stage_engines(config, start, now)
    path = tmp_path / "pairs.sqlite3"
    first_registry = PairMatchRegistry(path)
    first = PairMatchEngine(config, first_registry).evaluate(states, now)
    assert first is not None
    first_registry.close()

    second_registry = PairMatchRegistry(path)
    restored = PairMatchEngine(config, second_registry)
    second = restored.evaluate(states, now + timedelta(milliseconds=1))

    assert second is not None
    assert second.direction == first.direction.opposite
    assert first.fingerprint != second.fingerprint
    assert second_registry.count(first.interval_key) == 2
    second_registry.close()


def test_two_stage_new_market_resets_to_first_order_thresholds(tmp_path) -> None:
    start = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
    now = start + timedelta(seconds=30)
    config = two_stage_config()
    registry = PairMatchRegistry(tmp_path / "pairs.sqlite3")
    matcher = PairMatchEngine(config, registry)
    first = matcher.evaluate(two_stage_engines(config, start, now), now)
    assert first is not None

    next_start = start + timedelta(minutes=5)
    next_now = next_start + timedelta(seconds=30)
    next_order = matcher.evaluate(
        two_stage_engines(config, next_start, next_now), next_now
    )

    assert next_order is not None
    assert next_order.direction == PairDirection.BTC_UP_ETH_DOWN
    assert matcher.candidates[PairDirection.BTC_DOWN_ETH_UP].meets_spread is False
    assert matcher.candidates[PairDirection.BTC_DOWN_ETH_UP].meets_leg_price_gap is False
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


def test_pair_order_numbers_are_sequential_and_survive_restart(tmp_path) -> None:
    start = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
    config = AppConfig(
        pair_match={"enabled": True, "min_spread_cents": -100, "alternate_directions": False}
    )
    path = tmp_path / "pairs.sqlite3"
    registry = PairMatchRegistry(path)
    matcher = PairMatchEngine(config, registry)

    first_now = start + timedelta(seconds=30)
    first = matcher.evaluate(engines(config, start, first_now), first_now)
    second_start = start + timedelta(minutes=5)
    second_now = second_start + timedelta(seconds=30)
    second = matcher.evaluate(engines(config, second_start, second_now), second_now)

    assert first is not None
    assert second is not None
    assert first.order_number == 1
    assert second.order_number == 2
    assert [order.order_number for order in registry.recent_orders()] == [2, 1]
    registry.close()

    reopened = PairMatchRegistry(path)
    third_start = start + timedelta(minutes=10)
    third_now = third_start + timedelta(seconds=30)
    third = PairMatchEngine(config, reopened).evaluate(
        engines(config, third_start, third_now), third_now
    )

    assert third is not None
    assert third.order_number == 3
    assert [order.order_number for order in reopened.recent_orders()] == [3, 2, 1]
    reopened.close()


def test_pair_registry_adds_and_backfills_order_numbers_for_legacy_database(tmp_path) -> None:
    path = tmp_path / "pairs.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE pair_orders (
            order_id TEXT PRIMARY KEY,
            interval_key TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            direction TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            opened_at TEXT NOT NULL,
            btc_leg_json TEXT NOT NULL,
            eth_leg_json TEXT NOT NULL,
            spread_cents REAL NOT NULL,
            total_cost_usd REAL NOT NULL,
            scenario_pnl_json TEXT NOT NULL,
            status TEXT NOT NULL,
            btc_outcome TEXT,
            eth_outcome TEXT,
            payout_usd REAL,
            realized_pnl REAL,
            settled_at TEXT,
            UNIQUE(interval_key, fingerprint)
        )
        """
    )
    leg = {
        "asset": "BTC",
        "market_id": "market",
        "market_slug": "btc-updown-5m-1",
        "token_id": "token",
        "direction": "UP",
        "avg_price": 0.4,
        "quantity": 10,
        "quote": 4,
        "fee_usd": 0.1,
        "slippage": 0,
        "levels_used": 1,
    }
    for order_id, opened_at in (("later", "2026-07-20T09:01:00Z"), ("earlier", "2026-07-20T09:00:00Z")):
        connection.execute(
            """
            INSERT INTO pair_orders (
                order_id, interval_key, start_time, end_time, direction, fingerprint, opened_at,
                btc_leg_json, eth_leg_json, spread_cents, total_cost_usd, scenario_pnl_json, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id,
                order_id,
                "2026-07-20T09:00:00Z",
                "2026-07-20T09:05:00Z",
                "BTC_UP_ETH_DOWN",
                order_id,
                opened_at,
                json.dumps(leg),
                json.dumps({**leg, "asset": "ETH", "direction": "DOWN"}),
                10,
                20,
                json.dumps({}),
                "PENDING",
            ),
        )
    connection.commit()
    connection.close()

    registry = PairMatchRegistry(path)
    orders = registry.recent_orders()

    assert [(order.order_id, order.order_number) for order in orders] == [
        ("later", 2),
        ("earlier", 1),
    ]
    columns = {
        row["name"] for row in registry.connection.execute("PRAGMA table_info(pair_orders)").fetchall()
    }
    assert "order_number" in columns
    registry.close()


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
