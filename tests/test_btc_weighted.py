from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from polybtc.btc_weighted import (
    BtcWeightedEngine,
    BtcWeightedRegistry,
    WeightedAttempt,
    WeightedPosition,
    base_weights,
    contract_price_score,
    time_score,
    volatility_adjusted_weights,
)
from polybtc.config import AppConfig
from polybtc.models import BookLevel, Direction, MarketState, OrderBookSnapshot, PriceTick


NOW = datetime(2026, 8, 9, 12, 4, 0, tzinfo=timezone.utc)


class FakeLatency:
    def __init__(self, milliseconds: float = 0.0, usable: bool = True):
        self.milliseconds = milliseconds
        self.usable = usable

    def snapshot(self, now=None):
        return {
            "latest_ms": self.milliseconds,
            "p95_ms": self.milliseconds,
            "fresh": self.usable,
            "warmed": self.usable,
            "sample_count": 40 if self.usable else 0,
        }


def market() -> MarketState:
    return MarketState(
        condition_id="weighted-market",
        slug="btc-updown-5m-weighted",
        question="BTC up or down",
        threshold_price=100.0,
        threshold_source="polymarket_twap_page",
        threshold_verified=True,
        start_time=NOW - timedelta(seconds=240),
        end_time=NOW + timedelta(seconds=60),
        up_token_id="up-token",
        down_token_id="down-token",
        min_order_size=5.0,
        tick_size=0.01,
    )


def book(
    direction: Direction,
    midpoint: float,
    observed_at: datetime,
    *,
    trusted: bool = True,
    depth: float = 100.0,
) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        token_id="up-token" if direction == Direction.UP else "down-token",
        market_id="weighted-market",
        timestamp=observed_at,
        received_at=observed_at,
        bids=[BookLevel(price=midpoint - 0.01, size=depth)],
        asks=[BookLevel(price=midpoint + 0.01, size=depth)],
        depth_trusted=trusted,
        min_order_size=5.0,
        tick_size=0.01,
    )


def engine(tmp_path, **settings) -> BtcWeightedEngine:
    config = AppConfig(
        data_dir=tmp_path,
        btc_weighted={"enabled": True, **settings},
    )
    registry = BtcWeightedRegistry(tmp_path / "weighted.sqlite3")
    return BtcWeightedEngine(config, registry, FakeLatency())


def seed_history(
    subject: BtcWeightedEngine,
    end: datetime,
    *,
    chain_start: float = 99.8,
    chain_step: float = 0.004,
    up_start: float = 0.55,
    up_step: float = 0.001,
) -> dict[Direction, OrderBookSnapshot]:
    latest = {}
    for offset in range(-65, 1):
        observed_at = end + timedelta(seconds=offset)
        index = offset + 65
        subject.add_chainlink_tick(
            PriceTick(source="chainlink", price=chain_start + chain_step * index, received_at=observed_at)
        )
        up_mid = up_start + up_step * index
        down_mid = 1.0 - up_mid
        for direction, midpoint in ((Direction.UP, up_mid), (Direction.DOWN, down_mid)):
            snapshot = book(direction, midpoint, observed_at)
            subject.add_book(direction, snapshot)
            latest[direction] = snapshot
    return latest


def test_time_and_contract_price_scores_follow_declared_anchors() -> None:
    assert time_score(300) == pytest.approx(50)
    assert time_score(180) == pytest.approx(80)
    assert time_score(60) == pytest.approx(100)
    assert time_score(10) == pytest.approx(0)
    assert contract_price_score(15) == pytest.approx(0)
    assert contract_price_score(55) == pytest.approx(80)
    assert contract_price_score(75) == pytest.approx(100)
    assert contract_price_score(90) == pytest.approx(100)


def test_time_and_volatility_weights_are_normalized() -> None:
    assert base_weights(300) == {
        "time": 10,
        "contract_price": 10,
        "distance": 15,
        "book_velocity_3s": 20,
        "gap_velocity_3s": 25,
        "book_acceleration_3s": 8,
        "gap_acceleration_3s": 12,
    }
    low = volatility_adjusted_weights(300, 0.75)
    high = volatility_adjusted_weights(300, 1.5)
    assert sum(low.values()) == pytest.approx(100)
    assert sum(high.values()) == pytest.approx(100)
    assert low["distance"] == pytest.approx(20)
    assert high["gap_velocity_3s"] == pytest.approx(18)


def test_acceleration_uses_two_consecutive_three_second_moves(tmp_path) -> None:
    subject = engine(tmp_path)
    subject.set_market(market(), NOW)
    chain_prices = ((-6, 100.0), (-3, 101.0), (0, 103.0))
    up_mids = ((-6, 0.50), (-3, 0.52), (0, 0.55))
    down_mids = ((-6, 0.50), (-3, 0.48), (0, 0.45))
    for offset, price in chain_prices:
        subject.add_chainlink_tick(PriceTick(source="chainlink", price=price, received_at=NOW + timedelta(seconds=offset)))
    books = {}
    for direction, rows in ((Direction.UP, up_mids), (Direction.DOWN, down_mids)):
        for offset, midpoint in rows:
            snapshot = book(direction, midpoint, NOW + timedelta(seconds=offset))
            subject.add_book(direction, snapshot)
            books[direction] = snapshot
    subject.evaluate(market(), books, NOW)
    up = subject.components["UP"]
    assert up["book_velocity_3s"]["raw"] == pytest.approx(3.0)
    assert up["book_acceleration_3s"]["raw"] == pytest.approx(1.0)
    expected_gap_acceleration = (math.log(103 / 101) - math.log(101 / 100)) * 10_000
    assert up["gap_acceleration_3s"]["raw"] == pytest.approx(expected_gap_acceleration)
    assert subject.components["DOWN"]["gap_acceleration_3s"]["raw"] == pytest.approx(-expected_gap_acceleration)


def test_missing_t_minus_six_makes_direction_ineligible(tmp_path) -> None:
    subject = engine(tmp_path)
    subject.set_market(market(), NOW)
    for offset in (-3, 0):
        observed_at = NOW + timedelta(seconds=offset)
        subject.add_chainlink_tick(PriceTick(source="chainlink", price=100 + offset / 100, received_at=observed_at))
        for direction in Direction:
            subject.add_book(direction, book(direction, 0.5, observed_at))
    books = {direction: book(direction, 0.5, NOW) for direction in Direction}
    subject.evaluate(market(), books, NOW)
    assert subject.scores == {"UP": None, "DOWN": None}
    assert subject.components["UP"]["book_acceleration_3s"]["reason"] == "t_minus_3_or_6_unavailable"
    assert subject.candidates["UP"]["reason"] == "score_history_warming"


def test_up_and_down_distance_scores_are_symmetric(tmp_path) -> None:
    subject = engine(tmp_path)
    subject.set_market(market(), NOW)
    books = seed_history(subject, NOW, chain_start=100.0, chain_step=0.002)
    subject.evaluate(market(), books, NOW)
    up = subject.components["UP"]["distance"]["score"]
    down = subject.components["DOWN"]["distance"]["score"]
    assert up + down == pytest.approx(100.0)


def test_no_net_edge_gate_blocks_an_otherwise_safe_candidate(tmp_path) -> None:
    subject = engine(tmp_path, entry_score_threshold=0, entry_lead_points=0)
    subject.set_market(market(), NOW)
    books = seed_history(
        subject,
        NOW,
        chain_start=100.0,
        chain_step=0.0,
        up_start=0.80,
        up_step=0.0,
    )
    subject.evaluate(market(), books, NOW)
    candidate = subject.candidates["UP"]
    assert candidate["diagnostics"]["diagnostic_edge_cents"] < 0
    assert candidate["eligible"] is True
    assert "net_edge" not in candidate["gates"]


def test_stale_or_untrusted_book_is_a_hard_gate(tmp_path) -> None:
    subject = engine(tmp_path, entry_score_threshold=0, entry_lead_points=0)
    subject.set_market(market(), NOW)
    books = seed_history(subject, NOW)
    stale = book(Direction.UP, 0.62, NOW - timedelta(seconds=2), trusted=False)
    books[Direction.UP] = stale
    subject.evaluate(market(), books, NOW)
    gates = subject.candidates["UP"]["gates"]
    assert gates["book_fresh"]["passed"] is False
    assert gates["depth_trusted"]["passed"] is False


def test_entry_confirmation_creates_one_observed_and_one_p95_attempt(tmp_path) -> None:
    subject = engine(
        tmp_path,
        entry_score_threshold=0,
        entry_lead_points=0,
        entry_confirmation_seconds=0.1,
        entry_confirmation_updates=1,
    )
    subject.set_market(market(), NOW)
    books = seed_history(subject, NOW, up_start=0.62, up_step=0.001)
    subject.evaluate(market(), books, NOW)
    later = NOW + timedelta(seconds=0.2)
    subject.add_chainlink_tick(PriceTick(source="chainlink", price=100.07, received_at=later))
    books = {
        Direction.UP: book(Direction.UP, 0.686, later),
        Direction.DOWN: book(Direction.DOWN, 0.314, later),
    }
    subject.evaluate(market(), books, later)
    attempts = subject.registry.recent_attempts()
    assert subject.current_round.entry_count == 1
    assert {(item.lane, item.side) for item in attempts} == {("observed", "BUY"), ("p95", "BUY")}
    subject.evaluate(
        market(),
        {
            Direction.UP: book(Direction.UP, 0.687, later + timedelta(milliseconds=100)),
            Direction.DOWN: book(Direction.DOWN, 0.313, later + timedelta(milliseconds=100)),
        },
        later + timedelta(milliseconds=100),
    )
    assert len([item for item in subject.positions.values() if item.status == "OPEN"]) == 2


def test_reverse_exit_retries_when_sell_depth_is_missing(tmp_path) -> None:
    subject = engine(tmp_path, min_hold_seconds=0.1, exit_confirmation_seconds=0.1, exit_confirmation_updates=1)
    subject.set_market(market(), NOW)
    position = WeightedPosition(
        market_id=market().condition_id,
        market_slug=market().slug,
        lane="observed",
        direction=Direction.UP,
        token_id="up-token",
        entry_attempt_id="entry",
        entry_price=0.6,
        quantity=8,
        entry_quote=4.8,
        entry_fee_usd=0,
        entry_score=80,
        opened_at=NOW - timedelta(seconds=20),
    )
    subject.positions[position.position_id] = position
    subject.registry.save_position(position)
    subject.scores = {"UP": 40.0, "DOWN": 80.0}
    empty = book(Direction.UP, 0.55, NOW, depth=1.0)
    subject._confirmation(
        f"exit:{position.position_id}", True, "first", NOW - timedelta(seconds=1), 0.1, 1
    )
    subject._evaluate_positions({Direction.UP: empty}, NOW, "second")
    assert position.status == "OPEN"
    assert position.exit_reason == "score_reversal"
    fresh = book(Direction.UP, 0.55, NOW + timedelta(seconds=1), depth=100)
    subject._evaluate_positions({Direction.UP: fresh}, NOW + timedelta(seconds=1), "third")
    assert position.status == "EXIT_PENDING"


def test_official_settlement_persists_lane_pnl(tmp_path) -> None:
    subject = engine(tmp_path)
    subject.set_market(market(), NOW)
    position = WeightedPosition(
        market_id=market().condition_id,
        market_slug=market().slug,
        lane="p95",
        direction=Direction.UP,
        token_id="up-token",
        status="HOLD_TO_SETTLEMENT",
        entry_attempt_id="entry",
        entry_price=0.5,
        quantity=10,
        entry_quote=5,
        entry_fee_usd=0.1,
        entry_score=75,
        opened_at=NOW,
    )
    subject.positions[position.position_id] = position
    subject.registry.save_position(position)
    assert subject.settle(market().slug, Direction.UP, NOW + timedelta(seconds=90)) is True
    assert position.status == "SETTLED"
    assert position.realized_pnl == pytest.approx(4.9)
    reloaded = BtcWeightedRegistry(tmp_path / "weighted.sqlite3")
    recent = reloaded.recent_positions()
    assert recent[0].status == "SETTLED"
    assert recent[0].realized_pnl == pytest.approx(4.9)
    reloaded.close()


def test_market_change_cancels_old_attempts_and_holds_positions(tmp_path) -> None:
    subject = engine(tmp_path)
    old_market = market()
    subject.set_market(old_market, NOW)
    attempt = WeightedAttempt(
        idempotency_key="old-market-pending",
        market_id=old_market.condition_id,
        market_slug=old_market.slug,
        lane="observed",
        side="BUY",
        direction=Direction.UP,
        token_id=old_market.up_token_id,
        requested_quote=5,
        limit_price=0.9,
        due_at=NOW + timedelta(seconds=5),
    )
    subject.pending[attempt.attempt_id] = attempt
    subject.registry.save_attempt(attempt)
    position = WeightedPosition(
        market_id=old_market.condition_id,
        market_slug=old_market.slug,
        lane="p95",
        direction=Direction.DOWN,
        token_id=old_market.down_token_id,
        entry_attempt_id="entry",
        entry_price=0.5,
        quantity=10,
        entry_quote=5,
        entry_fee_usd=0,
        entry_score=75,
        opened_at=NOW,
    )
    subject.positions[position.position_id] = position
    subject.registry.save_position(position)

    new_market = old_market.model_copy(
        update={
            "condition_id": "weighted-market-next",
            "slug": "btc-updown-5m-weighted-next",
            "start_time": old_market.end_time,
            "end_time": old_market.end_time + timedelta(minutes=5),
            "up_token_id": "next-up-token",
            "down_token_id": "next-down-token",
        }
    )
    subject.set_market(new_market, NOW + timedelta(seconds=61))

    assert subject.pending == {}
    assert position.status == "HOLD_TO_SETTLEMENT"
    saved = subject.registry.recent_attempts()[0]
    assert saved.status == "UNMEASURABLE"
    assert saved.reason == "market_changed_before_shadow_result"


def test_market_end_stops_scoring_and_waits_for_official_settlement(tmp_path) -> None:
    subject = engine(tmp_path)
    current_market = market()
    subject.set_market(current_market, NOW)
    position = WeightedPosition(
        market_id=current_market.condition_id,
        market_slug=current_market.slug,
        lane="observed",
        direction=Direction.UP,
        token_id=current_market.up_token_id,
        status="EXIT_PENDING",
        entry_attempt_id="entry",
        entry_price=0.5,
        quantity=10,
        entry_quote=5,
        entry_fee_usd=0,
        entry_score=80,
        opened_at=NOW,
    )
    subject.positions[position.position_id] = position
    subject.registry.save_position(position)
    attempt = WeightedAttempt(
        idempotency_key="end-pending-sell",
        market_id=current_market.condition_id,
        market_slug=current_market.slug,
        lane="observed",
        side="SELL",
        direction=Direction.UP,
        token_id=current_market.up_token_id,
        requested_quantity=10,
        limit_price=0.4,
        due_at=NOW + timedelta(seconds=59),
    )
    subject.pending[attempt.attempt_id] = attempt
    subject.registry.save_attempt(attempt)

    ended_at = current_market.end_time + timedelta(milliseconds=1)
    subject.evaluate(
        current_market,
        {
            Direction.UP: book(Direction.UP, 0.55, ended_at),
            Direction.DOWN: book(Direction.DOWN, 0.45, ended_at),
        },
        ended_at,
    )

    assert subject.status == "waiting_official_settlement"
    assert subject.pending == {}
    assert position.status == "HOLD_TO_SETTLEMENT"
    saved = subject.registry.recent_attempts()[0]
    assert saved.status == "UNMEASURABLE"
    assert saved.reason == "market_ended_before_shadow_result"


def test_restart_marks_inflight_attempt_unmeasurable(tmp_path) -> None:
    path = tmp_path / "restart.sqlite3"
    registry = BtcWeightedRegistry(path)
    attempt = WeightedAttempt(
        idempotency_key="restart-pending",
        market_id="market",
        market_slug="slug",
        lane="p95",
        side="BUY",
        direction=Direction.DOWN,
        token_id="down",
        requested_quote=5,
        limit_price=0.9,
        due_at=NOW + timedelta(seconds=1),
    )
    registry.save_attempt(attempt)
    registry.close()

    reloaded = BtcWeightedRegistry(path)
    saved = reloaded.recent_attempts()[0]
    assert saved.status == "UNMEASURABLE"
    assert saved.reason == "process_restarted_before_shadow_result"
    reloaded.close()
