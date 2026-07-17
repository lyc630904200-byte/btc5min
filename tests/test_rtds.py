import asyncio
from datetime import datetime, timezone

import pytest

from polybtc.clients import PolymarketClient, parse_rtds_crypto_price_message
from polybtc.config import SourceConfig


def test_parse_rtds_crypto_price_history_message_uses_latest_rows() -> None:
    received_at = datetime(2026, 7, 12, 14, 0, tzinfo=timezone.utc)

    ticks = parse_rtds_crypto_price_message(
        {
            "topic": "crypto_prices",
            "payload": {
                "symbol": "btc/usd",
                "data": [
                    {"timestamp": 1783867993000, "value": 64150.1},
                    {"timestamp": 1783867994000, "value": 64160.65},
                ],
            },
        },
        received_at=received_at,
    )

    assert len(ticks) == 2
    assert ticks[-1].source == "polymarket_rtds"
    assert ticks[-1].symbol == "BTC/USD"
    assert ticks[-1].price == 64160.65
    assert ticks[-1].exchange_timestamp == datetime(2026, 7, 12, 14, 53, 14, tzinfo=timezone.utc)
    assert ticks[-1].received_at == received_at


def test_parse_rtds_crypto_price_message_ignores_other_symbols() -> None:
    ticks = parse_rtds_crypto_price_message({"payload": {"symbol": "eth/usd", "value": 3000}})

    assert ticks == []


def test_parse_rtds_crypto_price_message_ignores_heartbeats() -> None:
    ticks = parse_rtds_crypto_price_message("PONG")

    assert ticks == []


def test_rtds_connection_restarts_after_no_valid_tick(monkeypatch) -> None:
    class SilentSocket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def send(self, message):
            return None

        async def recv(self):
            await asyncio.Event().wait()

    monkeypatch.setattr("polybtc.clients.websockets.connect", lambda *args, **kwargs: SilentSocket())

    async def receive_first_tick() -> None:
        stream = PolymarketClient(SourceConfig(proxy_url=None, rtds_stale_seconds=0.01)).rtds_crypto_price_ticks()
        try:
            with pytest.raises(TimeoutError, match="RTDS stale"):
                await anext(stream)
        finally:
            await stream.aclose()

    asyncio.run(receive_first_tick())
