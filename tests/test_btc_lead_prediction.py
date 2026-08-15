from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from polybtc.btc_lead_prediction import (
    BtcLeadPredictionEngine,
    BtcLeadPredictionRegistry,
    LeadAttempt,
    LeadPrediction,
    LeadPosition,
    LeadShock,
    weighted_median,
)
from polybtc.config import AppConfig
from polybtc.models import BookLevel, Direction, MarketState, OrderBookSnapshot, PriceTick
from polybtc.orderbook import simulate_buy
from polybtc.signal_sources import SignalEvent


NOW = datetime(2026, 8, 12, 12, 0, 0, tzinfo=timezone.utc)


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
            "sample_count": 50 if self.usable else 0,
        }


def market(now: datetime = NOW) -> MarketState:
    return MarketState(
        condition_id="lead-market",
        slug="btc-updown-5m-lead",
        question="BTC up or down",
        threshold_price=100.0,
        threshold_source="polymarket_twap_page",
        threshold_verified=True,
        start_time=now - timedelta(seconds=100),
        end_time=now + timedelta(seconds=200),
        up_token_id="up-token",
        down_token_id="down-token",
        min_order_size=5.0,
        tick_size=0.01,
    )


def book(
    direction: Direction,
    observed_at: datetime,
    bid: float = 0.54,
    ask: float = 0.55,
    size: float = 100.0,
) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        token_id="up-token" if direction == Direction.UP else "down-token",
        market_id="lead-market",
        timestamp=observed_at,
        received_at=observed_at,
        bids=[BookLevel(price=bid, size=size)],
        asks=[BookLevel(price=ask, size=size)],
        depth_trusted=True,
        min_order_size=5.0,
        tick_size=0.01,
    )


def engine(tmp_path, **settings) -> BtcLeadPredictionEngine:
    config = AppConfig(
        data_dir=tmp_path,
        btc_lead_prediction={"enabled": True, **settings},
    )
    registry = BtcLeadPredictionRegistry(tmp_path / "lead.sqlite3")
    subject = BtcLeadPredictionEngine(config, registry, FakeLatency())
    subject.set_market(market(), NOW)
    return subject


def spot_book_event(source: str, observed_at: datetime, mid: float) -> SignalEvent:
    return SignalEvent(
        source=source,
        market_type="spot",
        kind="book",
        bids=[BookLevel(price=mid - 0.01, size=2.0)],
        asks=[BookLevel(price=mid + 0.01, size=2.0)],
        exchange_timestamp=observed_at,
        received_at=observed_at,
    )


def test_default_config_and_validation() -> None:
    settings = AppConfig().btc_lead_prediction
    assert settings.enabled is False
    assert settings.spot_sources == ["binance", "coinbase", "kraken"]
    assert settings.prediction_horizons_seconds == [1, 3, 5, 8]
    assert settings.quote_amount_usd == 1.0
    assert settings.max_hold_seconds == 8.0
    with pytest.raises(ValidationError):
        AppConfig(btc_lead_prediction={"prediction_horizons_seconds": [1, 3, 8]})
    with pytest.raises(ValidationError):
        AppConfig(
            btc_lead_prediction={
                "spot_sources": ["binance"],
                "min_healthy_sources": 2,
            }
        )


def test_weighted_median_resists_low_weight_outlier() -> None:
    assert weighted_median([(100.0, 45.0), (100.1, 45.0), (110.0, 10.0)]) == 100.1
    assert weighted_median([]) is None


def test_exchange_time_reversal_invalidates_source(tmp_path) -> None:
    subject = engine(tmp_path)
    subject.add_chainlink_tick(PriceTick(source="chainlink", price=100.0, received_at=NOW))
    subject.add_signal(spot_book_event("binance", NOW, 100.0))
    subject.add_signal(spot_book_event("binance", NOW - timedelta(milliseconds=1), 100.1))
    snapshot = subject._source_snapshot(NOW)
    assert snapshot["binance"]["healthy"] is False
    assert snapshot["binance"]["reason"] == "exchange_time_reversed"


def test_source_snapshot_tolerates_small_exchange_clock_lead(tmp_path) -> None:
    subject = engine(tmp_path)
    subject.add_chainlink_tick(PriceTick(source="chainlink", price=100.0, received_at=NOW))
    event = spot_book_event("binance", NOW + timedelta(milliseconds=200), 100.0)
    event.received_at = NOW
    subject.add_signal(event)
    snapshot = subject._source_snapshot(NOW)
    assert snapshot["binance"]["healthy"] is True
    assert snapshot["binance"]["reason"] == "healthy"


def test_asof_sample_does_not_select_newer_observation() -> None:
    history = deque(
        [
            (NOW - timedelta(milliseconds=600), 100.0),
            (NOW - timedelta(milliseconds=100), 101.0),
        ]
    )
    sample = BtcLeadPredictionEngine._sample_at_or_before(
        history, NOW - timedelta(milliseconds=500), 0.2
    )
    assert sample == history[0]


def test_shock_signal_uses_distinct_asof_history_samples(tmp_path) -> None:
    subject = engine(
        tmp_path,
        shock_threshold_bps=0.1,
        min_healthy_sources=1,
        max_source_dispersion_bps=10.0,
    )
    subject._append_history(
        subject.external_history,
        (NOW - timedelta(milliseconds=600), 100.0),
        3700.0,
    )
    subject._append_history(
        subject.external_history,
        (NOW - timedelta(milliseconds=100), 100.1),
        3700.0,
    )
    signal = subject._shock_signal(
        {
            "binance": {
                "healthy": True,
                "velocity_bps_per_second": 20.0,
            }
        },
        100.1,
        0.0,
        NOW,
    )
    assert signal["condition"] is True
    assert signal["reason"] == "shock_candidate"
    assert signal["change_bps"] > 0.1


def test_projection_uses_30_second_replacement_window(tmp_path) -> None:
    subject = engine(tmp_path)
    for step in range(-124, 1):
        offset = step * 0.25
        observed = NOW + timedelta(seconds=offset)
        subject.add_chainlink_tick(
            PriceTick(source="chainlink", price=100.0, received_at=observed)
        )
        subject._append_history(
            subject.external_history,
            (observed, 100.0 + max(offset + 2, 0) * 0.01),
            3700.0,
        )
    projection = subject._projection(NOW, subject.external_history[-1][1])
    assert projection["horizons"]["3"]["ready"] is True
    assert projection["horizons"]["5"]["predicted_twap"] > 100.0
    assert projection["horizons"]["8"]["valid_until"].endswith("+00:00")


def test_prediction_actuals_are_written_only_after_expiry(tmp_path) -> None:
    subject = engine(tmp_path)
    subject.add_chainlink_tick(PriceTick(source="chainlink", price=100.0, received_at=NOW))
    prediction = LeadPrediction(
        market_id="lead-market",
        market_slug="btc-updown-5m-lead",
        config_version=subject.current_round.config_version,
        created_at=NOW,
        external_index=100.1,
        chainlink_price=100.0,
        projections={
            "1": {
                "predicted_twap": 100.1,
                "raw_twap_delta": 0.1,
                "up_probability_change_points": 2.0,
            }
        },
        initial_books={"UP": {"bid": 0.50}, "DOWN": {"bid": 0.50}},
        source_prices={"binance": 100.1},
    )
    subject.registry.save_prediction(prediction)
    subject.pending_predictions[prediction.prediction_id] = prediction
    subject._mature_predictions(NOW + timedelta(milliseconds=999))
    assert prediction.actuals == {}
    due = NOW + timedelta(seconds=1)
    subject.add_chainlink_tick(PriceTick(source="chainlink", price=100.08, received_at=due))
    subject.add_book(Direction.UP, book(Direction.UP, due, bid=0.56, ask=0.57))
    subject.add_book(Direction.DOWN, book(Direction.DOWN, due, bid=0.43, ask=0.44))
    subject._mature_predictions(due)
    assert prediction.actuals["1"]["status"] == "matured"
    assert prediction.actuals["1"]["chainlink_price"] == pytest.approx(100.08)


def test_shock_confirmation_creates_one_unique_event(tmp_path) -> None:
    subject = engine(tmp_path, confirmation_seconds=0.8, confirmation_updates=3)
    signal = {
        "condition": True,
        "direction": Direction.UP,
        "change_bps": 2.0,
        "velocity_bps_per_second": 4.0,
        "acceleration_bps_per_second2": 1.0,
        "supporting_sources": ["binance", "coinbase"],
    }
    subject._update_shock(signal, NOW, "1")
    subject._update_shock(signal, NOW + timedelta(seconds=0.4), "2")
    subject._update_shock(signal, NOW + timedelta(seconds=0.8), "3")
    first_id = subject.active_shock.shock_id
    subject._update_shock(signal, NOW + timedelta(seconds=0.9), "4")
    assert subject.active_shock.shock_id == first_id
    subject.active_shock.expires_at = NOW + timedelta(seconds=0.85)
    subject._update_shock(signal, NOW + timedelta(seconds=0.95), "5")
    assert subject.active_shock is None
    assert len(subject.registry.recent_shocks()) == 1


def test_duplicate_market_timestamp_does_not_advance_shock_confirmation(tmp_path) -> None:
    subject = engine(tmp_path, confirmation_seconds=0.1, confirmation_updates=2)
    signal = {
        "condition": True,
        "direction": Direction.UP,
        "change_bps": 2.0,
        "supporting_sources": ["binance", "coinbase"],
    }
    subject._update_shock(signal, NOW, "external:same")
    subject._update_shock(signal, NOW + timedelta(seconds=1), "external:same")
    assert subject.shock_confirmation["updates"] == 1
    assert subject.active_shock is None


def test_one_dollar_buy_creates_observed_and_p95_even_below_book_minimum(tmp_path) -> None:
    subject = engine(tmp_path)
    initial = book(Direction.UP, NOW, bid=0.54, ask=0.55)
    subject.add_book(Direction.UP, initial)
    execution = simulate_buy(initial.model_copy(deep=True), 1.0, subject.config.strategy.taker_fee_rate)
    shock = LeadShock(
        market_id="lead-market",
        market_slug="btc-updown-5m-lead",
        direction=Direction.UP,
        started_at=NOW - timedelta(seconds=1),
        confirmed_at=NOW,
        peak_at=NOW,
        expires_at=NOW + timedelta(seconds=8),
        amplitude_bps=2.0,
        velocity_bps_per_second=4.0,
        acceleration_bps_per_second2=1.0,
        sources=["binance", "coinbase"],
    )
    candidate = {
        "eligible": True,
        "limit_price": 0.55,
        "execution": execution,
        "simulated_quantity": execution.quantity,
        "book_min_order_size": 5.0,
        "below_book_min_order_size": True,
        "predicted_bid": 0.62,
    }
    subject._create_buy_attempts(shock, candidate, NOW)
    attempts = subject.registry.recent_attempts()
    assert {item.lane for item in attempts} == {"observed", "p95"}
    assert all(item.requested_quote == 1.0 for item in attempts)
    assert all(item.expected_quantity < 5.0 for item in attempts)
    newer = book(
        Direction.UP,
        NOW + timedelta(milliseconds=1),
        bid=0.54,
        ask=0.55,
    )
    subject.add_book(Direction.UP, newer)
    subject._resolve_pending(NOW + timedelta(milliseconds=1))
    assert len(subject.positions) == 2
    assert all(item.entry_quote == pytest.approx(1.0) for item in subject.positions.values())


def test_entry_candidate_adds_configured_buy_limit_buffer(tmp_path) -> None:
    subject = engine(tmp_path, buy_limit_buffer_cents=2.0)
    initial = book(Direction.UP, NOW, bid=0.54, ask=0.55)
    subject.add_book(Direction.UP, initial)
    subject.add_chainlink_tick(PriceTick(source="chainlink", price=100.0, received_at=NOW))
    subject.latest_projection = {
        "horizons": {
            "3": {"predicted_twap": 100.1},
            "5": {"predicted_twap": 100.2},
        }
    }
    subject.latest_book_projection["UP"] = {
        "response_coefficient": 1.0,
        "expected_reprice_cents": 5.0,
        "predicted_bid": 0.60,
        "p95_predicted_bid": 0.60,
    }

    candidate = subject._entry_candidate(Direction.UP, NOW)

    assert candidate["book_limit_price"] == pytest.approx(0.55)
    assert candidate["buy_limit_buffer_cents"] == pytest.approx(2.0)
    assert candidate["limit_price"] == pytest.approx(0.57)


def test_buy_limit_buffer_is_capped_below_one_dollar(tmp_path) -> None:
    subject = engine(tmp_path, buy_limit_buffer_cents=5.0)
    initial = book(Direction.UP, NOW, bid=0.97, ask=0.98)
    subject.add_book(Direction.UP, initial)
    subject.add_chainlink_tick(PriceTick(source="chainlink", price=100.0, received_at=NOW))
    subject.latest_projection = {
        "horizons": {
            "3": {"predicted_twap": 100.1},
            "5": {"predicted_twap": 100.2},
        }
    }
    subject.latest_book_projection["UP"] = {
        "response_coefficient": 1.0,
        "expected_reprice_cents": 5.0,
        "predicted_bid": 0.99,
        "p95_predicted_bid": 0.99,
    }

    candidate = subject._entry_candidate(Direction.UP, NOW)

    assert candidate["limit_price"] == pytest.approx(0.99)


def test_unready_latency_probe_does_not_consume_shock_or_trade_slot(tmp_path) -> None:
    subject = engine(tmp_path)
    subject.latency = FakeLatency(usable=False)
    initial = book(Direction.UP, NOW, bid=0.54, ask=0.55)
    subject.add_book(Direction.UP, initial)
    execution = simulate_buy(initial.model_copy(deep=True), 1.0, subject.config.strategy.taker_fee_rate)
    shock = LeadShock(
        market_id="lead-market",
        market_slug="btc-updown-5m-lead",
        direction=Direction.UP,
        started_at=NOW - timedelta(seconds=1),
        confirmed_at=NOW,
        peak_at=NOW,
        expires_at=NOW + timedelta(seconds=8),
        amplitude_bps=2.0,
        velocity_bps_per_second=4.0,
        acceleration_bps_per_second2=1.0,
        sources=["binance", "coinbase"],
    )
    candidate = {
        "eligible": True,
        "limit_price": 0.55,
        "execution": execution,
    }

    subject._create_buy_attempts(shock, candidate, NOW)

    assert subject.registry.recent_attempts() == []
    assert subject.current_round.trade_count == 0
    assert subject.current_round.used_shock_ids == []
    assert shock.submitted is False
    assert subject.status == "waiting_for_latency_probe"
    assert subject.last_reason == "latency_probe_warming"


def test_entry_candidate_does_not_require_entire_adverse_interval_in_direction(tmp_path) -> None:
    subject = engine(
        tmp_path,
        min_expected_reprice_cents=0.1,
        min_p95_net_edge_cents=0.1,
        target_net_profit_cents=1.0,
    )
    subject.add_chainlink_tick(PriceTick(source="chainlink", price=100.0, received_at=NOW))
    subject.add_book(Direction.DOWN, book(Direction.DOWN, NOW, bid=0.54, ask=0.55))
    subject.latest_projection = {
        "horizons": {
            "3": {"ready": True, "predicted_twap": 100.1},
            "5": {
                "ready": True,
                "predicted_twap": 99.9,
                "upper_90": 100.1,
            },
        }
    }
    subject.latest_book_projection[Direction.DOWN.value] = {
        "response_coefficient": 1.0,
        "expected_reprice_cents": 10.0,
        "predicted_bid": 0.65,
        "p95_predicted_bid": 0.65,
    }
    candidate = subject._entry_candidate(Direction.DOWN, NOW)
    assert candidate["adverse_interval_direction"] is False
    assert "adverse_interval_direction" not in candidate["gates"]
    assert candidate["eligible"] is True


def test_shadow_entry_keeps_horizon_and_p95_edge_as_nonblocking_diagnostics(tmp_path) -> None:
    subject = engine(
        tmp_path,
        min_expected_reprice_cents=0.1,
        min_p95_net_edge_cents=0.1,
        target_net_profit_cents=1.0,
    )
    subject.add_chainlink_tick(PriceTick(source="chainlink", price=100.0, received_at=NOW))
    subject.add_book(Direction.UP, book(Direction.UP, NOW, bid=0.54, ask=0.55))
    subject.latest_projection = {
        "horizons": {
            "3": {"ready": True, "predicted_twap": 100.1},
            "5": {
                "ready": True,
                "predicted_twap": 99.9,
                "lower_90": 99.8,
            },
        }
    }
    subject.latest_book_projection[Direction.UP.value] = {
        "response_coefficient": 1.0,
        "expected_reprice_cents": 1.0,
        "predicted_bid": 0.60,
        "p95_predicted_bid": 0.54,
    }

    candidate = subject._entry_candidate(Direction.UP, NOW)

    assert candidate["gates"]["horizon_direction"] is False
    assert candidate["gates"]["p95_net_edge"] is False
    assert candidate["diagnostic_gates"] == {
        "horizon_direction": False,
        "p95_net_edge": False,
    }
    assert "horizon_direction" not in candidate["blocking_gates"]
    assert "p95_net_edge" not in candidate["blocking_gates"]
    assert candidate["eligible"] is True
    assert candidate["failed_gate"] is None
    assert candidate["gates"]["expected_net_edge"] is True

    subject.latest_book_projection[Direction.UP.value]["expected_reprice_cents"] = 0.0
    blocked = subject._entry_candidate(Direction.UP, NOW)
    assert blocked["eligible"] is False
    assert blocked["failed_gate"] == "expected_reprice"


def test_entry_requires_central_prediction_to_cover_costs_and_profit_target(tmp_path) -> None:
    subject = engine(
        tmp_path,
        min_expected_reprice_cents=0.1,
        target_net_profit_cents=1.0,
    )
    subject.add_chainlink_tick(PriceTick(source="chainlink", price=100.0, received_at=NOW))
    subject.add_book(Direction.UP, book(Direction.UP, NOW, bid=0.50, ask=0.51))
    subject.latest_projection = {
        "horizons": {
            "3": {"ready": True, "predicted_twap": 100.1},
            "5": {"ready": True, "predicted_twap": 100.2, "lower_90": 99.8},
        }
    }
    subject.latest_book_projection[Direction.UP.value] = {
        "response_coefficient": 1.0,
        "expected_reprice_cents": 3.0,
        "predicted_bid": 0.53,
        "p95_predicted_bid": 0.50,
    }

    candidate = subject._entry_candidate(Direction.UP, NOW)

    assert candidate["gates"]["expected_reprice"] is True
    assert candidate["gates"]["expected_net_edge"] is False
    assert candidate["expected_net_edge_cents"] < 1.0
    assert candidate["eligible"] is False
    assert candidate["failed_gate"] == "expected_net_edge"


def test_entry_net_edge_reserves_full_buy_limit_buffer(tmp_path) -> None:
    subject = engine(
        tmp_path,
        buy_limit_buffer_cents=2.0,
        min_expected_reprice_cents=0.1,
        target_net_profit_cents=1.0,
    )
    subject.add_chainlink_tick(PriceTick(source="chainlink", price=100.0, received_at=NOW))
    subject.add_book(Direction.UP, book(Direction.UP, NOW, bid=0.50, ask=0.51))
    subject.latest_projection = {
        "horizons": {
            "3": {"ready": True, "predicted_twap": 100.1},
            "5": {"ready": True, "predicted_twap": 100.2, "lower_90": 99.8},
        }
    }
    subject.latest_book_projection[Direction.UP.value] = {
        "response_coefficient": 1.0,
        "expected_reprice_cents": 6.0,
        "predicted_bid": 0.56,
        "p95_predicted_bid": 0.50,
    }

    candidate = subject._entry_candidate(Direction.UP, NOW)

    assert candidate["limit_price"] == pytest.approx(0.53)
    assert candidate["entry_cost_price"] == pytest.approx(0.53)
    assert candidate["expected_net_edge_cents"] < 1.0
    assert candidate["gates"]["expected_net_edge"] is False
    assert candidate["failed_gate"] == "expected_net_edge"


def test_negative_p95_edge_does_not_force_immediate_exit_but_max_hold_does(tmp_path) -> None:
    subject = engine(tmp_path, max_hold_seconds=8.0)
    observed_at = NOW + timedelta(seconds=1)
    subject.add_chainlink_tick(
        PriceTick(source="chainlink", price=100.0, received_at=observed_at)
    )
    subject.add_book(Direction.UP, book(Direction.UP, observed_at, bid=0.50, ask=0.51))
    subject.latest_projection = {
        "horizons": {"5": {"predicted_twap": 100.1}}
    }
    subject.latest_book_projection[Direction.UP.value] = {
        "p95_predicted_bid": 0.50,
    }
    position = LeadPosition(
        market_id="lead-market",
        market_slug="btc-updown-5m-lead",
        lane="observed",
        direction=Direction.UP,
        token_id="up-token",
        shock_id="negative-p95",
        config_version=subject.current_round.config_version,
        entry_attempt_id="entry-negative-p95",
        entry_price=0.51,
        quantity=10.0,
        entry_quote=5.10,
        entry_fee_usd=0.10,
        predicted_target_bid=0.59,
        predicted_expiry=NOW + timedelta(seconds=5),
        opened_at=NOW,
    )

    signal = subject._exit_signal(position, observed_at)

    assert signal["p95_projected_edge_cents"] < 0
    assert signal["condition"] is False
    assert signal["reason"] is None

    past_max_hold = subject._exit_signal(position, NOW + timedelta(seconds=8.1))
    assert past_max_hold["condition"] is True
    assert past_max_hold["reason"] == "max_hold"

    near_market_end_position = position.model_copy(
        update={"opened_at": NOW + timedelta(seconds=180)}
    )
    near_market_end = subject._exit_signal(
        near_market_end_position, NOW + timedelta(seconds=181)
    )
    assert near_market_end["condition"] is True
    assert near_market_end["reason"] == "force_exit_remaining"


def test_registry_marks_interrupted_intent_unmeasurable(tmp_path) -> None:
    path = tmp_path / "restart.sqlite3"
    registry = BtcLeadPredictionRegistry(path)
    attempt = LeadAttempt(
        idempotency_key="restart-test",
        market_id="lead-market",
        market_slug="btc-updown-5m-lead",
        lane="observed",
        side="BUY",
        direction=Direction.UP,
        token_id="up-token",
        requested_quote=1.0,
        limit_price=0.55,
    )
    registry.save_attempt(attempt)
    registry.close()
    restarted = BtcLeadPredictionRegistry(path)
    restored = restarted.recent_attempts()[0]
    assert restored.status == "UNMEASURABLE"
    assert restored.reason == "process_restarted_before_shadow_result"
