from __future__ import annotations

import ast
import asyncio
import inspect
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx

from polybtc.btc_v8 import BtcV8Engine, BtcV8Registry, V8Trade, v8_buy_candidate
from polybtc.config import AppConfig
from polybtc.dashboard import DashboardHub
from polybtc.models import BookLevel, Direction, MarketState, OrderBookSnapshot
from polybtc.orderbook import simulate_sell
from polybtc.orderbook_chase import (
    ChaseAttempt,
    ChaseAttemptStatus,
    ChasePositionStatus,
    ClobLatencyProbe,
    OrderbookChaseEngine,
    OrderbookChaseRegistry,
)
from polybtc.real_trading import PolymarketSdkAdapter, RealTradingCredentials


def make_market(now: datetime) -> MarketState:
    return MarketState(
        asset="BTC",
        condition_id="chase-market",
        slug="btc-updown-5m-shadow",
        question="BTC Up or Down",
        threshold_price=100_000.0,
        threshold_source="polymarket_rtds",
        threshold_verified=True,
        start_time=now,
        end_time=now + timedelta(minutes=5),
        up_token_id="up-token",
        down_token_id="down-token",
        min_order_size=5,
        tick_size=0.01,
    )


def make_book(
    market: MarketState,
    received_at: datetime,
    *,
    bid: float = 0.39,
    ask: float = 0.40,
) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        token_id=market.up_token_id,
        market_id=market.condition_id,
        timestamp=received_at,
        received_at=received_at,
        bids=[BookLevel(price=bid, size=100)],
        asks=[BookLevel(price=ask, size=100)],
        depth_trusted=True,
        min_order_size=5,
        tick_size=0.01,
    )


class RestrictedSigner:
    def __init__(self) -> None:
        self.buy_calls: list[tuple[str, float, float]] = []
        self.sell_calls: list[tuple[str, float, float]] = []
        self.closed = False

    async def connect(self) -> bool:
        return True

    async def build_fok_buy(
        self, token_id: str, amount_usd: float, max_price: float
    ) -> dict[str, Any]:
        self.buy_calls.append((token_id, amount_usd, max_price))
        return {"signed": "buy"}

    async def build_fok_sell(
        self, token_id: str, quantity: float, min_price: float
    ) -> dict[str, Any]:
        self.sell_calls.append((token_id, quantity, min_price))
        return {"signed": "sell"}

    async def close(self) -> None:
        self.closed = True


def make_engine(tmp_path):
    config = AppConfig(
        data_dir=tmp_path,
        risk={"max_data_age_ms": 5_000},
        btc_v8={"enabled": True, "orderbook_chase_mode": True},
        orderbook_chase={
            "enabled": True,
            "latency_min_samples": 1,
        },
    )
    registry = OrderbookChaseRegistry(tmp_path / "orderbook-chase-ledger.sqlite3")
    source_registry = BtcV8Registry(tmp_path / "btc-v8-ledger.sqlite3")
    source_v8 = BtcV8Engine(config, source_registry)
    signer = RestrictedSigner()
    engine = OrderbookChaseEngine(config, registry, source_v8, signer=signer)
    return engine, source_v8, registry, source_registry, signer


def test_public_buy_candidate_matches_engine_path(tmp_path) -> None:
    engine, source_v8, registry, source_registry, signer = make_engine(tmp_path)
    now = datetime.now(timezone.utc)
    market = make_market(now)
    book = make_book(market, now)
    source_v8.set_market(market, now)
    engine.set_market(market, now)

    direct = v8_buy_candidate(
        market=market,
        direction=Direction.UP,
        probability=0.65,
        formula_probability=0.62,
        settings=source_v8.current_round.settings,
        book=book,
        book_reason=None,
        taker_fee_rate=engine.config.strategy.taker_fee_rate,
        valuation_probability=0.66,
        strategy_mode="orderbook_chase",
        decision_details={"signal_strength": 2.0},
    )
    through_engine = source_v8._buy_candidate(
        market,
        Direction.UP,
        0.65,
        0.62,
        {Direction.UP: book},
        now,
        valuation_probability=0.66,
        strategy_mode="orderbook_chase",
        decision_details={"signal_strength": 2.0},
    )

    assert through_engine == direct
    asyncio.run(engine.close())
    registry.close()
    source_registry.close()
    assert signer.closed is True


def test_delayed_paths_use_new_book_and_never_submit(tmp_path) -> None:
    engine, source_v8, registry, source_registry, signer = make_engine(tmp_path)
    now = datetime.now(timezone.utc)
    market = make_market(now)
    signal_book = make_book(market, now, ask=0.40)
    source_v8.set_market(market, now)
    engine.set_market(market, now)
    engine.latency.record(now, 20.0)
    source_v8.candidates[Direction.UP.value] = {
        "limit": 0.50,
        "valuation_probability": 0.65,
        "decision_details": {"signal_strength": 2.0},
    }
    trade = V8Trade(
        market_id=market.condition_id,
        market_slug=market.slug,
        position_id="instant-position",
        action="BUY",
        direction=Direction.UP,
        model_probability=0.65,
        formula_probability=0.62,
        avg_price=0.40,
        quantity=12.5,
        quote=5.0,
        fee_usd=0.0,
        edge_per_share=0.20,
        reason="chase_buy",
        strategy_mode="orderbook_chase",
        created_at=now,
    )

    async def run() -> None:
        def duplicate_evaluate(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("shadow executor must not evaluate V8 again")

        source_v8.evaluate = duplicate_evaluate  # type: ignore[method-assign]
        await engine.evaluate(
            market,
            {Direction.UP: signal_book},
            [trade],
            now,
        )
        execution_time = now + timedelta(seconds=1)
        moved_book = make_book(
            market,
            now + timedelta(milliseconds=50),
            ask=0.45,
        )
        await engine._resolve_pending(
            {Direction.UP: moved_book},
            execution_time,
        )
        await engine.close()

    asyncio.run(run())

    attempts = registry.recent_attempts()
    assert len(attempts) == 2
    assert {attempt.lane for attempt in attempts} == {"observed", "p95"}
    assert all(attempt.status == ChaseAttemptStatus.MATCHED for attempt in attempts)
    assert all(attempt.expected_avg_price == 0.40 for attempt in attempts)
    assert all(attempt.filled_avg_price == 0.45 for attempt in attempts)
    assert len(registry.open_positions()) == 2
    assert signer.buy_calls == [(market.up_token_id, 5.0, 0.5)]
    assert not hasattr(signer, "post_order")

    engine_tree = ast.parse(inspect.getsource(OrderbookChaseEngine))
    called_attributes = {
        node.func.attr
        for node in ast.walk(engine_tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "post_order" not in called_attributes
    assert "post" not in called_attributes
    assert engine.source_v8 is source_v8
    registry.close()
    source_registry.close()


def test_position_crossing_market_end_waits_for_official_settlement(tmp_path) -> None:
    engine, source_v8, registry, source_registry, signer = make_engine(tmp_path)
    now = datetime.now(timezone.utc)
    market = make_market(now)
    buy_book = make_book(market, now, ask=0.40)
    source_v8.set_market(market, now)
    engine.set_market(market, now)
    engine.latency.record(now, 20.0)
    source_v8.candidates[Direction.UP.value] = {
        "limit": 0.50,
        "valuation_probability": 0.65,
        "decision_details": {"signal_strength": 2.0},
    }
    trade = V8Trade(
        market_id=market.condition_id,
        market_slug=market.slug,
        position_id="settlement-position",
        action="BUY",
        direction=Direction.UP,
        model_probability=0.65,
        formula_probability=0.62,
        avg_price=0.40,
        quantity=12.5,
        quote=5.0,
        fee_usd=0.0,
        edge_per_share=0.20,
        reason="chase_buy",
        strategy_mode="orderbook_chase",
        created_at=now,
    )

    async def run() -> None:
        await engine._handle_instant_buy(trade, {Direction.UP: buy_book}, now)
        entry_time = now + timedelta(seconds=1)
        entry_book = make_book(
            market,
            now + timedelta(milliseconds=50),
            bid=0.40,
            ask=0.41,
        )
        await engine._resolve_pending({Direction.UP: entry_book}, entry_time)
        assert len(registry.open_positions()) == 2

        after_end = market.end_time + timedelta(seconds=1)
        settlement_book = make_book(market, after_end, bid=0.55, ask=0.56)
        await engine._evaluate_positions({Direction.UP: settlement_book}, after_end)

        positions = list(engine.positions.values())
        assert {position.status for position in positions} == {
            ChasePositionStatus.HOLD_TO_SETTLEMENT
        }
        assert all(position.exit_reason == "sell_window_closed" for position in positions)
        assert signer.sell_calls == []

        engine.settle(market.slug, Direction.UP, after_end + timedelta(seconds=1))
        assert {position.status for position in positions} == {ChasePositionStatus.SETTLED}
        assert all(position.exit_reason == "official_settlement" for position in positions)
        assert all(position.realized_pnl is not None for position in positions)
        await engine.close()

    asyncio.run(run())
    registry.close()
    source_registry.close()


def test_sell_retries_only_after_a_newer_book(tmp_path) -> None:
    engine, source_v8, registry, source_registry, signer = make_engine(tmp_path)
    now = datetime.now(timezone.utc)
    market = make_market(now)
    source_v8.set_market(market, now)
    engine.set_market(market, now)
    engine.latency.record(now, 10.0)
    buy_book = make_book(market, now, ask=0.40)
    source_v8.candidates[Direction.UP.value] = {
        "limit": 0.50,
        "valuation_probability": 0.65,
        "decision_details": {"signal_strength": 2.0},
    }
    trade = V8Trade(
        market_id=market.condition_id,
        market_slug=market.slug,
        position_id="instant-position",
        action="BUY",
        direction=Direction.UP,
        model_probability=0.65,
        formula_probability=0.62,
        avg_price=0.40,
        quantity=12.5,
        quote=5.0,
        fee_usd=0.0,
        edge_per_share=0.20,
        reason="chase_buy",
        strategy_mode="orderbook_chase",
        created_at=now,
    )

    async def run() -> None:
        await engine._handle_instant_buy(trade, {Direction.UP: buy_book}, now)
        entry_book = make_book(
            market,
            now + timedelta(milliseconds=20),
            bid=0.40,
            ask=0.41,
        )
        await engine._resolve_pending(
            {Direction.UP: entry_book},
            now + timedelta(seconds=1),
        )
        position = next(item for item in engine.positions.values() if item.lane == "observed")
        position.exit_reason = "chase_timeout"
        execution = simulate_sell(
            entry_book,
            position.strategy_position.quantity,
            engine.config.strategy.taker_fee_rate,
        )
        await engine._create_sell_attempt(
            position,
            entry_book,
            execution,
            now + timedelta(seconds=1.1),
        )

        moved_book = make_book(
            market,
            now + timedelta(seconds=1.2),
            bid=0.30,
            ask=0.31,
        )
        await engine._resolve_pending(
            {Direction.UP: moved_book},
            now + timedelta(seconds=2),
        )
        assert len(signer.sell_calls) == 1
        await engine._evaluate_positions(
            {Direction.UP: moved_book},
            now + timedelta(seconds=2.1),
        )
        assert len(signer.sell_calls) == 1

        newer_book = make_book(
            market,
            now + timedelta(seconds=2.2),
            bid=0.30,
            ask=0.31,
        )
        await engine._evaluate_positions(
            {Direction.UP: newer_book},
            now + timedelta(seconds=2.2),
        )
        assert len(signer.sell_calls) == 2
        await engine.close()

    asyncio.run(run())
    registry.close()
    source_registry.close()


def test_restart_marks_unresolved_attempt_unmeasurable(tmp_path) -> None:
    path = tmp_path / "orderbook-chase-ledger.sqlite3"
    registry = OrderbookChaseRegistry(path)
    attempt = ChaseAttempt(
        idempotency_key="restart-1",
        strategy_trade_id="trade-1",
        market_id="market-1",
        market_slug="slug-1",
        lane="observed",
        side="BUY",
        direction=Direction.UP,
        token_id="up-token",
        requested_quote=5.0,
        limit_price=0.50,
    )
    registry.save_attempt(attempt)
    registry.close()

    reopened = OrderbookChaseRegistry(path)
    restored = reopened.recent_attempts(1)[0]
    assert restored.status == ChaseAttemptStatus.UNMEASURABLE_RESTART
    assert restored.reason == "process_restarted_before_shadow_result"
    summary = reopened.summary(200, 0.9, 0.8)
    assert summary["lanes"]["observed"]["trials"] == 0
    assert summary["lanes"]["observed"]["unmeasurable"] == 1
    reopened.close()


def test_latency_probe_only_requests_clob_time(tmp_path) -> None:
    config = AppConfig(
        data_dir=tmp_path,
        orderbook_chase={"enabled": True, "latency_min_samples": 1},
    )
    registry = OrderbookChaseRegistry(tmp_path / "orderbook-chase-ledger.sqlite3")
    probe = ClobLatencyProbe(config, registry)
    requests: list[tuple[str, str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        assert request.method == "GET"
        assert request.url.path == "/time"
        return httpx.Response(200, json={"timestamp": 1})

    async def run() -> None:
        await probe._client.aclose()
        probe._client = httpx.AsyncClient(
            base_url=config.sources.clob_url,
            transport=httpx.MockTransport(handle),
        )
        await probe.sample()
        await probe.close()

    asyncio.run(run())
    assert requests == [("GET", "/time")]
    assert probe.snapshot()["sample_count"] == 1
    registry.close()


def test_latency_probe_is_enabled_for_lead_prediction_only(tmp_path) -> None:
    config = AppConfig(
        data_dir=tmp_path,
        btc_lead_prediction={"enabled": True},
    )
    registry = OrderbookChaseRegistry(tmp_path / "orderbook-chase-ledger.sqlite3")
    probe = ClobLatencyProbe(config, registry)

    assert config.orderbook_chase.enabled is False
    assert config.btc_weighted.enabled is False
    assert probe.sampling_enabled() is True

    asyncio.run(probe.close())
    registry.close()


def test_latency_probe_rebuilds_exhausted_connection_pool(tmp_path) -> None:
    config = AppConfig(data_dir=tmp_path, orderbook_chase={"enabled": True})
    registry = OrderbookChaseRegistry(tmp_path / "orderbook-chase-ledger.sqlite3")
    probe = ClobLatencyProbe(config, registry)

    class ExhaustedClient:
        def __init__(self) -> None:
            self.closed = False

        async def get(self, path: str) -> httpx.Response:
            raise httpx.PoolTimeout("pool busy")

        async def aclose(self) -> None:
            self.closed = True

    exhausted = ExhaustedClient()

    async def run() -> None:
        await probe._client.aclose()
        probe._client = exhausted  # type: ignore[assignment]
        await probe.sample()
        assert exhausted.closed is True
        assert probe._client is not exhausted
        await probe.close()

    asyncio.run(run())
    assert probe.last_error == "PoolTimeout: pool busy"
    registry.close()


def test_sdk_buy_and_sell_share_market_order_builder() -> None:
    calls: list[dict[str, Any]] = []

    class Client:
        async def create_market_order(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {"signed": True}

        async def close(self) -> None:
            return None

    adapter = PolymarketSdkAdapter(RealTradingCredentials("0xprivate"))
    adapter.client = Client()

    async def run() -> None:
        await adapter.build_fok_buy("token-1", 5.0, 0.55)
        await adapter.build_fok_sell("token-1", 10.0, 0.45)
        await adapter.close()

    asyncio.run(run())
    assert calls == [
        {
            "token_id": "token-1",
            "side": "BUY",
            "amount": Decimal("5.0"),
            "max_price": Decimal("0.55"),
            "order_type": "FOK",
        },
        {
            "token_id": "token-1",
            "side": "SELL",
            "shares": Decimal("10.0"),
            "min_price": Decimal("0.45"),
            "order_type": "FOK",
        },
    ]


def test_dashboard_saves_chase_config_for_next_btc_market(tmp_path) -> None:
    hub = DashboardHub(
        "127.0.0.1",
        8765,
        "127.0.0.1",
        8766,
        AppConfig(data_dir=tmp_path),
    )
    hub.asset_snapshots["BTC"] = {
        "market": {"asset": "BTC", "condition_id": "btc-old"}
    }

    response = hub.set_runtime_config(
        {
            "orderbook_chase": {
                "enabled": True,
                "latency_min_samples": 40,
            }
        }
    )

    assert response["config_status"] == "pending_next_btc_market"
    assert response["pending_orderbook_chase"]["enabled"] is True
    assert hub.apply_pending_config_for_market("btc-new") is True
    assert hub.config.orderbook_chase.enabled is True
    assert hub.config.orderbook_chase.latency_min_samples == 40
