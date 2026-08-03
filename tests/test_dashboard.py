import asyncio
import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from polybtc.btc_dynamic import BtcDynamicEngine, BtcDynamicRegistry
from polybtc.btc_v8 import BtcV8Engine, BtcV8Registry
from polybtc.config import AppConfig
from polybtc.dashboard import DashboardHub, DashboardRequestHandler
from polybtc.models import MarketState


def test_recovery_orders_table_uses_merged_order_columns() -> None:
    html = (
        Path(__file__).resolve().parents[1] / "web" / "index.html"
    ).read_text(encoding="utf-8")
    header = (
        "<th>订单编号</th><th>方向</th><th>买入时间</th>"
        "<th>买入均价</th><th>买入金额</th><th>卖出时间</th>"
        "<th>卖出均价</th><th>卖出金额</th><th>数量</th>"
        "<th>手续费</th><th>官方结果</th><th>净盈亏</th>"
    )

    assert header in html
    assert "<th>方向</th><th>买卖</th>" not in html
    assert "${order.side || '--'}" not in html
    assert "${recoveryReasonText(order.reason)}" not in html
    assert 'id="recoveryTargetPrice" type="number" min="1" max="100"' in html
    assert 'id="recoveryTriggerPrice" type="number" min="0" max="99"' in html


def test_dynamic_sizing_controls_are_mutually_exclusive() -> None:
    html = (
        Path(__file__).resolve().parents[1] / "web" / "index.html"
    ).read_text(encoding="utf-8")

    assert 'id="dynamicSizingQuantity"' in html
    assert 'id="dynamicSizingQuote"' in html
    assert 'name="dynamicSizingMode"' in html
    assert 'id="dynamicQuoteAmount"' in html
    assert 'id="dynamicLossStreak"' in html
    assert 'id="dynamicCooldownMinutes"' in html
    assert "$('dynamicQuantity').disabled = quoteMode;" in html
    assert "$('dynamicQuoteAmount').disabled = !quoteMode;" in html
    assert "sizing_mode: $('dynamicSizingQuote').checked ? 'quote' : 'quantity'" in html
    assert "loss_streak_limit: runtimeNumber('dynamicLossStreak')" in html
    assert "loss_cooldown_minutes: runtimeNumber('dynamicCooldownMinutes')" in html


def test_v8_dashboard_has_independent_health_trading_and_config_views() -> None:
    html = (
        Path(__file__).resolve().parents[1] / "web" / "index.html"
    ).read_text(encoding="utf-8")

    assert 'id="assetV8"' in html
    assert 'id="v8SourceBinance"' in html
    assert 'id="v8SourceCoinbase"' in html
    assert 'id="v8SourceKraken"' in html
    assert 'id="v8SourceFutures"' in html
    assert 'id="v8Trades"' in html
    assert '<th>买入：时间 / 均价 / 金额</th>' in html
    assert '<th>卖出或结算：时间 / 均价 / 金额</th>' in html
    assert "function groupV8Trades(trades)" in html
    assert "const tradeGroups = groupV8Trades(trades);" in html
    assert 'id="v8SourceMetrics"' in html
    assert 'id="v8Contributions"' in html
    assert 'id="v8Calibration"' in html
    assert 'id="v8Form"' in html
    assert 'id="v8AutoDecision" type="checkbox"' in html
    assert 'id="v8OrderbookChase" type="checkbox"' in html
    assert 'id="v8AutoEmergencyLoss" type="checkbox"' in html
    assert "auto_decision_mode: $('v8AutoDecision').checked" in html
    assert "orderbook_chase_mode: $('v8OrderbookChase').checked" in html
    assert "auto_emergency_loss_enabled: $('v8AutoEmergencyLoss').checked" in html
    assert "function syncV8DecisionModeState()" in html
    assert "'v8MaxLoss'," in html
    assert '<strong>持仓方向 / 均价</strong>' in html
    assert 'id="v8ChaseLead"' in html
    assert 'id="v8ChaseTarget"' in html
    assert "`${position.direction} · ${positionPrice}`" in html
    assert 'id="v8QuoteAmount" type="number" min="0.01" step="0.01" value="5"' in html
    assert 'id="v8BuyEdge" type="number" min="0" step="0.01" value="5"' in html
    assert 'id="v8EvalMs" type="number" min="1" step="1" value="250"' in html
    assert 'id="v8UseBinance" type="checkbox" checked' in html
    assert 'id="v8UseCoinbase" type="checkbox" checked' in html
    assert 'id="v8UseKraken" type="checkbox" checked' in html
    assert "function escapeHtml(value)" in html
    assert "spot_exchanges: spotExchanges" in html
    assert "buy_edge_cents: runtimeNumber('v8BuyEdge')" in html
    assert "sell_edge_cents: runtimeNumber('v8SellEdge')" in html


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


def test_recovery_order_stop_command_is_queued() -> None:
    hub = DashboardHub("127.0.0.1", 8765, "127.0.0.1", 8766, AppConfig())

    response = hub.request_recovery_orders_stopped(True)

    assert response == {
        "accepted": True,
        "recovery_orders_stopped": True,
    }
    assert hub.control_commands.get_nowait() == (
        "btc_recovery_orders_stopped",
        True,
    )


def test_recovery_statistics_reset_command_is_queued() -> None:
    hub = DashboardHub("127.0.0.1", 8765, "127.0.0.1", 8766, AppConfig())

    response = hub.request_btc_recovery_statistics_reset()

    assert response["accepted"] is True
    assert response["statistics_reset_at"]
    command, reset_at = hub.control_commands.get_nowait()
    assert command == "btc_recovery_statistics_reset"
    assert reset_at.tzinfo == timezone.utc


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


def test_dashboard_keeps_global_history_out_of_asset_snapshots() -> None:
    hub = DashboardHub("127.0.0.1", 8765, "127.0.0.1", 8766, AppConfig())
    pair_match = {
        "status": "running",
        "recent_orders": [{"payload": "p" * 1000} for _ in range(100)],
    }
    btc_recovery = {
        "status": "running",
        "recent_orders": [{"payload": "r" * 1000} for _ in range(300)],
    }

    for asset in ("BTC", "ETH"):
        asyncio.run(
            hub.publish(
                {
                    "asset": asset,
                    "event": {
                        "type": "pair_state",
                        "payload": pair_match,
                    },
                    "market": {
                        "asset": asset,
                        "condition_id": f"{asset}-market",
                    },
                    "books": {},
                    "pair_match": pair_match,
                    "btc_recovery": btc_recovery,
                }
            )
        )

    body = hub.state_json()
    state = json.loads(body)

    assert state["pair_match"] == pair_match
    assert state["btc_recovery"] == btc_recovery
    assert state["event"] == {"type": "pair_state", "payload": {}}
    assert all(
        "pair_match" not in snapshot and "btc_recovery" not in snapshot
        for snapshot in state["assets"].values()
    )
    assert len(body) < 500_000


def test_dashboard_throttles_frequent_book_snapshots() -> None:
    hub = DashboardHub("127.0.0.1", 8765, "127.0.0.1", 8766, AppConfig())
    last_push_at = datetime.now(timezone.utc)
    hub.last_push_at = last_push_at

    asyncio.run(
        hub.publish(
            {
                "asset": "BTC",
                "event": {
                    "type": "book",
                    "payload": {
                        "direction": "UP",
                        "bids": [{"price": 0.51, "size": 20}],
                        "asks": [{"price": 0.53, "size": 15}],
                    },
                },
                "books": {},
            }
        )
    )

    assert hub.last_push_at == last_push_at
    assert hub.latest["event"]["type"] == "book"


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
        "second_order_min_spread_cents": 0.0,
        "min_leg_price_gap_cents": 0.0,
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
                "second_order_min_spread_cents": 7.5,
                "min_leg_price_gap_cents": 12.5,
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
    assert response["pending_pair_match"]["second_order_min_spread_cents"] == 7.5
    assert response["pending_pair_match"]["min_leg_price_gap_cents"] == 12.5
    assert hub.apply_pending_config_for_market("aligned-1") is True

    reloaded = DashboardHub("127.0.0.1", 8765, "127.0.0.1", 8766, AppConfig(data_dir=tmp_path))
    assert reloaded.config.pair_match.enabled is True
    assert reloaded.config.pair_match.leg_quote_usd == 25.0
    assert reloaded.config.pair_match.second_order_min_spread_cents == 7.5
    assert reloaded.config.pair_match.min_leg_price_gap_cents == 12.5
    assert reloaded.config.pair_match.max_pairs_per_market == 3
    assert reloaded.config.pair_match.alternate_directions is False
    assert reloaded.config.pair_match.alternation_mode == "continuous_abab"


def test_fixed_pair_modes_are_pending_and_persist_after_activation(tmp_path) -> None:
    for mode in ("always_a", "always_b"):
        data_dir = tmp_path / mode
        hub = DashboardHub("127.0.0.1", 8765, "127.0.0.1", 8766, AppConfig(data_dir=data_dir))

        response = hub.set_runtime_config(
            {
                "pair_match": {
                    "enabled": True,
                    "alternate_directions": True,
                    "alternation_mode": mode,
                }
            }
        )

        assert response["pending_pair_match"]["alternation_mode"] == mode
        assert hub.apply_pending_config_for_market("aligned-1") is True

        reloaded = DashboardHub(
            "127.0.0.1", 8765, "127.0.0.1", 8766, AppConfig(data_dir=data_dir)
        )
        assert reloaded.config.pair_match.alternate_directions is True
        assert reloaded.config.pair_match.alternation_mode == mode
        assert reloaded.config.pair_match.max_pairs_per_market == 1


def test_sequence_pair_modes_default_to_two_and_preserve_explicit_limit(tmp_path) -> None:
    for mode in ("per_market_ab", "per_market_ba"):
        data_dir = tmp_path / mode
        hub = DashboardHub("127.0.0.1", 8765, "127.0.0.1", 8766, AppConfig(data_dir=data_dir))

        response = hub.set_runtime_config(
            {"pair_match": {"alternate_directions": True, "alternation_mode": mode}}
        )

        assert response["pending_pair_match"]["alternation_mode"] == mode
        assert response["pending_pair_match"]["max_pairs_per_market"] == 2
        assert hub.apply_pending_config_for_market("aligned-1") is True

        explicit = hub.set_runtime_config(
            {
                "pair_match": {
                    "alternation_mode": mode,
                    "max_pairs_per_market": 4,
                }
            }
        )
        assert explicit["pending_pair_match"]["max_pairs_per_market"] == 4
        assert hub.apply_pending_config_for_market("aligned-2") is True

        reloaded = DashboardHub(
            "127.0.0.1", 8765, "127.0.0.1", 8766, AppConfig(data_dir=data_dir)
        )
        assert reloaded.config.pair_match.alternation_mode == mode
        assert reloaded.config.pair_match.max_pairs_per_market == 4


def test_two_stage_mode_forces_pair_limit_and_strict_direction_and_persists(tmp_path) -> None:
    hub = DashboardHub("127.0.0.1", 8765, "127.0.0.1", 8766, AppConfig(data_dir=tmp_path))

    response = hub.set_runtime_config(
        {
            "pair_match": {
                "alternation_mode": "per_market_two_stage",
                "second_order_min_spread_cents": 6.5,
                "max_pairs_per_market": 9,
                "alternate_directions": False,
            }
        }
    )

    pending = response["pending_pair_match"]
    assert pending["alternation_mode"] == "per_market_two_stage"
    assert pending["second_order_min_spread_cents"] == 6.5
    assert pending["max_pairs_per_market"] == 2
    assert pending["alternate_directions"] is True
    assert hub.apply_pending_config_for_market("aligned-1") is True

    reloaded = DashboardHub(
        "127.0.0.1", 8765, "127.0.0.1", 8766, AppConfig(data_dir=tmp_path)
    )
    assert reloaded.config.pair_match.alternation_mode == "per_market_two_stage"
    assert reloaded.config.pair_match.second_order_min_spread_cents == 6.5
    assert reloaded.config.pair_match.max_pairs_per_market == 2
    assert reloaded.config.pair_match.alternate_directions is True


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


def test_btc_recovery_config_waits_only_for_next_btc_market_and_persists(tmp_path) -> None:
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
            "btc_recovery": {},
        }

    old_start, old_end = "2026-07-20T00:00:00Z", "2026-07-20T00:05:00Z"
    asyncio.run(hub.publish(snapshot("BTC", "btc-old", old_start, old_end)))
    asyncio.run(hub.publish(snapshot("ETH", "eth-old", old_start, old_end)))

    response = hub.set_runtime_config(
        {
            "btc_recovery": {
                "enabled": True,
                "entry_price_cents": 68,
                "max_entry_price_cents": 92,
                "target_price_cents": 100,
                "recovery_target_price_cents": 87,
                "recovery_trigger_cents": 0,
                "stop_price_cents": 28,
                "initial_quantity": 6,
                "recovery_quantity": 18,
                "entry_seconds_after_open": 15,
                "exit_seconds_after_open": 270,
            }
        }
    )

    assert response["config_status"] == "pending_next_btc_market"
    assert response["btc_recovery"]["enabled"] is False
    assert response["pending_btc_recovery"]["enabled"] is True
    assert response["pending_btc_recovery"]["max_entry_price_cents"] == 92.0
    assert response["pending_btc_recovery"]["initial_quantity"] == 6.0

    new_start, new_end = "2026-07-20T00:05:00Z", "2026-07-20T00:10:00Z"
    asyncio.run(hub.publish(snapshot("ETH", "eth-new", new_start, new_end)))
    assert hub.config.btc_recovery.enabled is False
    asyncio.run(hub.publish(snapshot("BTC", "btc-old", old_start, old_end)))
    assert hub.config.btc_recovery.enabled is False

    asyncio.run(hub.publish(snapshot("BTC", "btc-new", new_start, new_end)))
    assert hub.config.btc_recovery.enabled is True
    assert hub.config.btc_recovery.recovery_trigger_cents == 0.0
    assert hub.pending_config is None

    reloaded = DashboardHub(
        "127.0.0.1", 8765, "127.0.0.1", 8766, AppConfig(data_dir=tmp_path)
    )
    assert reloaded.config.btc_recovery.enabled is True
    assert reloaded.config.btc_recovery.max_entry_price_cents == 92.0
    assert reloaded.config.btc_recovery.target_price_cents == 100.0
    assert reloaded.config.btc_recovery.recovery_target_price_cents == 87.0
    assert reloaded.config.btc_recovery.exit_seconds_after_open == 270.0


def test_real_trading_config_is_backward_compatible_and_activates_next_btc_market(
    tmp_path,
) -> None:
    settings_path = tmp_path / "dashboard-settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "active": {"btc_recovery": {"enabled": True}},
                "pending": None,
                "apply_after_market_id": None,
            }
        ),
        encoding="utf-8",
    )
    hub = DashboardHub(
        "127.0.0.1", 8765, "127.0.0.1", 8766, AppConfig(data_dir=tmp_path)
    )
    assert hub.config_json()["real_trading"] == {
        "enabled": False,
        "order_quantity": 1.0,
        "max_order_notional_usd": 5.0,
        "daily_loss_limit_usd": 10.0,
        "max_orders_per_day": 10,
        "auto_redeem": True,
        "shadow_required_signals": 20,
    }

    hub.asset_snapshots["BTC"] = {
        "market": {"asset": "BTC", "condition_id": "btc-old"}
    }
    response = hub.set_runtime_config(
        {
            "real_trading": {
                "enabled": True,
                "order_quantity": 5,
                "max_order_notional_usd": 5,
            }
        }
    )

    assert response["config_status"] == "pending_next_btc_market"
    assert response["real_trading"]["enabled"] is False
    assert response["pending_real_trading"]["enabled"] is True
    assert hub.apply_pending_config_for_market("btc-old") is False
    assert hub.apply_pending_config_for_market("btc-new") is True
    assert hub.config.real_trading.enabled is True
    assert hub.config.real_trading.order_quantity == 5

    reloaded = DashboardHub(
        "127.0.0.1", 8765, "127.0.0.1", 8766, AppConfig(data_dir=tmp_path)
    )
    assert reloaded.config.real_trading.enabled is True
    assert reloaded.config.real_trading.order_quantity == 5


def test_real_trading_controls_require_token_and_confirmation(tmp_path) -> None:
    hub = DashboardHub(
        "127.0.0.1", 8765, "127.0.0.1", 8766, AppConfig(data_dir=tmp_path)
    )
    handler = type(
        "TestDashboardRequestHandler",
        (DashboardRequestHandler,),
        {"hub": hub},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = (
        f"http://127.0.0.1:{server.server_address[1]}/api/real-trading/arm"
    )

    def post(token: str | None, confirmation: str | None) -> int:
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-Polybtc-Control-Token"] = token
        payload = json.dumps({"confirmation": confirmation}).encode()
        request = urllib.request.Request(
            endpoint, data=payload, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    try:
        assert post(None, "ENABLE_REAL_TRADING") == 403
        assert post(hub.control_token, "wrong") == 400
        assert post(hub.control_token, "ENABLE_REAL_TRADING") == 202
        assert hub.control_commands.get_nowait() == ("real_trading_arm", True)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_btc_dynamic_config_is_backward_compatible_and_activates_next_btc_market(
    tmp_path,
) -> None:
    (tmp_path / "dashboard-settings.json").write_text(
        json.dumps({"active": {"btc_recovery": {"enabled": True}}, "pending": None}),
        encoding="utf-8",
    )
    hub = DashboardHub(
        "127.0.0.1", 8765, "127.0.0.1", 8766, AppConfig(data_dir=tmp_path)
    )
    assert hub.config.btc_dynamic.enabled is False
    assert hub.config.btc_dynamic.sizing_mode == "quantity"
    assert hub.config.btc_dynamic.quantity == 10
    assert hub.config.btc_dynamic.quote_amount_usd == 5
    assert hub.config.btc_dynamic.slippage_reserve_cents == 1.35
    assert hub.config.btc_dynamic.loss_streak_limit == 5
    assert hub.config.btc_dynamic.loss_cooldown_minutes == 30

    hub.asset_snapshots["BTC"] = {
        "market": {"asset": "BTC", "condition_id": "btc-old"}
    }
    response = hub.set_runtime_config(
        {
            "btc_dynamic": {
                "enabled": True,
                "sizing_mode": "quote",
                "quantity": 12,
                "quote_amount_usd": 7,
                "min_net_edge_cents": 4,
                "loss_streak_limit": 4,
                "loss_cooldown_minutes": 20,
            }
        }
    )
    assert response["config_status"] == "pending_next_btc_market"
    assert response["btc_dynamic"]["enabled"] is False
    assert response["pending_btc_dynamic"]["enabled"] is True
    assert response["pending_btc_dynamic"]["sizing_mode"] == "quote"
    assert response["pending_btc_dynamic"]["quantity"] == 12
    assert response["pending_btc_dynamic"]["quote_amount_usd"] == 7
    assert response["pending_btc_dynamic"]["loss_streak_limit"] == 4
    assert response["pending_btc_dynamic"]["loss_cooldown_minutes"] == 20
    assert hub.apply_pending_config_for_market("btc-old") is False
    assert hub.apply_pending_config_for_market("btc-new") is True
    assert hub.config.btc_dynamic.enabled is True
    assert hub.config.btc_dynamic.min_net_edge_cents == 4
    assert hub.config.btc_dynamic.loss_streak_limit == 4
    assert hub.config.btc_dynamic.loss_cooldown_minutes == 20

    reloaded = DashboardHub(
        "127.0.0.1", 8765, "127.0.0.1", 8766, AppConfig(data_dir=tmp_path)
    )
    assert reloaded.config.btc_dynamic.enabled is True
    assert reloaded.config.btc_dynamic.sizing_mode == "quote"
    assert reloaded.config.btc_dynamic.quantity == 12
    assert reloaded.config.btc_dynamic.quote_amount_usd == 7
    assert reloaded.config.btc_dynamic.loss_streak_limit == 4
    assert reloaded.config.btc_dynamic.loss_cooldown_minutes == 20


def test_btc_dynamic_config_applies_before_next_round_is_created(tmp_path) -> None:
    config = AppConfig(
        data_dir=tmp_path,
        btc_dynamic={"enabled": True, "max_probability_correction_points": 10},
    )
    hub = DashboardHub("127.0.0.1", 8765, "127.0.0.1", 8766, config)
    hub.asset_snapshots["BTC"] = {
        "market": {"asset": "BTC", "condition_id": "btc-old"}
    }
    hub.set_runtime_config(
        {"btc_dynamic": {"max_probability_correction_points": 50}}
    )
    start = datetime(2026, 7, 29, 0, 0, tzinfo=timezone.utc)
    next_market = MarketState(
        asset="BTC",
        condition_id="btc-new",
        slug=f"btc-updown-5m-{int(start.timestamp())}",
        question="Bitcoin Up or Down",
        threshold_price=None,
        start_time=start,
        end_time=start + timedelta(minutes=5),
        up_token_id="up",
        down_token_id="down",
    )

    assert hub.apply_pending_config_before_markets({"BTC": next_market}) is True
    registry = BtcDynamicRegistry(tmp_path / "btc-dynamic-ledger.sqlite3")
    try:
        engine = BtcDynamicEngine(config, registry)
        engine.set_market(next_market, now=start)
        assert engine.current_round is not None
        assert engine.current_round.settings.max_probability_correction_points == 50
    finally:
        registry.close()


def test_btc_v8_config_persists_and_applies_before_next_btc_round(tmp_path) -> None:
    config = AppConfig(data_dir=tmp_path)
    config.pair_match.enabled = True
    config.btc_recovery.enabled = True
    config.btc_dynamic.enabled = True
    config.real_trading.enabled = True
    hub = DashboardHub("127.0.0.1", 8765, "127.0.0.1", 8766, config)
    hub.asset_snapshots["BTC"] = {
        "market": {"asset": "BTC", "condition_id": "btc-old"}
    }
    response = hub.set_runtime_config(
        {
            "btc_v8": {
                "enabled": True,
                "auto_decision_mode": True,
                "orderbook_chase_mode": False,
                "auto_emergency_loss_enabled": False,
                "quote_amount_usd": 6,
                "buy_edge_cents": 6.5,
                "sell_edge_cents": 2.5,
                "spot_exchanges": ["binance", "coinbase"],
                "min_fresh_spot_exchanges": 2,
            }
        }
    )
    assert response["config_status"] == "pending_next_btc_market"
    assert response["btc_v8"]["enabled"] is False
    assert response["pending_btc_v8"]["enabled"] is True
    assert response["pending_btc_v8"]["auto_decision_mode"] is True
    assert response["pending_btc_v8"]["orderbook_chase_mode"] is False
    assert response["pending_btc_v8"]["auto_emergency_loss_enabled"] is False
    assert response["pending_btc_v8"]["quote_amount_usd"] == 6
    assert response["pending_pair_match"]["enabled"] is False
    assert response["pending_btc_recovery"]["enabled"] is False
    assert response["pending_btc_dynamic"]["enabled"] is False
    assert response["pending_real_trading"]["enabled"] is False
    assert hub.apply_pending_config_for_market("btc-old") is False

    start = datetime(2026, 8, 3, 0, 0, tzinfo=timezone.utc)
    next_market = MarketState(
        asset="BTC",
        condition_id="btc-new",
        slug=f"btc-updown-5m-{int(start.timestamp())}",
        question="Bitcoin Up or Down",
        threshold_price=100_000,
        threshold_verified=True,
        start_time=start,
        end_time=start + timedelta(minutes=5),
        up_token_id="up",
        down_token_id="down",
    )
    assert hub.apply_pending_config_before_markets({"BTC": next_market}) is True
    assert config.pair_match.enabled is False
    assert config.btc_recovery.enabled is False
    assert config.btc_dynamic.enabled is False
    assert config.real_trading.enabled is False
    registry = BtcV8Registry(tmp_path / "btc-v8-ledger.sqlite3")
    try:
        engine = BtcV8Engine(config, registry)
        engine.set_market(next_market, start)
        assert engine.current_round is not None
        assert engine.current_round.settings.auto_decision_mode is True
        assert engine.current_round.settings.orderbook_chase_mode is False
        assert engine.current_round.settings.auto_emergency_loss_enabled is False
        assert engine.current_round.settings.quote_amount_usd == 6
        assert engine.current_round.settings.buy_edge_cents == 6.5
        assert engine.current_round.settings.spot_exchanges == ["binance", "coinbase"]
    finally:
        registry.close()

    reloaded = DashboardHub(
        "127.0.0.1", 8765, "127.0.0.1", 8766, AppConfig(data_dir=tmp_path)
    )
    assert reloaded.config.btc_v8.enabled is True
    assert reloaded.config.btc_v8.auto_decision_mode is True
    assert reloaded.config.btc_v8.orderbook_chase_mode is False
    assert reloaded.config.btc_v8.auto_emergency_loss_enabled is False
    assert reloaded.config.btc_v8.quote_amount_usd == 6
    assert reloaded.config.btc_v8.spot_exchanges == ["binance", "coinbase"]
    assert reloaded.config.pair_match.enabled is False
    assert reloaded.config.btc_recovery.enabled is False
    assert reloaded.config.btc_dynamic.enabled is False
    assert reloaded.config.real_trading.enabled is False


def test_btc_v8_disables_legacy_parallel_strategies_on_dashboard_start(tmp_path) -> None:
    config = AppConfig(data_dir=tmp_path)
    config.btc_v8.enabled = True
    config.pair_match.enabled = True
    config.btc_recovery.enabled = True
    config.btc_dynamic.enabled = True
    config.real_trading.enabled = True

    hub = DashboardHub("127.0.0.1", 8765, "127.0.0.1", 8766, config)

    assert hub.config.btc_v8.enabled is True
    assert hub.config.pair_match.enabled is False
    assert hub.config.btc_recovery.enabled is False
    assert hub.config.btc_dynamic.enabled is False
    assert hub.config.real_trading.enabled is False
    saved = json.loads((tmp_path / "dashboard-settings.json").read_text(encoding="utf-8"))
    assert saved["active"]["btc_v8"]["enabled"] is True
    assert saved["active"]["btc_dynamic"]["enabled"] is False


def test_btc_dynamic_model_reset_requires_confirmation(tmp_path) -> None:
    hub = DashboardHub(
        "127.0.0.1", 8765, "127.0.0.1", 8766, AppConfig(data_dir=tmp_path)
    )
    handler = type(
        "TestDynamicDashboardRequestHandler",
        (DashboardRequestHandler,),
        {"hub": hub},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = (
        f"http://127.0.0.1:{server.server_address[1]}/api/btc-dynamic/model/reset"
    )

    def post(confirmation: str) -> int:
        request = urllib.request.Request(
            endpoint,
            data=json.dumps({"confirmation": confirmation}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    try:
        assert post("wrong") == 400
        assert post("RESET_DYNAMIC_MODEL") == 202
        command, requested_at = hub.control_commands.get_nowait()
        assert command == "btc_dynamic_model_reset"
        assert isinstance(requested_at, datetime)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
