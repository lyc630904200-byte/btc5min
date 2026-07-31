from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from polybtc.btc_dynamic import (
    BtcDynamicEngine,
    BtcDynamicRegistry,
    CURRENT_SNAPSHOT_POLICY_VERSION,
    DynamicModel,
    DynamicRound,
    DynamicSnapshot,
    FEATURE_NAMES,
    LEGACY_FEATURE_SCHEMA_VERSION,
    LEGACY_MODEL_KEY,
    LEGACY_SNAPSHOT_POLICY_VERSION,
    CURRENT_FEATURE_SCHEMA_VERSION,
    V2_MODEL_KEY,
    dynamic_max_price,
    remaining_time_feature,
    snapshot_elapsed_seconds,
    training_snapshot_seconds,
    training_snapshot_slot,
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


def dual_model_engine(
    tmp_path,
    start: datetime,
    *,
    legacy_model: DynamicModel | None = None,
    v2_model: DynamicModel | None = None,
):
    config = AppConfig(
        data_dir=tmp_path,
        btc_dynamic={
            "enabled": True,
            "confirmation_seconds": 0,
            "confirmation_updates": 1,
        },
        risk={"max_data_age_ms": 5000},
    )
    registry = BtcDynamicRegistry(tmp_path / "dynamic.sqlite3")
    registry.save_model(legacy_model or DynamicModel(), LEGACY_MODEL_KEY)
    registry.save_model(
        v2_model
        or DynamicModel(feature_schema_version=CURRENT_FEATURE_SCHEMA_VERSION),
        V2_MODEL_KEY,
    )
    registry.set_control("active_model_key", V2_MODEL_KEY, start)
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
    assert training_snapshot_seconds(settings) == (
        240.0,
        247.5,
        255.0,
        262.5,
        270.0,
    )
    assert training_snapshot_slot(settings, 248.0) == 247.5

    start = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)
    strategy, registry, current = engine(
        tmp_path,
        start,
        entry_seconds_after_open=240,
        exit_seconds_after_open=270,
    )
    assert strategy.current_round is not None
    strategy.model = DynamicModel(
        feature_schema_version=CURRENT_FEATURE_SCHEMA_VERSION
    )
    strategy.current_round.model_key = V2_MODEL_KEY
    strategy.current_round.feature_schema_version = CURRENT_FEATURE_SCHEMA_VERSION
    strategy.current_round.snapshot_policy_version = (
        CURRENT_SNAPSHOT_POLICY_VERSION
    )
    registry.save_model(strategy.model, V2_MODEL_KEY)
    registry.save_round(strategy.current_round)
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
        270.0,
    )
    assert strategy.diagnostics["snapshot_policy_version"] == (
        CURRENT_SNAPSHOT_POLICY_VERSION
    )
    registry.close()


def test_complete_snapshot_slots_store_actual_observation_time(tmp_path) -> None:
    start = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)
    strategy, registry, current = engine(
        tmp_path,
        start,
        entry_seconds_after_open=0,
        exit_seconds_after_open=290,
    )
    assert strategy.current_round is not None
    strategy.model = DynamicModel(
        feature_schema_version=CURRENT_FEATURE_SCHEMA_VERSION
    )
    strategy.current_round.model_key = V2_MODEL_KEY
    strategy.current_round.feature_schema_version = CURRENT_FEATURE_SCHEMA_VERSION
    strategy.current_round.snapshot_policy_version = (
        CURRENT_SNAPSHOT_POLICY_VERSION
    )
    registry.save_model(strategy.model, V2_MODEL_KEY)
    registry.save_round(strategy.current_round)

    for elapsed in (8.0, 30.0, 75.0, 100.0, 150.0, 220.0, 291.0, 295.0):
        now = start + timedelta(seconds=elapsed)
        feed_prices(strategy, start, now)
        strategy.evaluate(current, books(current, now), now)

    snapshots = registry.snapshots(current.condition_id)
    assert [snapshot.snapshot_second for snapshot in snapshots] == [
        0.0,
        72.5,
        145.0,
        217.5,
        290.0,
    ]
    assert [snapshot.observed_second for snapshot in snapshots] == [
        8.0,
        75.0,
        150.0,
        220.0,
        291.0,
    ]
    assert all(
        snapshot.snapshot_policy_version == CURRENT_SNAPSHOT_POLICY_VERSION
        for snapshot in snapshots
    )
    assert snapshots[0].features["remaining_time"] == pytest.approx(
        1.0 - 2.0 * 8.0 / 290.0
    )
    assert snapshots[-1].features["remaining_time"] == pytest.approx(-1.0)
    registry.close()


def test_complete_snapshot_slots_do_not_backfill_expired_slots(tmp_path) -> None:
    start = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)
    strategy, registry, current = engine(
        tmp_path,
        start,
        entry_seconds_after_open=0,
        exit_seconds_after_open=290,
    )
    assert strategy.current_round is not None
    strategy.model = DynamicModel(
        feature_schema_version=CURRENT_FEATURE_SCHEMA_VERSION
    )
    strategy.current_round.model_key = V2_MODEL_KEY
    strategy.current_round.feature_schema_version = CURRENT_FEATURE_SCHEMA_VERSION
    strategy.current_round.snapshot_policy_version = (
        CURRENT_SNAPSHOT_POLICY_VERSION
    )
    registry.save_model(strategy.model, V2_MODEL_KEY)
    registry.save_round(strategy.current_round)
    now = start + timedelta(seconds=75)
    feed_prices(strategy, start, now)
    strategy.evaluate(current, books(current, now), now)

    snapshots = registry.snapshots(current.condition_id)
    assert len(snapshots) == 1
    assert snapshots[0].snapshot_second == 72.5
    assert snapshots[0].observed_second == 75.0
    registry.close()


def test_legacy_snapshot_policy_keeps_original_schedule_and_capture_window(
    tmp_path,
) -> None:
    settings = BtcDynamicConfig(
        entry_seconds_after_open=0,
        exit_seconds_after_open=290,
    )
    assert training_snapshot_seconds(
        settings,
        LEGACY_SNAPSHOT_POLICY_VERSION,
    ) == (0.0, 72.5, 145.0, 217.5)

    start = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)
    strategy, registry, current = engine(
        tmp_path,
        start,
        entry_seconds_after_open=0,
        exit_seconds_after_open=290,
    )
    assert strategy.current_round is not None
    assert (
        strategy.current_round.snapshot_policy_version
        == LEGACY_SNAPSHOT_POLICY_VERSION
    )
    now = start + timedelta(seconds=8)
    feed_prices(strategy, start, now)
    strategy.evaluate(current, books(current, now), now)

    assert registry.snapshots(current.condition_id) == []
    registry.close()


def test_remaining_time_feature_is_versioned_and_window_normalized() -> None:
    settings = BtcDynamicConfig(
        entry_seconds_after_open=0,
        exit_seconds_after_open=290,
    )
    assert remaining_time_feature(
        0, 300, settings, CURRENT_FEATURE_SCHEMA_VERSION
    ) == pytest.approx(1)
    assert remaining_time_feature(
        145, 300, settings, CURRENT_FEATURE_SCHEMA_VERSION
    ) == pytest.approx(0)
    assert remaining_time_feature(
        290, 300, settings, CURRENT_FEATURE_SCHEMA_VERSION
    ) == pytest.approx(-1)
    assert remaining_time_feature(
        400, 300, settings, CURRENT_FEATURE_SCHEMA_VERSION
    ) == pytest.approx(-1)

    assert remaining_time_feature(
        217.5, 300, settings, LEGACY_FEATURE_SCHEMA_VERSION
    ) == pytest.approx(1)
    assert remaining_time_feature(
        285, 300, settings, LEGACY_FEATURE_SCHEMA_VERSION
    ) == pytest.approx(0)

    late_settings = BtcDynamicConfig(
        entry_seconds_after_open=240,
        exit_seconds_after_open=270,
    )
    assert remaining_time_feature(
        240, 300, late_settings, CURRENT_FEATURE_SCHEMA_VERSION
    ) == pytest.approx(1)
    assert remaining_time_feature(
        255, 300, late_settings, CURRENT_FEATURE_SCHEMA_VERSION
    ) == pytest.approx(0)
    assert remaining_time_feature(
        270, 300, late_settings, CURRENT_FEATURE_SCHEMA_VERSION
    ) == pytest.approx(-1)


def test_legacy_payloads_default_to_v1_model_metadata() -> None:
    model = DynamicModel.model_validate({"bias": 0.25})
    assert model.feature_schema_version == LEGACY_FEATURE_SCHEMA_VERSION

    start = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)
    round_ = DynamicRound.model_validate(
        {
            "market_id": "legacy",
            "market_slug": "legacy-market",
            "start_time": start,
            "end_time": start + timedelta(minutes=5),
            "settings": BtcDynamicConfig(),
            "created_at": start,
            "updated_at": start,
        }
    )
    assert round_.model_key == LEGACY_MODEL_KEY
    assert round_.feature_schema_version == LEGACY_FEATURE_SCHEMA_VERSION
    assert round_.snapshot_policy_version == LEGACY_SNAPSHOT_POLICY_VERSION
    assert round_.comparison_model_key is None
    assert round_.comparison_feature_schema_version is None
    assert round_.comparison_order is None

    snapshot = DynamicSnapshot.model_validate(
        {
            "market_id": "legacy",
            "snapshot_second": 145,
            "formula_probability": 0.5,
            "online_probability": 0.5,
            "features": {},
            "created_at": start + timedelta(seconds=146),
        }
    )
    assert snapshot.observed_second is None
    assert snapshot.snapshot_policy_version == LEGACY_SNAPSHOT_POLICY_VERSION
    assert snapshot.comparison_model_key is None
    assert snapshot.comparison_probability is None
    assert snapshot.comparison_features is None
    assert snapshot_elapsed_seconds(snapshot, round_) == pytest.approx(146.0)


def test_dynamic_sizing_defaults_preserve_quantity_mode() -> None:
    settings = BtcDynamicConfig.model_validate({"quantity": 12})

    assert settings.sizing_mode == "quantity"
    assert settings.quantity == 12
    assert settings.quote_amount_usd == 5


def test_v2_history_replay_preserves_v1_and_recomputes_probabilities(tmp_path) -> None:
    registry = BtcDynamicRegistry(tmp_path / "dynamic.sqlite3")
    start = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)
    legacy = DynamicModel(bias=0.75, trained_markets=12, version=13)
    registry.save_model(legacy, LEGACY_MODEL_KEY)
    original_payload = registry.connection.execute(
        "SELECT payload_json FROM btc_dynamic_model WHERE key = ?",
        (LEGACY_MODEL_KEY,),
    ).fetchone()["payload_json"]

    settings = BtcDynamicConfig(
        entry_seconds_after_open=0,
        exit_seconds_after_open=290,
    )
    for index, outcome in enumerate((Direction.UP, Direction.DOWN)):
        market_start = start + timedelta(minutes=5 * index)
        market_id = f"history-{index}"
        round_ = DynamicRound(
            market_id=market_id,
            market_slug=f"history-market-{index}",
            start_time=market_start,
            end_time=market_start + timedelta(minutes=5),
            settings=settings,
            created_at=market_start,
            updated_at=market_start + timedelta(minutes=5),
            official_outcome=outcome,
            trained=True,
            closed_at=market_start + timedelta(minutes=5),
        )
        registry.save_round(round_)
        for snapshot_second in (0.0, 145.0):
            registry.save_snapshot(
                DynamicSnapshot(
                    market_id=market_id,
                    snapshot_second=snapshot_second,
                    formula_probability=0.6,
                    online_probability=0.99,
                    features={name: 0.0 for name in FEATURE_NAMES},
                    created_at=market_start
                    + timedelta(seconds=snapshot_second),
                )
            )

    trained_at = start + timedelta(hours=1)
    result = registry.train_v2_from_history(trained_at)
    v2 = registry.load_model(V2_MODEL_KEY)
    preserved_payload = registry.connection.execute(
        "SELECT payload_json FROM btc_dynamic_model WHERE key = ?",
        (LEGACY_MODEL_KEY,),
    ).fetchone()["payload_json"]

    assert preserved_payload == original_payload
    assert v2.feature_schema_version == CURRENT_FEATURE_SCHEMA_VERSION
    assert v2.trained_markets == 2
    assert result["snapshots"] == 4
    assert result["metrics"]["v1_stored"]["brier"] > 0.4
    assert result["metrics"]["v2_replay"]["brier"] < 0.3
    assert registry.active_model_key() == LEGACY_MODEL_KEY
    assert registry.pending_model_activation() == {
        "model_key": V2_MODEL_KEY,
        "not_before": trained_at.isoformat(),
    }
    assert all(
        DynamicRound.model_validate_json(row["payload_json"]).trained
        for row in registry.connection.execute(
            "SELECT payload_json FROM btc_dynamic_rounds"
        )
    )
    with pytest.raises(ValueError, match="already exists"):
        registry.train_v2_from_history(trained_at)
    registry.close()


def test_v2_history_replay_uses_actual_observation_time(tmp_path) -> None:
    registry = BtcDynamicRegistry(tmp_path / "dynamic.sqlite3")
    start = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)
    registry.save_model(DynamicModel(), LEGACY_MODEL_KEY)
    settings = BtcDynamicConfig(
        entry_seconds_after_open=0,
        exit_seconds_after_open=290,
    )
    registry.save_round(
        DynamicRound(
            market_id="observed-time",
            market_slug="observed-time",
            start_time=start,
            end_time=start + timedelta(minutes=5),
            settings=settings,
            snapshot_policy_version=CURRENT_SNAPSHOT_POLICY_VERSION,
            created_at=start,
            updated_at=start + timedelta(minutes=5),
            official_outcome=Direction.UP,
            trained=True,
            closed_at=start + timedelta(minutes=5),
        )
    )
    registry.save_snapshot(
        DynamicSnapshot(
            market_id="observed-time",
            snapshot_second=0.0,
            observed_second=145.0,
            snapshot_policy_version=CURRENT_SNAPSHOT_POLICY_VERSION,
            formula_probability=0.6,
            online_probability=0.99,
            features={name: 0.0 for name in FEATURE_NAMES},
            created_at=start + timedelta(seconds=145),
        )
    )

    registry.train_v2_from_history(start + timedelta(hours=1))
    v2 = registry.load_model(V2_MODEL_KEY)

    assert v2.weights["remaining_time"] == pytest.approx(0.0)
    assert v2.bias > 0
    registry.close()


def test_summary_splits_snapshot_quality_by_capture_policy(tmp_path) -> None:
    registry = BtcDynamicRegistry(tmp_path / "dynamic.sqlite3")
    start = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)
    settings = BtcDynamicConfig(
        entry_seconds_after_open=0,
        exit_seconds_after_open=290,
    )
    for index, (policy_version, probability) in enumerate(
        (
            (LEGACY_SNAPSHOT_POLICY_VERSION, 1.0),
            (CURRENT_SNAPSHOT_POLICY_VERSION, 0.0),
        )
    ):
        market_start = start + timedelta(minutes=5 * index)
        market_id = f"policy-{policy_version}"
        registry.save_round(
            DynamicRound(
                market_id=market_id,
                market_slug=market_id,
                start_time=market_start,
                end_time=market_start + timedelta(minutes=5),
                settings=settings,
                model_key=V2_MODEL_KEY,
                feature_schema_version=CURRENT_FEATURE_SCHEMA_VERSION,
                snapshot_policy_version=policy_version,
                created_at=market_start,
                updated_at=market_start + timedelta(minutes=5),
                official_outcome=Direction.UP,
                trained=True,
                closed_at=market_start + timedelta(minutes=5),
            )
        )
        registry.save_snapshot(
            DynamicSnapshot(
                market_id=market_id,
                snapshot_second=0.0,
                observed_second=0.0,
                model_key=V2_MODEL_KEY,
                feature_schema_version=CURRENT_FEATURE_SCHEMA_VERSION,
                snapshot_policy_version=policy_version,
                formula_probability=0.5,
                online_probability=probability,
                features={name: 0.0 for name in FEATURE_NAMES},
                created_at=market_start,
            )
        )

    model_summary = registry.summary()["models"][V2_MODEL_KEY]

    assert model_summary["brier_online"] == pytest.approx(0.5)
    assert model_summary["forward_accuracy"] == pytest.approx(0.5)
    assert model_summary["snapshot_policies"]["1"] == {
        "brier_online": 0.0,
        "forward_accuracy": 1.0,
        "evaluated_snapshots": 1,
    }
    assert model_summary["snapshot_policies"]["2"] == {
        "brier_online": 1.0,
        "forward_accuracy": 0.0,
        "evaluated_snapshots": 1,
    }
    registry.close()


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
    assert registry.pending_model_reset() == {
        "model_key": LEGACY_MODEL_KEY,
        "requested_at": (start + timedelta(seconds=10)).isoformat(),
    }

    next_market = market(start + timedelta(minutes=5), "btc-next")
    strategy.set_market(next_market, next_market.start_time)
    assert strategy.model.trained_markets == 0
    assert strategy.model.bias == 0
    registry.close()


def test_v2_activation_waits_for_next_market_and_survives_restart(tmp_path) -> None:
    start = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)
    config = AppConfig(
        data_dir=tmp_path,
        btc_dynamic={"enabled": True},
    )
    path = tmp_path / "dynamic.sqlite3"
    registry = BtcDynamicRegistry(path)
    registry.save_model(DynamicModel(), LEGACY_MODEL_KEY)
    registry.save_model(
        DynamicModel(
            feature_schema_version=CURRENT_FEATURE_SCHEMA_VERSION,
            bias=0.5,
        ),
        V2_MODEL_KEY,
    )
    requested_at = start + timedelta(seconds=10)
    registry.stage_model_activation(V2_MODEL_KEY, requested_at)
    registry.close()

    reopened = BtcDynamicRegistry(path)
    strategy = BtcDynamicEngine(config, reopened)
    current = market(start)
    strategy.set_market(current, start + timedelta(seconds=20))
    assert strategy.current_round is not None
    assert strategy.current_round.model_key == LEGACY_MODEL_KEY
    assert reopened.pending_model_activation() is not None

    next_market = market(start + timedelta(minutes=5), "btc-v2")
    strategy.set_market(next_market, next_market.start_time)
    assert strategy.active_model_key == V2_MODEL_KEY
    assert strategy.current_round is not None
    assert strategy.current_round.model_key == V2_MODEL_KEY
    assert (
        strategy.current_round.feature_schema_version
        == CURRENT_FEATURE_SCHEMA_VERSION
    )
    assert reopened.pending_model_activation() is None
    reopened.close()


def test_comparison_model_starts_only_on_a_new_round(tmp_path) -> None:
    start = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)
    config = AppConfig(data_dir=tmp_path, btc_dynamic={"enabled": True})
    registry = BtcDynamicRegistry(tmp_path / "dynamic.sqlite3")
    registry.save_model(DynamicModel(), LEGACY_MODEL_KEY)
    registry.save_model(
        DynamicModel(feature_schema_version=CURRENT_FEATURE_SCHEMA_VERSION),
        V2_MODEL_KEY,
    )
    registry.set_control("active_model_key", V2_MODEL_KEY, start)
    current = market(start)
    registry.save_round(
        DynamicRound(
            market_id=current.condition_id,
            market_slug=current.slug,
            start_time=current.start_time,
            end_time=current.end_time,
            settings=config.btc_dynamic,
            model_key=V2_MODEL_KEY,
            feature_schema_version=CURRENT_FEATURE_SCHEMA_VERSION,
            snapshot_policy_version=CURRENT_SNAPSHOT_POLICY_VERSION,
            created_at=start,
            updated_at=start,
        )
    )

    strategy = BtcDynamicEngine(config, registry)
    strategy.set_market(current, start + timedelta(seconds=10))

    assert strategy.current_round is not None
    assert strategy.current_round.comparison_model_key is None
    assert strategy.comparison_model is None

    next_market = market(start + timedelta(minutes=5), "comparison-next")
    strategy.set_market(next_market, next_market.start_time)

    assert strategy.current_round is not None
    assert strategy.current_round.model_key == V2_MODEL_KEY
    assert strategy.current_round.comparison_model_key == LEGACY_MODEL_KEY
    assert (
        strategy.current_round.comparison_feature_schema_version
        == LEGACY_FEATURE_SCHEMA_VERSION
    )
    assert strategy.comparison_model is not None
    registry.close()


def test_same_tick_prediction_uses_each_models_time_schema(tmp_path) -> None:
    start = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)
    legacy_weights = {name: 0.0 for name in FEATURE_NAMES}
    legacy_weights["remaining_time"] = 1.0
    strategy, registry, current = dual_model_engine(
        tmp_path,
        start,
        legacy_model=DynamicModel(weights=legacy_weights),
    )
    now = start + timedelta(seconds=280)
    feed_prices(
        strategy,
        start,
        now,
        chainlink=100_005.0,
        binance=71_005.0,
    )
    strategy.evaluate(current, books(current, now), now)

    comparison = strategy.diagnostics["comparison"]
    assert comparison["model_key"] == LEGACY_MODEL_KEY
    assert comparison["feature_schema_version"] == LEGACY_FEATURE_SCHEMA_VERSION
    assert strategy.diagnostics["features"]["remaining_time"] == pytest.approx(0.0)
    assert comparison["features"]["remaining_time"] == pytest.approx(1 / 3)
    assert (
        comparison["probability_up"]
        > strategy.diagnostics["online_probability_up"]
    )

    snapshots = registry.snapshots(current.condition_id)
    assert len(snapshots) == 1
    assert snapshots[0].model_key == V2_MODEL_KEY
    assert snapshots[0].comparison_model_key == LEGACY_MODEL_KEY
    assert snapshots[0].comparison_probability == pytest.approx(
        comparison["probability_up"]
    )
    assert snapshots[0].comparison_features == comparison["features"]
    registry.close()


def test_comparison_order_settles_separately_without_training_v1(tmp_path) -> None:
    start = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)
    strategy, registry, current = dual_model_engine(tmp_path, start)
    v1_before = registry.load_model(LEGACY_MODEL_KEY).model_dump(mode="json")
    now = start + timedelta(seconds=270)
    feed_prices(strategy, start, now)
    strategy.evaluate(current, books(current, now), now)

    assert strategy.current_round is not None
    assert strategy.current_round.online_order is not None
    assert strategy.current_round.comparison_order is not None
    assert strategy.current_round.formula_order is not None
    assert strategy.current_round.online_order.variant == "online"
    assert strategy.current_round.online_order.model_key == V2_MODEL_KEY
    assert strategy.current_round.comparison_order.variant == "comparison"
    assert (
        strategy.current_round.comparison_order.model_key
        == LEGACY_MODEL_KEY
    )

    settled = strategy.settle(
        current.slug,
        Direction.UP,
        now + timedelta(seconds=31),
    )

    assert settled is not None
    assert settled.online_order is not None
    assert settled.comparison_order is not None
    assert settled.online_order.realized_pnl is not None
    assert settled.comparison_order.realized_pnl is not None
    assert (
        registry.load_model(LEGACY_MODEL_KEY).model_dump(mode="json")
        == v1_before
    )
    assert registry.load_model(V2_MODEL_KEY).trained_markets == 1

    summary = registry.summary()
    assert summary["head_to_head"]["markets"] == 1
    assert summary["head_to_head"]["evaluated_snapshots"] == 1
    assert summary["head_to_head"]["models"][LEGACY_MODEL_KEY]["orders"] == 1
    assert summary["head_to_head"]["models"][V2_MODEL_KEY]["orders"] == 1
    assert summary["comparison"]["orders"] == 1
    assert summary["daily"][0]["comparison_orders"] == 1
    registry.close()


def test_late_v1_settlement_does_not_replace_active_v2(tmp_path) -> None:
    start = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)
    strategy, registry, current = engine(tmp_path, start)
    registry.save_model(
        DynamicModel(
            feature_schema_version=CURRENT_FEATURE_SCHEMA_VERSION,
            bias=0.5,
        ),
        V2_MODEL_KEY,
    )
    registry.save_snapshot(
        DynamicSnapshot(
            market_id=current.condition_id,
            snapshot_second=0,
            formula_probability=0.5,
            online_probability=0.5,
            features={name: 0.0 for name in FEATURE_NAMES},
            created_at=start,
        )
    )
    registry.stage_model_activation(V2_MODEL_KEY, start + timedelta(seconds=1))
    next_market = market(start + timedelta(minutes=5), "btc-v2")
    strategy.set_market(next_market, next_market.start_time)
    v2_before = registry.load_model(V2_MODEL_KEY).model_dump()

    strategy.settle(
        current.slug,
        Direction.UP,
        next_market.start_time + timedelta(seconds=1),
    )

    assert registry.load_model(LEGACY_MODEL_KEY).trained_markets == 1
    assert registry.load_model(V2_MODEL_KEY).model_dump() == v2_before
    assert strategy.model.feature_schema_version == CURRENT_FEATURE_SCHEMA_VERSION
    registry.close()


def test_model_reset_is_rejected_while_activation_is_pending(tmp_path) -> None:
    start = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)
    strategy, registry, _ = engine(tmp_path, start)
    registry.save_model(
        DynamicModel(feature_schema_version=CURRENT_FEATURE_SCHEMA_VERSION),
        V2_MODEL_KEY,
    )
    registry.stage_model_activation(V2_MODEL_KEY, start)

    assert strategy.request_model_reset(start + timedelta(seconds=1)) is False
    assert registry.control("model_reset_pending") is None
    assert strategy.drain_events()[-1][0] == "btc_dynamic_model_reset_rejected"
    registry.close()


def test_model_activation_is_rejected_while_reset_is_pending(tmp_path) -> None:
    start = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)
    strategy, registry, _ = engine(tmp_path, start)
    registry.save_model(
        DynamicModel(
            feature_schema_version=CURRENT_FEATURE_SCHEMA_VERSION,
            bias=0.75,
        ),
        V2_MODEL_KEY,
    )
    assert strategy.request_model_reset(start + timedelta(seconds=1)) is True

    with pytest.raises(ValueError, match="while reset is pending"):
        registry.stage_model_activation(V2_MODEL_KEY, start + timedelta(seconds=2))

    assert registry.pending_model_activation() is None
    assert registry.pending_model_reset()["model_key"] == LEGACY_MODEL_KEY
    registry.close()


def test_model_reset_remains_bound_to_requested_model(tmp_path) -> None:
    start = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)
    strategy, registry, _ = engine(tmp_path, start)
    v2 = DynamicModel(
        feature_schema_version=CURRENT_FEATURE_SCHEMA_VERSION,
        bias=0.75,
        trained_markets=9,
    )
    registry.save_model(DynamicModel(bias=-0.25, trained_markets=4), LEGACY_MODEL_KEY)
    registry.save_model(v2, V2_MODEL_KEY)
    registry.set_control("active_model_key", V2_MODEL_KEY, start)
    strategy.active_model_key = V2_MODEL_KEY
    strategy.model = v2
    assert strategy.request_model_reset(start + timedelta(seconds=1)) is True

    registry.set_control("active_model_key", LEGACY_MODEL_KEY, start + timedelta(seconds=2))
    legacy_before = registry.load_model(LEGACY_MODEL_KEY).model_dump()
    next_market = market(start + timedelta(minutes=5), "btc-next")
    strategy.set_market(next_market, next_market.start_time)

    assert registry.load_model(LEGACY_MODEL_KEY).model_dump() == legacy_before
    assert registry.load_model(V2_MODEL_KEY).trained_markets == 0
    assert registry.load_model(V2_MODEL_KEY).bias == 0
    assert strategy.active_model_key == LEGACY_MODEL_KEY
    assert registry.pending_model_reset() is None
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
    assert state["model_key"] == LEGACY_MODEL_KEY
    assert state["active_model_key"] == LEGACY_MODEL_KEY
    assert state["pending_model"] is None
    registry.close()
