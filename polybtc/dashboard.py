from __future__ import annotations

import asyncio
import math
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
            "events": [],
            "strategy": self.config_json()["strategy"],
            "ws_url": self.ws_url,
        }
        self.events: list[dict[str, Any]] = []
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
            "condition_id",
            "slug",
            "question",
            "threshold_price",
            "threshold_source",
            "threshold_observed_at",
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
            "best_bid": best_bid,
            "best_ask": best_ask,
            "min_order_size": book.get("min_order_size"),
            "tick_size": book.get("tick_size"),
        }

    def compact_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        payload = dict(snapshot)
        payload["market"] = self.compact_market(payload.get("market"))
        payload["books"] = {direction: self.compact_book(book) for direction, book in (payload.get("books") or {}).items()}
        payload["strategy"] = {**self.config_json()["strategy"], **(payload.get("strategy") or {})}
        return payload

    def config_json(self) -> dict[str, Any]:
        return {"strategy": {"edge_correction_usd": self.config.strategy.edge_correction_usd}}

    def set_edge_correction(self, value: Any) -> dict[str, Any]:
        correction = float(value)
        if not math.isfinite(correction):
            raise ValueError("edge_correction_usd must be finite")
        self.config.strategy.edge_correction_usd = correction
        return self.config_json()

    async def publish(self, snapshot: dict[str, Any]) -> None:
        message: str
        snapshot = self.compact_snapshot(snapshot)
        async with self.lock:
            event = snapshot.get("event")
            compacted_event = self.compact_event(event) if event else None
            if compacted_event and compacted_event.get("type") == "fill":
                self.events.append(compacted_event)
                self.events = self.events[-250:]
            snapshot["event"] = compacted_event
            snapshot["status"] = "running"
            snapshot["events"] = list(self.events)
            snapshot["ws_url"] = self.ws_url
            self.latest = snapshot
            now = datetime.now(timezone.utc)
            event_type = event.get("type") if isinstance(event, dict) else None
            should_push = event_type not in {"tick", "polymarket_tick"} or now - self.last_push_at >= self.push_interval
            if not should_push:
                return
            self.last_push_at = now
            message = json.dumps(snapshot, ensure_ascii=False)
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
        payload["events"] = list(self.events)
        payload["ws_url"] = self.ws_url
        payload["strategy"] = {**self.config_json()["strategy"], **(payload.get("strategy") or {})}
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
            strategy = payload.get("strategy") if isinstance(payload, dict) else None
            value = strategy.get("edge_correction_usd") if isinstance(strategy, dict) else payload.get("edge_correction_usd")
            self.send_json(200, self.hub.set_edge_correction(value))
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
