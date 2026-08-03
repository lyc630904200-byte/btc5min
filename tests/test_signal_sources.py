from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from polybtc.config import SourceConfig
from polybtc.models import BookLevel
from polybtc.signal_sources import (
    BinanceFuturesSignalClient,
    BinanceSpotSignalClient,
    CoinbaseSignalClient,
    KrakenSignalClient,
    kraken_book_checksum,
)


NOW = datetime(2026, 8, 3, 0, 0, tzinfo=timezone.utc)


def test_binance_depth_sequence_and_gap_detection() -> None:
    client = BinanceSpotSignalClient(SourceConfig())
    client.books = {
        "bids": {100.0: 2.0},
        "asks": {101.0: 3.0},
    }
    client.last_update_id = 10

    event = client._apply_depth(
        {"U": 11, "u": 12, "E": 1_786_000_000_000, "b": [["100", "4"]], "a": []},
        NOW,
    )
    assert event is not None
    assert event.kind == "book"
    assert event.sequence == 12
    assert event.bids[0].size == 4

    gap = client._apply_depth(
        {"U": 15, "u": 16, "E": 1_786_000_000_100, "b": [], "a": []},
        NOW,
    )
    assert gap is not None
    assert gap.kind == "health"
    assert gap.valid is False
    assert gap.reason == "sequence_gap"
    assert client.last_update_id is None


def test_coinbase_trade_side_gap_and_rest_backfill() -> None:
    client = CoinbaseSignalClient(SourceConfig())
    first = client.parse(
        {
            "type": "match",
            "product_id": "BTC-USD",
            "trade_id": 10,
            "price": "100",
            "size": "0.2",
            "side": "sell",
            "time": NOW.isoformat(),
        },
        NOW,
    )[0]
    assert first.taker_side == "buy"
    gap = client.parse(
        {
            "type": "match",
            "product_id": "BTC-USD",
            "trade_id": 13,
            "price": "101",
            "size": "0.3",
            "side": "buy",
            "time": NOW.isoformat(),
        },
        NOW,
    )[0]
    assert gap.valid is False
    assert client.pending_trade_gap == (10, 13)

    class Response:
        headers = {}

        def raise_for_status(self) -> None:
            return None

        def json(self) -> list[dict[str, str]]:
            return [
                {
                    "trade_id": "12",
                    "price": "100.8",
                    "size": "0.1",
                    "side": "sell",
                    "time": NOW.isoformat(),
                },
                {
                    "trade_id": "11",
                    "price": "100.4",
                    "size": "0.1",
                    "side": "buy",
                    "time": NOW.isoformat(),
                },
            ]

    class Http:
        async def get(self, *_args, **_kwargs) -> Response:
            return Response()

    recovered = asyncio.run(client._backfill_trades(Http(), 10, 13, NOW))
    assert [event.sequence for event in recovered] == [11, 12]
    assert all(event.valid and event.reason == "rest_backfill" for event in recovered)


def test_coinbase_level2_batch_builds_local_book() -> None:
    client = CoinbaseSignalClient(SourceConfig())
    snapshot = client.parse(
        {
            "type": "snapshot",
            "product_id": "BTC-USD",
            "bids": [["100", "2"]],
            "asks": [["101", "3"]],
        },
        NOW,
    )[0]
    assert snapshot.valid is True
    update = client.parse(
        {
            "type": "l2update",
            "product_id": "BTC-USD",
            "changes": [["buy", "100", "0"], ["buy", "99", "5"]],
        },
        NOW,
    )[0]
    assert [level.price for level in update.bids] == [99.0]
    assert update.asks[0].price == 101.0


def test_kraken_book_checksum_is_enforced() -> None:
    client = KrakenSignalClient(SourceConfig())
    bids = [BookLevel(price=100.0, size=2.0)]
    asks = [BookLevel(price=101.0, size=3.0)]
    checksum = kraken_book_checksum(bids, asks)
    payload = {
        "channel": "book",
        "type": "snapshot",
        "data": [
            {
                "symbol": "BTC/USD",
                "bids": [{"price": "100.0", "qty": "2.0"}],
                "asks": [{"price": "101.0", "qty": "3.0"}],
                "checksum": checksum,
                "timestamp": NOW.isoformat(),
            }
        ],
    }
    valid = client.parse(payload, NOW)[0]
    assert valid.valid is True

    payload["data"][0]["checksum"] = checksum + 1
    invalid = client.parse(payload, NOW)[0]
    assert invalid.valid is False
    assert invalid.reason == "checksum_mismatch"


def test_binance_futures_parses_trade_book_mark_funding_and_liquidation() -> None:
    client = BinanceFuturesSignalClient(SourceConfig())
    trade = client.parse(
        {"e": "aggTrade", "E": 1_786_000_000_000, "p": "100", "q": "2", "m": False, "a": 7},
        NOW,
    )[0]
    assert trade.kind == "trade"
    assert trade.taker_side == "buy"

    book = client.parse(
        {"e": "depthUpdate", "E": 1_786_000_000_000, "u": 8, "b": [["99", "4"]], "a": [["101", "5"]]},
        NOW,
    )[0]
    assert book.kind == "book"
    assert book.valid is True

    mark, funding = client.parse(
        {"e": "markPriceUpdate", "E": 1_786_000_000_000, "p": "100.5", "r": "0.0001"},
        NOW,
    )
    assert mark.kind == "mark_price"
    assert funding.kind == "funding"

    liquidation = client.parse(
        {"e": "forceOrder", "E": 1_786_000_000_000, "o": {"ap": "100", "q": "3", "S": "SELL"}},
        NOW,
    )[0]
    assert liquidation.kind == "liquidation"
    assert liquidation.taker_side == "sell"


def test_binance_futures_rest_fallback_seeds_then_emits_only_new_trades() -> None:
    client = BinanceFuturesSignalClient(SourceConfig())
    first = [
        {"a": 10, "T": 1_786_000_000_000, "p": "100", "q": "1", "m": False},
        {"a": 11, "T": 1_786_000_000_100, "p": "101", "q": "2", "m": True},
    ]
    assert client.parse_rest_trades(first, NOW) == []

    second = [
        {"a": 11, "T": 1_786_000_000_100, "p": "101", "q": "2", "m": True},
        {"a": 12, "T": 1_786_000_000_200, "p": "102", "q": "3", "m": False},
    ]
    events = client.parse_rest_trades(second, NOW)
    assert [event.sequence for event in events] == [12]
    assert events[0].taker_side == "buy"
    assert events[0].reason == "rest_fallback"

    assert client.parse(
        {"e": "aggTrade", "E": 1_786_000_000_200, "p": "102", "q": "3", "m": False, "a": 12},
        NOW,
    ) == []


def test_binance_futures_rest_premium_emits_mark_and_funding() -> None:
    client = BinanceFuturesSignalClient(SourceConfig())
    mark, funding = client.parse_rest_premium(
        {
            "time": 1_786_000_000_000,
            "markPrice": "100.5",
            "lastFundingRate": "0.0002",
        },
        NOW,
    )
    assert mark.kind == "mark_price"
    assert mark.price == 100.5
    assert funding.kind == "funding"
    assert funding.value == 0.0002
