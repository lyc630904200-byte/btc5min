from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any, AsyncIterator

import httpx
import websockets
import json

from .config import SourceConfig
from .market import choose_current_market, parse_market
from .models import BookLevel, MarketState, OrderBookSnapshot, PriceTick


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
        async with httpx.AsyncClient(timeout=8) as client:
            start = datetime.now(timezone.utc)
            response = await client.get(f"{self.config.binance_rest_url}/api/v3/time")
            response.raise_for_status()
            end = datetime.now(timezone.utc)
        payload = response.json()
        server_time = datetime.fromtimestamp(payload["serverTime"] / 1000, tz=timezone.utc)
        local_midpoint = start + (end - start) / 2
        return {
            "ok": True,
            "latency_ms": (end - start).total_seconds() * 1000,
            "clock_offset_ms": (server_time - local_midpoint).total_seconds() * 1000,
            "server_time": server_time.isoformat(),
        }

    async def trades(self) -> AsyncIterator[PriceTick]:
        async for websocket in websockets.connect(self.config.binance_ws_url, ping_interval=20):
            try:
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
                continue


class PolymarketClient:
    def __init__(self, config: SourceConfig):
        self.config = config

    async def discover_markets(self) -> list[MarketState]:
        async with httpx.AsyncClient(timeout=10) as client:
            direct_requests = []
            if self.config.market_slug:
                slugs = expand_market_slugs(self.config.market_slug)
                direct_requests.extend(
                    [
                        request
                        for slug in slugs
                        for request in (
                            client.get(f"{self.config.gamma_url}/markets", params={"slug": slug}),
                            client.get(f"{self.config.gamma_url}/events", params={"slug": slug}),
                        )
                    ]
                )
            page_requests = [
                client.get(
                    f"{self.config.gamma_url}/markets",
                    params={"limit": 500, "offset": offset, "active": "true", "closed": "false"},
                )
                for offset in (0, 500, 1000)
            ]
            search_requests = [
                client.get(
                    f"{self.config.gamma_url}/markets",
                    params={"limit": 100, "active": "true", "closed": "false", "search": query},
                )
                for query in ("Bitcoin", "BTC", "up-or-down")
            ]
            responses = await asyncio.gather(*(direct_requests + page_requests + search_requests), return_exceptions=True)
        now = datetime.now(timezone.utc)
        seen: set[str] = set()
        markets: list[MarketState] = []
        for response in responses:
            if isinstance(response, Exception):
                continue
            response.raise_for_status()
            payload = response.json()
            items = payload.get("value") if isinstance(payload, dict) else payload
            for item in items or []:
                candidates = item.get("markets") if isinstance(item, dict) and item.get("markets") else [item]
                for candidate in candidates:
                    market = parse_market(candidate, self.config, now=now)
                    if market and market.condition_id not in seen:
                        seen.add(market.condition_id)
                        markets.append(market)
        return sorted(markets, key=lambda market: market.end_time)

    async def current_market(self) -> MarketState | None:
        return choose_current_market(
            await self.discover_markets(),
            max_start_price_lag_ms=self.config.max_start_price_lag_ms,
        )

    async def book(self, token_id: str) -> OrderBookSnapshot:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.get(f"{self.config.clob_url}/book", params={"token_id": token_id})
            response.raise_for_status()
        return parse_clob_book(response.json())

    async def price_probe(self, token_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=8) as client:
            start = datetime.now(timezone.utc)
            response = await client.get(f"{self.config.clob_url}/price", params={"token_id": token_id, "side": "BUY"})
            response.raise_for_status()
            end = datetime.now(timezone.utc)
        return {"ok": True, "latency_ms": (end - start).total_seconds() * 1000, "payload": response.json()}

    async def past_results(self, market_slug: str) -> list[PolymarketPastResult]:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True, headers={"user-agent": "Mozilla/5.0"}) as client:
            response = await client.get(f"https://polymarket.com/event/{market_slug}")
            response.raise_for_status()
        return parse_polymarket_past_results(response.text)

    async def outcome_price(self, market_slug: str) -> PolymarketOutcomePrice | None:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True, headers={"user-agent": "Mozilla/5.0"}) as client:
            response = await client.get(f"https://polymarket.com/event/{market_slug}")
            response.raise_for_status()
        for price in parse_polymarket_outcome_prices(response.text):
            if price.slug == market_slug:
                return price
        return None


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
