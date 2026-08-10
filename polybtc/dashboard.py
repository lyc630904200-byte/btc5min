from __future__ import annotations

import asyncio
import json
import socket
import secrets
import traceback
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Queue
from threading import Thread
from typing import Any, Callable

import orjson
import websockets

from .config import AppConfig
from .real_trading import RealTradingCredentials
from .runner import run_live


DASHBOARD_SEND_TIMEOUT_SECONDS = 1.0
DASHBOARD_SHUTDOWN_TIMEOUT_SECONDS = 5.0
LIVE_RESTART_DELAY_SECONDS = 1.0


class DashboardHub:
    def __init__(self, http_host: str, http_port: int, ws_host: str, ws_port: int, config: AppConfig):
        self.http_host = http_host
        self.http_port = http_port
        self.ws_host = ws_host
        self.ws_port = ws_port
        self.config = config
        self.runtime_settings_path = self.config.data_dir / "dashboard-settings.json"
        self.control_token = secrets.token_urlsafe(32)
        self.pending_config: dict[str, dict[str, Any]] | None = None
        self.pending_config_scope = "aligned"
        self.pending_after_market_id: str | None = None
        self.pending_after_market_ids: set[str] = set()
        self._load_runtime_settings()
        self._enforce_v8_exclusivity()
        self.latest: dict[str, Any] = {
            "type": "snapshot",
            "created_at": None,
            "status": "starting",
            "market": None,
            "tick": None,
            "polymarket_tick": None,
            "polymarket_twap_tick": None,
            "settlement_tick": None,
            "books": {},
            "open_position": None,
            "summary": {},
            "pair_match": {
                "status": "starting",
                "config": self.config.pair_match.model_dump(mode="json"),
                "candidates": {},
                "summary": {},
                "recent_orders": [],
                "recent_markets": [],
            },
            "btc_recovery": {
                "status": "starting",
                "recovery_orders_stopped": False,
                "config": self.config.btc_recovery.model_dump(mode="json"),
                "round": None,
                "positions": {},
                "arbitrage_check": {},
                "summary": {},
                "recent_orders": [],
                "recent_rounds": [],
            },
            "btc_dynamic": {
                "status": "starting",
                "config": self.config.btc_dynamic.model_dump(mode="json"),
                "round": None,
                "diagnostics": {},
                "candidates": {},
                "confirmations": {},
                "loss_cooldown": {
                    "active": False,
                    "consecutive_losses": 0,
                    "cooldown_until": None,
                    "remaining_seconds": 0.0,
                },
                "summary": {},
                "recent_orders": [],
                "recent_rounds": [],
            },
            "btc_weighted": {
                "mode": "SHADOW_ONLY",
                "enabled": self.config.btc_weighted.enabled,
                "status": "starting",
                "last_reason": "starting",
                "config": self.config.btc_weighted.model_dump(mode="json"),
                "round": None,
                "scores": {"UP": None, "DOWN": None},
                "components": {"UP": {}, "DOWN": {}},
                "weights": {},
                "confirmations": {},
                "positions": [],
                "recent_attempts": [],
                "recent_positions": [],
                "score_history": [],
                "summary": {},
            },
            "btc_v8": {
                "enabled": self.config.btc_v8.enabled,
                "status": "starting",
                "last_reason": "starting",
                "config": self.config.btc_v8.model_dump(mode="json"),
                "model": {},
                "round": None,
                "position": None,
                "diagnostics": {},
                "candidates": {},
                "summary": {},
                "recent_trades": [],
            },
            "orderbook_chase": {
                "mode": "SHADOW_ONLY",
                "enabled": self.config.orderbook_chase.enabled,
                "status": "starting",
                "last_reason": "starting",
                "paused": False,
                "emergency_stopped": False,
                "signer": {
                    "ephemeral": True,
                    "connected": False,
                    "error": None,
                    "private_key_persisted": False,
                    "post_order_available": False,
                },
                "config": self.config.orderbook_chase.model_dump(mode="json"),
                "latency": {},
                "strategy": {},
                "signal_source": {
                    "engine": "btc_v8",
                    "shared_instance": True,
                    "duplicate_v8_engine": False,
                    "confirmed_buy_results": True,
                    "lane_local_sell_state": True,
                },
                "positions": [],
                "recent_attempts": [],
                "summary": {},
            },
            "real_trading": {
                "status": "starting",
                "armed": False,
                "credentials_loaded": False,
                "config": self.config.real_trading.model_dump(mode="json"),
                "account": {},
                "shadow_gate": {},
                "summary": {},
                "recent_orders": [],
            },
            "events": [],
            "assets": {},
            "strategy": self.config_json()["strategy"],
            "risk": self.config_json()["risk"],
            **self.config_status_json(),
            "ws_url": self.ws_url,
            "control_token": self.control_token,
        }
        self.pair_match_state = self.latest["pair_match"]
        self.btc_recovery_state = self.latest["btc_recovery"]
        self.btc_dynamic_state = self.latest["btc_dynamic"]
        self.btc_weighted_state = self.latest["btc_weighted"]
        self.btc_v8_state = self.latest["btc_v8"]
        self.orderbook_chase_state = self.latest["orderbook_chase"]
        self.real_trading_state = self.latest["real_trading"]
        self.events: list[dict[str, Any]] = []
        self.events_by_asset: dict[str, list[dict[str, Any]]] = {
            asset: [] for asset in self.config.sources.enabled_assets
        }
        self.asset_snapshots: dict[str, dict[str, Any]] = {}
        self.clients: set[Any] = set()
        self.lock = asyncio.Lock()

    def _enforce_v8_exclusivity(self) -> None:
        changed = False
        if self.config.btc_v8.enabled:
            for settings in (
                self.config.pair_match,
                self.config.btc_recovery,
                self.config.btc_dynamic,
                self.config.real_trading,
            ):
                if settings.enabled:
                    settings.enabled = False
                    changed = True
        pending_v8 = (self.pending_config or {}).get("btc_v8") or {}
        if pending_v8.get("enabled"):
            for key in (
                "pair_match",
                "btc_recovery",
                "btc_dynamic",
                "real_trading",
            ):
                settings = (self.pending_config or {}).get(key)
                if isinstance(settings, dict) and settings.get("enabled"):
                    settings["enabled"] = False
                    changed = True
        if changed:
            self._save_runtime_settings()
        self.last_push_at = datetime.min.replace(tzinfo=timezone.utc)
        self.push_interval = timedelta(milliseconds=250)
        self.control_commands: Queue[tuple[str, Any]] = Queue()

    @property
    def ws_url(self) -> str:
        return f"ws://{self.http_host}:{self.ws_port}/ws"

    def compact_event(self, event: dict[str, Any]) -> dict[str, Any]:
        event_type = event.get("type")
        payload = event.get("payload") or {}
        if event_type == "fill" and isinstance(payload, dict):
            return {
                "type": "fill",
                "payload": {
                    "side": payload.get("side"),
                    "direction": payload.get("direction"),
                    "avg_price": payload.get("avg_price"),
                    "quantity": payload.get("quantity"),
                    "quote": payload.get("quote"),
                    "fee_usd": payload.get("fee_usd"),
                    "reason": payload.get("reason"),
                    "created_at": payload.get("created_at"),
                },
            }
        if event_type == "book" and isinstance(payload, dict):
            bids = payload.get("bids") or []
            asks = payload.get("asks") or []
            best_bid = max((float(level.get("price")) for level in bids), default=None)
            best_ask = min((float(level.get("price")) for level in asks), default=None)
            return {
                "type": "book",
                "payload": {
                    "direction": payload.get("direction"),
                    "token_id": payload.get("token_id"),
                    "timestamp": payload.get("timestamp"),
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                },
            }
        if event_type == "tick" and isinstance(payload, dict):
            return {
                "type": "tick",
                "payload": {
                    "price": payload.get("price"),
                    "received_at": payload.get("received_at"),
                    "exchange_timestamp": payload.get("exchange_timestamp"),
                },
            }
        if event_type == "polymarket_tick" and isinstance(payload, dict):
            return {
                "type": "polymarket_tick",
                "payload": {
                    "price": payload.get("price"),
                    "received_at": payload.get("received_at"),
                    "exchange_timestamp": payload.get("exchange_timestamp"),
                    "source": payload.get("source"),
                    "symbol": payload.get("symbol"),
                },
            }
        if event_type == "polymarket_twap_tick" and isinstance(payload, dict):
            return {
                "type": "polymarket_twap_tick",
                "payload": {
                    "price": payload.get("price"),
                    "received_at": payload.get("received_at"),
                    "exchange_timestamp": payload.get("exchange_timestamp"),
                    "source": payload.get("source"),
                    "symbol": payload.get("symbol"),
                },
            }
        if event_type == "market" and isinstance(payload, dict):
            return {
                "type": "market",
                "payload": {
                    "slug": payload.get("slug"),
                    "threshold_price": payload.get("threshold_price"),
                    "threshold_source": payload.get("threshold_source"),
                    "start_time": payload.get("start_time"),
                    "end_time": payload.get("end_time"),
                },
            }
        if event_type in {
            "pair_state",
            "pair_order",
            "pair_settlement",
            "btc_recovery_fill",
            "btc_recovery_round",
            "btc_recovery_result",
            "btc_recovery_control",
            "btc_dynamic_order",
            "btc_dynamic_round",
            "btc_dynamic_settlement",
            "btc_dynamic_statistics_reset",
            "btc_dynamic_model_reset",
            "btc_dynamic_model_reset_pending",
            "btc_weighted_attempt",
            "btc_weighted_position",
            "btc_weighted_round",
            "btc_weighted_settlement",
            "btc_v8_trade",
            "btc_v8_round",
            "btc_v8_settlement",
            "orderbook_chase_attempt",
            "orderbook_chase_position",
        }:
            return {"type": event_type, "payload": {}}
        return event

    def compact_market(self, market: dict[str, Any] | None) -> dict[str, Any] | None:
        if not market:
            return None
        keys = [
            "asset",
            "condition_id",
            "slug",
            "question",
            "threshold_price",
            "threshold_source",
            "threshold_observed_at",
            "threshold_verified",
            "threshold_fetched_at",
            "threshold_candidate_price",
            "threshold_candidate_source",
            "threshold_candidate_observed_at",
            "threshold_candidate_received_at",
            "threshold_candidate_conflicted",
            "start_time",
            "end_time",
            "up_token_id",
            "down_token_id",
            "min_order_size",
            "tick_size",
            "accepting_orders",
            "settlement_verified",
            "observe_only",
        ]
        return {key: market.get(key) for key in keys if key in market}

    def compact_book(self, book: dict[str, Any] | None) -> dict[str, Any] | None:
        if not book:
            return None
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        best_bid = max((float(level.get("price")) for level in bids), default=None)
        best_ask = min((float(level.get("price")) for level in asks), default=None)
        return {
            "token_id": book.get("token_id"),
            "market_id": book.get("market_id"),
            "timestamp": book.get("timestamp"),
            "received_at": book.get("received_at"),
            "best_bid": best_bid,
            "best_ask": best_ask,
            "depth_trusted": bool(book.get("depth_trusted")),
            "min_order_size": book.get("min_order_size"),
            "tick_size": book.get("tick_size"),
        }

    def compact_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        payload = dict(snapshot)
        payload["market"] = self.compact_market(payload.get("market"))
        payload["books"] = {direction: self.compact_book(book) for direction, book in (payload.get("books") or {}).items()}
        payload["strategy"] = {**self.config_json()["strategy"], **(payload.get("strategy") or {})}
        payload["risk"] = {**self.config_json()["risk"], **(payload.get("risk") or {})}
        return payload

    def config_json(self) -> dict[str, Any]:
        strategy = self.config.strategy
        risk = self.config.risk
        return {
            "strategy": {
                "min_entry_edge_usd": strategy.min_entry_edge_usd,
                "stop_edge_usd": strategy.stop_edge_usd,
                "min_buy_price": strategy.min_buy_price,
                "max_buy_price": strategy.max_buy_price,
                "take_profit_ticks": strategy.take_profit_ticks,
                "min_seconds_to_entry": strategy.min_seconds_to_entry,
                "max_seconds_to_entry": strategy.max_seconds_to_entry,
                "reverse_entry_enabled": strategy.reverse_entry_enabled,
                "entry_confirmation_enabled": strategy.entry_confirmation_enabled,
                "entry_confirmation_seconds": strategy.entry_confirmation_seconds,
                "entry_confirmation_updates": strategy.entry_confirmation_updates,
                "taker_fee_rate": strategy.taker_fee_rate,
            },
            "risk": {
                "max_order_usd": risk.max_order_usd,
                "max_loss_usd": risk.max_loss_usd,
                "max_trades_per_market": risk.max_trades_per_market,
            },
            "pair_match": self.config.pair_match.model_dump(mode="json"),
            "btc_recovery": self.config.btc_recovery.model_dump(mode="json"),
            "btc_dynamic": self.config.btc_dynamic.model_dump(mode="json"),
            "btc_weighted": self.config.btc_weighted.model_dump(mode="json"),
            "btc_v8": self.config.btc_v8.model_dump(mode="json"),
            "orderbook_chase": self.config.orderbook_chase.model_dump(mode="json"),
            "real_trading": self.config.real_trading.model_dump(mode="json"),
        }

    def config_status_json(self) -> dict[str, Any]:
        pending = self.pending_config or {}
        return {
            "config_status": (
                "pending_next_btc_market"
                if self.pending_config and self.pending_config_scope == "btc"
                else "pending_next_market"
                if self.pending_config
                else "active"
            ),
            "pending_strategy": pending.get("strategy"),
            "pending_risk": pending.get("risk"),
            "pending_pair_match": pending.get("pair_match"),
            "pending_btc_recovery": pending.get("btc_recovery"),
            "pending_btc_dynamic": pending.get("btc_dynamic"),
            "pending_btc_weighted": pending.get("btc_weighted"),
            "pending_btc_v8": pending.get("btc_v8"),
            "pending_orderbook_chase": pending.get("orderbook_chase"),
            "pending_real_trading": pending.get("real_trading"),
        }

    def _load_runtime_settings(self) -> None:
        try:
            payload = json.loads(self.runtime_settings_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return

        active = payload.get("active")
        if isinstance(active, dict):
            try:
                strategy_payload = self.config.strategy.model_dump()
                strategy_payload.update(active.get("strategy") or {})
                risk_payload = self.config.risk.model_dump()
                risk_payload.update(active.get("risk") or {})
                pair_payload = self.config.pair_match.model_dump()
                pair_payload.update(active.get("pair_match") or {})
                recovery_payload = self.config.btc_recovery.model_dump()
                recovery_payload.update(active.get("btc_recovery") or {})
                dynamic_payload = self.config.btc_dynamic.model_dump()
                dynamic_payload.update(active.get("btc_dynamic") or {})
                weighted_payload = self.config.btc_weighted.model_dump()
                weighted_payload.update(active.get("btc_weighted") or {})
                v8_payload = self.config.btc_v8.model_dump()
                v8_payload.update(active.get("btc_v8") or {})
                chase_payload = self.config.orderbook_chase.model_dump()
                chase_payload.update(active.get("orderbook_chase") or {})
                real_payload = self.config.real_trading.model_dump()
                real_payload.update(active.get("real_trading") or {})
                strategy = type(self.config.strategy).model_validate(strategy_payload)
                risk = type(self.config.risk).model_validate(risk_payload)
                pair_match = type(self.config.pair_match).model_validate(pair_payload)
                btc_recovery = type(self.config.btc_recovery).model_validate(recovery_payload)
                btc_dynamic = type(self.config.btc_dynamic).model_validate(dynamic_payload)
                btc_weighted = type(self.config.btc_weighted).model_validate(weighted_payload)
                btc_v8 = type(self.config.btc_v8).model_validate(v8_payload)
                orderbook_chase = type(self.config.orderbook_chase).model_validate(
                    chase_payload
                )
                real_trading = type(self.config.real_trading).model_validate(real_payload)
            except (TypeError, ValueError):
                pass
            else:
                self.config.strategy = strategy
                self.config.risk = risk
                self.config.pair_match = pair_match
                self.config.btc_recovery = btc_recovery
                self.config.btc_dynamic = btc_dynamic
                self.config.btc_weighted = btc_weighted
                self.config.btc_v8 = btc_v8
                self.config.orderbook_chase = orderbook_chase
                self.config.real_trading = real_trading

        pending = payload.get("pending")
        if isinstance(pending, dict):
            try:
                strategy_payload = self.config.strategy.model_dump()
                strategy_payload.update(pending.get("strategy") or {})
                risk_payload = self.config.risk.model_dump()
                risk_payload.update(pending.get("risk") or {})
                pair_payload = self.config.pair_match.model_dump()
                pair_payload.update(pending.get("pair_match") or {})
                recovery_payload = self.config.btc_recovery.model_dump()
                recovery_payload.update(pending.get("btc_recovery") or {})
                dynamic_payload = self.config.btc_dynamic.model_dump()
                dynamic_payload.update(pending.get("btc_dynamic") or {})
                weighted_payload = self.config.btc_weighted.model_dump()
                weighted_payload.update(pending.get("btc_weighted") or {})
                v8_payload = self.config.btc_v8.model_dump()
                v8_payload.update(pending.get("btc_v8") or {})
                chase_payload = self.config.orderbook_chase.model_dump()
                chase_payload.update(pending.get("orderbook_chase") or {})
                real_payload = self.config.real_trading.model_dump()
                real_payload.update(pending.get("real_trading") or {})
                strategy = type(self.config.strategy).model_validate(strategy_payload)
                risk = type(self.config.risk).model_validate(risk_payload)
                pair_match = type(self.config.pair_match).model_validate(pair_payload)
                btc_recovery = type(self.config.btc_recovery).model_validate(recovery_payload)
                btc_dynamic = type(self.config.btc_dynamic).model_validate(dynamic_payload)
                btc_weighted = type(self.config.btc_weighted).model_validate(weighted_payload)
                btc_v8 = type(self.config.btc_v8).model_validate(v8_payload)
                orderbook_chase = type(self.config.orderbook_chase).model_validate(
                    chase_payload
                )
                real_trading = type(self.config.real_trading).model_validate(real_payload)
            except (TypeError, ValueError):
                return
            self.pending_config = {
                "strategy": strategy.model_dump(),
                "risk": risk.model_dump(),
                "pair_match": pair_match.model_dump(),
                "btc_recovery": btc_recovery.model_dump(),
                "btc_dynamic": btc_dynamic.model_dump(),
                "btc_weighted": btc_weighted.model_dump(),
                "btc_v8": btc_v8.model_dump(),
                "orderbook_chase": orderbook_chase.model_dump(),
                "real_trading": real_trading.model_dump(),
            }
            scope = payload.get("pending_scope")
            self.pending_config_scope = "btc" if scope == "btc" else "aligned"
            after_market = payload.get("apply_after_market_id")
            self.pending_after_market_id = str(after_market) if after_market else None
            after_markets = payload.get("apply_after_market_ids")
            if isinstance(after_markets, list):
                self.pending_after_market_ids = {str(value) for value in after_markets if value}
            elif self.pending_after_market_id:
                self.pending_after_market_ids = {self.pending_after_market_id}

    def _save_runtime_settings(self) -> None:
        payload = {
            "active": {
                "strategy": self.config.strategy.model_dump(),
                "risk": self.config.risk.model_dump(),
                "pair_match": self.config.pair_match.model_dump(),
                "btc_recovery": self.config.btc_recovery.model_dump(),
                "btc_dynamic": self.config.btc_dynamic.model_dump(),
                "btc_weighted": self.config.btc_weighted.model_dump(),
                "btc_v8": self.config.btc_v8.model_dump(),
                "orderbook_chase": self.config.orderbook_chase.model_dump(),
                "real_trading": self.config.real_trading.model_dump(),
            },
            "pending": self.pending_config,
            "pending_scope": self.pending_config_scope if self.pending_config else None,
            "apply_after_market_id": self.pending_after_market_id,
            "apply_after_market_ids": sorted(self.pending_after_market_ids),
        }
        self.runtime_settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.runtime_settings_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def current_market_id(self) -> str | None:
        market = self.latest.get("market") or {}
        if not isinstance(market, dict):
            return None
        market_id = market.get("condition_id")
        return str(market_id) if market_id else None

    def current_market_ids(self) -> set[str]:
        market_ids = {
            str((snapshot.get("market") or {}).get("condition_id"))
            for snapshot in self.asset_snapshots.values()
            if (snapshot.get("market") or {}).get("condition_id")
        }
        current = self.current_market_id()
        if current:
            market_ids.add(current)
        return market_ids

    def current_btc_market_id(self) -> str | None:
        market = (self.asset_snapshots.get("BTC") or {}).get("market") or {}
        market_id = market.get("condition_id") if isinstance(market, dict) else None
        if market_id:
            return str(market_id)
        latest_market = self.latest.get("market") or {}
        if (
            isinstance(latest_market, dict)
            and str(latest_market.get("asset") or "").upper() == "BTC"
            and latest_market.get("condition_id")
        ):
            return str(latest_market["condition_id"])
        return None

    def apply_pending_config_for_market(self, market_id: str | None) -> bool:
        if not self.pending_config or not market_id or market_id in self.pending_after_market_ids:
            return False
        self.config.strategy = type(self.config.strategy).model_validate(self.pending_config["strategy"])
        self.config.risk = type(self.config.risk).model_validate(self.pending_config["risk"])
        self.config.pair_match = type(self.config.pair_match).model_validate(self.pending_config["pair_match"])
        self.config.btc_recovery = type(self.config.btc_recovery).model_validate(
            self.pending_config["btc_recovery"]
        )
        self.config.btc_dynamic = type(self.config.btc_dynamic).model_validate(
            self.pending_config["btc_dynamic"]
        )
        self.config.btc_weighted = type(self.config.btc_weighted).model_validate(
            self.pending_config["btc_weighted"]
        )
        self.config.btc_v8 = type(self.config.btc_v8).model_validate(
            self.pending_config["btc_v8"]
        )
        self.config.orderbook_chase = type(self.config.orderbook_chase).model_validate(
            self.pending_config["orderbook_chase"]
        )
        self.config.real_trading = type(self.config.real_trading).model_validate(
            self.pending_config["real_trading"]
        )
        self.pending_config = None
        self.pending_config_scope = "aligned"
        self.pending_after_market_id = None
        self.pending_after_market_ids = set()
        self._save_runtime_settings()
        return True

    def set_runtime_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        strategy_update = payload.get("strategy")
        risk_update = payload.get("risk")
        pair_update = payload.get("pair_match")
        recovery_update = payload.get("btc_recovery")
        dynamic_update = payload.get("btc_dynamic")
        weighted_update = payload.get("btc_weighted")
        v8_update = payload.get("btc_v8")
        chase_update = payload.get("orderbook_chase")
        real_update = payload.get("real_trading")
        if strategy_update is not None and not isinstance(strategy_update, dict):
            raise ValueError("strategy must be an object")
        if risk_update is not None and not isinstance(risk_update, dict):
            raise ValueError("risk must be an object")
        if pair_update is not None and not isinstance(pair_update, dict):
            raise ValueError("pair_match must be an object")
        if recovery_update is not None and not isinstance(recovery_update, dict):
            raise ValueError("btc_recovery must be an object")
        if dynamic_update is not None and not isinstance(dynamic_update, dict):
            raise ValueError("btc_dynamic must be an object")
        if weighted_update is not None and not isinstance(weighted_update, dict):
            raise ValueError("btc_weighted must be an object")
        if v8_update is not None and not isinstance(v8_update, dict):
            raise ValueError("btc_v8 must be an object")
        if chase_update is not None and not isinstance(chase_update, dict):
            raise ValueError("orderbook_chase must be an object")
        if real_update is not None and not isinstance(real_update, dict):
            raise ValueError("real_trading must be an object")
        if not any(
            (
                strategy_update,
                risk_update,
                pair_update,
                recovery_update,
                dynamic_update,
                weighted_update,
                v8_update,
                chase_update,
                real_update,
            )
        ):
            raise ValueError(
                "strategy, risk, pair_match, btc_recovery, btc_dynamic, btc_weighted, btc_v8, orderbook_chase, or real_trading settings are required"
            )

        strategy_fields = {
            "min_entry_edge_usd",
            "stop_edge_usd",
            "min_buy_price",
            "max_buy_price",
            "take_profit_ticks",
            "min_seconds_to_entry",
            "max_seconds_to_entry",
            "reverse_entry_enabled",
            "entry_confirmation_enabled",
            "entry_confirmation_seconds",
            "entry_confirmation_updates",
            "taker_fee_rate",
        }
        risk_fields = {"max_order_usd", "max_loss_usd", "max_trades_per_market"}
        pair_fields = {
            "enabled",
            "leg_quote_usd",
            "min_spread_cents",
            "second_order_min_spread_cents",
            "min_leg_price_gap_cents",
            "start_seconds_after_open",
            "end_seconds_after_open",
            "max_pairs_per_market",
            "alternate_directions",
            "alternation_mode",
        }
        recovery_fields = {
            "enabled",
            "entry_price_cents",
            "max_entry_price_cents",
            "target_price_cents",
            "recovery_target_price_cents",
            "recovery_trigger_cents",
            "stop_price_cents",
            "initial_quantity",
            "recovery_quantity",
            "entry_seconds_after_open",
            "exit_seconds_after_open",
        }
        dynamic_fields = {
            "enabled",
            "sizing_mode",
            "quantity",
            "quote_amount_usd",
            "entry_seconds_after_open",
            "exit_seconds_after_open",
            "min_net_edge_cents",
            "slippage_reserve_cents",
            "confirmation_seconds",
            "confirmation_updates",
            "loss_streak_limit",
            "loss_cooldown_minutes",
            "short_volatility_window_seconds",
            "long_volatility_window_seconds",
            "volatility_floor_bps",
            "max_probability_correction_points",
        }
        weighted_fields = set(type(self.config.btc_weighted).model_fields)
        v8_fields = set(type(self.config.btc_v8).model_fields)
        chase_fields = set(type(self.config.orderbook_chase).model_fields)
        real_fields = {
            "enabled",
            "order_quantity",
            "max_order_notional_usd",
            "daily_loss_limit_usd",
            "max_orders_per_day",
            "auto_redeem",
            "shadow_required_signals",
        }
        unexpected_strategy = set(strategy_update or {}) - strategy_fields
        unexpected_risk = set(risk_update or {}) - risk_fields
        unexpected_pair = set(pair_update or {}) - pair_fields
        unexpected_recovery = set(recovery_update or {}) - recovery_fields
        unexpected_dynamic = set(dynamic_update or {}) - dynamic_fields
        unexpected_weighted = set(weighted_update or {}) - weighted_fields
        unexpected_v8 = set(v8_update or {}) - v8_fields
        unexpected_chase = set(chase_update or {}) - chase_fields
        unexpected_real = set(real_update or {}) - real_fields
        if any(
            (
                unexpected_strategy,
                unexpected_risk,
                unexpected_pair,
                unexpected_recovery,
                unexpected_dynamic,
                unexpected_weighted,
                unexpected_v8,
                unexpected_chase,
                unexpected_real,
            )
        ):
            names = sorted(
                unexpected_strategy
                | unexpected_risk
                | unexpected_pair
                | unexpected_recovery
                | unexpected_dynamic
                | unexpected_weighted
                | unexpected_v8
                | unexpected_chase
                | unexpected_real
            )
            raise ValueError(f"unsupported runtime settings: {', '.join(names)}")

        pending = self.pending_config or {}
        strategy_payload = dict(pending.get("strategy") or self.config.strategy.model_dump())
        strategy_payload.update(strategy_update or {})
        risk_payload = dict(pending.get("risk") or self.config.risk.model_dump())
        risk_payload.update(risk_update or {})
        pair_payload = dict(pending.get("pair_match") or self.config.pair_match.model_dump())
        pair_payload.update(pair_update or {})
        recovery_payload = dict(
            pending.get("btc_recovery") or self.config.btc_recovery.model_dump()
        )
        recovery_payload.update(recovery_update or {})
        dynamic_payload = dict(
            pending.get("btc_dynamic") or self.config.btc_dynamic.model_dump()
        )
        dynamic_payload.update(dynamic_update or {})
        weighted_payload = dict(
            pending.get("btc_weighted") or self.config.btc_weighted.model_dump()
        )
        weighted_payload.update(weighted_update or {})
        v8_payload = dict(
            pending.get("btc_v8") or self.config.btc_v8.model_dump()
        )
        v8_payload.update(v8_update or {})
        chase_payload = dict(
            pending.get("orderbook_chase") or self.config.orderbook_chase.model_dump()
        )
        chase_payload.update(chase_update or {})
        real_payload = dict(
            pending.get("real_trading") or self.config.real_trading.model_dump()
        )
        real_payload.update(real_update or {})
        if v8_update and bool(v8_update.get("enabled")):
            pair_payload["enabled"] = False
            recovery_payload["enabled"] = False
            dynamic_payload["enabled"] = False
            real_payload["enabled"] = False
        if pair_payload.get("alternation_mode") == "per_market_two_stage":
            pair_payload["alternate_directions"] = True
            pair_payload["max_pairs_per_market"] = 2
        elif (
            pair_update
            and pair_update.get("alternation_mode") in {"per_market_ab", "per_market_ba"}
            and "max_pairs_per_market" not in pair_update
        ):
            pair_payload["max_pairs_per_market"] = 2
        strategy = type(self.config.strategy).model_validate(strategy_payload)
        risk = type(self.config.risk).model_validate(risk_payload)
        pair_match = type(self.config.pair_match).model_validate(pair_payload)
        btc_recovery = type(self.config.btc_recovery).model_validate(recovery_payload)
        btc_dynamic = type(self.config.btc_dynamic).model_validate(dynamic_payload)
        btc_weighted = type(self.config.btc_weighted).model_validate(weighted_payload)
        btc_v8 = type(self.config.btc_v8).model_validate(v8_payload)
        orderbook_chase = type(self.config.orderbook_chase).model_validate(chase_payload)
        real_trading = type(self.config.real_trading).model_validate(real_payload)
        self.pending_config = {
            "strategy": strategy.model_dump(),
            "risk": risk.model_dump(),
            "pair_match": pair_match.model_dump(),
            "btc_recovery": btc_recovery.model_dump(),
            "btc_dynamic": btc_dynamic.model_dump(),
            "btc_weighted": btc_weighted.model_dump(),
            "btc_v8": btc_v8.model_dump(),
            "orderbook_chase": orderbook_chase.model_dump(),
            "real_trading": real_trading.model_dump(),
        }
        btc_only = bool(
            recovery_update or dynamic_update or weighted_update or v8_update or chase_update or real_update
        ) and not any(
            (strategy_update, risk_update, pair_update)
        )
        self.pending_config_scope = "btc" if btc_only else "aligned"
        if self.pending_config_scope == "btc":
            current_btc = self.current_btc_market_id()
            self.pending_after_market_ids = {current_btc} if current_btc else set()
        else:
            self.pending_after_market_ids = self.current_market_ids()
        self.pending_after_market_id = next(iter(self.pending_after_market_ids), None)
        self._save_runtime_settings()
        return {**self.config_json(), **self.config_status_json()}

    def pending_config_ready_for_snapshot(self, snapshot: dict[str, Any], asset: str) -> bool:
        market = snapshot.get("market") or {}
        if not isinstance(market, dict):
            return False
        return self.pending_config_ready_for_markets({asset: market})

    def pending_config_ready_for_markets(
        self, market_updates: dict[str, dict[str, Any]]
    ) -> bool:
        if not self.pending_config:
            return False
        if self.pending_config_scope == "btc":
            market = market_updates.get("BTC")
            if not isinstance(market, dict) or not market.get("condition_id"):
                return False
            return str(market["condition_id"]) not in self.pending_after_market_ids
        prospective = dict(self.asset_snapshots)
        for asset, market in market_updates.items():
            prospective[asset] = {"market": market}
        markets: list[dict[str, Any]] = []
        for required_asset in self.config.sources.enabled_assets:
            market = prospective.get(required_asset, {}).get("market") or {}
            if not isinstance(market, dict) or not market.get("condition_id"):
                return False
            markets.append(market)
        market_ids = {str(market["condition_id"]) for market in markets}
        if market_ids & self.pending_after_market_ids:
            return False
        starts = {market.get("start_time") for market in markets}
        ends = {market.get("end_time") for market in markets}
        return len(starts) == 1 and len(ends) == 1

    def apply_pending_config_before_markets(
        self, market_updates: dict[str, Any]
    ) -> bool:
        serialized: dict[str, dict[str, Any]] = {}
        for asset, market in market_updates.items():
            if isinstance(market, dict):
                payload = market
            elif hasattr(market, "model_dump"):
                payload = market.model_dump(mode="json")
            else:
                continue
            serialized[str(asset).upper()] = payload
        if not self.pending_config_ready_for_markets(serialized):
            return False
        market_ids = [
            str(market["condition_id"])
            for market in serialized.values()
            if market.get("condition_id")
        ]
        return self.apply_pending_config_for_market(market_ids[0] if market_ids else None)

    async def publish(self, snapshot: dict[str, Any]) -> None:
        message: str
        event = snapshot.get("event")
        market = snapshot.get("market") or {}
        asset = str(snapshot.get("asset") or (snapshot.get("market") or {}).get("asset") or "BTC").upper()
        if (
            isinstance(event, dict)
            and event.get("type") == "market"
            and isinstance(market, dict)
            and self.pending_config_ready_for_snapshot(snapshot, asset)
        ):
            market_id = market.get("condition_id")
            self.apply_pending_config_for_market(str(market_id) if market_id else None)
        snapshot = self.compact_snapshot(snapshot)
        snapshot["asset"] = asset
        async with self.lock:
            pair_match = snapshot.pop("pair_match", None)
            if isinstance(pair_match, dict) and pair_match:
                self.pair_match_state = pair_match
            btc_recovery = snapshot.pop("btc_recovery", None)
            if isinstance(btc_recovery, dict) and btc_recovery:
                self.btc_recovery_state = btc_recovery
            btc_dynamic = snapshot.pop("btc_dynamic", None)
            if isinstance(btc_dynamic, dict) and btc_dynamic:
                self.btc_dynamic_state = btc_dynamic
            btc_weighted = snapshot.pop("btc_weighted", None)
            if isinstance(btc_weighted, dict) and btc_weighted:
                self.btc_weighted_state = btc_weighted
            btc_v8 = snapshot.pop("btc_v8", None)
            if isinstance(btc_v8, dict) and btc_v8:
                self.btc_v8_state = btc_v8
            orderbook_chase = snapshot.pop("orderbook_chase", None)
            if isinstance(orderbook_chase, dict) and orderbook_chase:
                self.orderbook_chase_state = orderbook_chase
            real_trading = snapshot.pop("real_trading", None)
            if isinstance(real_trading, dict) and real_trading:
                self.real_trading_state = real_trading
            event = snapshot.get("event")
            compacted_event = self.compact_event(event) if event else None
            if compacted_event and compacted_event.get("type") == "fill":
                asset_events = self.events_by_asset.setdefault(asset, [])
                asset_events.append(compacted_event)
                self.events_by_asset[asset] = asset_events[-250:]
                self.events.append(compacted_event)
                self.events = self.events[-500:]
            snapshot["event"] = compacted_event
            snapshot["status"] = "running"
            snapshot.update(self.config_status_json())
            snapshot["events"] = list(self.events_by_asset.get(asset, []))
            snapshot["ws_url"] = self.ws_url
            self.asset_snapshots[asset] = snapshot
            primary_asset = self.config.sources.enabled_assets[0]
            primary = self.asset_snapshots.get(primary_asset, snapshot)
            combined = dict(primary)
            combined["assets"] = dict(self.asset_snapshots)
            combined["pair_match"] = self.pair_match_state
            combined["btc_recovery"] = self.btc_recovery_state
            combined["btc_dynamic"] = self.btc_dynamic_state
            combined["btc_weighted"] = self.btc_weighted_state
            combined["btc_v8"] = self.btc_v8_state
            combined["orderbook_chase"] = self.orderbook_chase_state
            combined["real_trading"] = self.real_trading_state
            combined["ws_url"] = self.ws_url
            combined["control_token"] = self.control_token
            combined.update(self.config_status_json())
            self.latest = combined
            now = datetime.now(timezone.utc)
            event_type = event.get("type") if isinstance(event, dict) else None
            minimum_interval = (
                self.push_interval
                if event_type in {"tick", "polymarket_tick", "polymarket_twap_tick", "book", "pair_state"}
                else None
            )
            should_push = minimum_interval is None or now - self.last_push_at >= minimum_interval
            if not should_push:
                return
            self.last_push_at = now
            message = orjson.dumps(combined).decode("utf-8")
            clients = set(self.clients)
        if clients:
            results = await asyncio.gather(
                *(
                    asyncio.wait_for(client.send(message), timeout=DASHBOARD_SEND_TIMEOUT_SECONDS)
                    for client in clients
                ),
                return_exceptions=True,
            )
            for client, result in zip(clients, results):
                if isinstance(result, BaseException):
                    self.clients.discard(client)

    async def ws_handler(self, websocket: Any) -> None:
        self.clients.add(websocket)
        try:
            async with self.lock:
                latest = orjson.dumps(self.latest).decode("utf-8")
            await websocket.send(latest)
            async for _ in websocket:
                pass
        finally:
            self.clients.discard(websocket)

    def state_json(self) -> bytes:
        payload = dict(self.latest)
        payload["events"] = list(payload.get("events") or [])
        payload["ws_url"] = self.ws_url
        payload["control_token"] = self.control_token
        payload["strategy"] = {**self.config_json()["strategy"], **(payload.get("strategy") or {})}
        payload["risk"] = {**self.config_json()["risk"], **(payload.get("risk") or {})}
        payload.update(self.config_status_json())
        return orjson.dumps(payload)

    def request_recovery_orders_stopped(self, stopped: bool) -> dict[str, Any]:
        self.control_commands.put(("btc_recovery_orders_stopped", stopped))
        return {
            "accepted": True,
            "recovery_orders_stopped": stopped,
        }

    def request_btc_recovery_statistics_reset(self) -> dict[str, Any]:
        reset_at = datetime.now(timezone.utc)
        self.control_commands.put(("btc_recovery_statistics_reset", reset_at))
        return {
            "accepted": True,
            "statistics_reset_at": reset_at.isoformat(),
        }

    def request_btc_dynamic_statistics_reset(self) -> dict[str, Any]:
        reset_at = datetime.now(timezone.utc)
        self.control_commands.put(("btc_dynamic_statistics_reset", reset_at))
        return {
            "accepted": True,
            "statistics_reset_at": reset_at.isoformat(),
        }

    def request_btc_dynamic_model_reset(self) -> dict[str, Any]:
        requested_at = datetime.now(timezone.utc)
        self.control_commands.put(("btc_dynamic_model_reset", requested_at))
        return {
            "accepted": True,
            "model_reset_pending": True,
            "requested_at": requested_at.isoformat(),
        }

    def request_real_trading_control(self, command: str) -> dict[str, Any]:
        self.control_commands.put((f"real_trading_{command}", True))
        return {"accepted": True, "command": command}

    def request_orderbook_chase_control(self, command: str) -> dict[str, Any]:
        self.control_commands.put((f"orderbook_chase_{command}", True))
        return {"accepted": True, "command": command, "mode": "SHADOW_ONLY"}


class DashboardRequestHandler(SimpleHTTPRequestHandler):
    hub: DashboardHub

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = orjson.dumps(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        super().end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/api/state"):
            body = self.hub.state_json()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/api/config"):
            self.send_json(
                200,
                {**self.hub.config_json(), **self.hub.config_status_json()},
            )
            return
        if self.path == "/" or self.path.startswith("/dashboard"):
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if self.path.startswith("/api/orderbook-chase/"):
            if self.headers.get("X-Polybtc-Control-Token") != self.hub.control_token:
                self.send_json(403, {"error": "invalid control token"})
                return
            action = self.path.removeprefix("/api/orderbook-chase/")
            allowed = {"pause", "resume", "emergency-stop"}
            if action not in allowed:
                self.send_error(404)
                return
            self.send_json(
                202,
                self.hub.request_orderbook_chase_control(action.replace("-", "_")),
            )
            return
        if self.path.startswith("/api/real-trading/"):
            if self.headers.get("X-Polybtc-Control-Token") != self.hub.control_token:
                self.send_json(403, {"error": "invalid control token"})
                return
            action = self.path.removeprefix("/api/real-trading/")
            allowed = {
                "connect",
                "prepare-allowance",
                "arm",
                "disarm",
                "emergency-stop",
                "resume",
                "shadow-reset",
            }
            if action not in allowed:
                self.send_error(404)
                return
            confirmations = {
                "connect": "CONNECT_REAL_ACCOUNT",
                "prepare-allowance": "PREPARE_TRADING_ALLOWANCE",
                "arm": "ENABLE_REAL_TRADING",
            }
            required_confirmation = confirmations.get(action)
            if required_confirmation is not None:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(
                        self.rfile.read(length).decode("utf-8") or "{}"
                    )
                except (ValueError, json.JSONDecodeError):
                    self.send_json(400, {"error": "invalid confirmation payload"})
                    return
                if payload.get("confirmation") != required_confirmation:
                    self.send_json(400, {"error": "explicit confirmation required"})
                    return
            self.send_json(
                202,
                self.hub.request_real_trading_control(action.replace("-", "_")),
            )
            return
        if self.path == "/api/btc-recovery/recovery-orders/stop":
            self.send_json(202, self.hub.request_recovery_orders_stopped(True))
            return
        if self.path == "/api/btc-recovery/recovery-orders/resume":
            self.send_json(202, self.hub.request_recovery_orders_stopped(False))
            return
        if self.path == "/api/btc-recovery/statistics/reset":
            self.send_json(202, self.hub.request_btc_recovery_statistics_reset())
            return
        if self.path == "/api/btc-dynamic/statistics/reset":
            self.send_json(202, self.hub.request_btc_dynamic_statistics_reset())
            return
        if self.path == "/api/btc-dynamic/model/reset":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except (ValueError, json.JSONDecodeError):
                self.send_json(400, {"error": "invalid confirmation payload"})
                return
            if payload.get("confirmation") != "RESET_DYNAMIC_MODEL":
                self.send_json(400, {"error": "explicit confirmation required"})
                return
            self.send_json(202, self.hub.request_btc_dynamic_model_reset())
            return
        if not self.path.startswith("/api/config"):
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            if not isinstance(payload, dict):
                raise ValueError("config payload must be an object")
            self.send_json(200, self.hub.set_runtime_config(payload))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc)})

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return


def port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((host, port)) != 0


def choose_port(host: str, preferred: int) -> int:
    for port in range(preferred, preferred + 20):
        if port_available(host, port):
            return port
    raise RuntimeError(f"no free port found from {preferred} to {preferred + 19}")


def start_http_server(web_dir: Path, hub: DashboardHub, host: str, port: int) -> ThreadingHTTPServer:
    class BoundDashboardRequestHandler(DashboardRequestHandler):
        pass

    BoundDashboardRequestHandler.hub = hub
    handler = partial(BoundDashboardRequestHandler, directory=str(web_dir))
    server = ThreadingHTTPServer((host, port), handler)
    thread = Thread(target=server.serve_forever, name="polybtc-dashboard-http", daemon=True)
    thread.start()
    return server


async def run_dashboard(
    config: AppConfig,
    host: str = "127.0.0.1",
    port: int = 8765,
    ws_port: int = 8766,
    max_seconds: int | None = None,
    on_started: Callable[[dict[str, Any]], None] | None = None,
    live_credentials: RealTradingCredentials | None = None,
) -> dict[str, Any]:
    if live_credentials is not None and host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("real trading credentials require a loopback dashboard host")
    http_port = choose_port(host, port)
    websocket_port = choose_port(host, ws_port if ws_port != http_port else http_port + 1)
    web_dir = Path(__file__).resolve().parent.parent / "web"
    hub = DashboardHub(host, http_port, host, websocket_port, config)
    http_server = start_http_server(web_dir, hub, host, http_port)
    ws_server = await websockets.serve(hub.ws_handler, host, websocket_port)
    started = {"url": f"http://{host}:{http_port}", "ws_url": hub.ws_url}
    if on_started:
        on_started(started)
    try:
        while True:
            try:
                output_dir = await run_live(
                    config,
                    max_seconds=max_seconds,
                    on_update=hub.publish,
                    before_market_updates=hub.apply_pending_config_before_markets,
                    control_commands=hub.control_commands,
                    live_credentials=live_credentials,
                )
                return {**started, "output_dir": str(output_dir)}
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if max_seconds is not None:
                    raise
                traceback.print_exc()
                async with hub.lock:
                    hub.latest["status"] = "restarting"
                    hub.latest["restart_error"] = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(LIVE_RESTART_DELAY_SECONDS)
    finally:
        ws_server.close()
        try:
            await asyncio.wait_for(
                ws_server.wait_closed(),
                timeout=DASHBOARD_SHUTDOWN_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            pass
        try:
            await asyncio.wait_for(
                asyncio.to_thread(http_server.shutdown),
                timeout=DASHBOARD_SHUTDOWN_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            pass
        http_server.server_close()
