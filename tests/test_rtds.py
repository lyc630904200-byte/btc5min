from datetime import datetime, timezone

from polybtc.clients import parse_rtds_crypto_price_message


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
