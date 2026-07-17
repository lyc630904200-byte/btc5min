from __future__ import annotations

import asyncio
import inspect
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Iterable

import httpx
import websockets

from .config import SourceConfig
from .market import choose_current_market, market_interval_from_slug, parse_market
from .models import BookLevel, MarketState, OrderBookSnapshot, PriceTick


CLOB_SUBSCRIPTION_TYPE = "market"
POLYMARKET_RTDS_CRYPTO_TOPIC = "crypto_prices_chainlink"
PROXY_ATTEMPT_TIMEOUT_SECONDS = 1.5
CLOB_HEARTBEAT_SECONDS = 10


def btc_updown_5m_slugs(now: datetime, *, before: int = 3, after: int = 12) -> list[str]:
    base = int(now.timestamp()) // 300 * 300
    return [f"btc-updown-5m-{base + offset * 300}" for offset in range(-before, after + 1)]


def direct_websocket_options() -> dict[str, Any]:
    if "proxy" in inspect.signature(websockets.connect).parameters:
        return {"proxy": None}
    return {}


def websocket_option_attempts(proxy_url: str | None = None) -> list[dict[str, Any]]:
    direct_options = direct_websocket_options()
    if proxy_url and "proxy" in direct_options:
        return [{"proxy": proxy_url}, direct_options, {}]
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
            # A stale local proxy can otherwise consume the complete threshold
            # lookup budget before the direct fallback gets a chance to run.
            attempt_timeout = min(timeout, PROXY_ATTEMPT_TIMEOUT_SECONDS) if proxy else timeout
            async with httpx.AsyncClient(
                timeout=attempt_timeout,
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
        depth_trusted=True,
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
        depth_trusted=False,
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
        depth_trusted=book.depth_trusted,
        min_order_size=book.min_order_size,
        tick_size=book.tick_size,
        raw=payload,
    )


def apply_clob_best_bid_ask(book: OrderBookSnapshot, payload: dict[str, Any]) -> OrderBookSnapshot:
    bids = list(book.bids)
    asks = list(book.asks)
    depth_trusted = book.depth_trusted
    best_bid = payload.get("best_bid") or payload.get("bid")
    best_ask = payload.get("best_ask") or payload.get("ask")

    if best_bid is not None:
        price = float(best_bid)
        known_size = next((level.size for level in bids if level.price == price), None)
        if known_size is None:
            depth_trusted = False
        size = known_size if known_size is not None else bids[0].size if bids else book.min_order_size
        bids = sort_book_levels(update_levels([level for level in bids if level.price <= price], price, size), reverse=True)

    if best_ask is not None:
        price = float(best_ask)
        known_size = next((level.size for level in asks if level.price == price), None)
        if known_size is None:
            depth_trusted = False
        size = known_size if known_size is not None else asks[0].size if asks else book.min_order_size
        asks = sort_book_levels(update_levels([level for level in asks if level.price >= price], price, size), reverse=False)

    return OrderBookSnapshot(
        token_id=book.token_id,
        market_id=str(payload.get("market") or book.market_id or ""),
        timestamp=clob_timestamp(payload.get("timestamp")),
        bids=bids,
        asks=asks,
        depth_trusted=depth_trusted,
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
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            # Market-channel heartbeats are plain-text PING/PONG frames.
            return []

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
    # A single price_change message can contain hundreds of depth deltas for
    # the same token.  Apply all of them locally, but publish only the final
    # snapshot for each token.  Publishing every intermediate snapshot floods
    # the runner, journal, and dashboard and makes the visible top of book lag.
    updates: dict[str, OrderBookSnapshot] = {}
    for event in parse_clob_market_messages(raw):
        event_type = str(event.get("type") or "")
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            continue

        if event_type == "book":
            book = parse_clob_book(payload)
            existing = books.get(book.token_id)
            if book.token_id and (existing is None or book.timestamp >= existing.timestamp):
                books[book.token_id] = book
                updates[book.token_id] = book
            continue

        if event_type == "price_change":
            for change in clob_price_changes(payload):
                token_id = str(change.get("asset_id") or change.get("token_id") or payload.get("asset_id") or payload.get("token_id") or "")
                if not token_id:
                    continue
                book = books.get(token_id)
                if book is None:
                    continue
                updated = apply_clob_price_change(book, payload, change)
                # Multiple messages and multiple deltas in one message often
                # share a millisecond timestamp.  Equal timestamps are valid;
                # only a strictly older update is out of order.
                if updated.timestamp < book.timestamp:
                    continue
                if change.get("best_bid") is not None or change.get("best_ask") is not None:
                    # price_change carries the authoritative top of book.  Use
                    # it to repair a locally incomplete depth cache immediately
                    # instead of waiting for a later best_bid_ask event.
                    updated = apply_clob_best_bid_ask(updated, {**payload, **change})
                books[token_id] = updated
                updates[token_id] = updated
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
            if updated.timestamp < (book.timestamp if book else datetime.min.replace(tzinfo=timezone.utc)):
                continue
            books[token_id] = updated
            updates[token_id] = updated
            continue
    return list(updates.items())


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
    start_time: datetime | None = None
    end_time: datetime | None = None
    updated_at_ms: int = 0


PAST_RESULT_RE = re.compile(
    r'\{"startTime":"(?P<start>[^"]+)","endTime":"(?P<end>[^"]+)",'
    r'"openPrice":(?P<open>[0-9.]+),"closePrice":(?P<close>[0-9.]+)'
    r'(?:,"outcome":"(?P<outcome>[^"]+)")?'
)

REACT_QUERY_OBJECT_START_RE = re.compile(r'\{(?="(?:dehydratedAt|state)":)')
NEXT_FLIGHT_PUSH_RE = re.compile(r'<script>self\.__next_f\.push\((?P<payload>.*?)\)</script>', re.DOTALL)
PRICE_CONFLICT_TOLERANCE = 1e-9


def parse_polymarket_past_results(html: str) -> list[PolymarketPastResult]:
    grouped: dict[tuple[datetime, datetime], list[PolymarketPastResult]] = {}
    normalized = html.replace('\\"', '"')
    for match in PAST_RESULT_RE.finditer(normalized):
        try:
            start = datetime.fromisoformat(match.group("start").replace("Z", "+00:00"))
            end = datetime.fromisoformat(match.group("end").replace("Z", "+00:00"))
            open_price = float(match.group("open"))
            close_price = float(match.group("close"))
            if not all(math.isfinite(value) and 1000 <= value <= 1_000_000 for value in (open_price, close_price)):
                continue
            result = PolymarketPastResult(
                start_time=start,
                end_time=end,
                open_price=open_price,
                close_price=close_price,
                outcome=match.group("outcome"),
            )
        except (TypeError, ValueError):
            continue
        grouped.setdefault((result.start_time, result.end_time), []).append(result)
    results: list[PolymarketPastResult] = []
    for candidates in grouped.values():
        opens = [candidate.open_price for candidate in candidates]
        closes = [candidate.close_price for candidate in candidates]
        if max(opens) - min(opens) > PRICE_CONFLICT_TOLERANCE:
            continue
        if max(closes) - min(closes) > PRICE_CONFLICT_TOLERANCE:
            continue
        results.append(candidates[-1])
    return sorted(results, key=lambda item: item.start_time)


def react_query_objects(html: str) -> list[dict[str, Any]]:
    """Decode complete React Query cache objects without crossing object boundaries."""
    decoder = json.JSONDecoder()
    sources = [html]
    for match in NEXT_FLIGHT_PUSH_RE.finditer(html):
        try:
            payload = json.loads(match.group("payload"))
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(payload, list) and len(payload) > 1 and isinstance(payload[1], str):
            sources.append(payload[1])

    objects: list[dict[str, Any]] = []
    for source in sources:
        for match in REACT_QUERY_OBJECT_START_RE.finditer(source):
            try:
                value, _ = decoder.raw_decode(source[match.start() :])
            except (json.JSONDecodeError, ValueError):
                continue
            if (
                isinstance(value, dict)
                and isinstance(value.get("queryKey"), list)
                and isinstance(value.get("state"), dict)
            ):
                objects.append(value)
    return objects


def parse_polymarket_outcome_prices(html: str) -> list[PolymarketOutcomePrice]:
    grouped: dict[str, list[PolymarketOutcomePrice]] = {}
    for item in react_query_objects(html):
        query_key = item.get("queryKey")
        if (
            len(query_key) != 6
            or query_key[:3] != ["crypto-prices", "price", "BTC"]
            or query_key[4] != "fiveminute"
        ):
            continue
        state = item.get("state")
        if not isinstance(state, dict) or state.get("status") != "success":
            continue
        data = state.get("data")
        if not isinstance(data, dict):
            continue
        try:
            start = datetime.fromisoformat(str(query_key[3]).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(query_key[5]).replace("Z", "+00:00"))
            open_price = float(data["openPrice"])
            close = data.get("closePrice")
            close_price = None if close is None else float(close)
            updated_at_ms = int(state.get("dataUpdatedAt") or item.get("dehydratedAt") or 0)
        except (TypeError, ValueError):
            continue
        numeric_prices = [open_price] + ([] if close_price is None else [close_price])
        if (
            end - start != timedelta(minutes=5)
            or not all(math.isfinite(value) and 1000 <= value <= 1_000_000 for value in numeric_prices)
        ):
            continue
        slug = f"btc-updown-5m-{int(start.timestamp())}"
        grouped.setdefault(slug, []).append(
            PolymarketOutcomePrice(
                slug=slug,
                open_price=open_price,
                close_price=close_price,
                start_time=start,
                end_time=end,
                updated_at_ms=updated_at_ms,
            )
        )
    prices: list[PolymarketOutcomePrice] = []
    for candidates in grouped.values():
        opens = [candidate.open_price for candidate in candidates]
        if max(opens) - min(opens) > PRICE_CONFLICT_TOLERANCE:
            continue
        closes = [candidate.close_price for candidate in candidates if candidate.close_price is not None]
        if closes and max(closes) - min(closes) > PRICE_CONFLICT_TOLERANCE:
            continue
        prices.append(max(candidates, key=lambda candidate: candidate.updated_at_ms))
    return sorted(prices, key=lambda candidate: candidate.start_time or datetime.min.replace(tzinfo=timezone.utc))


class BinanceClient:
    def __init__(self, config: SourceConfig):
        self.config = config

    async def server_time(self) -> dict[str, Any]:
        response, start, end, used_env_proxy = await get_direct_first(
            f"{self.config.binance_rest_url}/api/v3/time",
            timeout=8,
            proxy_url=self.config.proxy_url,
        )
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
            proxy_url=self.config.proxy_url,
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
            options_list = websocket_option_attempts(self.config.proxy_url)
            for options in options_list:
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
                    if connected or options == options_list[-1]:
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
            *(get_direct_first(url, timeout=timeout, params=params, proxy_url=self.config.proxy_url) for url, params in requests),
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

    async def event_threshold(self, market_slug: str) -> float | None:
        """Read the event's price-to-beat from Gamma's compact JSON response."""
        response, _, _, _ = await get_direct_first(
            f"{self.config.gamma_url}/events",
            timeout=self.config.threshold_page_timeout_seconds,
            params={"slug": market_slug},
            proxy_url=self.config.proxy_url,
        )
        payload = response.json()
        events = payload if isinstance(payload, list) else [payload]
        for event in events:
            if not isinstance(event, dict) or event.get("slug") != market_slug:
                continue
            metadata = event.get("eventMetadata") or {}
            try:
                threshold = float(metadata.get("priceToBeat"))
            except (TypeError, ValueError):
                continue
            if threshold >= 1000:
                return threshold
        return None

    async def current_market(self) -> MarketState | None:
        return choose_current_market(
            await self.discover_markets(),
            max_start_price_lag_ms=self.config.max_start_price_lag_ms,
        )

    async def book(self, token_id: str) -> OrderBookSnapshot:
        response, start, end, _ = await get_direct_first(
            f"{self.config.clob_url}/book",
            timeout=8,
            params={"token_id": token_id},
            proxy_url=self.config.proxy_url,
        )
        book = parse_clob_book(response.json())
        # Preserve the CLOB timestamp for ordering against WebSocket updates.
        # Using the request end time here made every following WebSocket update
        # look older and caused it to be discarded.
        book.received_at = end
        book.raw = {
            **(book.raw or {}),
            "_transport": "rest",
            "_request_started_at": start.isoformat(),
        }
        return book

    async def price_probe(self, token_id: str) -> dict[str, Any]:
        response, start, end, used_env_proxy = await get_direct_first(
            f"{self.config.clob_url}/price",
            timeout=8,
            params={"token_id": token_id, "side": "BUY"},
            proxy_url=self.config.proxy_url,
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
        options_list = websocket_option_attempts(self.config.proxy_url)
        for options in options_list:
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
                    loop = asyncio.get_running_loop()
                    last_tick_at = loop.time()
                    while True:
                        remaining = self.config.rtds_stale_seconds - (loop.time() - last_tick_at)
                        if remaining <= 0:
                            raise TimeoutError(
                                f"Polymarket RTDS stale: no valid {symbol.upper()} tick for "
                                f"{self.config.rtds_stale_seconds:g} seconds"
                            )
                        try:
                            message = await asyncio.wait_for(websocket.recv(), timeout=remaining)
                        except asyncio.TimeoutError as exc:
                            raise TimeoutError(
                                f"Polymarket RTDS stale: no valid {symbol.upper()} tick for "
                                f"{self.config.rtds_stale_seconds:g} seconds"
                            ) from exc
                        ticks = parse_rtds_crypto_price_message(message, symbol=symbol)
                        if ticks:
                            last_tick_at = loop.time()
                        for tick in ticks:
                            yield tick
            except Exception:
                if connected or options == options_list[-1]:
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
        interval = market_interval_from_slug(market_slug)
        previous_slug = None
        requests = [self.event_page_text(market_slug, self.config.threshold_page_timeout_seconds)]
        if interval is not None:
            previous_start = interval[0] - timedelta(minutes=5)
            previous_slug = f"btc-updown-5m-{int(previous_start.timestamp())}"
            requests.append(self.event_page_text(previous_slug, self.config.threshold_page_timeout_seconds))
        responses = await asyncio.gather(*requests, return_exceptions=True)
        if isinstance(responses[0], Exception):
            raise responses[0]
        current_text = responses[0]
        assert isinstance(current_text, str)
        outcome_price = next(
            (price for price in parse_polymarket_outcome_prices(current_text) if price.slug == market_slug),
            None,
        )
        results = parse_polymarket_past_results(current_text)
        if interval is not None:
            # The previous close must come from the previous market's own page.
            # Do not let a cached/current-page past-results list satisfy the
            # independent second-source requirement.
            results = [
                result
                for result in results
                if not (
                    result.start_time == interval[0] - timedelta(minutes=5)
                    and result.end_time == interval[0]
                )
            ]
        if previous_slug is not None and len(responses) > 1 and isinstance(responses[1], str):
            previous_outcome = next(
                (price for price in parse_polymarket_outcome_prices(responses[1]) if price.slug == previous_slug),
                None,
            )
            if (
                previous_outcome is not None
                and previous_outcome.start_time == interval[0] - timedelta(minutes=5)
                and previous_outcome.end_time == interval[0]
                and previous_outcome.close_price is not None
            ):
                results.append(
                    PolymarketPastResult(
                        start_time=previous_outcome.start_time,
                        end_time=previous_outcome.end_time,
                        open_price=previous_outcome.open_price,
                        close_price=previous_outcome.close_price,
                    )
                )
        return outcome_price, results

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
            options_list = websocket_option_attempts(self.config.proxy_url)
            for options in options_list:
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
                        heartbeat = asyncio.create_task(send_clob_heartbeats(websocket))
                        try:
                            async for message in websocket:
                                for token_id, book in update_books_from_market_message(message, books):
                                    if token_id in ids:
                                        yield token_id, book
                        finally:
                            heartbeat.cancel()
                            await asyncio.gather(heartbeat, return_exceptions=True)
                except Exception:
                    if connected or options == options_list[-1]:
                        raise
                    continue
        except websockets.ConnectionClosed as exc:
            raise ConnectionError("polymarket websocket disconnected") from exc


async def send_clob_heartbeats(websocket: Any) -> None:
    while True:
        await asyncio.sleep(CLOB_HEARTBEAT_SECONDS)
        await websocket.send("PING")


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
