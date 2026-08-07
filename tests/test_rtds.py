import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from polybtc.clients import BinanceClient, PolymarketClient, parse_rtds_crypto_price_message
from polybtc.config import AppConfig, SourceConfig
from polybtc.engine import PaperEngine
from polybtc.market import (
    TWAP_RTDS_CANDIDATE_SOURCE,
    TWAP_RTDS_THRESHOLD_SOURCE,
    threshold_is_tradable,
    threshold_needs_page_confirmation,
)
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


def test_parse_rtds_twap_message_requires_exact_topic_and_window() -> None:
    received_at = datetime(2026, 8, 7, 12, 10, 1, 250000, tzinfo=timezone.utc)
    message = {
        "topic": "crypto_prices_twap_thirty",
        "type": "update",
        "payload": {
            "symbol": "btc/usd",
            "timestamp": 1786104600000,
            "value": 65031.38890253371,
            "window_s": 30,
        },
    }

    ticks = parse_rtds_crypto_price_message(
        message,
        received_at=received_at,
        source="polymarket_rtds_twap_30s",
        expected_topic="crypto_prices_twap_thirty",
        window_seconds=30,
    )

    assert len(ticks) == 1
    assert ticks[0].source == "polymarket_rtds_twap_30s"
    assert ticks[0].exchange_timestamp == datetime(2026, 8, 7, 12, 10, tzinfo=timezone.utc)
    assert parse_rtds_crypto_price_message(
        {**message, "topic": "crypto_prices_chainlink"},
        expected_topic="crypto_prices_twap_thirty",
        window_seconds=30,
    ) == []
    wrong_window = {**message, "payload": {**message["payload"], "window_s": 60}}
    assert parse_rtds_crypto_price_message(
        wrong_window,
        expected_topic="crypto_prices_twap_thirty",
        window_seconds=30,
    ) == []


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


def test_engine_accepts_exact_twap_boundary_tick_while_page_confirmation_is_pending() -> None:
    start = datetime(2026, 8, 7, 12, 10, tzinfo=timezone.utc)
    engine = PaperEngine(AppConfig())
    market = MarketState(
        condition_id="twap-market",
        slug=f"btc-updown-5m-{int(start.timestamp())}",
        question="Bitcoin Up or Down",
        threshold_price=None,
        threshold_source="dynamic_start_price",
        start_time=start,
        end_time=start + timedelta(minutes=5),
        up_token_id="up",
        down_token_id="down",
        raw={
            "cryptoMarketConfig": {
                "twapEnabled": True,
                "twapLookbackSeconds": 30,
            }
        },
    )
    engine.set_market(market)

    # The point-price stream must not become a candidate for a TWAP market.
    engine.set_polymarket_tick(
        PriceTick(
            source="polymarket_rtds",
            symbol="BTC/USD",
            price=65020,
            exchange_timestamp=start,
            received_at=start + timedelta(seconds=1),
        )
    )
    assert market.threshold_candidate_price is None

    changed = engine.set_polymarket_twap_tick(
        PriceTick(
            source="polymarket_rtds_twap_30s",
            symbol="BTC/USD",
            price=65031.38890253371,
            exchange_timestamp=start,
            received_at=start + timedelta(seconds=1, milliseconds=250),
        )
    )

    assert changed is True
    assert market.threshold_price == 65031.38890253371
    assert market.threshold_source == TWAP_RTDS_THRESHOLD_SOURCE
    assert market.threshold_candidate_source == TWAP_RTDS_CANDIDATE_SOURCE
    assert market.threshold_verified is True
    assert threshold_is_tradable(market) is True
    assert threshold_needs_page_confirmation(market) is True
    assert engine.polymarket_tick is not None
    assert engine.polymarket_tick.price == 65020
    assert engine.settlement_price_tick() is engine.polymarket_twap_tick

    engine.set_tick(
        PriceTick(
            source="binance",
            symbol="BTCUSDT",
            price=65080,
            exchange_timestamp=start + timedelta(seconds=2),
            received_at=start + timedelta(seconds=2),
        )
    )
    assert engine.edge_correction_usd() == 65080 - 65031.38890253371
    assert engine.edge_correction_source() == "binance_minus_polymarket_twap_30s"


def test_twap_market_does_not_fall_back_to_chainlink_point_price() -> None:
    start = datetime(2026, 8, 7, 12, 10, tzinfo=timezone.utc)
    engine = PaperEngine(AppConfig())
    engine.set_market(
        MarketState(
            condition_id="twap-market",
            slug=f"btc-updown-5m-{int(start.timestamp())}",
            question="Bitcoin Up or Down",
            threshold_price=None,
            start_time=start,
            end_time=start + timedelta(minutes=5),
            up_token_id="up",
            down_token_id="down",
            raw={"cryptoMarketConfig": {"twapEnabled": True, "twapLookbackSeconds": 30}},
        )
    )
    engine.set_polymarket_tick(
        PriceTick(
            source="polymarket_rtds",
            symbol="BTC/USD",
            price=65020,
            exchange_timestamp=start + timedelta(seconds=1),
            received_at=start + timedelta(seconds=2),
        )
    )
    engine.set_tick(
        PriceTick(
            source="binance",
            symbol="BTCUSDT",
            price=65080,
            exchange_timestamp=start + timedelta(seconds=2),
            received_at=start + timedelta(seconds=2),
        )
    )

    assert engine.settlement_price_tick() is None
    assert engine.edge_correction_usd() is None
    assert engine.edge_correction_source() == "polymarket_price_unavailable"


def test_engine_rejects_late_twap_boundary_tick() -> None:
    start = datetime(2026, 8, 7, 12, 10, tzinfo=timezone.utc)
    engine = PaperEngine(AppConfig())
    market = MarketState(
        condition_id="twap-market",
        slug=f"btc-updown-5m-{int(start.timestamp())}",
        question="Bitcoin Up or Down",
        threshold_price=None,
        threshold_source="dynamic_start_price",
        start_time=start,
        end_time=start + timedelta(minutes=5),
        up_token_id="up",
        down_token_id="down",
        raw={"cryptoMarketConfig": {"twapEnabled": True, "twapLookbackSeconds": 30}},
    )
    engine.set_market(market)

    changed = engine.set_polymarket_twap_tick(
        PriceTick(
            source="polymarket_rtds_twap_30s",
            symbol="BTC/USD",
            price=65031.38,
            exchange_timestamp=start,
            received_at=start + timedelta(seconds=4),
        )
    )

    assert changed is False
    assert market.threshold_price is None
    assert threshold_is_tradable(market) is False


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
