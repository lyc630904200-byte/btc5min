from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from polybtc.btc_recovery import (
    BtcRecoveryEngine,
    RecoveryFill,
    BtcRecoveryRegistry,
    RecoveryPhase,
    RecoveryRound,
    recovery_order_summaries,
)
from polybtc.config import AppConfig, BtcRecoveryConfig
from polybtc.models import (
    BookLevel,
    Direction,
    MarketState,
    OrderBookSnapshot,
    OrderSide,
)


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
        up_ask=0.71,
        up_bid=0.70,
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
    assert config.max_entry_price_cents == 100
    assert config.recovery_target_price_cents == 80
    assert BtcRecoveryConfig(
        target_price_cents=100,
        recovery_trigger_cents=0,
    ).target_price_cents == 100

    with pytest.raises(ValueError):
        BtcRecoveryConfig(entry_seconds_after_open=200, exit_seconds_after_open=100)
    with pytest.raises(ValueError):
        BtcRecoveryConfig(entry_price_cents=100)
    with pytest.raises(ValueError):
        BtcRecoveryConfig(entry_price_cents=90, max_entry_price_cents=90)
    with pytest.raises(ValueError):
        BtcRecoveryConfig(max_entry_price_cents=100.01)
    with pytest.raises(ValueError):
        BtcRecoveryConfig(target_price_cents=100.01)
    with pytest.raises(ValueError):
        BtcRecoveryConfig(recovery_trigger_cents=-0.01)


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
    recent_orders = strategy.dashboard_state()["recent_orders"]
    assert len(recent_orders) == 1
    assert recent_orders[0]["direction"] == Direction.UP.value
    assert recent_orders[0]["buy_avg_price"] == pytest.approx(0.71)
    assert recent_orders[0]["sell_avg_price"] == pytest.approx(0.81)
    assert recent_orders[0]["quantity"] == pytest.approx(5)
    assert recent_orders[0]["fee_usd"] == pytest.approx(
        sum(fill.fee_usd for fill in fills)
    )
    assert "side" not in recent_orders[0]
    assert "reason" not in recent_orders[0]
    registry.close()


def test_initial_target_100_holds_for_official_settlement(tmp_path) -> None:
    start = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)
    strategy, registry, current = engine(
        tmp_path,
        start,
        target_price_cents=100,
        exit_seconds_after_open=200,
    )
    open_initial(strategy, current, start)

    strategy.evaluate(
        current,
        books(
            start + timedelta(seconds=2),
            up_ask=1.0,
            up_bid=1.0,
            down_ask=0.01,
            down_bid=0.0,
        ),
        start + timedelta(seconds=2),
    )

    assert strategy.current_round is not None
    assert strategy.current_round.phase == RecoveryPhase.INITIAL_OPEN
    assert strategy.current_round.exit_fills == []

    strategy.evaluate(
        current,
        books(
            start + timedelta(seconds=201),
            up_ask=1.0,
            up_bid=1.0,
            down_ask=0.01,
            down_bid=0.0,
        ),
        start + timedelta(seconds=201),
    )

    assert strategy.current_round.phase == RecoveryPhase.INITIAL_OPEN
    assert strategy.current_round.exit_fills == []
    assert strategy.last_reason == "holding_for_official_settlement"

    strategy.evaluate(
        current,
        books(
            current.end_time,
            up_ask=1.0,
            up_bid=1.0,
            down_ask=0.01,
            down_bid=0.0,
        ),
        current.end_time,
    )

    assert strategy.current_round.phase == RecoveryPhase.PENDING_SETTLEMENT
    assert strategy.current_round.close_reason == "awaiting_official_settlement"
    registry.close()


def test_initial_stop_zero_holds_when_recovery_orders_are_stopped(tmp_path) -> None:
    start = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)
    strategy, registry, current = engine(
        tmp_path,
        start,
        recovery_trigger_cents=0,
    )
    open_initial(strategy, current, start)
    strategy.set_recovery_orders_stopped(True, start + timedelta(seconds=2))

    strategy.evaluate(
        current,
        books(
            start + timedelta(seconds=2),
            up_ask=0.01,
            up_bid=0.0,
            down_ask=1.0,
            down_bid=0.99,
        ),
        start + timedelta(seconds=2),
    )

    assert strategy.current_round is not None
    assert strategy.current_round.phase == RecoveryPhase.INITIAL_OPEN
    assert strategy.current_round.initial_stop_requested is False
    assert strategy.current_round.exit_fills == []
    registry.close()


def test_order_summary_aggregates_multiple_exit_fills() -> None:
    start = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)
    settings = BtcRecoveryConfig(enabled=True)
    buy = RecoveryFill(
        trade_order_number=7,
        round_id="round-7",
        market_id="market-7",
        market_slug="btc-updown-5m-7",
        stage="initial",
        direction=Direction.UP,
        side=OrderSide.BUY,
        avg_price=0.70,
        quantity=10,
        quote=7,
        fee_usd=0.10,
        levels_used=1,
        reason="initial_entry",
        created_at=start,
    )
    first_sell = RecoveryFill(
        trade_order_number=7,
        round_id=buy.round_id,
        market_id=buy.market_id,
        market_slug=buy.market_slug,
        stage="exit",
        direction=Direction.UP,
        side=OrderSide.SELL,
        avg_price=0.80,
        quantity=4,
        quote=3.2,
        fee_usd=0.04,
        levels_used=1,
        reason="direct_target",
        created_at=start + timedelta(seconds=1),
    )
    second_sell = first_sell.model_copy(
        update={
            "order_id": "second-sell",
            "avg_price": 0.85,
            "quantity": 6,
            "quote": 5.1,
            "fee_usd": 0.06,
            "created_at": start + timedelta(seconds=2),
        }
    )
    round_ = RecoveryRound(
        round_id=buy.round_id,
        market_id=buy.market_id,
        market_slug=buy.market_slug,
        start_time=start,
        end_time=start + timedelta(minutes=5),
        settings=settings,
        phase=RecoveryPhase.CLOSED,
        created_at=start,
        updated_at=start + timedelta(seconds=2),
        initial_fill=buy,
        exit_fills=[first_sell, second_sell],
        realized_pnl=1.1,
        close_reason="direct_target",
        closed_at=start + timedelta(seconds=2),
    )

    summaries = recovery_order_summaries(
        [second_sell, buy, first_sell],
        {round_.round_id: round_},
    )

    assert len(summaries) == 1
    assert summaries[0]["display_order_number"] == 7
    assert summaries[0]["buy_quote"] == pytest.approx(7)
    assert summaries[0]["sell_quote"] == pytest.approx(8.3)
    assert summaries[0]["sell_avg_price"] == pytest.approx(0.83)
    assert summaries[0]["fee_usd"] == pytest.approx(0.20)
    assert summaries[0]["net_pnl"] == pytest.approx(1.1)
    assert summaries[0]["sell_created_at"] == second_sell.created_at.isoformat()


def test_recent_order_fills_keeps_300_logical_orders(tmp_path) -> None:
    start = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)
    registry = BtcRecoveryRegistry(tmp_path / "recovery.sqlite3")
    round_ = RecoveryRound(
        round_id="history-round",
        market_id="history-market",
        market_slug="btc-updown-5m-history",
        start_time=start,
        end_time=start + timedelta(minutes=5),
        settings=BtcRecoveryConfig(enabled=True),
        phase=RecoveryPhase.CLOSED,
        created_at=start,
        updated_at=start,
    )
    registry.save_round(round_)

    for number in range(1, 302):
        fills = [
            RecoveryFill(
                trade_order_number=number,
                round_id=round_.round_id,
                market_id=round_.market_id,
                market_slug=round_.market_slug,
                stage="initial",
                direction=Direction.UP,
                side=OrderSide.BUY,
                avg_price=0.70,
                quantity=5,
                quote=3.5,
                fee_usd=0.01,
                levels_used=1,
                reason="initial_entry",
                created_at=start + timedelta(seconds=number),
            )
        ]
        if number == 301:
            fills.append(
                fills[0].model_copy(
                    update={
                        "order_id": "latest-sell",
                        "stage": "exit",
                        "side": OrderSide.SELL,
                        "avg_price": 0.80,
                        "quote": 4,
                        "reason": "direct_target",
                        "created_at": start + timedelta(seconds=number + 1),
                    }
                )
            )
        registry.record_transition(round_, fills)

    recent = registry.recent_order_fills(300)
    display_numbers = [
        fill.trade_order_number or fill.order_number for fill in recent
    ]

    assert len(set(display_numbers)) == 300
    assert min(display_numbers) == 2
    assert max(display_numbers) == 301
    assert display_numbers.count(301) == 2
    registry.close()


def test_entry_buys_immediately_when_current_price_is_above_threshold(
    tmp_path,
) -> None:
    start = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)
    strategy, registry, current = engine(tmp_path, start)
    above = books(
        start,
        up_ask=0.71,
        up_bid=0.70,
        down_ask=0.29,
        down_bid=0.29,
    )
    strategy.evaluate(current, above, start)

    assert strategy.current_round is not None
    assert strategy.current_round.locked_direction == Direction.UP
    assert strategy.current_round.initial_fill is not None
    assert strategy.current_round.initial_fill.avg_price == pytest.approx(0.71)
    registry.close()


def test_entry_does_not_buy_when_current_price_equals_threshold(tmp_path) -> None:
    start = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)
    strategy, registry, current = engine(tmp_path, start)
    strategy.evaluate(
        current,
        books(
            start,
            up_ask=0.70,
            up_bid=0.69,
            down_ask=0.30,
            down_bid=0.29,
        ),
        start,
    )

    assert strategy.current_round is not None
    assert strategy.current_round.locked_direction is None
    assert strategy.current_round.initial_fill is None
    registry.close()


def test_entry_requires_complete_depth_within_maximum_price(tmp_path) -> None:
    start = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)
    strategy, registry, current = engine(
        tmp_path,
        start,
        entry_price_cents=90,
        max_entry_price_cents=92,
        initial_quantity=10,
    )
    limited = books(
        start,
        up_ask=0.91,
        up_bid=0.90,
        down_ask=0.09,
        down_bid=0.08,
    )
    limited[Direction.UP].asks = [
        BookLevel(price=0.91, size=5),
        BookLevel(price=0.93, size=5),
    ]
    strategy.evaluate(current, limited, start)

    assert strategy.current_round is not None
    assert strategy.current_round.initial_fill is None
    assert strategy.last_reason == "strict_limit_or_depth_unavailable"

    fillable = books(
        start + timedelta(seconds=1),
        up_ask=0.91,
        up_bid=0.90,
        down_ask=0.09,
        down_bid=0.08,
    )
    fillable[Direction.UP].asks = [
        BookLevel(price=0.91, size=5),
        BookLevel(price=0.92, size=5),
    ]
    strategy.evaluate(current, fillable, start + timedelta(seconds=1))

    fill = strategy.current_round.initial_fill
    assert fill is not None
    assert fill.avg_price == pytest.approx(0.915)
    assert fill.levels_used == 2
    registry.close()


def test_entry_does_not_lock_direction_above_maximum_price(tmp_path) -> None:
    start = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)
    strategy, registry, current = engine(
        tmp_path,
        start,
        entry_price_cents=90,
        max_entry_price_cents=92,
    )
    strategy.evaluate(
        current,
        books(
            start,
            up_ask=0.93,
            up_bid=0.92,
            down_ask=0.07,
            down_bid=0.06,
        ),
        start,
    )

    assert strategy.current_round is not None
    assert strategy.current_round.locked_direction is None
    assert strategy.current_round.initial_fill is None
    assert strategy.last_reason == "entry_price_above_limit"
    registry.close()


def test_failed_entry_unlocks_at_threshold_and_retries_above_it(tmp_path) -> None:
    start = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)
    strategy, registry, current = engine(tmp_path, start)
    strategy.evaluate(
        current,
        books(
            start,
            up_ask=0.69,
            up_bid=0.68,
            down_ask=0.31,
            down_bid=0.30,
        ),
        start,
    )

    strategy.evaluate(
        current,
        books(
            start + timedelta(seconds=1),
            up_ask=0.71,
            up_bid=0.70,
            down_ask=0.29,
            down_bid=0.28,
            size=4,
        ),
        start + timedelta(seconds=1),
    )
    assert strategy.current_round is not None
    assert strategy.current_round.locked_direction == Direction.UP
    assert strategy.current_round.initial_fill is None

    strategy.evaluate(
        current,
        books(
            start + timedelta(seconds=2),
            up_ask=0.40,
            up_bid=0.39,
            down_ask=0.61,
            down_bid=0.60,
        ),
        start + timedelta(seconds=2),
    )
    assert strategy.current_round.locked_direction is None
    assert strategy.current_round.initial_fill is None

    strategy.evaluate(
        current,
        books(
            start + timedelta(seconds=3),
            up_ask=0.71,
            up_bid=0.70,
            down_ask=0.30,
            down_bid=0.29,
        ),
        start + timedelta(seconds=3),
    )
    assert strategy.current_round.initial_fill is not None
    assert strategy.current_round.initial_fill.avg_price == pytest.approx(0.71)
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


def test_recovery_uses_its_independent_target_price(tmp_path) -> None:
    start = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)
    strategy, registry, current = engine(
        tmp_path,
        start,
        recovery_target_price_cents=85,
    )
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
            up_ask=0.17,
            up_bid=0.16,
            down_ask=0.85,
            down_bid=0.84,
        ),
        start + timedelta(seconds=3),
    )
    assert strategy.current_round is not None
    assert strategy.current_round.phase == RecoveryPhase.RECOVERY_OPEN

    strategy.evaluate(
        current,
        books(
            start + timedelta(seconds=4),
            up_ask=0.13,
            up_bid=0.12,
            down_ask=0.87,
            down_bid=0.86,
        ),
        start + timedelta(seconds=4),
    )
    assert strategy.current_round.phase == RecoveryPhase.CLOSED
    assert strategy.current_round.close_reason == "recovery_target"
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


@pytest.mark.parametrize("stop_bid", [0.39, 0.40])
def test_stopped_recovery_orders_use_trigger_as_initial_stop(
    tmp_path,
    stop_bid: float,
) -> None:
    start = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)
    strategy, registry, current = engine(tmp_path, start)
    open_initial(strategy, current, start)

    strategy.set_recovery_orders_stopped(True, start + timedelta(seconds=2))
    strategy.evaluate(
        current,
        books(
            start + timedelta(seconds=2),
            up_ask=stop_bid + 0.01,
            up_bid=stop_bid,
            down_ask=0.61,
            down_bid=0.60,
        ),
        start + timedelta(seconds=2),
    )

    round_ = strategy.current_round
    assert round_ is not None
    assert round_.phase == RecoveryPhase.CLOSED
    assert round_.close_reason == "initial_stop"
    assert round_.recovery_fill is None
    assert len(round_.exit_fills) == 1
    assert strategy.recovery_orders_stopped is True
    assert registry.summary()["stop_exits"] == 1
    registry.close()


def test_stopped_recovery_orders_hold_above_initial_stop_and_persist(tmp_path) -> None:
    start = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)
    strategy, registry, current = engine(tmp_path, start)
    open_initial(strategy, current, start)

    strategy.set_recovery_orders_stopped(True, start + timedelta(seconds=2))
    strategy.evaluate(
        current,
        books(
            start + timedelta(seconds=2),
            up_ask=0.43,
            up_bid=0.42,
            down_ask=0.59,
            down_bid=0.58,
        ),
        start + timedelta(seconds=2),
    )

    assert strategy.current_round is not None
    assert strategy.current_round.phase == RecoveryPhase.INITIAL_OPEN
    assert strategy.current_round.recovery_fill is None
    registry.close()

    config = AppConfig(
        data_dir=tmp_path,
        btc_recovery={"enabled": True},
        risk={"max_data_age_ms": 5000},
    )
    reopened_registry = BtcRecoveryRegistry(tmp_path / "recovery.sqlite3")
    reopened = BtcRecoveryEngine(config, reopened_registry)
    assert reopened.recovery_orders_stopped is True

    reopened.set_market(current, start + timedelta(seconds=3))
    reopened.set_recovery_orders_stopped(False, start + timedelta(seconds=3))
    resumed_books = books(
        start + timedelta(seconds=3),
        up_ask=0.40,
        up_bid=0.39,
        down_ask=0.62,
        down_bid=0.61,
    )
    reopened.evaluate(current, resumed_books, start + timedelta(seconds=3))

    assert reopened.current_round is not None
    assert reopened.current_round.phase == RecoveryPhase.RECOVERY_OPEN
    assert reopened.current_round.recovery_fill is not None
    reopened_registry.close()


def test_statistics_reset_preserves_history_and_persists(tmp_path) -> None:
    start = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)
    strategy, registry, current = engine(tmp_path, start)
    strategy.evaluate(current, {}, current.end_time)

    assert registry.summary()["observed_markets"] == 1
    assert len(registry.recent_rounds()) == 1

    reset_at = current.end_time + timedelta(seconds=1)
    strategy.reset_statistics(reset_at)

    summary = registry.summary()
    assert summary["observed_markets"] == 0
    assert summary["realized_pnl"] == 0
    assert summary["statistics_reset_at"] == reset_at.isoformat()
    assert len(registry.recent_rounds()) == 1
    registry.close()

    reopened = BtcRecoveryRegistry(tmp_path / "recovery.sqlite3")
    assert reopened.summary()["observed_markets"] == 0
    assert len(reopened.recent_rounds()) == 1
    reopened.close()


def test_initial_stop_waits_for_full_depth_and_retries(tmp_path) -> None:
    start = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)
    strategy, registry, current = engine(tmp_path, start)
    open_initial(strategy, current, start)
    strategy.set_recovery_orders_stopped(True, start + timedelta(seconds=2))

    strategy.evaluate(
        current,
        books(
            start + timedelta(seconds=2),
            up_ask=0.40,
            up_bid=0.39,
            down_ask=0.62,
            down_bid=0.61,
            size=2,
        ),
        start + timedelta(seconds=2),
    )

    assert strategy.current_round is not None
    assert strategy.current_round.phase == RecoveryPhase.INITIAL_OPEN
    assert strategy.current_round.initial_stop_requested is True
    assert strategy.current_round.exit_fills == []
    assert strategy.last_reason == "initial_stop_waiting_for_depth"

    strategy.evaluate(
        current,
        books(
            start + timedelta(seconds=3),
            up_ask=0.51,
            up_bid=0.50,
            down_ask=0.51,
            down_bid=0.50,
        ),
        start + timedelta(seconds=3),
    )

    assert strategy.current_round.phase == RecoveryPhase.CLOSED
    assert strategy.current_round.close_reason == "initial_stop"
    assert strategy.current_round.exit_fills[0].avg_price == pytest.approx(0.50)
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
    strategy.set_recovery_orders_stopped(True, start + timedelta(seconds=2, milliseconds=1))
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
    order_pnls = {
        order["display_order_number"]: order["net_pnl"]
        for order in strategy.dashboard_state()["recent_orders"]
    }
    assert len(order_pnls) == 2
    assert all(pnl is not None for pnl in order_pnls.values())
    assert sum(order_pnls.values()) == pytest.approx(round_.realized_pnl)
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
        up_ask=0.71,
        up_bid=0.70,
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


def test_official_outcome_is_backfilled_without_changing_closed_trade(tmp_path) -> None:
    start = datetime.now(timezone.utc) - timedelta(minutes=10)
    strategy, registry, current = engine(tmp_path, start)
    open_initial(strategy, current, start)
    strategy.evaluate(
        current,
        books(
            start + timedelta(seconds=2),
            up_ask=0.82,
            up_bid=0.81,
            down_ask=0.20,
            down_bid=0.19,
        ),
        start + timedelta(seconds=2),
    )
    assert strategy.current_round is not None
    before = strategy.current_round
    assert before.close_reason == "direct_target"
    before_pnl = before.realized_pnl
    assert current.slug in strategy.unresolved_slugs()

    recorded = strategy.settle(
        current.slug,
        Direction.DOWN,
        current.end_time + timedelta(seconds=2),
    )

    assert recorded is not None
    assert recorded.official_outcome == Direction.DOWN
    assert recorded.phase == RecoveryPhase.CLOSED
    assert recorded.close_reason == "direct_target"
    assert recorded.realized_pnl == before_pnl
    assert recorded.payout_usd == 0
    assert strategy.status == "direct_target"
    assert current.slug not in strategy.unresolved_slugs()
    recent_orders = strategy.dashboard_state()["recent_orders"]
    assert recent_orders
    assert {
        order["official_outcome"] for order in recent_orders
    } == {Direction.DOWN.value}
    assert all(
        order["net_pnl"] == pytest.approx(recorded.realized_pnl)
        for order in recent_orders
    )
    assert strategy.settle(current.slug, Direction.DOWN) is None
    registry.close()


def test_official_outcome_is_recorded_for_no_trade_round(tmp_path) -> None:
    start = datetime.now(timezone.utc) - timedelta(minutes=10)
    strategy, registry, current = engine(tmp_path, start)
    strategy.evaluate(current, {}, current.end_time)

    assert strategy.current_round is not None
    assert strategy.current_round.phase == RecoveryPhase.NO_TRADE

    recorded = strategy.settle(
        current.slug,
        Direction.UP,
        current.end_time + timedelta(seconds=2),
    )

    assert recorded is not None
    assert recorded.official_outcome == Direction.UP
    assert recorded.phase == RecoveryPhase.NO_TRADE
    assert recorded.close_reason == "no_trade"
    assert recorded.realized_pnl is None
    assert registry.summary()["official_outcomes_recorded"] == 1
    registry.close()


def test_restart_restores_open_position_and_resumes_waiting_round(tmp_path) -> None:
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
    assert restarted.current_round.phase == RecoveryPhase.WAITING_ENTRY_WINDOW
    restarted.evaluate(
        waiting_market,
        books(
            start + timedelta(seconds=1),
            up_ask=0.71,
            up_bid=0.70,
            down_ask=0.29,
            down_bid=0.28,
        ),
        start + timedelta(seconds=1),
    )
    assert restarted.current_round.initial_fill is not None
    assert restarted.current_round.initial_fill.direction == Direction.UP
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
