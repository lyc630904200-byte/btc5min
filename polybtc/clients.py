from __future__ import annotations

import asyncio
import inspect
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Iterable

import httpx
import websockets

from .config import SourceConfig
from .market import choose_current_market, parse_market
from .models import BookLevel, MarketState, OrderBookSnapshot, PriceTick


CLOB_SUBSCRIPTION_TYPE = "market"
POLYMARKET_RTDS_CRYPTO_TOPIC = "crypto_prices_chainlink"


def btc_updown_5m_slugs(now: datetime, *, before: int = 3, after: int = 12) -> list[str]:
    base = int(now.timestamp()) // 300 * 300
    return [f"btc-updown-5m-{base + offset * 300}" for offset in range(-before, after + 1)]


def direct_websocket_options() -> dict[str, Any]:
    if "proxy" in inspect.signature(websockets.connect).parameters:
        return {"proxy": None}
    return {}


def websocket_option_attempts() -> list[dict[str, Any]]:
    direct_options = direct_websocket_options()
    if "proxy" in direct_options:
        return [direct_options, {}]
    return [{}]


def should_retry_with_env_proxy(exc: Exception) -> bool:
    return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError, httpx.ReadTimeout))


async def get_direct_first(
    url: str,
    *,
    timeout: float,
    follow_redirects: bool = False,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    proxy_url: str | None = None,
) -> tuple[httpx.Response, datetime, datetime, bool]:
    attempts = [(proxy_url, False)] if proxy_url else []
    attempts.extend([(None, False), (None, True)])
    for index, (proxy, trust_env) in enumerate(attempts):
        start = datetime.now(timezone.utc)
        try:
            options: dict[str, Any] = {"proxy": proxy} if proxy else {}
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=follow_redirects,
                headers=headers,
                trust_env=trust_env,
                **options,
            ) as client:
                response = await client.get(url, params=params)
                response.raise_for_status()
            end = datetime.now(timezone.utc)
            return response, start, end, trust_env
        except Exception as exc:
            if index == len(attempts) - 1 or not should_retry_with_env_proxy(exc):
                raise
    raise RuntimeError("unreachable direct-first HTTP retry state")


def parse_clob_book(payload: dict[str, Any]) -> OrderBookSnapshot:
    timestamp_ms = payload.get("timestamp")
    try:
        timestamp = datetime.fromtimestamp(float(timestamp_ms) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        timestamp = datetime.now(timezone.utc)
    return OrderBookSnapshot(
        token_id=str(payload.get("asset_id") or payload.get("token_id") or ""),
        market_id=str(payload.get("market") or ""),
        timestamp=timestamp,
        bids=[BookLevel(price=float(level["price"]), size=float(level["size"])) for level in payload.get("bids", [])],
        asks=[BookLevel(price=float(level["price"]), size=float(level["size"])) for level in payload.get("asks", [])],
        min_order_size=float(payload.get("min_order_size") or 5),
        tick_size=float(payload.get("tick_size") or 0.01),
        raw=payload,
    )


def parse_clob_best_bid_ask(payload: dict[str, Any]) -> OrderBookSnapshot:
    best_bid = payload.get("best_bid") or payload.get("bid")
    best_ask = payload.get("best_ask") or payload.get("ask")
    min_order_size = float(payload.get("min_order_size") or 5)
    return OrderBookSnapshot(
        token_id=str(payload.get("asset_id") or payload.get("token_id") or ""),
        market_id=str(payload.get("market") or ""),
        timestamp=clob_timestamp(payload.get("timestamp")),
        bids=[BookLevel(price=float(best_bid), size=min_order_size)] if best_bid is not None else [],
        asks=[BookLevel(price=float(best_ask), size=min_order_size)] if best_ask is not None else [],
        min_order_size=min_order_size,
        tick_size=float(payload.get("tick_size") or 0.01),
        raw=payload,
    )


def clob_timestamp(value: Any) -> datetime:
    try:
        return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def update_levels(levels: list[BookLevel], price: float, size: float) -> list[BookLevel]:
    updated = [level for level in levels if level.price != price]
    if size > 0:
        updated.append(BookLevel(price=price, size=size))
    return updated


def sort_book_levels(levels: list[BookLevel], *, reverse: bool) -> list[BookLevel]:
    return sorted(levels, key=lambda level: level.price, reverse=reverse)


def apply_clob_price_change(book: OrderBookSnapshot, payload: dict[str, Any], change: dict[str, Any]) -> OrderBookSnapshot:
    side = str(change.get("side") or "").upper()
    price = float(change["price"])
    size = float(change.get("size") or 0)
    bids = list(book.bids)
    asks = list(book.asks)
    if side == "BUY":
        bids = sort_book_levels(update_levels(bids, price, size), reverse=True)
    elif side == "SELL":
        asks = sort_book_levels(update_levels(asks, price, size), reverse=False)
    return OrderBookSnapshot(
        token_id=book.token_id,
        market_id=str(change.get("market") or payload.get("market") or book.market_id or ""),
        timestamp=clob_timestamp(change.get("timestamp") or payload.get("timestamp")),
        bids=bids,
        asks=asks,
        min_order_size=book.min_order_size,
        tick_size=book.tick_size,
        raw=payload,
    )


def apply_clob_best_bid_ask(book: OrderBookSnapshot, payload: dict[str, Any]) -> OrderBookSnapshot:
    bids = list(book.bids)
    asks = list(book.asks)
    best_bid = payload.get("best_bid") or payload.get("bid")
    best_ask = payload.get("best_ask") or payload.get("ask")

    if best_bid is not None:
        price = float(best_bid)
        size = next((level.size for level in bids if level.price == price), bids[0].size if bids else book.min_order_size)
        bids = sort_book_levels(update_levels([level for level in bids if level.price <= price], price, size), reverse=True)

    if best_ask is not None:
        price = float(best_ask)
        size = next((level.size for level in asks if level.price == price), asks[0].size if asks else book.min_order_size)
        asks = sort_book_levels(update_levels([level for level in asks if level.price >= price], price, size), reverse=False)

    return OrderBookSnapshot(
        token_id=book.token_id,
        market_id=str(payload.get("market") or book.market_id or ""),
        timestamp=clob_timestamp(payload.get("timestamp")),
        bids=bids,
        asks=asks,
        min_order_size=book.min_order_size,
        tick_size=book.tick_size,
        raw=payload,
    )


def clob_price_changes(payload: dict[str, Any]) -> list[dict[str, Any]]:
    changes = payload.get("price_changes")
    if isinstance(changes, list):
        return [change for change in changes if isinstance(change, dict)]
    if "price" in payload and "side" in payload:
        return [payload]
    return []


def parse_clob_market_messages(raw: object) -> list[dict[str, Any]]:
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        raw = json.loads(raw)

    items = list(raw) if isinstance(raw, list) else [raw]
    events: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("topic") == "market" and "type" in item and "payload" in item:
            events.append(item)
            continue
        event_type = item.get("event_type") or item.get("type")
        if not event_type:
            continue
        payload = {k: v for k, v in item.items() if k not in {"event_type", "type", "topic"}}
        events.append({"topic": "market", "type": event_type, "payload": payload})
    return events


def update_books_from_market_message(
    raw: object,
    books: dict[str, OrderBookSnapshot],
) -> list[tuple[str, OrderBookSnapshot]]:
    updates: list[tuple[str, OrderBookSnapshot]] = []
    for event in parse_clob_market_messages(raw):
        event_type = str(event.get("type") or "")
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            continue

        if event_type == "book":
            book = parse_clob_book(payload)
            existing = books.get(book.token_id)
            if book.token_id and (existing is None or book.timestamp > existing.timestamp):
                books[book.token_id] = book
                updates.append((book.token_id, book))
            continue

        if event_type == "price_change":
            prior_timestamps: dict[str, datetime] = {}
            for change in clob_price_changes(payload):
                token_id = str(change.get("asset_id") or change.get("token_id") or payload.get("asset_id") or payload.get("token_id") or "")
                if not token_id:
                    continue
                book = books.get(token_id)
                if book is None:
                    continue
                prior_timestamp = prior_timestamps.setdefault(token_id, book.timestamp)
                updated = apply_clob_price_change(book, payload, change)
                if updated.timestamp <= prior_timestamp:
                    continue
                books[token_id] = updated
                updates.append((token_id, updated))
            continue

        if event_type == "best_bid_ask":
            token_id = str(payload.get("asset_id") or payload.get("token_id") or "")
            if not token_id:
                continue
            book = books.get(token_id)
            if book is None:
                updated = parse_clob_best_bid_ask(payload)
            else:
                updated = apply_clob_best_bid_ask(book, payload)
            if updated.timestamp <= (book.timestamp if book else datetime.min.replace(tzinfo=timezone.utc)):
                continue
            books[token_id] = updated
            updates.append((token_id, updated))
            continue
    return updates


def parse_rtds_crypto_price_message(
    raw: object,
    *,
    symbol: str = "btc/usd",
    received_at: datetime | None = None,
) -> list[PriceTick]:
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return []
    elif isinstance(raw, dict):
        payload = raw
    else:
        return []

    message_payload = payload.get("payload") if isinstance(payload, dict) else None
    if not isinstance(message_payload, dict):
        return []
    if str(message_payload.get("symbol") or "").lower() != symbol.lower():
        return []

    now = received_at or datetime.now(timezone.utc)
    rows = message_payload.get("data")
    if not isinstance(rows, list):
        rows = [message_payload] if "value" in message_payload else []

    ticks: list[PriceTick] = []
    for row in rows:
        if not isinstance(row, dict) or "value" not in row:
            continue
        timestamp = row.get("timestamp") or payload.get("timestamp")
        try:
            price = float(row["value"])
            exchange_timestamp = datetime.fromtimestamp(float(timestamp) / 1000, tz=timezone.utc) if timestamp else None
        except (TypeError, ValueError):
            continue
        ticks.append(
            PriceTick(
                source="polymarket_rtds",
                symbol=symbol.upper(),
                price=price,
                exchange_timestamp=exchange_timestamp,
                received_at=now,
            )
        )
    return ticks


@dataclass(frozen=True)
class PolymarketPastResult:
    start_time: datetime
    end_time: datetime
    open_price: float
    close_price: float
    outcome: str | None = None


@dataclass(frozen=True)
class PolymarketOutcomePrice:
    slug: str
    open_price: float
    close_price: float | None = None


PAST_RESULT_RE = re.compile(
    r'\{"startTime":"(?P<start>[^"]+)","endTime":"(?P<end>[^"]+)",'
    r'"openPrice":(?P<open>[0-9.]+),"closePrice":(?P<close>[0-9.]+)'
    r'(?:,"outcome":"(?P<outcome>[^"]+)")?'
)

OUTCOME_PRICE_RE = re.compile(
    r'"slug":"(?P<slug>btc-updown-5m-\d+)"(?:(?!"slug":).){0,500}?'
    r'"data":\{"openPrice":(?P<open>[0-9.]+)(?:,"closePrice":(?P<close>[0-9.]+))?',
    re.DOTALL,
)
CRYPTO_PRICE_RE = re.compile(
    r'"state":\{"data":\{"openPrice":(?P<open>[0-9.]+),"closePrice":(?P<close>null|[0-9.]+)\}.*?'
    r'"queryKey":\["crypto-prices","price","BTC","(?P<start>[^"]+)","fiveminute","(?P<end>[^"]+)"\]',
    re.DOTALL,
)


def parse_polymarket_past_results(html: str) -> list[PolymarketPastResult]:
    results: list[PolymarketPastResult] = []
    seen: set[tuple[datetime, datetime]] = set()
    normalized = html.replace('\\"', '"')
    for match in PAST_RESULT_RE.finditer(normalized):
        try:
            start = datetime.fromisoformat(match.group("start").replace("Z", "+00:00"))
            end = datetime.fromisoformat(match.group("end").replace("Z", "+00:00"))
            result = PolymarketPastResult(
                start_time=start,
                end_time=end,
                open_price=float(match.group("open")),
                close_price=float(match.group("close")),
                outcome=match.group("outcome"),
            )
        except (TypeError, ValueError):
            continue
        key = (result.start_time, result.end_time)
        if key not in seen:
            seen.add(key)
            results.append(result)
    return sorted(results, key=lambda item: item.start_time)


def parse_polymarket_outcome_prices(html: str) -> list[PolymarketOutcomePrice]:
    prices: list[PolymarketOutcomePrice] = []
    seen: set[str] = set()
    normalized = html.replace('\\"', '"')
    for match in CRYPTO_PRICE_RE.finditer(normalized):
        try:
            start = datetime.fromisoformat(match.group("start").replace("Z", "+00:00"))
            open_price = float(match.group("open"))
            close = match.group("close")
            close_price = None if close == "null" else float(close)
        except (TypeError, ValueError):
            continue
        slug = f"btc-updown-5m-{int(start.timestamp())}"
        if slug in seen:
            continue
        seen.add(slug)
        prices.append(PolymarketOutcomePrice(slug=slug, open_price=open_price, close_price=close_price))
    for match in OUTCOME_PRICE_RE.finditer(normalized):
        slug = match.group("slug")
        if slug in seen:
            continue
        try:
            open_price = float(match.group("open"))
            close = match.group("close")
            close_price = float(close) if close is not None else None
        except (TypeError, ValueError):
            continue
        seen.add(slug)
        prices.append(PolymarketOutcomePrice(slug=slug, open_price=open_price, close_price=close_price))
    return prices


class BinanceClient:
    def __init__(self, config: SourceConfig):
        self.config = config

    async def server_time(self) -> dict[str, Any]:
        response, start, end, used_env_proxy = await get_direct_first(f"{self.config.binance_rest_url}/api/v3/time", timeout=8)
        payload = response.json()
        server_time = datetime.fromtimestamp(payload["serverTime"] / 1000, tz=timezone.utc)
        local_midpoint = start + (end - start) / 2
        return {
            "ok": True,
            "latency_ms": (end - start).total_seconds() * 1000,
            "used_env_proxy": used_env_proxy,
            "clock_offset_ms": (server_time - local_midpoint).total_seconds() * 1000,
            "server_time": server_time.isoformat(),
        }

    async def rest_price_tick(self) -> PriceTick:
        response, _, _, _ = await get_direct_first(
            f"{self.config.binance_rest_url}/api/v3/ticker/price",
            timeout=5,
            params={"symbol": self.config.binance_symbol},
        )
        payload = response.json()
        now = datetime.now(timezone.utc)
        return PriceTick(
            source="binance_rest",
            symbol=str(payload.get("symbol") or self.config.binance_symbol),
            price=float(payload["price"]),
            exchange_timestamp=now,
            received_at=now,
        )

    async def trades(self) -> AsyncIterator[PriceTick]:
        while True:
            for options in websocket_option_attempts():
                connected = False
                try:
                    async with websockets.connect(
                        self.config.binance_ws_url,
                        ping_interval=20,
                        ping_timeout=20,
                        close_timeout=5,
                        open_timeout=5,
                        **options,
                    ) as websocket:
                        connected = True
                        async for message in websocket:
                            payload = json.loads(message)
                            price = float(payload.get("p") or payload.get("price"))
                            event_time = payload.get("E") or payload.get("T")
                            exchange_ts = datetime.fromtimestamp(event_time / 1000, tz=timezone.utc) if event_time else None
                            yield PriceTick(
                                source="binance",
                                symbol=self.config.binance_symbol,
                                price=price,
                                exchange_timestamp=exchange_ts,
                                received_at=datetime.now(timezone.utc),
                            )
                except websockets.ConnectionClosed:
                    await asyncio.sleep(1)
                    break
                except Exception:
                    if connected or options == websocket_option_attempts()[-1]:
                        try:
                            yield await self.rest_price_tick()
                        except Exception as exc:
                            raise ConnectionError(f"binance websocket/rest unavailable: {exc}") from exc
                        await asyncio.sleep(1)
                        break
                    continue


class PolymarketClient:
    def __init__(self, config: SourceConfig):
        self.config = config

    async def _discover_from_requests(
        self,
        requests: list[tuple[str, dict[str, Any]]],
        now: datetime,
        timeout: float,
    ) -> list[MarketState]:
        responses = await asyncio.gather(
            *(get_direct_first(url, timeout=timeout, params=params) for url, params in requests),
            return_exceptions=True,
        )
        seen: set[str] = set()
        markets: list[MarketState] = []
        for response in responses:
            if isinstance(response, Exception):
                continue
            http_response = response[0]
            payload = http_response.json()
            items = payload.get("value") if isinstance(payload, dict) else payload
            for item in items or []:
                candidates = item.get("markets") if isinstance(item, dict) and item.get("markets") else [item]
                for candidate in candidates:
                    market = parse_market(candidate, self.config, now=now)
                    if market and market.condition_id not in seen:
                        seen.add(market.condition_id)
                        markets.append(market)
        return sorted(markets, key=lambda market: market.end_time)

    async def discover_markets(self) -> list[MarketState]:
        priority_requests: list[tuple[str, dict[str, Any]]] = []
        now = datetime.now(timezone.utc)
        if self.config.market_slug:
            slugs = expand_market_slugs(self.config.market_slug)
            priority_requests.extend(
                (f"{self.config.gamma_url}/{path}", {"slug": slug})
                for slug in slugs
                for path in ("markets", "events")
            )
        priority_requests.extend(
            (f"{self.config.gamma_url}/markets", {"slug": slug})
            for slug in btc_updown_5m_slugs(now)
        )
        markets = await self._discover_from_requests(priority_requests, now=now, timeout=3)
        if markets:
            return markets

        fallback_requests: list[tuple[str, dict[str, Any]]] = []
        fallback_requests.extend(
            (
                f"{self.config.gamma_url}/markets",
                {"limit": 500, "offset": offset, "active": "true", "closed": "false"},
            )
            for offset in (0, 500, 1000)
        )
        fallback_requests.extend(
            (
                f"{self.config.gamma_url}/markets",
                {"limit": 100, "active": "true", "closed": "false", "search": query},
            )
            for query in ("Bitcoin", "BTC", "up-or-down")
        )
        return await self._discover_from_requests(fallback_requests, now=now, timeout=6)

    async def current_market(self) -> MarketState | None:
        return choose_current_market(
            await self.discover_markets(),
            max_start_price_lag_ms=self.config.max_start_price_lag_ms,
        )

    async def book(self, token_id: str) -> OrderBookSnapshot:
        response, _, end, _ = await get_direct_first(f"{self.config.clob_url}/book", timeout=8, params={"token_id": token_id})
        book = parse_clob_book(response.json())
        book.timestamp = end
        return book

    async def price_probe(self, token_id: str) -> dict[str, Any]:
        response, start, end, used_env_proxy = await get_direct_first(
            f"{self.config.clob_url}/price",
            timeout=8,
            params={"token_id": token_id, "side": "BUY"},
        )
        return {"ok": True, "latency_ms": (end - start).total_seconds() * 1000, "used_env_proxy": used_env_proxy, "payload": response.json()}

    async def rtds_crypto_price_ticks(self, symbol: str = "btc/usd") -> AsyncIterator[PriceTick]:
        subscription = {
            "action": "subscribe",
            "subscriptions": [
                {
                    "topic": POLYMARKET_RTDS_CRYPTO_TOPIC,
                    "type": "*",
                    "filters": json.dumps({"symbol": symbol}, separators=(",", ":")),
                }
            ],
        }
        for options in websocket_option_attempts():
            connected = False
            try:
                async with websockets.connect(
                    self.config.rtds_ws_url,
                    origin="https://polymarket.com",
                    ping_interval=10,
                    ping_timeout=10,
                    close_timeout=5,
                    open_timeout=15,
                    **options,
                ) as websocket:
                    connected = True
                    await websocket.send(json.dumps(subscription, separators=(",", ":"), ensure_ascii=False))
                    async for message in websocket:
                        for tick in parse_rtds_crypto_price_message(message, symbol=symbol):
                            yield tick
            except Exception:
                if connected or options == websocket_option_attempts()[-1]:
                    raise
                continue

    async def event_page_text(self, market_slug: str, timeout: float | None = None) -> str:
        response, _, _, _ = await get_direct_first(
            f"https://polymarket.com/event/{market_slug}",
            timeout=timeout or self.config.threshold_page_timeout_seconds,
            follow_redirects=True,
            headers={"user-agent": "Mozilla/5.0"},
            proxy_url=self.config.proxy_url,
        )
        return response.text

    async def market_page_data(self, market_slug: str) -> tuple[PolymarketOutcomePrice | None, list[PolymarketPastResult]]:
        text = await self.event_page_text(market_slug, self.config.threshold_page_timeout_seconds)
        outcome_price = next((price for price in parse_polymarket_outcome_prices(text) if price.slug == market_slug), None)
        return outcome_price, parse_polymarket_past_results(text)

    async def past_results(self, market_slug: str) -> list[PolymarketPastResult]:
        return parse_polymarket_past_results(await self.event_page_text(market_slug))

    async def outcome_price(self, market_slug: str) -> PolymarketOutcomePrice | None:
        for price in parse_polymarket_outcome_prices(await self.event_page_text(market_slug)):
            if price.slug == market_slug:
                return price
        return None

    async def book_stream(self, token_ids: Iterable[str]) -> AsyncIterator[tuple[str, OrderBookSnapshot]]:
        ids = [str(token_id) for token_id in token_ids if str(token_id)]
        if not ids:
            return
        books: dict[str, OrderBookSnapshot] = {}
        subscription = {
            "type": CLOB_SUBSCRIPTION_TYPE,
            "assets_ids": ids,
            "custom_feature_enabled": True,
        }
        try:
            for options in websocket_option_attempts():
                connected = False
                try:
                    async with websockets.connect(
                        self.config.clob_ws_url,
                        ping_interval=20,
                        ping_timeout=20,
                        close_timeout=5,
                        open_timeout=10,
                        **options,
                    ) as websocket:
                        connected = True
                        await websocket.send(json.dumps(subscription, separators=(",", ":"), ensure_ascii=False))
                        async for message in websocket:
                            for token_id, book in update_books_from_market_message(message, books):
                                if token_id in ids:
                                    yield token_id, book
                except Exception:
                    if connected or options == websocket_option_attempts()[-1]:
                        raise
                    continue
        except websockets.ConnectionClosed as exc:
            raise ConnectionError("polymarket websocket disconnected") from exc


def expand_market_slugs(slug: str) -> list[str]:
    match = re.fullmatch(r"(btc-updown-5m-)(\d+)", slug)
    if not match:
        return [slug]
    now_ts = int(datetime.now(timezone.utc).timestamp())
    current = now_ts // 300 * 300
    prefix = match.group(1)
    candidates = [current - 300, current, current + 300, int(match.group(2))]
    seen: set[str] = set()
    expanded: list[str] = []
    for ts in candidates:
        candidate = f"{prefix}{ts}"
        if candidate not in seen:
            seen.add(candidate)
            expanded.append(candidate)
    return expanded
