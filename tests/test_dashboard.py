import asyncio
import json

from polybtc.config import AppConfig
from polybtc.dashboard import DashboardHub


def test_compact_book_keeps_only_top_prices() -> None:
    hub = DashboardHub("127.0.0.1", 8765, "127.0.0.1", 8766, AppConfig())

    compact = hub.compact_book(
        {
            "token_id": "yes",
            "market_id": "m1",
            "timestamp": "2026-07-12T15:00:00Z",
            "received_at": "2026-07-12T15:00:01Z",
            "bids": [{"price": 0.52, "size": 10}, {"price": 0.51, "size": 20}],
            "asks": [{"price": 0.55, "size": 15}, {"price": 0.56, "size": 25}],
            "min_order_size": 5,
            "tick_size": 0.01,
        }
    )

    assert compact == {
        "token_id": "yes",
        "market_id": "m1",
        "timestamp": "2026-07-12T15:00:00Z",
        "received_at": "2026-07-12T15:00:01Z",
        "best_bid": 0.52,
        "best_ask": 0.55,
        "min_order_size": 5,
        "tick_size": 0.01,
    }
    assert "bids" not in compact
    assert "asks" not in compact


def test_recent_events_keep_only_fills() -> None:
    hub = DashboardHub("127.0.0.1", 8765, "127.0.0.1", 8766, AppConfig())

    asyncio.run(
        hub.publish(
            {
                "event": {
                    "type": "book",
                    "payload": {
                        "direction": "UP",
                        "token_id": "yes",
                        "timestamp": "2026-07-12T15:00:00Z",
                        "bids": [{"price": 0.51, "size": 20}],
                        "asks": [{"price": 0.53, "size": 15}],
                    },
                },
                "books": {},
            }
        )
    )

    asyncio.run(
        hub.publish(
            {
                "event": {
                    "type": "fill",
                    "payload": {
                        "side": "BUY",
                        "direction": "UP",
                        "avg_price": 0.53,
                        "quantity": 9.43,
                        "quote": 5.0,
                        "reason": "entry",
                        "created_at": "2026-07-12T15:00:01Z",
                    },
                },
                "books": {},
            }
        )
    )

    state = json.loads(hub.state_json())
    assert state["events"] == [
        {
            "type": "fill",
            "payload": {
                "side": "BUY",
                "direction": "UP",
                "avg_price": 0.53,
                "quantity": 9.43,
                "quote": 5.0,
                "reason": "entry",
                "created_at": "2026-07-12T15:00:01Z",
            },
        }
    ]


def test_runtime_config_updates_strategy_and_risk() -> None:
    hub = DashboardHub("127.0.0.1", 8765, "127.0.0.1", 8766, AppConfig())

    config = hub.set_runtime_config(
        {
            "strategy": {
                "min_entry_edge_usd": 18,
                "stop_edge_usd": 20,
                "min_buy_price": 0.42,
                "max_buy_price": 0.72,
                "take_profit_ticks": 0.12,
                "min_seconds_to_entry": 45,
                "max_seconds_to_entry": 180,
            },
            "risk": {"max_order_usd": 12},
        }
    )

    assert config["strategy"] == {
        "min_entry_edge_usd": 18.0,
        "stop_edge_usd": 20.0,
        "min_buy_price": 0.42,
        "max_buy_price": 0.72,
        "take_profit_ticks": 0.12,
        "min_seconds_to_entry": 45.0,
        "max_seconds_to_entry": 180.0,
    }
    assert config["risk"] == {"max_order_usd": 12.0}
