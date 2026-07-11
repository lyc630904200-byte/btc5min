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

    assert [token_id for token_id, _ in updates] == ["up", "up"]
    assert books["up"].market_id == "m1"
    assert [(level.price, level.size) for level in books["up"].bids] == []
    assert [(level.price, level.size) for level in books["up"].asks] == [(0.52, 12.0), (0.53, 8.0)]


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


def test_clob_market_ignores_best_bid_ask_without_depth() -> None:
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

    assert updates == []
    assert [(level.price, level.size) for level in books["up"].bids] == [(0.48, 10.0)]
    assert [(level.price, level.size) for level in books["up"].asks] == [(0.52, 12.0)]


def test_websocket_connections_try_direct_before_env_proxy() -> None:
    attempts = websocket_option_attempts()
    if "proxy" in attempts[0]:
        assert attempts == [{"proxy": None}, {}]
