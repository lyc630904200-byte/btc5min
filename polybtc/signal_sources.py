from __future__ import annotations

import asyncio
import json
import math
import zlib
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, AsyncIterator, Literal

import httpx
from pydantic import BaseModel, Field

from .clients import connect_websocket, websocket_option_attempts
from .config import SourceConfig
from .models import BookLevel


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def epoch_ms(value: Any, fallback: datetime | None = None) -> datetime:
    try:
        return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return fallback or utc_now()


def iso_time(value: Any, fallback: datetime | None = None) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return fallback or utc_now()


class SignalEvent(BaseModel):
    source: str
    market_type: Literal["spot", "futures", "polymarket"]
    kind: Literal[
        "trade",
        "book",
        "mark_price",
        "open_interest",
        "funding",
        "liquidation",
        "health",
    ]
    symbol: str = "BTC/USD"
    price: float | None = None
    quantity: float | None = None
    taker_side: Literal["buy", "sell"] | None = None
    bids: list[BookLevel] = Field(default_factory=list)
    asks: list[BookLevel] = Field(default_factory=list)
    sequence: int | None = None
    exchange_timestamp: datetime
    received_at: datetime
    processed_at: datetime = Field(default_factory=utc_now)
    valid: bool = True
    reason: str | None = None
    value: float | None = None
    raw: dict[str, Any] | None = None


def connection_health_event(
    source: str,
    market_type: Literal["spot", "futures", "polymarket"],
    symbol: str,
    reason: str,
) -> SignalEvent:
    now = utc_now()
    return SignalEvent(
        source=source,
        market_type=market_type,
        kind="health",
        symbol=symbol,
        exchange_timestamp=now,
        received_at=now,
        valid=False,
        reason="connection_error",
        raw={"error": reason[:500]},
    )


def _levels(values: Any, *, reverse: bool) -> list[BookLevel]:
    levels: list[BookLevel] = []
    for item in values or []:
        try:
            if isinstance(item, dict):
                price = float(item.get("price"))
                size = float(item.get("qty", item.get("size")))
            else:
                price, size = float(item[0]), float(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        if math.isfinite(price) and math.isfinite(size) and price > 0 and size > 0:
            levels.append(BookLevel(price=price, size=size))
    return sorted(levels, key=lambda level: level.price, reverse=reverse)


class BinanceSpotSignalClient:
    def __init__(self, config: SourceConfig):
        self.config = config
        self.symbol = "BTCUSDT"
        self.books: dict[str, dict[float, float]] = {"bids": {}, "asks": {}}
        self.last_update_id: int | None = None

    async def _snapshot(self, http: httpx.AsyncClient) -> SignalEvent:
        response = await http.get(
            f"{self.config.binance_rest_url}/api/v3/depth",
            params={"symbol": self.symbol, "limit": 100},
        )
        response.raise_for_status()
        payload = response.json()
        self.books = {
            "bids": {float(price): float(size) for price, size in payload["bids"]},
            "asks": {float(price): float(size) for price, size in payload["asks"]},
        }
        self.last_update_id = int(payload["lastUpdateId"])
        now = utc_now()
        return self._book_event(now, now, payload)

    def _book_event(
        self, exchange_time: datetime, received_at: datetime, raw: dict[str, Any]
    ) -> SignalEvent:
        bids = [BookLevel(price=p, size=s) for p, s in self.books["bids"].items()]
        asks = [BookLevel(price=p, size=s) for p, s in self.books["asks"].items()]
        return SignalEvent(
            source="binance",
            market_type="spot",
            kind="book",
            symbol=self.symbol,
            bids=sorted(bids, key=lambda level: level.price, reverse=True)[:20],
            asks=sorted(asks, key=lambda level: level.price)[:20],
            sequence=self.last_update_id,
            exchange_timestamp=exchange_time,
            received_at=received_at,
            valid=bool(bids and asks),
            raw=raw,
        )

    def _apply_depth(self, payload: dict[str, Any], received_at: datetime) -> SignalEvent | None:
        first = int(payload.get("U", 0))
        final = int(payload.get("u", 0))
        if self.last_update_id is None:
            return None
        if final <= self.last_update_id:
            return None
        if first > self.last_update_id + 1:
            self.last_update_id = None
            return SignalEvent(
                source="binance",
                market_type="spot",
                kind="health",
                symbol=self.symbol,
                exchange_timestamp=epoch_ms(payload.get("E"), received_at),
                received_at=received_at,
                valid=False,
                reason="sequence_gap",
                raw=payload,
            )
        for side, key in (("bids", "b"), ("asks", "a")):
            for raw_price, raw_size in payload.get(key) or []:
                price, size = float(raw_price), float(raw_size)
                if size <= 0:
                    self.books[side].pop(price, None)
                else:
                    self.books[side][price] = size
        self.last_update_id = final
        return self._book_event(
            epoch_ms(payload.get("E"), received_at), received_at, payload
        )

    async def events(self) -> AsyncIterator[SignalEvent]:
        streams = "btcusdt@aggTrade/btcusdt@depth@100ms"
        ws_url = f"{self.config.binance_signal_ws_url.rstrip('/')}?streams={streams}"
        while True:
            for options in websocket_option_attempts(self.config.proxy_url):
                try:
                    async with httpx.AsyncClient(
                        timeout=8, proxy=self.config.proxy_url, trust_env=True
                    ) as http:
                        async with connect_websocket(
                            ws_url,
                            ping_interval=20,
                            ping_timeout=20,
                            close_timeout=5,
                            open_timeout=8,
                            **options,
                        ) as websocket:
                            yield await self._snapshot(http)
                            async for message in websocket:
                                received_at = utc_now()
                                wrapper = json.loads(message)
                                payload = wrapper.get("data", wrapper)
                                event_type = payload.get("e")
                                if event_type == "aggTrade":
                                    yield SignalEvent(
                                        source="binance",
                                        market_type="spot",
                                        kind="trade",
                                        symbol=self.symbol,
                                        price=float(payload["p"]),
                                        quantity=float(payload["q"]),
                                        taker_side="sell" if payload.get("m") else "buy",
                                        sequence=int(payload.get("a", 0)),
                                        exchange_timestamp=epoch_ms(
                                            payload.get("T") or payload.get("E"), received_at
                                        ),
                                        received_at=received_at,
                                        raw=payload,
                                    )
                                elif event_type == "depthUpdate":
                                    event = self._apply_depth(payload, received_at)
                                    if event is not None:
                                        yield event
                                    if self.last_update_id is None:
                                        yield await self._snapshot(http)
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if options == websocket_option_attempts(self.config.proxy_url)[-1]:
                        yield connection_health_event(
                            "binance", "spot", self.symbol, str(exc)
                        )
                        await asyncio.sleep(1)
                        break
                    continue


class CoinbaseSignalClient:
    def __init__(self, config: SourceConfig):
        self.config = config
        self.symbol = "BTC-USD"
        self.books: dict[str, dict[float, float]] = {"bids": {}, "asks": {}}
        self.last_trade_id: int | None = None
        self.pending_trade_gap: tuple[int, int] | None = None

    def _book_event(self, timestamp: datetime, received_at: datetime, raw: dict[str, Any]) -> SignalEvent:
        return SignalEvent(
            source="coinbase",
            market_type="spot",
            kind="book",
            symbol=self.symbol,
            bids=[
                BookLevel(price=p, size=s)
                for p, s in sorted(self.books["bids"].items(), reverse=True)[:20]
            ],
            asks=[
                BookLevel(price=p, size=s)
                for p, s in sorted(self.books["asks"].items())[:20]
            ],
            exchange_timestamp=timestamp,
            received_at=received_at,
            valid=bool(self.books["bids"] and self.books["asks"]),
            raw=raw,
        )

    def parse(self, payload: dict[str, Any], received_at: datetime) -> list[SignalEvent]:
        event_type = str(payload.get("type") or "")
        timestamp = iso_time(payload.get("time"), received_at)
        if event_type == "snapshot" and payload.get("product_id") == self.symbol:
            self.books = {
                "bids": {float(p): float(s) for p, s in payload.get("bids") or []},
                "asks": {float(p): float(s) for p, s in payload.get("asks") or []},
            }
            return [self._book_event(timestamp, received_at, payload)]
        if event_type == "l2update" and payload.get("product_id") == self.symbol:
            for side, raw_price, raw_size in payload.get("changes") or []:
                key = "bids" if str(side).lower() == "buy" else "asks"
                price, size = float(raw_price), float(raw_size)
                if size <= 0:
                    self.books[key].pop(price, None)
                else:
                    self.books[key][price] = size
            return [self._book_event(timestamp, received_at, payload)]
        if event_type in {"match", "last_match"} and payload.get("product_id") == self.symbol:
            trade_id = int(payload.get("trade_id", 0))
            previous_trade_id = self.last_trade_id
            gap = previous_trade_id is not None and trade_id > previous_trade_id + 1
            if gap:
                self.pending_trade_gap = (previous_trade_id, trade_id)
            self.last_trade_id = max(trade_id, self.last_trade_id or trade_id)
            # Coinbase Exchange reports the maker order side in match messages.
            taker_side = "buy" if str(payload.get("side")).lower() == "sell" else "sell"
            return [
                SignalEvent(
                    source="coinbase",
                    market_type="spot",
                    kind="trade",
                    symbol=self.symbol,
                    price=float(payload["price"]),
                    quantity=float(payload["size"]),
                    taker_side=taker_side,
                    sequence=trade_id,
                    exchange_timestamp=timestamp,
                    received_at=received_at,
                    valid=event_type != "last_match" and not gap,
                    reason="trade_gap" if gap else ("snapshot_trade" if event_type == "last_match" else None),
                    raw=payload,
                )
            ]
        if event_type == "heartbeat" and payload.get("product_id") == self.symbol:
            return [
                SignalEvent(
                    source="coinbase",
                    market_type="spot",
                    kind="health",
                    symbol=self.symbol,
                    sequence=int(payload.get("sequence", 0)),
                    exchange_timestamp=timestamp,
                    received_at=received_at,
                    valid=True,
                    reason="heartbeat",
                    raw=payload,
                )
            ]
        return []

    async def _backfill_trades(
        self,
        http: httpx.AsyncClient,
        first_exclusive: int,
        last_exclusive: int,
        received_at: datetime,
    ) -> list[SignalEvent]:
        if last_exclusive <= first_exclusive + 1:
            return []
        if last_exclusive - first_exclusive > 2_000:
            return []
        expected = set(range(first_exclusive + 1, last_exclusive))
        found: dict[int, SignalEvent] = {}
        cursor: str | None = None
        for _ in range(20):
            params: dict[str, Any] = {"limit": 100}
            if cursor:
                params["after"] = cursor
            response = await http.get(
                f"{self.config.coinbase_rest_url}/products/{self.symbol}/trades",
                params=params,
            )
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list) or not rows:
                break
            ids: list[int] = []
            for row in rows:
                try:
                    trade_id = int(row["trade_id"])
                    ids.append(trade_id)
                    if trade_id not in expected:
                        continue
                    maker_side = str(row.get("side") or "").lower()
                    found[trade_id] = SignalEvent(
                        source="coinbase",
                        market_type="spot",
                        kind="trade",
                        symbol=self.symbol,
                        price=float(row["price"]),
                        quantity=float(row["size"]),
                        taker_side="buy" if maker_side == "sell" else "sell",
                        sequence=trade_id,
                        exchange_timestamp=iso_time(row.get("time"), received_at),
                        received_at=received_at,
                        valid=True,
                        reason="rest_backfill",
                        raw=row,
                    )
                except (KeyError, TypeError, ValueError):
                    continue
            if expected.issubset(found):
                break
            if ids and min(ids) <= first_exclusive + 1:
                break
            next_cursor = response.headers.get("cb-after")
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        return [found[trade_id] for trade_id in sorted(found)]

    async def events(self) -> AsyncIterator[SignalEvent]:
        subscription = {
            "type": "subscribe",
            "product_ids": [self.symbol],
            "channels": ["matches", "heartbeat", "level2_batch"],
        }
        while True:
            for options in websocket_option_attempts(self.config.proxy_url):
                try:
                    async with httpx.AsyncClient(
                        timeout=8, proxy=self.config.proxy_url, trust_env=True
                    ) as http:
                        async with connect_websocket(
                            self.config.coinbase_ws_url,
                            ping_interval=20,
                            ping_timeout=20,
                            close_timeout=5,
                            open_timeout=8,
                            max_size=16 * 1024 * 1024,
                            max_queue=256,
                            **options,
                        ) as websocket:
                            await websocket.send(json.dumps(subscription))
                            async for message in websocket:
                                received_at = utc_now()
                                payload = json.loads(message)
                                for event in self.parse(payload, received_at):
                                    if event.reason == "trade_gap" and self.pending_trade_gap:
                                        first_id, last_id = self.pending_trade_gap
                                        self.pending_trade_gap = None
                                        try:
                                            recovered = await self._backfill_trades(
                                                http, first_id, last_id, received_at
                                            )
                                        except Exception:
                                            recovered = []
                                        for recovered_event in recovered:
                                            yield recovered_event
                                        if len(recovered) == last_id - first_id - 1:
                                            event.valid = True
                                            event.reason = None
                                    yield event
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if options == websocket_option_attempts(self.config.proxy_url)[-1]:
                        yield connection_health_event(
                            "coinbase", "spot", self.symbol, str(exc)
                        )
                        await asyncio.sleep(1)
                        break
                    continue


def _kraken_number(value: Any) -> str:
    decimal = Decimal(str(value))
    text = format(decimal, "f").replace(".", "").lstrip("0")
    return text or "0"


def kraken_book_checksum(bids: list[BookLevel], asks: list[BookLevel]) -> int:
    ordered_asks = sorted(asks, key=lambda level: level.price)[:10]
    ordered_bids = sorted(bids, key=lambda level: level.price, reverse=True)[:10]
    payload = "".join(
        _kraken_number(level.price) + _kraken_number(level.size)
        for level in (*ordered_asks, *ordered_bids)
    )
    return zlib.crc32(payload.encode("ascii")) & 0xFFFFFFFF


class KrakenSignalClient:
    def __init__(self, config: SourceConfig):
        self.config = config
        self.symbol = "BTC/USD"
        self.books: dict[str, dict[float, float]] = {"bids": {}, "asks": {}}

    def parse(self, payload: dict[str, Any], received_at: datetime) -> list[SignalEvent]:
        channel = payload.get("channel")
        event_type = payload.get("type")
        rows = payload.get("data") or []
        if channel == "trade" and event_type in {"snapshot", "update"}:
            events: list[SignalEvent] = []
            for row in rows:
                if row.get("symbol") != self.symbol:
                    continue
                events.append(
                    SignalEvent(
                        source="kraken",
                        market_type="spot",
                        kind="trade",
                        symbol=self.symbol,
                        price=float(row["price"]),
                        quantity=float(row["qty"]),
                        taker_side=str(row.get("side") or "buy").lower(),
                        sequence=int(row.get("trade_id", 0)),
                        exchange_timestamp=iso_time(row.get("timestamp"), received_at),
                        received_at=received_at,
                        valid=event_type == "update",
                        reason="snapshot_trade" if event_type == "snapshot" else None,
                        raw=row,
                    )
                )
            return events
        if channel != "book" or event_type not in {"snapshot", "update"} or not rows:
            return []
        row = rows[0]
        if row.get("symbol") != self.symbol:
            return []
        if event_type == "snapshot":
            self.books = {
                "bids": {float(item["price"]): float(item["qty"]) for item in row.get("bids") or []},
                "asks": {float(item["price"]): float(item["qty"]) for item in row.get("asks") or []},
            }
        else:
            for side in ("bids", "asks"):
                for item in row.get(side) or []:
                    price, size = float(item["price"]), float(item["qty"])
                    if size <= 0:
                        self.books[side].pop(price, None)
                    else:
                        self.books[side][price] = size
        bids = [
            BookLevel(price=p, size=s)
            for p, s in sorted(self.books["bids"].items(), reverse=True)[:25]
        ]
        asks = [
            BookLevel(price=p, size=s)
            for p, s in sorted(self.books["asks"].items())[:25]
        ]
        expected = row.get("checksum")
        valid = bool(bids and asks)
        if expected is not None:
            valid = valid and kraken_book_checksum(bids, asks) == int(expected)
        return [
            SignalEvent(
                source="kraken",
                market_type="spot",
                kind="book",
                symbol=self.symbol,
                bids=bids[:20],
                asks=asks[:20],
                exchange_timestamp=iso_time(row.get("timestamp"), received_at),
                received_at=received_at,
                valid=valid,
                reason=None if valid else "checksum_mismatch",
                raw=row,
            )
        ]

    async def events(self) -> AsyncIterator[SignalEvent]:
        requests = [
            {
                "method": "subscribe",
                "params": {"channel": "book", "symbol": [self.symbol], "depth": 25, "snapshot": True},
            },
            {
                "method": "subscribe",
                "params": {"channel": "trade", "symbol": [self.symbol], "snapshot": False},
            },
        ]
        while True:
            for options in websocket_option_attempts(self.config.proxy_url):
                try:
                    async with connect_websocket(
                        self.config.kraken_ws_url,
                        ping_interval=20,
                        ping_timeout=20,
                        close_timeout=5,
                        open_timeout=8,
                        **options,
                    ) as websocket:
                        for request in requests:
                            await websocket.send(json.dumps(request))
                        async for message in websocket:
                            received_at = utc_now()
                            for event in self.parse(json.loads(message), received_at):
                                yield event
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if options == websocket_option_attempts(self.config.proxy_url)[-1]:
                        yield connection_health_event(
                            "kraken", "spot", self.symbol, str(exc)
                        )
                        await asyncio.sleep(1)
                        break
                    continue


class BinanceFuturesSignalClient:
    def __init__(self, config: SourceConfig):
        self.config = config
        self.symbol = "BTCUSDT"
        self.last_trade_id: int | None = None

    def parse_rest_trades(
        self, payload: list[dict[str, Any]], received_at: datetime
    ) -> list[SignalEvent]:
        if not payload:
            return []
        newest_id = max(int(item["a"]) for item in payload)
        if self.last_trade_id is None:
            self.last_trade_id = newest_id
            return []
        events: list[SignalEvent] = []
        for item in sorted(payload, key=lambda value: int(value["a"])):
            trade_id = int(item["a"])
            if trade_id <= self.last_trade_id:
                continue
            events.append(
                SignalEvent(
                    source="binance_futures",
                    market_type="futures",
                    kind="trade",
                    symbol=self.symbol,
                    price=float(item["p"]),
                    quantity=float(item["q"]),
                    taker_side="sell" if item.get("m") else "buy",
                    sequence=trade_id,
                    exchange_timestamp=epoch_ms(item.get("T"), received_at),
                    received_at=received_at,
                    reason="rest_fallback",
                    raw=item,
                )
            )
        self.last_trade_id = max(self.last_trade_id, newest_id)
        return events

    def parse_rest_premium(
        self, payload: dict[str, Any], received_at: datetime
    ) -> list[SignalEvent]:
        common = dict(
            source="binance_futures",
            market_type="futures",
            symbol=self.symbol,
            exchange_timestamp=epoch_ms(payload.get("time"), received_at),
            received_at=received_at,
            reason="rest_fallback",
            raw=payload,
        )
        return [
            SignalEvent(**common, kind="mark_price", price=float(payload["markPrice"])),
            SignalEvent(
                **common,
                kind="funding",
                value=float(payload.get("lastFundingRate") or 0.0),
            ),
        ]

    def parse(self, payload: dict[str, Any], received_at: datetime) -> list[SignalEvent]:
        payload = payload.get("data", payload)
        event_type = payload.get("e")
        timestamp = epoch_ms(payload.get("E") or payload.get("T"), received_at)
        common = dict(
            source="binance_futures",
            market_type="futures",
            symbol=self.symbol,
            exchange_timestamp=timestamp,
            received_at=received_at,
            raw=payload,
        )
        if event_type == "aggTrade":
            trade_id = int(payload.get("a", 0))
            if self.last_trade_id is not None and trade_id <= self.last_trade_id:
                return []
            self.last_trade_id = trade_id
            return [
                SignalEvent(
                    **common,
                    kind="trade",
                    price=float(payload["p"]),
                    quantity=float(payload["q"]),
                    taker_side="sell" if payload.get("m") else "buy",
                    sequence=trade_id,
                )
            ]
        if event_type == "depthUpdate":
            bids = _levels(payload.get("b"), reverse=True)[:20]
            asks = _levels(payload.get("a"), reverse=False)[:20]
            return [
                SignalEvent(
                    **common,
                    kind="book",
                    bids=bids,
                    asks=asks,
                    sequence=int(payload.get("u", 0)),
                    valid=bool(bids and asks),
                    reason=None if bids and asks else "empty_book",
                )
            ]
        if event_type == "markPriceUpdate":
            return [
                SignalEvent(**common, kind="mark_price", price=float(payload["p"])),
                SignalEvent(**common, kind="funding", value=float(payload.get("r") or 0.0)),
            ]
        if event_type == "forceOrder":
            order = payload.get("o") or {}
            return [
                SignalEvent(
                    **common,
                    kind="liquidation",
                    price=float(order.get("ap") or order.get("p") or 0.0),
                    quantity=float(order.get("q") or 0.0),
                    taker_side=str(order.get("S") or "BUY").lower(),
                )
            ]
        return []

    async def events(self) -> AsyncIterator[SignalEvent]:
        streams = [
            "btcusdt@aggTrade",
            "btcusdt@depth20@100ms",
            "btcusdt@markPrice@1s",
            "btcusdt@forceOrder",
        ]
        request = {"method": "SUBSCRIBE", "params": streams, "id": 1}
        queue: asyncio.Queue[SignalEvent] = asyncio.Queue(maxsize=512)

        def enqueue(event: SignalEvent) -> None:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(event)

        async def open_interest_loop() -> None:
            async with httpx.AsyncClient(
                timeout=8, proxy=self.config.proxy_url, trust_env=True
            ) as http:
                while True:
                    try:
                        response = await http.get(
                            f"{self.config.binance_futures_rest_url}/fapi/v1/openInterest",
                            params={"symbol": self.symbol},
                        )
                        response.raise_for_status()
                        payload = response.json()
                        now = utc_now()
                        event = SignalEvent(
                            source="binance_futures",
                            market_type="futures",
                            kind="open_interest",
                            symbol=self.symbol,
                            value=float(payload["openInterest"]),
                            exchange_timestamp=epoch_ms(payload.get("time"), now),
                            received_at=now,
                            raw=payload,
                        )
                        enqueue(event)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        event = connection_health_event(
                            "binance_futures", "futures", self.symbol, str(exc)
                        )
                        enqueue(event)
                    await asyncio.sleep(5)

        async def market_data_fallback_loop() -> None:
            async with httpx.AsyncClient(
                timeout=8, proxy=self.config.proxy_url, trust_env=True
            ) as http:
                next_premium_at = 0.0
                while True:
                    try:
                        now = utc_now()
                        response = await http.get(
                            f"{self.config.binance_futures_rest_url}/fapi/v1/aggTrades",
                            params={"symbol": self.symbol, "limit": 100},
                        )
                        response.raise_for_status()
                        for event in self.parse_rest_trades(response.json(), now):
                            enqueue(event)

                        loop_time = asyncio.get_running_loop().time()
                        if loop_time >= next_premium_at:
                            response = await http.get(
                                f"{self.config.binance_futures_rest_url}/fapi/v1/premiumIndex",
                                params={"symbol": self.symbol},
                            )
                            response.raise_for_status()
                            premium_now = utc_now()
                            for event in self.parse_rest_premium(response.json(), premium_now):
                                enqueue(event)
                            next_premium_at = loop_time + 1.0
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        enqueue(
                            connection_health_event(
                                "binance_futures", "futures", self.symbol, str(exc)
                            )
                        )
                    await asyncio.sleep(0.5)

        async def websocket_loop() -> None:
            while True:
                for options in websocket_option_attempts(self.config.proxy_url):
                    try:
                        async with connect_websocket(
                            self.config.binance_futures_ws_url,
                            ping_interval=20,
                            ping_timeout=20,
                            close_timeout=5,
                            open_timeout=8,
                            **options,
                        ) as websocket:
                            await websocket.send(json.dumps(request))
                            async for message in websocket:
                                now = utc_now()
                                for event in self.parse(json.loads(message), now):
                                    enqueue(event)
                        break
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        if options == websocket_option_attempts(self.config.proxy_url)[-1]:
                            event = connection_health_event(
                                "binance_futures", "futures", self.symbol, str(exc)
                            )
                            enqueue(event)
                            await asyncio.sleep(1)
                            break
                        continue

        tasks = [
            asyncio.create_task(open_interest_loop()),
            asyncio.create_task(market_data_fallback_loop()),
            asyncio.create_task(websocket_loop()),
        ]
        try:
            while True:
                yield await queue.get()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
