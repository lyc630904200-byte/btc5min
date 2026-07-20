import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from polybtc.clients import BinanceClient, PolymarketClient, parse_rtds_crypto_price_message
from polybtc.config import AppConfig, SourceConfig
from polybtc.engine import PaperEngine
from polybtc.models import MarketState, PriceTick


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


def test_eth_clients_use_eth_spot_and_chainlink_symbols() -> None:
    received_at = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
    ticks = parse_rtds_crypto_price_message(
        {"payload": {"symbol": "eth/usd", "data": [{"timestamp": 1784534400000, "value": 3540.25}]}},
        symbol="eth/usd",
        received_at=received_at,
    )
    binance = BinanceClient(SourceConfig(), "ETH")
    polymarket = PolymarketClient(SourceConfig(), "ETH")

    assert ticks[0].symbol == "ETH/USD"
    assert ticks[0].price == 3540.25
    assert binance.symbol == "ETHUSDT"
    assert binance.ws_url.endswith("/ethusdt@trade")
    assert polymarket.rtds_symbol == "eth/usd"


def test_eth_engine_captures_only_eth_boundary_tick() -> None:
    start = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
    engine = PaperEngine(AppConfig(), asset="ETH")
    engine.set_polymarket_tick(
        PriceTick(
            source="polymarket_rtds",
            symbol="BTC/USD",
            price=64000,
            exchange_timestamp=start,
            received_at=start,
        )
    )
    engine.set_polymarket_tick(
        PriceTick(
            source="polymarket_rtds",
            symbol="ETH/USD",
            price=3540.25,
            exchange_timestamp=start,
            received_at=start,
        )
    )
    engine.set_market(
        MarketState(
            asset="ETH",
            condition_id="eth-market",
            slug=f"eth-updown-5m-{int(start.timestamp())}",
            question="Ethereum Up or Down",
            threshold_price=None,
            threshold_source="dynamic_start_price",
            start_time=start,
            end_time=start + timedelta(minutes=5),
            up_token_id="eth-up",
            down_token_id="eth-down",
        )
    )

    assert engine.polymarket_tick is not None
    assert engine.polymarket_tick.symbol == "ETH/USD"
    assert engine.market is not None
    assert engine.market.threshold_candidate_price == 3540.25


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
