from __future__ import annotations

import asyncio
import json
import socket
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, Callable

import websockets

from .config import AppConfig
from .runner import run_live


class DashboardHub:
    def __init__(self, http_host: str, http_port: int, ws_host: str, ws_port: int, config: AppConfig):
        self.http_host = http_host
        self.http_port = http_port
        self.ws_host = ws_host
        self.ws_port = ws_port
        self.config = config
        self.runtime_settings_path = self.config.data_dir / "dashboard-settings.json"
        self.pending_config: dict[str, dict[str, Any]] | None = None
        self.pending_after_market_id: str | None = None
        self.pending_after_market_ids: set[str] = set()
        self._load_runtime_settings()
        self.latest: dict[str, Any] = {
            "type": "snapshot",
            "created_at": None,
            "status": "starting",
            "market": None,
            "tick": None,
            "polymarket_tick": None,
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
            "events": [],
            "assets": {},
            "strategy": self.config_json()["strategy"],
            "risk": self.config_json()["risk"],
            **self.config_status_json(),
            "ws_url": self.ws_url,
        }
        self.events: list[dict[str, Any]] = []
        self.events_by_asset: dict[str, list[dict[str, Any]]] = {
            asset: [] for asset in self.config.sources.enabled_assets
        }
        self.asset_snapshots: dict[str, dict[str, Any]] = {}
        self.clients: set[Any] = set()
        self.lock = asyncio.Lock()
        self.last_push_at = datetime.min.replace(tzinfo=timezone.utc)
        self.push_interval = timedelta(milliseconds=50)

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
        }

    def config_status_json(self) -> dict[str, Any]:
        pending = self.pending_config or {}
        return {
            "config_status": "pending_next_market" if self.pending_config else "active",
            "pending_strategy": pending.get("strategy"),
            "pending_risk": pending.get("risk"),
            "pending_pair_match": pending.get("pair_match"),
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
                strategy = type(self.config.strategy).model_validate(strategy_payload)
                risk = type(self.config.risk).model_validate(risk_payload)
                pair_match = type(self.config.pair_match).model_validate(pair_payload)
            except (TypeError, ValueError):
                pass
            else:
                self.config.strategy = strategy
                self.config.risk = risk
                self.config.pair_match = pair_match

        pending = payload.get("pending")
        if isinstance(pending, dict):
            try:
                strategy_payload = self.config.strategy.model_dump()
                strategy_payload.update(pending.get("strategy") or {})
                risk_payload = self.config.risk.model_dump()
                risk_payload.update(pending.get("risk") or {})
                pair_payload = self.config.pair_match.model_dump()
                pair_payload.update(pending.get("pair_match") or {})
                strategy = type(self.config.strategy).model_validate(strategy_payload)
                risk = type(self.config.risk).model_validate(risk_payload)
                pair_match = type(self.config.pair_match).model_validate(pair_payload)
            except (TypeError, ValueError):
                return
            self.pending_config = {
                "strategy": strategy.model_dump(),
                "risk": risk.model_dump(),
                "pair_match": pair_match.model_dump(),
            }
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
            },
            "pending": self.pending_config,
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

    def apply_pending_config_for_market(self, market_id: str | None) -> bool:
        if not self.pending_config or not market_id or market_id in self.pending_after_market_ids:
            return False
        self.config.strategy = type(self.config.strategy).model_validate(self.pending_config["strategy"])
        self.config.risk = type(self.config.risk).model_validate(self.pending_config["risk"])
        self.config.pair_match = type(self.config.pair_match).model_validate(self.pending_config["pair_match"])
        self.pending_config = None
        self.pending_after_market_id = None
        self.pending_after_market_ids = set()
        self._save_runtime_settings()
        return True

    def set_runtime_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        strategy_update = payload.get("strategy")
        risk_update = payload.get("risk")
        pair_update = payload.get("pair_match")
        if strategy_update is not None and not isinstance(strategy_update, dict):
            raise ValueError("strategy must be an object")
        if risk_update is not None and not isinstance(risk_update, dict):
            raise ValueError("risk must be an object")
        if pair_update is not None and not isinstance(pair_update, dict):
            raise ValueError("pair_match must be an object")
        if not strategy_update and not risk_update and not pair_update:
            raise ValueError("strategy, risk, or pair_match settings are required")

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
            "start_seconds_after_open",
            "end_seconds_after_open",
            "max_pairs_per_market",
            "alternate_directions",
        }
        unexpected_strategy = set(strategy_update or {}) - strategy_fields
        unexpected_risk = set(risk_update or {}) - risk_fields
        unexpected_pair = set(pair_update or {}) - pair_fields
        if unexpected_strategy or unexpected_risk or unexpected_pair:
            names = sorted(unexpected_strategy | unexpected_risk | unexpected_pair)
            raise ValueError(f"unsupported runtime settings: {', '.join(names)}")

        pending = self.pending_config or {}
        strategy_payload = dict(pending.get("strategy") or self.config.strategy.model_dump())
        strategy_payload.update(strategy_update or {})
        risk_payload = dict(pending.get("risk") or self.config.risk.model_dump())
        risk_payload.update(risk_update or {})
        pair_payload = dict(pending.get("pair_match") or self.config.pair_match.model_dump())
        pair_payload.update(pair_update or {})
        strategy = type(self.config.strategy).model_validate(strategy_payload)
        risk = type(self.config.risk).model_validate(risk_payload)
        pair_match = type(self.config.pair_match).model_validate(pair_payload)
        self.pending_config = {
            "strategy": strategy.model_dump(),
            "risk": risk.model_dump(),
            "pair_match": pair_match.model_dump(),
        }
        self.pending_after_market_ids = self.current_market_ids()
        self.pending_after_market_id = next(iter(self.pending_after_market_ids), None)
        self._save_runtime_settings()
        return {**self.config_json(), **self.config_status_json()}

    def pending_config_ready_for_snapshot(self, snapshot: dict[str, Any], asset: str) -> bool:
        if not self.pending_config:
            return False
        prospective = dict(self.asset_snapshots)
        prospective[asset] = snapshot
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
            combined["ws_url"] = self.ws_url
            combined.update(self.config_status_json())
            self.latest = combined
            now = datetime.now(timezone.utc)
            event_type = event.get("type") if isinstance(event, dict) else None
            should_push = event_type not in {"tick", "polymarket_tick"} or now - self.last_push_at >= self.push_interval
            if not should_push:
                return
            self.last_push_at = now
            message = json.dumps(combined, ensure_ascii=False)
            clients = set(self.clients)
        if clients:
            await asyncio.gather(*(client.send(message) for client in clients), return_exceptions=True)

    async def ws_handler(self, websocket: Any) -> None:
        self.clients.add(websocket)
        try:
            async with self.lock:
                latest = json.dumps(self.latest, ensure_ascii=False)
            await websocket.send(latest)
            async for _ in websocket:
                pass
        finally:
            self.clients.discard(websocket)

    def state_json(self) -> bytes:
        payload = dict(self.latest)
        payload["events"] = list(payload.get("events") or [])
        payload["ws_url"] = self.ws_url
        payload["strategy"] = {**self.config_json()["strategy"], **(payload.get("strategy") or {})}
        payload["risk"] = {**self.config_json()["risk"], **(payload.get("risk") or {})}
        payload.update(self.config_status_json())
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class DashboardRequestHandler(SimpleHTTPRequestHandler):
    hub: DashboardHub

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
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
            self.send_json(200, self.hub.config_json())
            return
        if self.path == "/" or self.path.startswith("/dashboard"):
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
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
) -> dict[str, Any]:
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
        output_dir = await run_live(config, max_seconds=max_seconds, on_update=hub.publish)
        return {**started, "output_dir": str(output_dir)}
    finally:
        ws_server.close()
        await ws_server.wait_closed()
        http_server.shutdown()
        http_server.server_close()
