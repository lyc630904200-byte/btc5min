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


def test_dashboard_keeps_btc_and_eth_snapshots_separate() -> None:
    hub = DashboardHub("127.0.0.1", 8765, "127.0.0.1", 8766, AppConfig())

    for asset, price in (("BTC", 64000), ("ETH", 3500)):
        asyncio.run(
            hub.publish(
                {
                    "asset": asset,
                    "event": {"type": "tick", "payload": {"price": price}},
                    "market": {"asset": asset, "condition_id": f"{asset}-market", "slug": f"{asset.lower()}-updown-5m-1"},
                    "tick": {"symbol": f"{asset}USDT", "price": price},
                    "books": {},
                }
            )
        )

    state = json.loads(hub.state_json())

    assert set(state["assets"]) == {"BTC", "ETH"}
    assert state["assets"]["BTC"]["tick"]["price"] == 64000
    assert state["assets"]["ETH"]["tick"]["price"] == 3500
    assert state["assets"]["ETH"]["market"]["asset"] == "ETH"


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
    assert hub.config_json()["pair_match"] == {
        "enabled": False,
        "leg_quote_usd": 10.0,
        "min_spread_cents": 0.0,
        "start_seconds_after_open": 20,
        "end_seconds_after_open": 280,
        "max_pairs_per_market": 1,
        "alternate_directions": True,
        "alternation_mode": "per_market",
    }


def test_pair_config_is_pending_and_persists_after_activation(tmp_path) -> None:
    hub = DashboardHub("127.0.0.1", 8765, "127.0.0.1", 8766, AppConfig(data_dir=tmp_path))

    response = hub.set_runtime_config(
        {
            "pair_match": {
                "enabled": True,
                "leg_quote_usd": 25,
                "min_spread_cents": 2,
                "start_seconds_after_open": 30,
                "end_seconds_after_open": 270,
                "max_pairs_per_market": 3,
                "alternate_directions": False,
                "alternation_mode": "continuous_abab",
            }
        }
    )

    assert response["pair_match"]["enabled"] is False
    assert response["pending_pair_match"]["enabled"] is True
    assert response["pending_pair_match"]["min_spread_cents"] == 2.0
    assert hub.apply_pending_config_for_market("aligned-1") is True

    reloaded = DashboardHub("127.0.0.1", 8765, "127.0.0.1", 8766, AppConfig(data_dir=tmp_path))
    assert reloaded.config.pair_match.enabled is True
    assert reloaded.config.pair_match.leg_quote_usd == 25.0
    assert reloaded.config.pair_match.max_pairs_per_market == 3
    assert reloaded.config.pair_match.alternate_directions is False
    assert reloaded.config.pair_match.alternation_mode == "continuous_abab"


def test_pending_config_waits_for_new_aligned_btc_and_eth_markets(tmp_path) -> None:
    config = AppConfig(data_dir=tmp_path)
    hub = DashboardHub("127.0.0.1", 8765, "127.0.0.1", 8766, config)

    def snapshot(asset: str, market_id: str, start: str, end: str) -> dict:
        return {
            "asset": asset,
            "event": {"type": "market", "payload": {}},
            "market": {
                "asset": asset,
                "condition_id": market_id,
                "slug": f"{asset.lower()}-updown-5m-1",
                "start_time": start,
                "end_time": end,
            },
            "books": {},
            "pair_match": {},
        }

    old_start, old_end = "2026-07-20T00:00:00Z", "2026-07-20T00:05:00Z"
    asyncio.run(hub.publish(snapshot("BTC", "btc-old", old_start, old_end)))
    asyncio.run(hub.publish(snapshot("ETH", "eth-old", old_start, old_end)))
    hub.set_runtime_config({"pair_match": {"enabled": True}})

    new_start, new_end = "2026-07-20T00:05:00Z", "2026-07-20T00:10:00Z"
    asyncio.run(hub.publish(snapshot("BTC", "btc-new", new_start, new_end)))
    assert hub.config.pair_match.enabled is False
    assert hub.pending_config is not None

    asyncio.run(hub.publish(snapshot("ETH", "eth-new", new_start, new_end)))
    assert hub.config.pair_match.enabled is True
    assert hub.pending_config is None
