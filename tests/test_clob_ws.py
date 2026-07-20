import asyncio
import json

from polybtc.clients import (
    PolymarketClient,
    ProxySafeClientConnection,
    http_option_attempts,
    update_books_from_market_message,
    websocket_option_attempts,
)
from polybtc.config import SourceConfig


def test_clob_market_book_and_price_change_update_local_depth() -> None:
    books = {}

    updates = update_books_from_market_message(
        {
            "event_type": "book",
            "asset_id": "up",
            "market": "m1",
            "timestamp": 1783739400000,
            "bids": [{"price": "0.48", "size": "10"}],
            "asks": [{"price": "0.52", "size": "12"}],
            "min_order_size": "5",
            "tick_size": "0.01",
        },
        books,
    )

    assert len(updates) == 1
    assert books["up"].token_id == "up"
    assert books["up"].market_id == "m1"
    assert books["up"].best_bid == 0.48
    assert books["up"].best_ask == 0.52
    assert books["up"].depth_trusted is True

    updates = update_books_from_market_message(
        {
            "event_type": "price_change",
            "market": "m1",
            "timestamp": 1783739401000,
            "price_changes": [
                {"asset_id": "up", "price": "0.53", "size": "8", "side": "SELL"},
                {"asset_id": "up", "price": "0.48", "size": "0", "side": "BUY"},
            ],
        },
        books,
    )

    assert [token_id for token_id, _ in updates] == ["up"]
    assert updates[0][1].best_bid is None
    assert updates[0][1].best_ask == 0.52
    assert books["up"].market_id == "m1"
    assert books["up"].depth_trusted is True
    assert [(level.price, level.size) for level in books["up"].bids] == []
    assert [(level.price, level.size) for level in books["up"].asks] == [(0.52, 12.0), (0.53, 8.0)]


def test_clob_price_change_publishes_one_final_snapshot_per_token() -> None:
    books = {}
    for token_id in ("up", "down"):
        update_books_from_market_message(
            {
                "event_type": "book",
                "asset_id": token_id,
                "market": "m1",
                "timestamp": 1783739400000,
                "bids": [{"price": "0.48", "size": "10"}],
                "asks": [{"price": "0.52", "size": "12"}],
            },
            books,
        )

    updates = update_books_from_market_message(
        {
            "event_type": "price_change",
            "market": "m1",
            "timestamp": 1783739401000,
            "price_changes": [
                {"asset_id": "up", "price": "0.49", "size": "7", "side": "BUY"},
                {"asset_id": "down", "price": "0.51", "size": "9", "side": "SELL"},
                {"asset_id": "up", "price": "0.52", "size": "0", "side": "SELL"},
            ],
        },
        books,
    )

    assert [token_id for token_id, _ in updates] == ["up", "down"]
    assert updates[0][1].best_bid == 0.49
    assert updates[0][1].best_ask is None
    assert updates[1][1].best_bid == 0.48
    assert updates[1][1].best_ask == 0.51


def test_clob_price_change_uses_authoritative_best_bid_and_ask() -> None:
    books = {}
    update_books_from_market_message(
        {
            "event_type": "book",
            "asset_id": "up",
            "market": "m1",
            "timestamp": 1783739400000,
            "bids": [{"price": "0.40", "size": "10"}],
            "asks": [{"price": "0.60", "size": "12"}],
        },
        books,
    )

    updates = update_books_from_market_message(
        {
            "event_type": "price_change",
            "market": "m1",
            "timestamp": 1783739401000,
            "price_changes": [
                {
                    "asset_id": "up",
                    "price": "0.42",
                    "size": "5",
                    "side": "BUY",
                    "best_bid": "0.55",
                    "best_ask": "0.56",
                }
            ],
        },
        books,
    )

    assert len(updates) == 1
    assert updates[0][1].best_bid == 0.55
    assert updates[0][1].best_ask == 0.56


def test_clob_price_change_accepts_equal_timestamp_but_rejects_older_update() -> None:
    books = {}
    update_books_from_market_message(
        {
            "event_type": "book",
            "asset_id": "up",
            "market": "m1",
            "timestamp": 1783739401000,
            "bids": [{"price": "0.48", "size": "10"}],
            "asks": [{"price": "0.52", "size": "12"}],
        },
        books,
    )

    equal_updates = update_books_from_market_message(
        {
            "event_type": "price_change",
            "market": "m1",
            "timestamp": 1783739401000,
            "price_changes": [{"asset_id": "up", "price": "0.49", "size": "8", "side": "BUY"}],
        },
        books,
    )
    stale_updates = update_books_from_market_message(
        {
            "event_type": "price_change",
            "market": "m1",
            "timestamp": 1783739400000,
            "price_changes": [{"asset_id": "up", "price": "0.50", "size": "6", "side": "BUY"}],
        },
        books,
    )

    assert len(equal_updates) == 1
    assert books["up"].best_bid == 0.49
    assert stale_updates == []


def test_clob_market_parser_accepts_enveloped_messages() -> None:
    books = {}

    updates = update_books_from_market_message(
        {
            "topic": "market",
            "type": "book",
            "payload": {
                "asset_id": "down",
                "market": "m1",
                "timestamp": 1783739400000,
                "bids": [{"price": "0.44", "size": "6"}],
                "asks": [{"price": "0.56", "size": "7"}],
            },
        },
        books,
    )

    assert len(updates) == 1
    assert updates[0][0] == "down"
    assert books["down"].best_bid == 0.44
    assert books["down"].best_ask == 0.56


def test_clob_market_updates_best_bid_ask_from_top_of_book_event() -> None:
    books = {}
    update_books_from_market_message(
        {
            "event_type": "book",
            "asset_id": "up",
            "market": "m1",
            "timestamp": 1783739400000,
            "bids": [{"price": "0.48", "size": "10"}],
            "asks": [{"price": "0.52", "size": "12"}],
        },
        books,
    )

    updates = update_books_from_market_message(
        {
            "event_type": "best_bid_ask",
            "asset_id": "up",
            "market": "m1",
            "timestamp": 1783739401000,
            "best_bid": "0.49",
            "best_ask": "0.51",
        },
        books,
    )

    assert [token_id for token_id, _ in updates] == ["up"]
    assert books["up"].best_bid == 0.49
    assert books["up"].best_ask == 0.51
    assert books["up"].depth_trusted is False


def test_clob_best_bid_ask_can_seed_a_book_and_reject_stale_update() -> None:
    books = {}

    updates = update_books_from_market_message(
        {
            "event_type": "best_bid_ask",
            "asset_id": "up",
            "market": "m1",
            "timestamp": 1783739401000,
            "best_bid": "0.49",
            "best_ask": "0.51",
        },
        books,
    )

    assert [token_id for token_id, _ in updates] == ["up"]
    assert books["up"].best_bid == 0.49
    assert books["up"].best_ask == 0.51
    assert books["up"].depth_trusted is False

    updates = update_books_from_market_message(
        {
            "event_type": "best_bid_ask",
            "asset_id": "up",
            "market": "m1",
            "timestamp": 1783739400000,
            "best_bid": "0.47",
            "best_ask": "0.53",
        },
        books,
    )

    assert updates == []
    assert books["up"].best_bid == 0.49
    assert books["up"].best_ask == 0.51


def test_connections_prefer_system_proxy_before_configured_proxy_and_direct() -> None:
    attempts = websocket_option_attempts()
    if "proxy" in attempts[0]:
        assert attempts == [{}, {"proxy": None}]
        assert websocket_option_attempts("http://127.0.0.1:10808") == [
            {},
            {"proxy": "http://127.0.0.1:10808"},
            {"proxy": None},
        ]
    assert http_option_attempts("http://127.0.0.1:10808") == [
        (None, True),
        ("http://127.0.0.1:10808", False),
        (None, False),
    ]


def test_clob_parser_ignores_plain_text_heartbeat() -> None:
    books = {}

    assert update_books_from_market_message("PONG", books) == []
    assert books == {}


def test_clob_stream_reconnects_preferred_route_without_ending(monkeypatch) -> None:
    payloads = [
        {
            "event_type": "book",
            "asset_id": "up",
            "market": "m1",
            "timestamp": 1783739400000,
            "bids": [{"price": "0.48", "size": "10"}],
            "asks": [{"price": "0.52", "size": "12"}],
        },
        {
            "event_type": "price_change",
            "market": "m1",
            "timestamp": 1783739401000,
            "price_changes": [
                {"asset_id": "up", "price": "0.51", "size": "8", "side": "SELL"}
            ],
        },
    ]
    connect_calls = []

    class Socket:
        def __init__(self, payload):
            self.payload = payload
            self.sent = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def send(self, message):
            return None

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.sent:
                raise StopAsyncIteration
            self.sent = True
            return json.dumps(self.payload)

    def connect(*args, **kwargs):
        connect_calls.append(kwargs)
        return Socket(payloads[len(connect_calls) - 1])

    monkeypatch.setattr("polybtc.clients.websockets.connect", connect)
    monkeypatch.setattr("polybtc.clients.websocket_option_attempts", lambda proxy: [{"route": "system"}])
    monkeypatch.setattr("polybtc.clients.CLOB_RECONNECT_DELAY_SECONDS", 0)

    async def receive_two_updates() -> None:
        stream = PolymarketClient(SourceConfig(proxy_url=None)).book_stream(["up"])
        try:
            first = await anext(stream)
            second = await anext(stream)
        finally:
            await stream.aclose()
        assert first[1].best_ask == 0.52
        assert second[1].best_ask == 0.51

    asyncio.run(receive_two_updates())

    assert len(connect_calls) == 2
    assert all(call["route"] == "system" for call in connect_calls)
    assert all(call["compression"] is None for call in connect_calls)
    assert all(call["max_queue"] == 256 for call in connect_calls)
    assert all(call["create_connection"] is ProxySafeClientConnection for call in connect_calls)


def test_proxy_safe_connection_handles_reset_before_connection_made() -> None:
    class Protocol:
        def __init__(self) -> None:
            self.eof_received = False

        def receive_eof(self) -> None:
            self.eof_received = True

    async def lose_early_connection() -> None:
        loop = asyncio.get_running_loop()
        connection = object.__new__(ProxySafeClientConnection)
        connection.protocol = Protocol()
        connection.recv_exc = None
        connection.keepalive_task = None
        connection.connection_lost_waiter = loop.create_future()
        connection.paused = False
        connection.drain_waiters = []
        reset = ConnectionResetError("proxy reset")

        connection.connection_lost(reset)

        assert connection.protocol.eof_received is True
        assert connection.recv_exc is reset
        assert connection.connection_lost_waiter.done()

    asyncio.run(lose_early_connection())


def test_book_http_requests_reuse_successful_system_proxy_session(monkeypatch) -> None:
    clients = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "asset_id": "up",
                "market": "m1",
                "timestamp": "1783739400000",
                "bids": [{"price": "0.48", "size": "10"}],
                "asks": [{"price": "0.52", "size": "12"}],
            }

    class Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.calls = 0
            self.closed = False
            clients.append(self)

        async def get(self, *args, **kwargs):
            self.calls += 1
            return Response()

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr("polybtc.clients.httpx.AsyncClient", Client)
    polymarket = PolymarketClient(SourceConfig(proxy_url="http://127.0.0.1:10808"))

    async def fetch_twice() -> None:
        await polymarket.book("up")
        await polymarket.book("up")
        await polymarket.aclose()

    asyncio.run(fetch_twice())

    assert len(clients) == 1
    assert clients[0].kwargs["trust_env"] is True
    assert clients[0].calls == 2
    assert clients[0].closed is True
