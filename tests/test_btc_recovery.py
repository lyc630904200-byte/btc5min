from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from polybtc.btc_recovery import (
    BtcRecoveryEngine,
    BtcRecoveryRegistry,
    RecoveryPhase,
)
from polybtc.config import AppConfig, BtcRecoveryConfig
from polybtc.models import BookLevel, Direction, MarketState, OrderBookSnapshot


def market(start: datetime) -> MarketState:
    return MarketState(
        asset="BTC",
        condition_id="btc-market",
        slug=f"btc-updown-5m-{int(start.timestamp())}",
        question="BTC Up or Down",
        threshold_price=None,
        start_time=start,
        end_time=start + timedelta(minutes=5),
        up_token_id="btc-up",
        down_token_id="btc-down",
        min_order_size=5,
    )


def book(
    direction: Direction,
    ask: float,
    bid: float,
    now: datetime,
    size: float = 100,
) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        token_id="btc-up" if direction == Direction.UP else "btc-down",
        market_id="btc-market",
        timestamp=now,
        received_at=now,
        bids=[BookLevel(price=bid, size=size)],
        asks=[BookLevel(price=ask, size=size)],
        depth_trusted=True,
        min_order_size=5,
    )


def books(
    now: datetime,
    *,
    up_ask: float,
    up_bid: float,
    down_ask: float,
    down_bid: float,
    size: float = 100,
) -> dict[Direction, OrderBookSnapshot]:
    return {
        Direction.UP: book(Direction.UP, up_ask, up_bid, now, size),
        Direction.DOWN: book(Direction.DOWN, down_ask, down_bid, now, size),
    }


def engine(tmp_path, start: datetime, **overrides):
    payload = {"enabled": True, **overrides}
    config = AppConfig(
        data_dir=tmp_path,
        btc_recovery=payload,
        risk={"max_data_age_ms": 5000},
    )
    registry = BtcRecoveryRegistry(tmp_path / "recovery.sqlite3")
    strategy = BtcRecoveryEngine(config, registry)
    current = market(start)
    strategy.set_market(current, start)
    return strategy, registry, current


def open_initial(
    strategy: BtcRecoveryEngine,
    current: MarketState,
    start: datetime,
) -> dict[Direction, OrderBookSnapshot]:
    initial = books(
        start,
        up_ask=0.69,
        up_bid=0.68,
        down_ask=0.32,
        down_bid=0.31,
    )
    strategy.evaluate(current, initial, start)
    crossed = books(
        start + timedelta(seconds=1),
        up_ask=0.70,
        up_bid=0.69,
        down_ask=0.31,
        down_bid=0.30,
    )
    strategy.evaluate(current, crossed, start + timedelta(seconds=1))
    assert strategy.current_round is not None
    assert strategy.current_round.initial_fill is not None
    return crossed


def test_config_defaults_and_validation() -> None:
    config = BtcRecoveryConfig()

    assert config.entry_seconds_after_open == 0
    assert config.exit_seconds_after_open == 300

    with pytest.raises(ValueError):
        BtcRecoveryConfig(entry_seconds_after_open=200, exit_seconds_after_open=100)
    with pytest.raises(ValueError):
        BtcRecoveryConfig(entry_price_cents=100)


def test_first_direction_crosses_entry_and_direct_target_closes(tmp_path) -> None:
    start = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)
    strategy, registry, current = engine(tmp_path, start)
    open_initial(strategy, current, start)

    target = books(
        start + timedelta(seconds=2),
        up_ask=0.82,
        up_bid=0.81,
        down_ask=0.20,
        down_bid=0.19,
    )
    strategy.evaluate(current, target, start + timedelta(seconds=2))

    round_ = strategy.current_round
    assert round_ is not None
    assert round_.phase == RecoveryPhase.CLOSED
    assert round_.close_reason == "direct_target"
    assert round_.realized_pnl is not None and round_.realized_pnl > 0
    fills = registry.recent_fills()
    assert [fill.side.value for fill in fills] == ["SELL", "BUY"]
    assert fills[0].order_number != fills[1].order_number
    assert fills[0].trade_order_number == fills[1].trade_order_number
    registry.close()


def test_strict_entry_does_not_chase_jump_and_keeps_first_direction_locked(tmp_path) -> None:
    start = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)
    strategy, registry, current = engine(tmp_path, start)
    initial = books(
        start,
        up_ask=0.69,
        up_bid=0.68,
        down_ask=0.31,
        down_bid=0.30,
    )
    strategy.evaluate(current, initial, start)

    jumped = books(
        start + timedelta(seconds=1),
        up_ask=0.71,
        up_bid=0.70,
        down_ask=0.69,
        down_bid=0.29,
    )
    strategy.evaluate(current, jumped, start + timedelta(seconds=1))
    assert strategy.current_round is not None
    assert strategy.current_round.locked_direction == Direction.UP
    assert strategy.current_round.initial_fill is None

    returned = books(
        start + timedelta(seconds=2),
        up_ask=0.70,
        up_bid=0.69,
        down_ask=0.72,
        down_bid=0.28,
    )
    strategy.evaluate(current, returned, start + timedelta(seconds=2))
    assert strategy.current_round.initial_fill is not None
    assert strategy.current_round.initial_fill.avg_price == pytest.approx(0.70)
    registry.close()


def test_recovery_waits_at_eighty_until_fees_are_profitable(tmp_path) -> None:
    start = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)
    strategy, registry, current = engine(tmp_path, start)
    open_initial(strategy, current, start)

    recovery = books(
        start + timedelta(seconds=2),
        up_ask=0.41,
        up_bid=0.40,
        down_ask=0.60,
        down_bid=0.59,
    )
    strategy.evaluate(current, recovery, start + timedelta(seconds=2))
    assert strategy.current_round is not None
    assert strategy.current_round.phase == RecoveryPhase.RECOVERY_OPEN

    eighty = books(
        start + timedelta(seconds=3),
        up_ask=0.21,
        up_bid=0.20,
        down_ask=0.81,
        down_bid=0.80,
    )
    strategy.evaluate(current, eighty, start + timedelta(seconds=3))
    assert strategy.current_round.phase == RecoveryPhase.RECOVERY_OPEN

    profitable = books(
        start + timedelta(seconds=4),
        up_ask=0.19,
        up_bid=0.18,
        down_ask=0.83,
        down_bid=0.82,
    )
    strategy.evaluate(current, profitable, start + timedelta(seconds=4))
    assert strategy.current_round.phase == RecoveryPhase.CLOSED
    assert strategy.current_round.close_reason == "recovery_target"
    assert strategy.current_round.realized_pnl is not None
    assert strategy.current_round.realized_pnl > 0
    assert len(strategy.current_round.exit_fills) == 2
    registry.close()


def test_recovery_trigger_buys_reverse_book_above_old_sixty_cent_limit(tmp_path) -> None:
    start = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)
    strategy, registry, current = engine(tmp_path, start)
    open_initial(strategy, current, start)

    strategy.evaluate(
        current,
        books(
            start + timedelta(seconds=2),
            up_ask=0.40,
            up_bid=0.39,
            down_ask=0.62,
            down_bid=0.61,
        ),
        start + timedelta(seconds=2),
    )

    assert strategy.current_round is not None
    assert strategy.current_round.phase == RecoveryPhase.RECOVERY_OPEN
    assert strategy.current_round.recovery_fill is not None
    assert strategy.current_round.recovery_fill.avg_price == pytest.approx(0.62)
    registry.close()


def test_recovery_stop_atomically_sells_both_sides(tmp_path) -> None:
    start = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)
    strategy, registry, current = engine(tmp_path, start)
    open_initial(strategy, current, start)
    strategy.evaluate(
        current,
        books(
            start + timedelta(seconds=2),
            up_ask=0.41,
            up_bid=0.40,
            down_ask=0.60,
            down_bid=0.59,
        ),
        start + timedelta(seconds=2),
    )
    strategy.evaluate(
        current,
        books(
            start + timedelta(seconds=3),
            up_ask=0.71,
            up_bid=0.70,
            down_ask=0.31,
            down_bid=0.30,
        ),
        start + timedelta(seconds=3),
    )

    round_ = strategy.current_round
    assert round_ is not None
    assert round_.phase == RecoveryPhase.CLOSED
    assert round_.close_reason == "recovery_stop"
    assert len(round_.exit_fills) == 2
    assert round_.realized_pnl is not None and round_.realized_pnl < 0
    fills_by_direction: dict[Direction, list] = {}
    for fill in registry.recent_fills():
        fills_by_direction.setdefault(fill.direction, []).append(fill)
    assert len(fills_by_direction) == 2
    assert all(
        len({fill.trade_order_number for fill in direction_fills}) == 1
        for direction_fills in fills_by_direction.values()
    )
    assert {
        direction_fills[0].trade_order_number
        for direction_fills in fills_by_direction.values()
    } == {1, 2}
    registry.close()


def test_entry_and_timed_exit_windows(tmp_path) -> None:
    start = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)
    strategy, registry, current = engine(
        tmp_path,
        start,
        entry_seconds_after_open=10,
        exit_seconds_after_open=20,
    )
    before = books(
        start + timedelta(seconds=5),
        up_ask=0.69,
        up_bid=0.68,
        down_ask=0.31,
        down_bid=0.30,
    )
    strategy.evaluate(current, before, start + timedelta(seconds=5))
    assert strategy.current_round is not None
    assert strategy.current_round.entry_observation_started is False

    baseline = books(
        start + timedelta(seconds=10),
        up_ask=0.69,
        up_bid=0.68,
        down_ask=0.31,
        down_bid=0.30,
    )
    strategy.evaluate(current, baseline, start + timedelta(seconds=10))
    crossed = books(
        start + timedelta(seconds=11),
        up_ask=0.70,
        up_bid=0.69,
        down_ask=0.31,
        down_bid=0.30,
    )
    strategy.evaluate(current, crossed, start + timedelta(seconds=11))
    strategy.evaluate(
        current,
        books(
            start + timedelta(seconds=20),
            up_ask=0.61,
            up_bid=0.60,
            down_ask=0.41,
            down_bid=0.40,
        ),
        start + timedelta(seconds=20),
    )

    assert strategy.current_round.phase == RecoveryPhase.CLOSED
    assert strategy.current_round.close_reason == "timed_exit"
    registry.close()


def test_entry_observation_waits_for_both_trusted_fresh_books(tmp_path) -> None:
    start = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)
    strategy, registry, current = engine(tmp_path, start)
    untrusted = books(
        start,
        up_ask=0.69,
        up_bid=0.68,
        down_ask=0.31,
        down_bid=0.30,
    )
    untrusted[Direction.DOWN].depth_trusted = False

    strategy.evaluate(current, untrusted, start)

    assert strategy.current_round is not None
    assert strategy.current_round.entry_observation_started is False
    assert strategy.status == "entry_books_waiting"
    assert strategy.last_reason == "book_depth_untrusted"
    registry.close()


def test_expiry_settles_open_shares_with_official_outcome(tmp_path) -> None:
    start = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)
    strategy, registry, current = engine(tmp_path, start)
    open_initial(strategy, current, start)

    strategy.evaluate(
        current,
        books(
            current.end_time,
            up_ask=0.99,
            up_bid=0.98,
            down_ask=0.02,
            down_bid=0.01,
        ),
        current.end_time,
    )
    assert strategy.current_round is not None
    assert strategy.current_round.phase == RecoveryPhase.PENDING_SETTLEMENT

    settled = strategy.settle(current.slug, Direction.UP, current.end_time + timedelta(seconds=2))
    assert settled is not None
    assert settled.phase == RecoveryPhase.CLOSED
    assert settled.close_reason == "official_settlement"
    assert settled.payout_usd == pytest.approx(5)
    assert settled.realized_pnl is not None and settled.realized_pnl > 0
    registry.close()


def test_restart_restores_open_position_but_skips_unordered_waiting_round(tmp_path) -> None:
    start = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)
    strategy, registry, current = engine(tmp_path, start)
    open_initial(strategy, current, start)
    registry.close()

    config = AppConfig(
        data_dir=tmp_path,
        btc_recovery={"enabled": True},
        risk={"max_data_age_ms": 5000},
    )
    restored_registry = BtcRecoveryRegistry(tmp_path / "recovery.sqlite3")
    restored = BtcRecoveryEngine(config, restored_registry)
    restored.set_market(current, start + timedelta(seconds=2))
    assert restored.current_round is not None
    assert restored.current_round.phase == RecoveryPhase.INITIAL_OPEN
    restored_registry.close()

    second_dir = tmp_path / "waiting"
    waiting, waiting_registry, waiting_market = engine(second_dir, start)
    waiting_registry.close()
    waiting_registry = BtcRecoveryRegistry(second_dir / "recovery.sqlite3")
    restarted = BtcRecoveryEngine(
        AppConfig(data_dir=second_dir, btc_recovery={"enabled": True}),
        waiting_registry,
    )
    restarted.set_market(waiting_market, start + timedelta(seconds=1))
    assert restarted.current_round is not None
    assert restarted.current_round.phase == RecoveryPhase.SKIPPED_RESTART
    waiting_registry.close()


def test_restart_before_entry_window_keeps_waiting_round(tmp_path) -> None:
    start = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)
    data_dir = tmp_path / "before-entry"
    config = AppConfig(
        data_dir=data_dir,
        btc_recovery={
            "enabled": True,
            "entry_seconds_after_open": 30,
            "exit_seconds_after_open": 250,
        },
    )
    current = market(start)
    first_registry = BtcRecoveryRegistry(data_dir / "recovery.sqlite3")
    first = BtcRecoveryEngine(config, first_registry)
    first.set_market(current, start)
    first_registry.close()

    restarted_registry = BtcRecoveryRegistry(data_dir / "recovery.sqlite3")
    restarted = BtcRecoveryEngine(config, restarted_registry)
    restarted.set_market(current, start + timedelta(seconds=10))

    assert restarted.current_round is not None
    assert restarted.current_round.phase == RecoveryPhase.WAITING_ENTRY_WINDOW
    restarted_registry.close()
