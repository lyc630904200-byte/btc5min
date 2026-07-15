from polybtc.clients import update_books_from_market_message, websocket_option_attempts


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


def test_websocket_connections_try_direct_before_env_proxy() -> None:
    attempts = websocket_option_attempts()
    if "proxy" in attempts[0]:
        assert attempts == [{"proxy": None}, {}]


def test_clob_parser_ignores_plain_text_heartbeat() -> None:
    books = {}

    assert update_books_from_market_message("PONG", books) == []
    assert books == {}
