import asyncio
import json

from polybtc.config import AppConfig
from polybtc.dashboard import DashboardHub


def test_compact_market_exposes_threshold_verification() -> None:
    hub = DashboardHub("127.0.0.1", 8765, "127.0.0.1", 8766, AppConfig())

    compact = hub.compact_market(
        {
            "condition_id": "m1",
            "slug": "btc-updown-5m-1784214900",
            "threshold_price": 64307.33159905584,
            "threshold_source": "polymarket_page_verified_open_price",
            "threshold_verified": True,
            "threshold_fetched_at": "2026-07-16T15:17:05Z",
        }
    )

    assert compact is not None
    assert compact["threshold_verified"] is True
    assert compact["threshold_fetched_at"] == "2026-07-16T15:17:05Z"


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
        "depth_trusted": False,
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
                        "fee_usd": None,
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
                "fee_usd": None,
                "reason": "entry",
                "created_at": "2026-07-12T15:00:01Z",
            },
        }
    ]


def test_runtime_config_saves_for_next_market_and_persists_active_values(tmp_path) -> None:
    config = AppConfig(data_dir=tmp_path)
    hub = DashboardHub("127.0.0.1", 8765, "127.0.0.1", 8766, config)
    hub.latest["market"] = {"condition_id": "m1"}

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
                "reverse_entry_enabled": True,
                "entry_confirmation_enabled": False,
            },
            "risk": {
                "max_order_usd": 12,
                "max_loss_usd": 3,
                "max_trades_per_market": 2,
            },
        }
    )

    assert config["config_status"] == "pending_next_market"
    assert config["strategy"]["min_entry_edge_usd"] == 10.0
    assert config["pending_strategy"]["min_entry_edge_usd"] == 18.0
    assert config["pending_strategy"]["reverse_entry_enabled"] is True
    assert config["pending_strategy"]["entry_confirmation_enabled"] is False
    assert config["pending_risk"]["max_order_usd"] == 12.0
    assert config["pending_risk"]["max_loss_usd"] == 3.0
    assert config["pending_risk"]["max_trades_per_market"] == 2
    assert hub.apply_pending_config_for_market("m1") is False
    assert hub.apply_pending_config_for_market("m2") is True
    assert hub.config_json()["strategy"]["min_entry_edge_usd"] == 18.0
    assert hub.config_json()["strategy"]["reverse_entry_enabled"] is True
    assert hub.config_json()["strategy"]["entry_confirmation_enabled"] is False
    assert hub.config_json()["risk"] == {
        "max_order_usd": 12.0,
        "max_loss_usd": 3.0,
        "max_trades_per_market": 2,
    }

    reloaded = DashboardHub("127.0.0.1", 8765, "127.0.0.1", 8766, AppConfig(data_dir=tmp_path))
    assert reloaded.config_json()["strategy"]["min_entry_edge_usd"] == 18.0
    assert reloaded.config_json()["strategy"]["reverse_entry_enabled"] is True
    assert reloaded.config_json()["risk"] == {
        "max_order_usd": 12.0,
        "max_loss_usd": 3.0,
        "max_trades_per_market": 2,
    }


def test_old_runtime_settings_gain_new_risk_defaults(tmp_path) -> None:
    (tmp_path / "dashboard-settings.json").write_text(
        json.dumps(
            {
                "active": {
                    "strategy": {"min_entry_edge_usd": 18},
                    "risk": {"max_order_usd": 12},
                },
                "pending": None,
                "apply_after_market_id": None,
            }
        ),
        encoding="utf-8",
    )
    config = AppConfig(
        data_dir=tmp_path,
        risk={"max_loss_usd": 3.5, "max_trades_per_market": 2},
    )

    hub = DashboardHub("127.0.0.1", 8765, "127.0.0.1", 8766, config)

    assert hub.config_json()["strategy"]["min_entry_edge_usd"] == 18.0
    assert hub.config_json()["strategy"]["reverse_entry_enabled"] is False
    assert hub.config_json()["risk"] == {
        "max_order_usd": 12.0,
        "max_loss_usd": 3.5,
        "max_trades_per_market": 2,
    }
