from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Protocol
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, Field

from .btc_recovery import RecoveryFill, simulate_buy_quantity_limit
from .config import AppConfig
from .models import Direction, MarketState, OrderBookSnapshot
from .sqlite_common import SqliteControlEventStore


SHANGHAI = ZoneInfo("Asia/Shanghai")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json")
        return dumped if isinstance(dumped, dict) else {}
    return {}


def _decimal_value(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _datetime_value(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class RealOrderStatus(StrEnum):
    BLOCKED = "BLOCKED"
    SHADOW_READY = "SHADOW_READY"
    SUBMITTING = "SUBMITTING"
    REJECTED = "REJECTED"
    UNCERTAIN = "UNCERTAIN"
    MATCHED = "MATCHED"
    CONFIRMED = "CONFIRMED"
    SETTLED_WIN = "SETTLED_WIN"
    SETTLED_LOSS = "SETTLED_LOSS"
    REDEEMING = "REDEEMING"
    REDEEMED = "REDEEMED"
    CRITICAL_ERROR = "CRITICAL_ERROR"


class RealOrderRecord(BaseModel):
    intent_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    display_order_number: int | None = None
    market_id: str
    market_slug: str
    token_id: str
    direction: Direction
    signal_order_number: int | None = None
    mode: str
    status: RealOrderStatus
    reason: str
    quantity: float
    max_price: float
    expected_avg_price: float | None = None
    expected_quote: float | None = None
    expected_fee_usd: float | None = None
    exchange_order_id: str | None = None
    exchange_status: str | None = None
    filled_quantity: float = 0.0
    filled_quote: float = 0.0
    fee_usd: float = 0.0
    official_outcome: Direction | None = None
    payout_usd: float | None = None
    net_pnl: float | None = None
    redeem_status: str | None = None
    redeem_tx_hash: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


@dataclass(slots=True)
class RealTradingCredentials:
    private_key: str
    wallet: str | None = None


@dataclass(slots=True)
class AccountSnapshot:
    connected: bool = False
    wallet: str | None = None
    wallet_type: str | None = None
    balance_usd: float | None = None
    allowance_usd: float | None = None
    geoblocked: bool | None = None
    country: str | None = None
    region: str | None = None
    user_ws_connected: bool = False
    last_error: str | None = None


@dataclass(slots=True)
class SubmissionResult:
    accepted: bool
    order_id: str | None = None
    status: str | None = None
    making_amount: float = 0.0
    taking_amount: float = 0.0
    reason: str | None = None


class RealTradingAdapter(Protocol):
    account: AccountSnapshot

    async def connect(self) -> AccountSnapshot: ...

    async def prepare_allowance(self) -> AccountSnapshot: ...

    async def build_fok_buy(
        self, token_id: str, amount_usd: float, max_price: float
    ) -> Any: ...

    async def build_fok_sell(
        self, token_id: str, quantity: float, min_price: float
    ) -> Any: ...

    async def post_order(self, signed_order: Any) -> SubmissionResult: ...

    async def reconcile(self, order: RealOrderRecord) -> dict[str, Any] | None: ...

    async def redeem(self, condition_id: str) -> str | None: ...

    async def close(self) -> None: ...


class PolymarketSdkAdapter:
    def __init__(
        self,
        credentials: RealTradingCredentials,
        on_user_event: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    ):
        self.credentials = credentials
        self.on_user_event = on_user_event
        self.account = AccountSnapshot()
        self.client: Any = None
        self._user_task: asyncio.Task[None] | None = None
        self._http = httpx.AsyncClient(timeout=10)

    async def connect(self) -> AccountSnapshot:
        geo_response = await self._http.get("https://polymarket.com/api/geoblock")
        geo_response.raise_for_status()
        geo = geo_response.json()
        self.account.geoblocked = bool(geo.get("blocked"))
        self.account.country = str(geo.get("country") or "") or None
        self.account.region = str(geo.get("region") or "") or None
        if self.account.geoblocked:
            self.account.last_error = "geographic_restriction"
            return self.account

        from polymarket import AsyncSecureClient

        self.client = await AsyncSecureClient.create(
            private_key=self.credentials.private_key,
            wallet=self.credentials.wallet,
        )
        self.account.connected = True
        self.account.wallet = str(self.client.wallet)
        self.account.wallet_type = str(self.client.wallet_type)
        await self._refresh_balance()
        self._user_task = asyncio.create_task(self._run_user_stream())
        return self.account

    async def _refresh_balance(self) -> None:
        balance = await self.client.get_balance_allowance(asset_type="COLLATERAL")
        self.account.balance_usd = _decimal_value(balance.balance) / 1_000_000
        allowances = [
            _decimal_value(value) / 1_000_000
            for value in dict(balance.allowances or {}).values()
        ]
        self.account.allowance_usd = min(allowances) if allowances else 0.0

    async def _run_user_stream(self) -> None:
        try:
            from polymarket.streams import UserSpec

            handle = await self.client.subscribe(UserSpec())
            async with handle:
                self.account.user_ws_connected = True
                async for event in handle:
                    callback = self.on_user_event
                    if callback is None:
                        continue
                    result = callback(_payload(event))
                    if result is not None:
                        await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.account.last_error = f"user_ws: {type(exc).__name__}: {exc}"
        finally:
            self.account.user_ws_connected = False

    async def prepare_allowance(self) -> AccountSnapshot:
        method = getattr(self.client, "setup_trading_approvals", None)
        if method is None:
            raise RuntimeError("official SDK does not expose setup_trading_approvals")
        result = method()
        if result is not None and hasattr(result, "__await__"):
            result = await result
        if result is not None and hasattr(result, "wait"):
            waited = result.wait()
            if waited is not None and hasattr(waited, "__await__"):
                await waited
        await self._refresh_balance()
        return self.account

    async def build_fok_buy(
        self, token_id: str, amount_usd: float, max_price: float
    ) -> Any:
        return await self._build_fok_market_order(
            token_id=token_id,
            side="BUY",
            size=amount_usd,
            limit_price=max_price,
        )

    async def build_fok_sell(
        self, token_id: str, quantity: float, min_price: float
    ) -> Any:
        return await self._build_fok_market_order(
            token_id=token_id,
            side="SELL",
            size=quantity,
            limit_price=min_price,
        )

    async def _build_fok_market_order(
        self,
        *,
        token_id: str,
        side: Literal["BUY", "SELL"],
        size: float,
        limit_price: float,
    ) -> Any:
        size_decimal = Decimal(str(size))
        price_decimal = Decimal(str(limit_price))
        if side == "BUY":
            return await self.client.create_market_order(
                token_id=token_id,
                side=side,
                amount=size_decimal,
                max_price=price_decimal,
                order_type="FOK",
            )
        return await self.client.create_market_order(
            token_id=token_id,
            side=side,
            shares=size_decimal,
            min_price=price_decimal,
            order_type="FOK",
        )

    async def post_order(self, signed_order: Any) -> SubmissionResult:
        response = await self.client.post_order(signed_order)
        payload = _payload(response)
        accepted = bool(payload.get("ok"))
        return SubmissionResult(
            accepted=accepted,
            order_id=str(payload.get("order_id") or "") or None,
            status=str(payload.get("status") or "") or None,
            making_amount=_decimal_value(payload.get("making_amount")),
            taking_amount=_decimal_value(payload.get("taking_amount")),
            reason=str(payload.get("message") or payload.get("code") or "") or None,
        )

    async def reconcile(self, order: RealOrderRecord) -> dict[str, Any] | None:
        page = await self.client.list_account_trades(
            market=order.market_id
        ).first_page()
        matching: list[dict[str, Any]] = []
        for trade in page.items:
            payload = _payload(trade)
            maker_ids = {
                str(item.get("order_id") or "")
                for item in payload.get("maker_orders") or []
                if isinstance(item, dict)
            }
            known_order_match = bool(order.exchange_order_id) and (
                str(payload.get("taker_order_id") or "") == order.exchange_order_id
                or order.exchange_order_id in maker_ids
            )
            unknown_order_match = (
                not order.exchange_order_id
                and str(payload.get("token_id") or payload.get("asset_id") or "")
                == order.token_id
                and str(payload.get("side") or "").upper() == "BUY"
                and abs(_decimal_value(payload.get("size")) - order.quantity) <= 1e-8
                and _decimal_value(payload.get("price")) <= order.max_price + 1e-9
                and (
                    (matched_at := _datetime_value(payload.get("matched_at")))
                    is not None
                )
                and abs((matched_at - order.created_at).total_seconds()) <= 30
            )
            if known_order_match or unknown_order_match:
                matching.append(payload)
        if not matching:
            return None
        if not order.exchange_order_id and len(matching) != 1:
            return None
        recovered_order_id = (
            str(matching[0].get("taker_order_id") or "") or None
            if not order.exchange_order_id
            else order.exchange_order_id
        )
        quantity = sum(
            _decimal_value(item.get("size") or item.get("taking_amount"))
            for item in matching
        )
        quote = sum(
            _decimal_value(item.get("size")) * _decimal_value(item.get("price"))
            for item in matching
        )
        fees = sum(
            _decimal_value(item.get("size"))
            * (_decimal_value(item.get("fee_rate_bps")) / 10_000)
            * _decimal_value(item.get("price"))
            * (1 - _decimal_value(item.get("price")))
            for item in matching
        )
        statuses = {str(item.get("status") or "") for item in matching}
        return {
            "quantity": quantity or order.filled_quantity,
            "quote": quote or order.filled_quote,
            "fee_usd": fees or order.fee_usd,
            "confirmed": bool(statuses & {"CONFIRMED", "confirmed"}),
            "exchange_order_id": recovered_order_id,
        }

    async def redeem(self, condition_id: str) -> str | None:
        handle = await self.client.redeem_positions(condition_id=condition_id)
        result = await handle.wait()
        payload = _payload(result)
        return str(
            payload.get("transaction_hash")
            or payload.get("tx_hash")
            or getattr(handle, "transaction_hash", "")
            or ""
        ) or None

    async def close(self) -> None:
        if self._user_task is not None:
            self._user_task.cancel()
            await asyncio.gather(self._user_task, return_exceptions=True)
        if self.client is not None:
            await self.client.close()
        await self._http.aclose()


class RealTradingRegistry:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS real_trading_orders (
                intent_id TEXT PRIMARY KEY,
                display_order_number INTEGER NOT NULL UNIQUE,
                market_id TEXT NOT NULL UNIQUE,
                market_slug TEXT NOT NULL,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS real_trading_controls (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS real_trading_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        self.connection.commit()
        self.common = SqliteControlEventStore(
            self.connection,
            controls_table="real_trading_controls",
            events_table="real_trading_events",
        )

    def close(self) -> None:
        self.connection.close()

    def save(self, order: RealOrderRecord) -> RealOrderRecord:
        payload = order.model_dump(mode="json")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            if order.display_order_number is None:
                row = self.connection.execute(
                    "SELECT COALESCE(MAX(display_order_number), 0) + 1 AS value "
                    "FROM real_trading_orders"
                ).fetchone()
                order.display_order_number = int(row["value"])
                payload["display_order_number"] = order.display_order_number
            self.connection.execute(
                """
                INSERT INTO real_trading_orders (
                    intent_id, display_order_number, market_id, market_slug,
                    mode, status, created_at, updated_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(intent_id) DO UPDATE SET
                    mode=excluded.mode,
                    status=excluded.status,
                    updated_at=excluded.updated_at,
                    payload_json=excluded.payload_json
                """,
                (
                    order.intent_id,
                    order.display_order_number,
                    order.market_id,
                    order.market_slug,
                    order.mode,
                    order.status.value,
                    payload["created_at"],
                    payload["updated_at"],
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            self.connection.commit()
            return order
        except Exception:
            self.connection.rollback()
            raise

    def by_market(self, market_id: str) -> RealOrderRecord | None:
        row = self.connection.execute(
            "SELECT payload_json FROM real_trading_orders WHERE market_id=?",
            (market_id,),
        ).fetchone()
        return (
            RealOrderRecord.model_validate(json.loads(row["payload_json"]))
            if row
            else None
        )

    def recent(self, limit: int = 300) -> list[RealOrderRecord]:
        rows = self.connection.execute(
            "SELECT payload_json FROM real_trading_orders "
            "ORDER BY display_order_number DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            RealOrderRecord.model_validate(json.loads(row["payload_json"]))
            for row in rows
        ]

    def nonterminal(self) -> list[RealOrderRecord]:
        terminal = {
            RealOrderStatus.BLOCKED.value,
            RealOrderStatus.SHADOW_READY.value,
            RealOrderStatus.REJECTED.value,
            RealOrderStatus.SETTLED_LOSS.value,
            RealOrderStatus.REDEEMED.value,
            RealOrderStatus.CRITICAL_ERROR.value,
        }
        placeholders = ",".join("?" for _ in terminal)
        rows = self.connection.execute(
            f"SELECT payload_json FROM real_trading_orders "
            f"WHERE status NOT IN ({placeholders})",
            tuple(terminal),
        ).fetchall()
        return [
            RealOrderRecord.model_validate(json.loads(row["payload_json"]))
            for row in rows
        ]

    def set_control(self, key: str, value: Any) -> None:
        self.common.set_control(key, value)

    def get_control(self, key: str, default: Any = None) -> Any:
        return self.common.get_control(key, default)

    def event(self, event_type: str, payload: dict[str, Any]) -> None:
        self.common.event(event_type, payload)

    def shadow_counts(self) -> tuple[int, int]:
        reset_at = self.get_control("shadow_reset_at")
        where = "WHERE mode='shadow'"
        args: tuple[Any, ...] = ()
        if reset_at:
            where += " AND created_at>=?"
            args = (str(reset_at),)
        rows = self.connection.execute(
            f"SELECT status, COUNT(*) AS count FROM real_trading_orders {where} "
            "GROUP BY status",
            args,
        ).fetchall()
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        return (
            counts.get(RealOrderStatus.SHADOW_READY.value, 0),
            counts.get(RealOrderStatus.CRITICAL_ERROR.value, 0),
        )

    def today_live_orders(self, now: datetime | None = None) -> list[RealOrderRecord]:
        current = (now or utc_now()).astimezone(SHANGHAI)
        return [
            order
            for order in self.recent(5000)
            if order.mode == "live"
            and order.created_at.astimezone(SHANGHAI).date() == current.date()
        ]


class RealTradingEngine:
    def __init__(
        self,
        config: AppConfig,
        registry: RealTradingRegistry,
        credentials: RealTradingCredentials | None = None,
        adapter_factory: Callable[
            [RealTradingCredentials, Callable[[dict[str, Any]], Awaitable[None] | None]],
            RealTradingAdapter,
        ] = PolymarketSdkAdapter,
    ):
        self.config = config
        self.registry = registry
        self.credentials = credentials
        self.adapter_factory = adapter_factory
        self.adapter: RealTradingAdapter | None = None
        self.account = AccountSnapshot()
        self.armed = False
        self.emergency_stopped = bool(
            self.registry.get_control("emergency_stopped", False)
        )
        self.status = "disabled" if not config.real_trading.enabled else "shadow"
        self.last_reason = self.status
        self.last_market: MarketState | None = None
        self.last_reconcile_at = datetime.min.replace(tzinfo=timezone.utc)
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def connect(self) -> None:
        if self.credentials is None:
            self.status = "credentials_missing"
            self.last_reason = self.status
            return
        if self.adapter is None:
            self.adapter = self.adapter_factory(
                self.credentials, self._on_user_event
            )
        self.status = "connecting"
        try:
            self.account = await self.adapter.connect()
        except Exception as exc:
            self.account.last_error = f"{type(exc).__name__}: {exc}"
            self.status = "connection_error"
            self.last_reason = self.account.last_error
            return
        if self.account.geoblocked:
            self.status = "geoblocked"
            self.last_reason = self.status
        elif self.account.connected:
            self.status = "shadow"
            self.last_reason = "connected"

    async def prepare_allowance(self) -> None:
        if self.adapter is None or not self.account.connected:
            self.last_reason = "account_not_connected"
            return
        try:
            self.account = await self.adapter.prepare_allowance()
            self.last_reason = "allowance_ready"
        except Exception as exc:
            self.last_reason = f"allowance_error: {type(exc).__name__}: {exc}"

    def shadow_gate(self) -> dict[str, Any]:
        successes, serious_errors = self.registry.shadow_counts()
        required = self.config.real_trading.shadow_required_signals
        return {
            "successful_signals": successes,
            "serious_errors": serious_errors,
            "required_signals": required,
            "unlocked": successes >= required and serious_errors == 0,
        }

    def arm(self) -> bool:
        gate = self.shadow_gate()
        if not self.config.real_trading.enabled:
            self.last_reason = "real_trading_disabled"
            return False
        if not gate["unlocked"]:
            self.last_reason = "shadow_gate_not_met"
            return False
        if self.emergency_stopped:
            self.last_reason = "emergency_stopped"
            return False
        if not self.account.connected or self.account.geoblocked:
            self.last_reason = "account_not_ready"
            return False
        if not self.account.user_ws_connected:
            self.last_reason = "user_ws_not_ready"
            return False
        self.armed = True
        self.status = "live_armed"
        self.last_reason = self.status
        self.registry.event("armed", {"at": utc_now().isoformat()})
        return True

    def disarm(self) -> None:
        self.armed = False
        self.status = "shadow"
        self.last_reason = "disarmed"
        self.registry.event("disarmed", {"at": utc_now().isoformat()})

    def emergency_stop(self) -> None:
        self.armed = False
        self.emergency_stopped = True
        self.registry.set_control("emergency_stopped", True)
        self.status = "emergency_stopped"
        self.last_reason = self.status
        self.registry.event("emergency_stop", {"at": utc_now().isoformat()})

    def resume(self) -> None:
        self.armed = False
        self.emergency_stopped = False
        self.registry.set_control("emergency_stopped", False)
        self.status = "shadow"
        self.last_reason = "emergency_stop_cleared"

    def reset_shadow(self) -> None:
        self.armed = False
        self.registry.set_control("shadow_reset_at", utc_now().isoformat())
        self.last_reason = "shadow_reset"

    async def _on_user_event(self, payload: dict[str, Any]) -> None:
        self.registry.event("user_ws", payload)
        self.events.append(("real_trading_user_event", payload))

    def _risk_reason(self, projected_cost: float) -> str | None:
        settings = self.config.real_trading
        if projected_cost > settings.max_order_notional_usd + 1e-9:
            return "max_order_notional_exceeded"
        orders = self.registry.today_live_orders()
        submitted = [
            order
            for order in orders
            if order.status
            not in {RealOrderStatus.BLOCKED, RealOrderStatus.CRITICAL_ERROR}
        ]
        if len(submitted) >= settings.max_orders_per_day:
            return "daily_order_limit_reached"
        realized_losses = sum(
            max(0.0, -(order.net_pnl or 0.0))
            for order in submitted
            if order.net_pnl is not None
        )
        active_statuses = {
            RealOrderStatus.SUBMITTING,
            RealOrderStatus.UNCERTAIN,
            RealOrderStatus.MATCHED,
            RealOrderStatus.CONFIRMED,
        }
        open_orders = [
            order
            for order in submitted
            if order.net_pnl is None and order.status in active_statuses
        ]
        open_risk = sum(
            (order.filled_quote or order.expected_quote)
            + (order.fee_usd or order.expected_fee_usd)
            for order in open_orders
        )
        if realized_losses + open_risk + projected_cost > settings.daily_loss_limit_usd:
            return "daily_loss_limit_reached"
        if open_orders:
            return "open_position_exists"
        return None

    async def on_initial_signal(
        self,
        signal: RecoveryFill,
        market: MarketState,
        books: dict[Direction, OrderBookSnapshot],
        now: datetime | None = None,
    ) -> RealOrderRecord | None:
        now = now or utc_now()
        self.last_market = market
        settings = self.config.real_trading
        if not settings.enabled:
            self.status = "disabled"
            self.last_reason = self.status
            return None
        existing = self.registry.by_market(market.condition_id)
        if existing is not None:
            self.last_reason = "duplicate_market_signal"
            return existing

        direction = signal.direction
        token_id = (
            market.up_token_id if direction == Direction.UP else market.down_token_id
        )
        quantity = settings.order_quantity
        max_price = self.config.btc_recovery.max_entry_price_cents / 100
        mode = "live" if self.armed and not self.emergency_stopped else "shadow"

        def blocked(reason: str, serious: bool = False) -> RealOrderRecord:
            order = RealOrderRecord(
                market_id=market.condition_id,
                market_slug=market.slug,
                token_id=token_id,
                direction=direction,
                signal_order_number=signal.trade_order_number or signal.order_number,
                mode=mode,
                status=(
                    RealOrderStatus.CRITICAL_ERROR
                    if serious
                    else RealOrderStatus.BLOCKED
                ),
                reason=reason,
                quantity=quantity,
                max_price=max_price,
                error=reason if serious else None,
                created_at=now,
                updated_at=now,
            )
            self.registry.save(order)
            self.status = order.status.value.lower()
            self.last_reason = reason
            self.events.append(
                ("real_trading_order", order.model_dump(mode="json"))
            )
            return order

        if market.observe_only or not market.accepting_orders:
            return blocked("market_not_tradeable")
        book = books.get(direction)
        if book is None:
            return blocked("book_unavailable")
        age = (now - book.received_at).total_seconds()
        if age < 0 or age > self.config.risk.max_data_age_ms / 1000:
            return blocked("book_stale")
        if not book.depth_trusted:
            return blocked("book_depth_untrusted")
        minimum = max(market.min_order_size, book.min_order_size)
        if quantity + 1e-12 < minimum:
            return blocked("below_official_min_order_size")
        if book.token_id != token_id:
            return blocked("market_token_mismatch", serious=True)

        execution = simulate_buy_quantity_limit(
            book,
            quantity,
            max_price,
            self.config.strategy.taker_fee_rate,
        )
        if not execution.complete or execution.avg_price > max_price + 1e-12:
            return blocked("fok_depth_or_limit_unavailable")
        projected_cost = execution.quote + execution.fee_usd
        risk_reason = self._risk_reason(projected_cost)
        if mode == "live" and risk_reason:
            return blocked(risk_reason)
        if self.adapter is None or not self.account.connected:
            return blocked("account_not_connected")
        if self.account.geoblocked:
            return blocked("geographic_restriction")
        if self.account.balance_usd is not None and self.account.balance_usd + 1e-9 < projected_cost:
            return blocked("insufficient_balance")
        if (
            mode == "live"
            and self.account.allowance_usd is not None
            and self.account.allowance_usd + 1e-9 < projected_cost
        ):
            return blocked("insufficient_allowance")

        try:
            signed = await self.adapter.build_fok_buy(
                token_id, execution.quote, max_price
            )
        except Exception as exc:
            return blocked(
                f"order_build_error: {type(exc).__name__}: {exc}", serious=True
            )

        order = RealOrderRecord(
            market_id=market.condition_id,
            market_slug=market.slug,
            token_id=token_id,
            direction=direction,
            signal_order_number=signal.trade_order_number or signal.order_number,
            mode=mode,
            status=(
                RealOrderStatus.SUBMITTING
                if mode == "live"
                else RealOrderStatus.SHADOW_READY
            ),
            reason="live_submit" if mode == "live" else "shadow_order_built",
            quantity=quantity,
            max_price=max_price,
            expected_avg_price=execution.avg_price,
            expected_quote=execution.quote,
            expected_fee_usd=execution.fee_usd,
            created_at=now,
            updated_at=now,
        )
        self.registry.save(order)
        if mode == "shadow":
            self.status = "shadow_ready"
            self.last_reason = order.reason
            self.events.append(
                ("real_trading_order", order.model_dump(mode="json"))
            )
            return order

        try:
            response = await self.adapter.post_order(signed)
        except Exception as exc:
            order.status = RealOrderStatus.UNCERTAIN
            order.reason = "submission_outcome_unknown"
            order.error = f"{type(exc).__name__}: {exc}"
            order.updated_at = utc_now()
            self.registry.save(order)
            self.armed = False
            self.status = "reconciliation_required"
            self.last_reason = order.reason
            return order
        order.exchange_order_id = response.order_id
        order.exchange_status = response.status
        order.updated_at = utc_now()
        if not response.accepted:
            order.status = RealOrderStatus.REJECTED
            order.reason = response.reason or "fok_rejected"
        else:
            order.status = RealOrderStatus.MATCHED
            order.reason = "fok_matched"
            order.filled_quote = response.making_amount or execution.quote
            order.filled_quantity = response.taking_amount or quantity
            order.fee_usd = execution.fee_usd
        self.registry.save(order)
        self.status = order.status.value.lower()
        self.last_reason = order.reason
        self.events.append(("real_trading_order", order.model_dump(mode="json")))
        return order

    async def reconcile(self, force: bool = False) -> None:
        now = utc_now()
        if self.adapter is None or not self.account.connected:
            return
        if not force and (now - self.last_reconcile_at).total_seconds() < 2:
            return
        self.last_reconcile_at = now
        for order in self.registry.nonterminal():
            if (
                order.status == RealOrderStatus.SETTLED_WIN
                and self.config.real_trading.auto_redeem
            ):
                await self._redeem_order(order)
                continue
            if order.mode != "live" or order.status not in {
                RealOrderStatus.SUBMITTING,
                RealOrderStatus.MATCHED,
                RealOrderStatus.UNCERTAIN,
            }:
                continue
            try:
                result = await self.adapter.reconcile(order)
            except Exception as exc:
                self.last_reason = f"reconcile_error: {type(exc).__name__}: {exc}"
                continue
            if result is None:
                continue
            order.filled_quantity = _decimal_value(
                result.get("quantity"), order.filled_quantity
            )
            order.filled_quote = _decimal_value(
                result.get("quote"), order.filled_quote
            )
            order.fee_usd = _decimal_value(result.get("fee_usd"), order.fee_usd)
            if result.get("exchange_order_id"):
                order.exchange_order_id = str(result["exchange_order_id"])
            if result.get("confirmed"):
                order.status = RealOrderStatus.CONFIRMED
                order.reason = "trade_confirmed"
            order.updated_at = now
            self.registry.save(order)

    async def _redeem_order(self, order: RealOrderRecord) -> None:
        if self.adapter is None or not self.account.connected:
            order.redeem_status = "waiting_for_credentials"
            order.updated_at = utc_now()
            self.registry.save(order)
            return
        order.status = RealOrderStatus.REDEEMING
        order.redeem_status = "submitting"
        order.updated_at = utc_now()
        self.registry.save(order)
        try:
            order.redeem_tx_hash = await self.adapter.redeem(order.market_id)
            order.status = RealOrderStatus.REDEEMED
            order.redeem_status = "confirmed"
            order.reason = "auto_redeemed"
        except Exception as exc:
            # The transaction may already have reached the relayer. Keep the
            # intent nonterminal and require reconciliation instead of retrying.
            order.status = RealOrderStatus.REDEEMING
            order.redeem_status = "outcome_unknown"
            order.error = f"{type(exc).__name__}: {exc}"
        order.updated_at = utc_now()
        self.registry.save(order)
        self.events.append(
            ("real_trading_settlement", order.model_dump(mode="json"))
        )

    async def settle(self, market_slug: str, outcome: Direction) -> None:
        matching = [
            order
            for order in self.registry.recent(1000)
            if order.market_slug == market_slug
        ]
        for order in matching:
            if order.mode != "live" or order.filled_quantity <= 0:
                continue
            if order.status in {
                RealOrderStatus.SETTLED_LOSS,
                RealOrderStatus.REDEEMING,
                RealOrderStatus.REDEEMED,
            }:
                continue
            order.official_outcome = outcome
            order.payout_usd = (
                order.filled_quantity if order.direction == outcome else 0.0
            )
            order.net_pnl = (
                order.payout_usd - order.filled_quote - order.fee_usd
            )
            order.updated_at = utc_now()
            if order.direction != outcome:
                order.status = RealOrderStatus.SETTLED_LOSS
                order.reason = "official_loss"
                self.registry.save(order)
                continue
            order.status = RealOrderStatus.SETTLED_WIN
            order.reason = "official_win"
            self.registry.save(order)
            if not self.config.real_trading.auto_redeem:
                continue
            await self._redeem_order(order)

    def drain_events(self) -> list[tuple[str, dict[str, Any]]]:
        events = list(self.events)
        self.events.clear()
        return events

    def dashboard_state(self) -> dict[str, Any]:
        orders = self.registry.recent(300)
        live_orders = [order for order in orders if order.mode == "live"]
        completed = [order for order in live_orders if order.net_pnl is not None]
        wins = [order for order in completed if (order.net_pnl or 0) > 0]
        open_orders = [
            order
            for order in live_orders
            if order.filled_quantity > 0 and order.net_pnl is None
        ]
        market = self.last_market
        official_minimum = market.min_order_size if market else None
        return {
            "status": self.status,
            "last_reason": self.last_reason,
            "mode": "live" if self.armed else "shadow",
            "armed": self.armed,
            "emergency_stopped": self.emergency_stopped,
            "credentials_loaded": self.credentials is not None,
            "config": self.config.real_trading.model_dump(mode="json"),
            "source_config": self.config.btc_recovery.model_dump(mode="json"),
            "account": {
                "connected": self.account.connected,
                "wallet": self.account.wallet,
                "wallet_type": self.account.wallet_type,
                "balance_usd": self.account.balance_usd,
                "allowance_usd": self.account.allowance_usd,
                "geoblocked": self.account.geoblocked,
                "country": self.account.country,
                "region": self.account.region,
                "user_ws_connected": self.account.user_ws_connected,
                "last_error": self.account.last_error,
            },
            "official_min_order_size": official_minimum,
            "quantity_meets_minimum": (
                None
                if official_minimum is None
                else self.config.real_trading.order_quantity + 1e-12
                >= official_minimum
            ),
            "shadow_gate": self.shadow_gate(),
            "current_position": (
                open_orders[0].model_dump(mode="json") if open_orders else None
            ),
            "summary": {
                "live_orders": len(live_orders),
                "completed_orders": len(completed),
                "winning_orders": len(wins),
                "losing_orders": len(completed) - len(wins),
                "win_rate": len(wins) / len(completed) if completed else 0.0,
                "fees_usd": sum(order.fee_usd for order in live_orders),
                "payout_usd": sum(order.payout_usd or 0 for order in completed),
                "net_pnl": sum(order.net_pnl or 0 for order in completed),
            },
            "recent_orders": [
                order.model_dump(mode="json") for order in orders
            ],
        }

    async def close(self) -> None:
        if self.adapter is not None:
            await self.adapter.close()
