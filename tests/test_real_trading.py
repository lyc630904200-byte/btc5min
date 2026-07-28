from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from polybtc.btc_recovery import RecoveryFill
from polybtc.config import AppConfig
from polybtc.models import (
    BookLevel,
    Direction,
    MarketState,
    OrderBookSnapshot,
    OrderSide,
)
from polybtc.real_trading import (
    AccountSnapshot,
    RealOrderRecord,
    RealOrderStatus,
    PolymarketSdkAdapter,
    RealTradingCredentials,
    RealTradingEngine,
    RealTradingRegistry,
    SubmissionResult,
)


NOW = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)


class FakeAdapter:
    def __init__(self) -> None:
        self.account = AccountSnapshot(
            connected=True,
            wallet="0x1234567890abcdef",
            wallet_type="DEPOSIT_WALLET",
            balance_usd=100,
            allowance_usd=100,
            geoblocked=False,
            user_ws_connected=True,
        )
        self.build_count = 0
        self.post_count = 0
        self.redeem_count = 0
        self.raise_post = False

    async def connect(self) -> AccountSnapshot:
        return self.account

    async def prepare_allowance(self) -> AccountSnapshot:
        return self.account

    async def build_fok_buy(
        self, token_id: str, amount_usd: float, max_price: float
    ) -> dict[str, Any]:
        self.build_count += 1
        return {
            "token_id": token_id,
            "amount_usd": amount_usd,
            "max_price": max_price,
        }

    async def post_order(self, signed_order: Any) -> SubmissionResult:
        self.post_count += 1
        if self.raise_post:
            raise TimeoutError("unknown submission result")
        return SubmissionResult(
            accepted=True,
            order_id=f"exchange-{self.post_count}",
            status="matched",
            making_amount=signed_order["amount_usd"],
            taking_amount=signed_order["amount_usd"] / 0.93,
        )

    async def reconcile(self, order: RealOrderRecord) -> dict[str, Any] | None:
        return {
            "quantity": order.filled_quantity,
            "quote": order.filled_quote,
            "fee_usd": order.fee_usd,
            "confirmed": True,
        }

    async def redeem(self, condition_id: str) -> str:
        self.redeem_count += 1
        return "0xredeemed"

    async def close(self) -> None:
        return None


def market(index: int, minimum: float = 5) -> MarketState:
    start = NOW + timedelta(minutes=5 * index)
    return MarketState(
        asset="BTC",
        condition_id=f"condition-{index}",
        slug=f"btc-updown-5m-{index}",
        question="BTC Up or Down",
        threshold_price=100_000,
        threshold_verified=True,
        start_time=start,
        end_time=start + timedelta(minutes=5),
        up_token_id=f"up-{index}",
        down_token_id=f"down-{index}",
        min_order_size=minimum,
        tick_size=0.01,
    )


def books(item: MarketState) -> dict[Direction, OrderBookSnapshot]:
    return {
        Direction.UP: OrderBookSnapshot(
            token_id=item.up_token_id,
            market_id=item.condition_id,
            timestamp=NOW,
            received_at=NOW,
            bids=[BookLevel(price=0.92, size=100)],
            asks=[BookLevel(price=0.93, size=100)],
            depth_trusted=True,
            min_order_size=item.min_order_size,
            tick_size=0.01,
        ),
        Direction.DOWN: OrderBookSnapshot(
            token_id=item.down_token_id,
            market_id=item.condition_id,
            timestamp=NOW,
            received_at=NOW,
            bids=[BookLevel(price=0.06, size=100)],
            asks=[BookLevel(price=0.07, size=100)],
            depth_trusted=True,
            min_order_size=item.min_order_size,
            tick_size=0.01,
        ),
    }


def signal(item: MarketState, number: int) -> RecoveryFill:
    return RecoveryFill(
        order_number=number,
        trade_order_number=number,
        round_id=f"round-{number}",
        market_id=item.condition_id,
        market_slug=item.slug,
        stage="initial",
        direction=Direction.UP,
        side=OrderSide.BUY,
        avg_price=0.93,
        quantity=10,
        quote=9.3,
        fee_usd=0.04,
        levels_used=1,
        reason="initial_entry",
        created_at=NOW,
    )


def engine(
    path: Path,
    *,
    quantity: float = 5,
    required: int = 2,
) -> tuple[RealTradingEngine, RealTradingRegistry, FakeAdapter]:
    config = AppConfig()
    config.btc_recovery.enabled = True
    config.btc_recovery.max_entry_price_cents = 95
    config.real_trading.enabled = True
    config.real_trading.order_quantity = quantity
    config.real_trading.max_order_notional_usd = 20
    config.real_trading.daily_loss_limit_usd = 50
    config.real_trading.shadow_required_signals = required
    registry = RealTradingRegistry(path)
    adapter = FakeAdapter()
    result = RealTradingEngine(
        config,
        registry,
        credentials=RealTradingCredentials("0xprivate"),
        adapter_factory=lambda _credentials, _callback: adapter,
    )
    return result, registry, adapter


def test_quantity_below_official_minimum_is_observe_only(tmp_path: Path) -> None:
    real, registry, adapter = engine(tmp_path / "real.sqlite3", quantity=1)
    item = market(1)

    async def run() -> RealOrderRecord | None:
        await real.connect()
        return await real.on_initial_signal(signal(item, 1), item, books(item), NOW)

    order = asyncio.run(run())
    assert order is not None
    assert order.status == RealOrderStatus.BLOCKED
    assert order.reason == "below_official_min_order_size"
    assert adapter.build_count == 0
    assert real.shadow_gate()["successful_signals"] == 0
    registry.close()


def test_shadow_orders_unlock_live_and_real_order_uses_fok_path(
    tmp_path: Path,
) -> None:
    real, registry, adapter = engine(tmp_path / "real.sqlite3", required=2)

    async def run() -> RealOrderRecord | None:
        await real.connect()
        for index in (1, 2):
            item = market(index)
            await real.on_initial_signal(
                signal(item, index), item, books(item), NOW
            )
        assert real.shadow_gate()["unlocked"] is True
        assert real.arm() is True
        item = market(3)
        return await real.on_initial_signal(signal(item, 3), item, books(item), NOW)

    order = asyncio.run(run())
    assert order is not None
    assert order.mode == "live"
    assert order.status == RealOrderStatus.MATCHED
    assert order.exchange_order_id == "exchange-1"
    assert order.filled_quantity == 5
    assert order.filled_quote == 4.65
    assert adapter.build_count == 3
    assert adapter.post_count == 1
    registry.close()


def test_unknown_submission_is_not_retried_for_same_market(tmp_path: Path) -> None:
    real, registry, adapter = engine(tmp_path / "real.sqlite3", required=1)

    async def run() -> tuple[RealOrderRecord | None, RealOrderRecord | None]:
        await real.connect()
        first_market = market(1)
        await real.on_initial_signal(
            signal(first_market, 1), first_market, books(first_market), NOW
        )
        assert real.arm()
        adapter.raise_post = True
        live_market = market(2)
        first = await real.on_initial_signal(
            signal(live_market, 2), live_market, books(live_market), NOW
        )
        second = await real.on_initial_signal(
            signal(live_market, 2), live_market, books(live_market), NOW
        )
        return first, second

    first, second = asyncio.run(run())
    assert first is not None and first.status == RealOrderStatus.UNCERTAIN
    assert second is not None and second.intent_id == first.intent_id
    assert adapter.post_count == 1
    assert real.armed is False
    registry.close()


def test_winning_live_order_is_settled_and_redeemed_once(tmp_path: Path) -> None:
    real, registry, adapter = engine(tmp_path / "real.sqlite3", required=1)

    async def run() -> RealOrderRecord:
        await real.connect()
        shadow_market = market(1)
        await real.on_initial_signal(
            signal(shadow_market, 1), shadow_market, books(shadow_market), NOW
        )
        assert real.arm()
        live_market = market(2)
        await real.on_initial_signal(
            signal(live_market, 2), live_market, books(live_market), NOW
        )
        await real.settle(live_market.slug, Direction.UP)
        await real.settle(live_market.slug, Direction.UP)
        order = registry.by_market(live_market.condition_id)
        assert order is not None
        return order

    order = asyncio.run(run())
    assert order.status == RealOrderStatus.REDEEMED
    assert order.redeem_tx_hash == "0xredeemed"
    assert order.payout_usd == 5
    assert order.net_pnl is not None and order.net_pnl > 0
    assert adapter.redeem_count == 1
    registry.close()


def test_emergency_stop_persists_but_arming_never_does(tmp_path: Path) -> None:
    path = tmp_path / "real.sqlite3"
    real, registry, _adapter = engine(path, required=1)
    real.emergency_stop()
    registry.close()

    restored, restored_registry, _adapter = engine(path, required=1)
    assert restored.emergency_stopped is True
    assert restored.armed is False
    restored.resume()
    restored_registry.close()

    again, again_registry, _adapter = engine(path, required=1)
    assert again.emergency_stopped is False
    assert again.armed is False
    again_registry.close()


def test_private_key_never_enters_dashboard_state_or_ledger(tmp_path: Path) -> None:
    path = tmp_path / "real.sqlite3"
    private_key = "0xdo-not-persist-this-private-key"
    config = AppConfig()
    config.real_trading.enabled = True
    registry = RealTradingRegistry(path)
    adapter = FakeAdapter()
    real = RealTradingEngine(
        config,
        registry,
        credentials=RealTradingCredentials(private_key),
        adapter_factory=lambda _credentials, _callback: adapter,
    )

    state = json.dumps(real.dashboard_state())
    registry.close()

    assert private_key not in state
    assert private_key.encode() not in path.read_bytes()


def test_local_block_does_not_consume_live_order_limit_or_create_position(
    tmp_path: Path,
) -> None:
    real, registry, adapter = engine(
        tmp_path / "real.sqlite3", quantity=5, required=1
    )

    async def run() -> RealOrderRecord | None:
        await real.connect()
        first = market(1)
        await real.on_initial_signal(signal(first, 1), first, books(first), NOW)
        assert real.arm()
        real.config.real_trading.order_quantity = 1
        blocked_market = market(2)
        blocked = await real.on_initial_signal(
            signal(blocked_market, 2), blocked_market, books(blocked_market), NOW
        )
        assert blocked is not None
        assert blocked.status == RealOrderStatus.BLOCKED

        real.config.real_trading.order_quantity = 5
        next_market = market(3)
        return await real.on_initial_signal(
            signal(next_market, 3), next_market, books(next_market), NOW
        )

    order = asyncio.run(run())
    assert order is not None
    assert order.status == RealOrderStatus.MATCHED
    assert adapter.post_count == 1
    registry.close()


def test_restart_submitting_intent_is_reconciled_without_resubmission(
    tmp_path: Path,
) -> None:
    real, registry, adapter = engine(
        tmp_path / "real.sqlite3", quantity=5, required=1
    )
    item = market(1)
    pending = RealOrderRecord(
        market_id=item.condition_id,
        market_slug=item.slug,
        token_id=item.up_token_id,
        direction=Direction.UP,
        mode="live",
        status=RealOrderStatus.SUBMITTING,
        reason="live_submit",
        quantity=5,
        max_price=0.95,
        filled_quantity=5,
        filled_quote=4.65,
        created_at=NOW,
        updated_at=NOW,
    )
    registry.save(pending)

    async def run() -> None:
        await real.connect()
        await real.reconcile(force=True)

    asyncio.run(run())
    restored = registry.by_market(item.condition_id)
    assert restored is not None
    assert restored.status == RealOrderStatus.CONFIRMED
    assert adapter.post_count == 0
    registry.close()


def test_sdk_adapter_builds_dollar_amount_fok_with_max_price() -> None:
    calls: list[dict[str, Any]] = []

    class Client:
        async def create_market_order(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {"signed": True}

        async def close(self) -> None:
            return None

    adapter = PolymarketSdkAdapter(RealTradingCredentials("0xprivate"))
    adapter.client = Client()

    async def run() -> dict[str, Any]:
        try:
            return await adapter.build_fok_buy("token-1", 4.65, 0.95)
        finally:
            await adapter.close()

    signed = asyncio.run(run())
    assert signed == {"signed": True}
    assert calls == [
        {
            "token_id": "token-1",
            "side": "BUY",
            "amount": Decimal("4.65"),
            "max_price": Decimal("0.95"),
            "order_type": "FOK",
        }
    ]
