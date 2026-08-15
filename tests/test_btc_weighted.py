from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from polybtc.btc_weighted import (
    BtcWeightedEngine,
    BtcWeightedRegistry,
    WeightedAttempt,
    WeightedPosition,
    WeightedRound,
    active_weighted_entry_segment,
    base_weights,
    contract_price_score,
    time_score,
    volatility_adjusted_weights,
    weighted_config_version,
    weighted_entry_segment_windows,
)
from polybtc.config import AppConfig
from polybtc.models import BookLevel, Direction, MarketState, OrderBookSnapshot, PriceTick
from polybtc.orderbook import ExecutionResult


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


def market_at_elapsed(elapsed_seconds: float) -> MarketState:
    return market().model_copy(
        update={
            "start_time": NOW - timedelta(seconds=elapsed_seconds),
            "end_time": NOW + timedelta(seconds=300 - elapsed_seconds),
        }
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


def test_entry_segment_defaults_validation_and_boundaries() -> None:
    settings = AppConfig().btc_weighted
    assert settings.reversal_sequence_enabled is False
    assert [segment.model_dump() for segment in settings.entry_segments] == [
        {"id": "early", "enabled": True, "duration_seconds": 80, "quote_amount_usd": 1.0},
        {"id": "middle", "enabled": True, "duration_seconds": 140, "quote_amount_usd": 3.0},
        {"id": "late", "enabled": True, "duration_seconds": 80, "quote_amount_usd": 5.0},
    ]
    assert [(row["start_seconds_after_open"], row["end_seconds_after_open"]) for row in weighted_entry_segment_windows(settings)] == [
        (0, 80),
        (80, 220),
        (220, 300),
    ]
    assert active_weighted_entry_segment(settings, 79.999)["id"] == "early"
    assert active_weighted_entry_segment(settings, 80)["id"] == "middle"
    assert active_weighted_entry_segment(settings, 219.999)["id"] == "middle"
    assert active_weighted_entry_segment(settings, 220)["id"] == "late"
    assert active_weighted_entry_segment(settings, 300) is None

    with pytest.raises(ValueError, match="total 300 seconds"):
        AppConfig(
            btc_weighted={
                "entry_segments": [
                    {"id": "early", "duration_seconds": 80, "quote_amount_usd": 1},
                    {"id": "middle", "duration_seconds": 140, "quote_amount_usd": 3},
                    {"id": "late", "duration_seconds": 79, "quote_amount_usd": 5},
                ]
            }
        )
    with pytest.raises(ValueError, match="must be positive"):
        AppConfig(
            btc_weighted={
                "entry_segments": [
                    {"id": "early", "duration_seconds": 80, "quote_amount_usd": 0},
                    {"id": "middle", "duration_seconds": 140, "quote_amount_usd": 3},
                    {"id": "late", "duration_seconds": 80, "quote_amount_usd": 5},
                ]
            }
        )
    with pytest.raises(ValueError, match="at most two decimals"):
        AppConfig(
            btc_weighted={
                "entry_segments": [
                    {"id": "early", "duration_seconds": 80, "quote_amount_usd": 1.001},
                    {"id": "middle", "duration_seconds": 140, "quote_amount_usd": 3},
                    {"id": "late", "duration_seconds": 80, "quote_amount_usd": 5},
                ]
            }
        )


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
    assert subject.candidates["UP"]["reason"] == "metric_history_warming"


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


def test_one_dollar_segment_ignores_share_minimum_but_requires_complete_quote(tmp_path) -> None:
    subject = engine(
        tmp_path,
        entry_score_threshold=0,
        entry_lead_points=0,
        entry_confirmation_seconds=0.1,
        entry_confirmation_updates=1,
    )
    current_market = market_at_elapsed(20)
    subject.set_market(current_market, NOW)
    books = seed_history(subject, NOW, up_start=0.55, up_step=0.0)
    subject.evaluate(current_market, books, NOW)
    target = max(subject.scores, key=lambda direction: subject.scores[direction] or 0)
    candidate = subject.candidates[target]
    assert candidate["diagnostics"]["requested_quote_usd"] == pytest.approx(1)
    assert candidate["diagnostics"]["simulated_quantity"] < 5
    assert candidate["diagnostics"]["below_book_min_order_size"] is True
    assert candidate["gates"]["minimum_quantity"] == {
        "passed": True,
        "blocking": False,
        "value": pytest.approx(candidate["diagnostics"]["simulated_quantity"]),
        "limit": 5.0,
        "reason": "below_book_min_order_size_diagnostic",
    }

    later = NOW + timedelta(seconds=0.2)
    subject.add_chainlink_tick(
        PriceTick(source="chainlink", price=100.07, received_at=later)
    )
    books = {
        Direction.UP: book(Direction.UP, 0.56, later),
        Direction.DOWN: book(Direction.DOWN, 0.44, later),
    }
    subject.evaluate(current_market, books, later)
    attempts = [attempt for attempt in subject.registry.recent_attempts() if attempt.side == "BUY"]
    assert len(attempts) == 2
    assert {attempt.entry_segment for attempt in attempts} == {"early"}
    assert {attempt.requested_quote for attempt in attempts} == {1.0}
    assert subject.current_round.used_entry_segments == ["early"]

    resolved_at = later + timedelta(milliseconds=100)
    subject.evaluate(
        current_market,
        {
            Direction.UP: book(Direction.UP, 0.56, resolved_at),
            Direction.DOWN: book(Direction.DOWN, 0.44, resolved_at),
        },
        resolved_at,
    )
    assert len([position for position in subject.positions.values() if position.status == "OPEN"]) == 2
    assert {position.entry_segment for position in subject.positions.values()} == {"early"}
    assert {position.segment_quote_amount_usd for position in subject.positions.values()} == {1.0}

    shallow = book(Direction.UP, 0.56, resolved_at + timedelta(seconds=1), depth=0.5)
    subject.evaluate(
        current_market,
        {
            Direction.UP: shallow,
            Direction.DOWN: book(Direction.DOWN, 0.44, shallow.received_at),
        },
        shallow.received_at,
    )
    assert subject.candidates["UP"]["gates"]["complete_fill"]["passed"] is False


def test_each_segment_submits_once_and_may_switch_direction(tmp_path, monkeypatch) -> None:
    subject = engine(
        tmp_path,
        entry_score_threshold=0,
        entry_lead_points=0,
        entry_confirmation_seconds=0.1,
        entry_confirmation_updates=1,
        score_exit_end_seconds_after_open=300,
    )
    current_market = market_at_elapsed(20)
    subject.set_market(current_market, NOW)
    seed_history(subject, NOW, up_start=0.55, up_step=0.0)
    leader = {"UP": 80.0, "DOWN": 20.0}

    def fake_components(direction, books, now, remaining, sigma_long):
        return {"test_direction": direction.value}

    monkeypatch.setattr(subject, "_direction_components", fake_components)
    monkeypatch.setattr(
        subject,
        "_score",
        lambda components: leader[components["test_direction"]],
    )

    def confirm_at(elapsed: float) -> None:
        observed_at = current_market.start_time + timedelta(seconds=elapsed)
        for advance in (0.0, 0.2):
            tick_at = observed_at + timedelta(seconds=advance)
            subject.add_chainlink_tick(
                PriceTick(source="chainlink", price=100.0, received_at=tick_at)
            )
            subject.evaluate(
                current_market,
                {
                    Direction.UP: book(Direction.UP, 0.55, tick_at),
                    Direction.DOWN: book(Direction.DOWN, 0.45, tick_at),
                },
                tick_at,
            )

    confirm_at(20)
    leader.update({"UP": 20.0, "DOWN": 80.0})
    confirm_at(100)
    confirm_at(230)

    buys = [attempt for attempt in subject.registry.recent_attempts() if attempt.side == "BUY"]
    assert len(buys) == 6
    assert subject.current_round.entry_count == 3
    assert subject.current_round.used_entry_segments == ["early", "middle", "late"]
    assert {(attempt.entry_segment, attempt.direction) for attempt in buys} == {
        ("early", Direction.UP),
        ("middle", Direction.DOWN),
        ("late", Direction.DOWN),
    }
    assert {
        (attempt.entry_segment, attempt.requested_quote) for attempt in buys
    } == {("early", 1.0), ("middle", 3.0), ("late", 5.0)}

    confirm_at(240)
    assert len([attempt for attempt in subject.registry.recent_attempts() if attempt.side == "BUY"]) == 6


def test_reversal_sequence_uses_each_enabled_amount_only_after_direction_flip(
    tmp_path, monkeypatch
) -> None:
    subject = engine(
        tmp_path,
        reversal_sequence_enabled=True,
        entry_score_threshold=70,
        entry_lead_points=0,
        entry_confirmation_seconds=0.1,
        entry_confirmation_updates=1,
        score_exit_end_seconds_after_open=300,
    )
    current_market = market_at_elapsed(20)
    subject.set_market(current_market, NOW)
    seed_history(subject, NOW, up_start=0.55, up_step=0.0)
    leader = {"UP": 80.0, "DOWN": 20.0}

    monkeypatch.setattr(
        subject,
        "_direction_components",
        lambda direction, books, now, remaining, sigma_long: {
            "test_direction": direction.value
        },
    )
    monkeypatch.setattr(
        subject,
        "_score",
        lambda components: leader[components["test_direction"]],
    )

    def confirm_at(elapsed: float) -> None:
        observed_at = current_market.start_time + timedelta(seconds=elapsed)
        for advance in (0.0, 0.2):
            tick_at = observed_at + timedelta(seconds=advance)
            subject.add_chainlink_tick(
                PriceTick(source="chainlink", price=100.0, received_at=tick_at)
            )
            subject.evaluate(
                current_market,
                {
                    Direction.UP: book(Direction.UP, 0.55, tick_at),
                    Direction.DOWN: book(Direction.DOWN, 0.45, tick_at),
                },
                tick_at,
            )

    confirm_at(20)
    assert subject.current_round.used_entry_segments == ["early"]
    assert subject.current_round.last_entry_direction == Direction.UP

    confirm_at(100)
    assert subject.current_round.used_entry_segments == ["early"]
    assert subject.reversal_sequence_state["state"] == "waiting_for_score_reversal"

    leader.update({"UP": 20.0, "DOWN": 80.0})
    confirm_at(101)
    assert subject.current_round.used_entry_segments == ["early", "middle"]
    assert subject.current_round.last_entry_direction == Direction.DOWN

    confirm_at(230)
    assert subject.current_round.used_entry_segments == ["early", "middle"]

    leader.update({"UP": 80.0, "DOWN": 20.0})
    confirm_at(231)

    buys = [attempt for attempt in subject.registry.recent_attempts() if attempt.side == "BUY"]
    assert len(buys) == 6
    assert subject.current_round.used_entry_segments == ["early", "middle", "late"]
    assert subject.current_round.last_entry_direction == Direction.UP
    assert subject.reversal_sequence_state["state"] == "complete"
    assert {
        (attempt.entry_segment, attempt.direction, attempt.requested_quote)
        for attempt in buys
    } == {
        ("early", Direction.UP, 1.0),
        ("middle", Direction.DOWN, 3.0),
        ("late", Direction.UP, 5.0),
    }
    middle_details = next(
        attempt.decision_details for attempt in buys if attempt.entry_segment == "middle"
    )
    assert middle_details["entry_mode"] == "reversal_sequence"
    assert middle_details["previous_entry_direction"] == "UP"
    assert middle_details["reversal_recognized_direction"] == "DOWN"


def test_reversal_recognition_cancels_when_leader_switches_back(
    tmp_path, monkeypatch
) -> None:
    subject = engine(
        tmp_path,
        reversal_sequence_enabled=True,
        entry_score_threshold=70,
        entry_lead_points=0,
        entry_confirmation_seconds=0.5,
        entry_confirmation_updates=1,
    )
    current_market = market_at_elapsed(20)
    subject.set_market(current_market, NOW)
    seed_history(subject, NOW, up_start=0.55, up_step=0.0)
    leader = {"UP": 80.0, "DOWN": 20.0}
    monkeypatch.setattr(
        subject,
        "_direction_components",
        lambda direction, books, now, remaining, sigma_long: {
            "test_direction": direction.value
        },
    )
    monkeypatch.setattr(
        subject,
        "_score",
        lambda components: leader[components["test_direction"]],
    )

    def evaluate_at(elapsed: float) -> None:
        observed_at = current_market.start_time + timedelta(seconds=elapsed)
        subject.add_chainlink_tick(
            PriceTick(source="chainlink", price=100.0, received_at=observed_at)
        )
        subject.evaluate(
            current_market,
            {
                Direction.UP: book(Direction.UP, 0.55, observed_at),
                Direction.DOWN: book(Direction.DOWN, 0.45, observed_at),
            },
            observed_at,
        )

    evaluate_at(20)
    evaluate_at(20.6)
    assert subject.current_round.used_entry_segments == ["early"]

    leader.update({"UP": 20.0, "DOWN": 60.0})
    evaluate_at(29)
    assert subject.reversal_sequence_state["recognized_direction"] == "DOWN"
    assert subject.candidates["DOWN"]["reason"] == "entry_score_below_threshold"
    assert "entry:middle:DOWN" not in subject.confirmations
    assert subject.current_round.used_entry_segments == ["early"]

    leader.update({"UP": 20.0, "DOWN": 80.0})
    evaluate_at(30)
    assert "entry:middle:DOWN" in subject.confirmations
    assert subject.reversal_sequence_state["recognized_direction"] == "DOWN"

    leader.update({"UP": 80.0, "DOWN": 20.0})
    evaluate_at(30.2)
    assert "entry:middle:DOWN" not in subject.confirmations
    assert subject.reversal_sequence_state["state"] == "waiting_for_score_reversal"
    assert subject.current_round.used_entry_segments == ["early"]

    leader.update({"UP": 20.0, "DOWN": 80.0})
    evaluate_at(31)
    evaluate_at(31.6)
    assert subject.current_round.used_entry_segments == ["early", "middle"]


def test_reversal_sequence_skips_disabled_slots_and_handles_no_enabled_slots(
    tmp_path, monkeypatch
) -> None:
    segments = [
        {"id": "early", "enabled": False, "duration_seconds": 80, "quote_amount_usd": 1},
        {"id": "middle", "enabled": True, "duration_seconds": 140, "quote_amount_usd": 3},
        {"id": "late", "enabled": True, "duration_seconds": 80, "quote_amount_usd": 5},
    ]
    subject = engine(
        tmp_path,
        reversal_sequence_enabled=True,
        entry_segments=segments,
        entry_score_threshold=0,
        entry_lead_points=0,
        entry_confirmation_seconds=0.1,
        entry_confirmation_updates=1,
    )
    current_market = market_at_elapsed(20)
    subject.set_market(current_market, NOW)
    seed_history(subject, NOW, up_start=0.55, up_step=0.0)
    monkeypatch.setattr(
        subject,
        "_direction_components",
        lambda direction, books, now, remaining, sigma_long: {
            "test_direction": direction.value
        },
    )
    monkeypatch.setattr(
        subject,
        "_score",
        lambda components: 80.0 if components["test_direction"] == "UP" else 20.0,
    )
    for advance in (0.0, 0.2):
        observed_at = NOW + timedelta(seconds=advance)
        subject.add_chainlink_tick(
            PriceTick(source="chainlink", price=100.0, received_at=observed_at)
        )
        subject.evaluate(
            current_market,
            {
                Direction.UP: book(Direction.UP, 0.55, observed_at),
                Direction.DOWN: book(Direction.DOWN, 0.45, observed_at),
            },
            observed_at,
        )
    assert subject.current_round.used_entry_segments == ["middle"]
    assert {
        attempt.requested_quote
        for attempt in subject.registry.recent_attempts()
        if attempt.side == "BUY"
    } == {3.0}

    disabled = engine(
        tmp_path / "disabled",
        reversal_sequence_enabled=True,
        entry_segments=[{**segment, "enabled": False} for segment in segments],
    )
    disabled.set_market(current_market, NOW)
    disabled._refresh_reversal_sequence_state(Direction.UP)
    disabled._refresh_entry_segment_state(NOW)
    assert disabled.reversal_sequence_state["state"] == "no_enabled_segments"
    assert disabled.entry_segment_state["active_id"] is None


def test_reversal_leader_requires_strict_unequal_scores() -> None:
    assert BtcWeightedEngine._score_leader(80.0, 20.0) == Direction.UP
    assert BtcWeightedEngine._score_leader(20.0, 80.0) == Direction.DOWN
    assert BtcWeightedEngine._score_leader(50.0, 50.0) is None
    assert BtcWeightedEngine._score_leader(None, 50.0) is None


def test_entry_confirmation_resets_at_segment_boundary(tmp_path) -> None:
    subject = engine(
        tmp_path,
        entry_score_threshold=0,
        entry_lead_points=0,
        entry_confirmation_seconds=0.2,
        entry_confirmation_updates=1,
    )
    current_market = market_at_elapsed(79.9)
    subject.set_market(current_market, NOW)
    books = seed_history(subject, NOW, up_start=0.55, up_step=0.0)
    subject.evaluate(current_market, books, NOW)
    assert any(key.startswith("entry:early:") for key in subject.confirmations)

    across_boundary = NOW + timedelta(seconds=0.2)
    subject.add_chainlink_tick(
        PriceTick(source="chainlink", price=100.1, received_at=across_boundary)
    )
    books = {
        Direction.UP: book(Direction.UP, 0.56, across_boundary),
        Direction.DOWN: book(Direction.DOWN, 0.44, across_boundary),
    }
    subject.evaluate(current_market, books, across_boundary)
    assert not any(key.startswith("entry:early:") for key in subject.confirmations)
    assert any(key.startswith("entry:middle:") for key in subject.confirmations)
    assert not subject.registry.recent_attempts()

    confirmed_at = across_boundary + timedelta(seconds=0.21)
    subject.add_chainlink_tick(
        PriceTick(source="chainlink", price=100.1, received_at=confirmed_at)
    )
    subject.evaluate(
        current_market,
        {
            Direction.UP: book(Direction.UP, 0.56, confirmed_at),
            Direction.DOWN: book(Direction.DOWN, 0.44, confirmed_at),
        },
        confirmed_at,
    )
    buys = [attempt for attempt in subject.registry.recent_attempts() if attempt.side == "BUY"]
    assert len(buys) == 2
    assert {attempt.entry_segment for attempt in buys} == {"middle"}
    assert {attempt.requested_quote for attempt in buys} == {3.0}


def test_disabled_segment_never_confirms_or_submits(tmp_path) -> None:
    subject = engine(
        tmp_path,
        entry_segments=[
            {"id": "early", "enabled": False, "duration_seconds": 80, "quote_amount_usd": 1},
            {"id": "middle", "enabled": True, "duration_seconds": 140, "quote_amount_usd": 3},
            {"id": "late", "enabled": True, "duration_seconds": 80, "quote_amount_usd": 5},
        ],
        entry_score_threshold=0,
        entry_lead_points=0,
        entry_confirmation_seconds=0.1,
        entry_confirmation_updates=1,
    )
    current_market = market_at_elapsed(20)
    subject.set_market(current_market, NOW)
    books = seed_history(subject, NOW, up_start=0.55, up_step=0.0)
    subject.evaluate(current_market, books, NOW)
    later = NOW + timedelta(seconds=0.2)
    subject.add_chainlink_tick(
        PriceTick(source="chainlink", price=100.1, received_at=later)
    )
    subject.evaluate(
        current_market,
        {
            Direction.UP: book(Direction.UP, 0.56, later),
            Direction.DOWN: book(Direction.DOWN, 0.44, later),
        },
        later,
    )

    assert subject.last_reason == "entry_segment_disabled"
    assert subject.entry_segment_state["current"]["status"] == "disabled"
    assert not any(key.startswith("entry:early:") for key in subject.confirmations)
    assert not subject.registry.recent_attempts()
    assert subject.current_round.used_entry_segments == []


def test_restart_infers_used_segment_from_legacy_attempt(tmp_path) -> None:
    current_market = market_at_elapsed(20)
    registry = BtcWeightedRegistry(tmp_path / "legacy-weighted.sqlite3")
    settings = AppConfig(data_dir=tmp_path, btc_weighted={"enabled": True}).btc_weighted
    registry.save_round(
        WeightedRound(
            market_id=current_market.condition_id,
            market_slug=current_market.slug,
            start_time=current_market.start_time,
            end_time=current_market.end_time,
            settings=settings,
            entry_count=1,
        )
    )
    registry.save_attempt(
        WeightedAttempt(
            idempotency_key="legacy-buy-observed",
            market_id=current_market.condition_id,
            market_slug=current_market.slug,
            lane="observed",
            side="BUY",
            direction=Direction.UP,
            token_id=current_market.up_token_id,
            requested_quote=5,
            limit_price=0.9,
            requested_at=NOW - timedelta(seconds=5),
        )
    )

    subject = BtcWeightedEngine(
        AppConfig(data_dir=tmp_path, btc_weighted={"enabled": True}),
        registry,
        FakeLatency(),
    )
    subject.set_market(current_market, NOW)

    assert subject.current_round.used_entry_segments == ["early"]
    assert subject.current_round.last_entry_direction == Direction.UP
    assert subject.entry_segment_state["segments"][0]["status"] == "submitted"


def test_reverse_exit_retries_when_sell_depth_is_missing(tmp_path) -> None:
    subject = engine(
        tmp_path,
        min_hold_seconds=0.1,
        exit_confirmation_seconds=0.1,
        exit_confirmation_updates=1,
        score_exit_end_seconds_after_open=300,
    )
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
    assert position.exit_reason is None
    assert position.exit_intent_active is True
    assert position.last_exit_intent_reason == "sell_depth_unavailable"
    fresh = book(Direction.UP, 0.55, NOW + timedelta(seconds=1), depth=100)
    subject._evaluate_positions({Direction.UP: fresh}, NOW + timedelta(seconds=1), "third")
    assert position.status == "EXIT_PENDING"
    attempt = subject.registry.recent_attempts()[0]
    assert attempt.position_id == position.position_id
    assert attempt.decision_details["opposite_score"] == pytest.approx(80)
    assert attempt.decision_details["effective_exit_price_cents"] == pytest.approx(54)


def test_exit_intent_is_cancelled_when_reversal_disappears(tmp_path) -> None:
    subject = engine(
        tmp_path,
        min_hold_seconds=0.1,
        exit_confirmation_seconds=0.1,
        exit_confirmation_updates=1,
        score_exit_end_seconds_after_open=300,
    )
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
    subject._confirmation(
        f"exit:{position.position_id}", True, "first", NOW - timedelta(seconds=1), 0.1, 1
    )
    subject._evaluate_positions(
        {Direction.UP: book(Direction.UP, 0.55, NOW, depth=1)}, NOW, "second"
    )
    assert position.exit_intent_active is True

    subject.scores = {"UP": 70.0, "DOWN": 60.0}
    subject._evaluate_positions(
        {Direction.UP: book(Direction.UP, 0.55, NOW + timedelta(seconds=1))},
        NOW + timedelta(seconds=1),
        "third",
    )

    assert position.status == "OPEN"
    assert position.exit_intent_active is False
    assert position.last_exit_intent_reason == "exit_signal_cancelled:exit_score_below_threshold"
    assert not [attempt for attempt in subject.registry.recent_attempts() if attempt.side == "SELL"]


def test_exit_intent_expires_before_retry(tmp_path) -> None:
    subject = engine(
        tmp_path,
        min_hold_seconds=0.1,
        exit_confirmation_seconds=0.1,
        exit_confirmation_updates=1,
        score_exit_end_seconds_after_open=300,
        exit_intent_ttl_seconds=3,
    )
    subject.set_market(market(), NOW)
    position = WeightedPosition(
        market_id=market().condition_id,
        market_slug=market().slug,
        lane="p95",
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
    subject._confirmation(
        f"exit:{position.position_id}", True, "first", NOW - timedelta(seconds=1), 0.1, 1
    )
    subject._evaluate_positions(
        {Direction.UP: book(Direction.UP, 0.55, NOW, depth=1)}, NOW, "second"
    )
    subject._evaluate_positions(
        {Direction.UP: book(Direction.UP, 0.55, NOW + timedelta(seconds=4))},
        NOW + timedelta(seconds=4),
        "third",
    )

    assert position.exit_intent_active is False
    assert position.last_exit_intent_reason == "exit_intent_expired"
    assert not [attempt for attempt in subject.registry.recent_attempts() if attempt.side == "SELL"]


def test_score_exit_respects_time_window_and_price_floor(tmp_path) -> None:
    subject = engine(tmp_path)
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
    subject.scores = {"UP": 40.0, "DOWN": 80.0}
    late = subject._exit_signal_details(
        position,
        book(Direction.UP, 0.55, NOW),
        NOW,
    )
    assert late["condition"] is False
    assert late["reason"] == "score_exit_window_closed"

    subject.current_round.settings.score_exit_end_seconds_after_open = 300
    cheap = subject._exit_signal_details(
        position,
        book(Direction.UP, 0.20, NOW),
        NOW,
    )
    assert cheap["condition"] is False
    assert cheap["reason"] == "score_exit_price_below_floor"


def test_sell_attempt_closes_exact_position_id(tmp_path) -> None:
    subject = engine(tmp_path)
    subject.set_market(market(), NOW)
    positions = []
    for entry in ("first", "second"):
        position = WeightedPosition(
            market_id=market().condition_id,
            market_slug=market().slug,
            lane="observed",
            direction=Direction.UP,
            token_id="up-token",
            entry_attempt_id=entry,
            entry_price=0.5,
            quantity=10,
            entry_quote=5,
            entry_fee_usd=0,
            entry_score=80,
            opened_at=NOW - timedelta(seconds=20),
        )
        subject.positions[position.position_id] = position
        positions.append(position)
    attempt = WeightedAttempt(
        idempotency_key="exact-position",
        market_id=market().condition_id,
        market_slug=market().slug,
        lane="observed",
        side="SELL",
        direction=Direction.UP,
        token_id="up-token",
        position_id=positions[1].position_id,
        requested_quantity=10,
        limit_price=0.5,
    )
    execution = ExecutionResult(
        complete=True,
        avg_price=0.6,
        quantity=10,
        quote=6,
        fee_usd=0,
        slippage=0,
        best_price=0.6,
        available_depth_quote=6,
        levels_used=1,
    )

    subject._close_position(attempt, execution, NOW)

    assert positions[0].status == "OPEN"
    assert positions[1].status == "CLOSED"
    assert positions[1].realized_pnl == pytest.approx(1)


def test_config_performance_is_versioned_and_applies_p95_risk_pause(tmp_path) -> None:
    registry = BtcWeightedRegistry(tmp_path / "performance.sqlite3")
    settings = AppConfig(
        data_dir=tmp_path,
        btc_weighted={"enabled": True, "loss_streak_pause_count": 3},
    ).btc_weighted
    other_settings = settings.model_copy(update={"entry_score_threshold": 71})
    version = weighted_config_version(settings)
    other_version = weighted_config_version(other_settings)
    for market_id, market_slug, config, config_version in (
        ("current-market", "current-slug", settings, version),
        ("other-market", "other-slug", other_settings, other_version),
    ):
        registry.save_round(
            WeightedRound(
                market_id=market_id,
                market_slug=market_slug,
                start_time=NOW - timedelta(minutes=5),
                end_time=NOW,
                settings=config,
                config_version=config_version,
            )
        )
    for index in range(3):
        registry.save_position(
            WeightedPosition(
                market_id="current-market",
                market_slug="current-slug",
                lane="p95",
                direction=Direction.UP,
                token_id="up",
                config_version=version,
                status="SETTLED",
                entry_attempt_id=f"entry-{index}",
                entry_price=0.5,
                quantity=2,
                entry_quote=1,
                entry_fee_usd=0,
                entry_score=70,
                opened_at=NOW - timedelta(minutes=10 - index),
                closed_at=NOW - timedelta(minutes=3 - index),
                exit_reason="official_settlement",
                realized_pnl=-1,
            )
        )
    registry.save_position(
        WeightedPosition(
            market_id="other-market",
            market_slug="other-slug",
            lane="p95",
            direction=Direction.UP,
            token_id="up",
            config_version=other_version,
            status="SETTLED",
            entry_attempt_id="other-entry",
            entry_price=0.5,
            quantity=200,
            entry_quote=100,
            entry_fee_usd=0,
            entry_score=70,
            opened_at=NOW - timedelta(minutes=5),
            closed_at=NOW,
            exit_reason="official_settlement",
            realized_pnl=100,
        )
    )

    performance = registry.config_performance(version, settings, NOW)

    assert performance["markets"] == 1
    assert performance["lanes"]["p95"]["completed"] == 3
    assert performance["lanes"]["p95"]["realized_pnl"] == pytest.approx(-3)
    assert performance["lanes"]["p95"]["max_drawdown"] == pytest.approx(3)
    assert performance["risk"]["paused"] is True
    assert performance["risk"]["reason"] == "loss_streak_pause"


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
