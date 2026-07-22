from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .models import ExitEvent, Fill, MarketState, OrderBookSnapshot, Position, PriceTick, Signal
from .pair_match import PairOrder


def json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return str(value)


def serialize(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return value
    return {"value": value}


class JsonlWriter:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, payload: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, default=json_default) + "\n")


class CsvTable:
    def __init__(self, path: Path, fieldnames: list[str]):
        self.path = path
        self.fieldnames = fieldnames
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            with self.path.open("w", encoding="utf-8", newline="") as fh:
                csv.DictWriter(fh, fieldnames=fieldnames).writeheader()

    def write(self, row: dict[str, Any]) -> None:
        normalized = {field: row.get(field) for field in self.fieldnames}
        with self.path.open("a", encoding="utf-8", newline="") as fh:
            csv.DictWriter(fh, fieldnames=self.fieldnames).writerow(normalized)


class RunJournal:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.events = JsonlWriter(run_dir / "events.jsonl")
        self.markets = JsonlWriter(run_dir / "markets.jsonl")
        self.ticks = JsonlWriter(run_dir / "ticks.jsonl")
        self.signals = JsonlWriter(run_dir / "signals.jsonl")
        self.fills = CsvTable(
            run_dir / "fills.csv",
            [
                "fill_id",
                "position_id",
                "market_id",
                "token_id",
                "direction",
                "side",
                "avg_price",
                "quantity",
                "quote",
                "slippage",
                "fee_usd",
                "strategy_direction",
                "reverse_entry",
                "created_at",
                "reason",
            ],
        )
        self.positions = CsvTable(
            run_dir / "positions.csv",
            [
                "position_id",
                "market_id",
                "token_id",
                "direction",
                "entry_price",
                "quantity",
                "entry_quote",
                "entry_fee_usd",
                "exit_fee_usd",
                "taker_fee_rate",
                "opened_at",
                "entry_edge_usd",
                "strategy_direction",
                "reverse_entry",
                "strategy_entry_price",
                "strategy_quantity",
                "strategy_entry_quote",
                "strategy_entry_fee_usd",
                "status",
                "exit_price",
                "exit_quote",
                "realized_pnl",
                "exit_reason",
                "closed_at",
            ],
        )
        pair_fields = [
            "order_number", "order_id", "interval_key", "direction", "opened_at", "start_time", "end_time",
            "btc_slug", "btc_token_id", "btc_direction", "btc_avg_price", "btc_quantity",
            "btc_quote", "btc_fee_usd", "eth_slug", "eth_token_id", "eth_direction",
            "eth_avg_price", "eth_quantity", "eth_quote", "eth_fee_usd", "spread_cents",
            "total_cost_usd", "status", "btc_outcome", "eth_outcome", "payout_usd",
            "realized_pnl", "settled_at",
        ]
        self.pair_orders = CsvTable(run_dir / "pair_orders.csv", pair_fields)
        self.pair_results = CsvTable(run_dir / "pair_results.csv", pair_fields)
        self.pair_markets = JsonlWriter(run_dir / "pair_markets.jsonl")
        self.latency = CsvTable(run_dir / "latency.csv", ["created_at", "source", "operation", "ok", "latency_ms", "detail"])

    def event(self, event_type: str, payload: Any) -> None:
        self.events.write({"type": event_type, "created_at": datetime.now(timezone.utc).isoformat(), "payload": serialize(payload)})

    def market(self, market: MarketState) -> None:
        payload = serialize(market)
        self.markets.write(payload)
        self.event("market", payload)

    def tick(self, tick: PriceTick) -> None:
        payload = serialize(tick)
        self.ticks.write(payload)
        self.event("tick", payload)

    def book(self, direction: str, book: OrderBookSnapshot) -> None:
        payload = {
            "direction": direction,
            "token_id": book.token_id,
            "market_id": book.market_id,
            "timestamp": book.timestamp.isoformat(),
            "best_bid": book.best_bid,
            "best_ask": book.best_ask,
        }
        self.ticks.write({"type": "book", **payload})
        self.event("book", payload)

    def signal(self, signal: Signal) -> None:
        payload = serialize(signal)
        self.signals.write(payload)
        self.event("signal", payload)

    def fill(self, fill: Fill) -> None:
        payload = serialize(fill)
        self.fills.write(payload)
        self.event("fill", payload)

    def position(self, position: Position) -> None:
        self.positions.write(serialize(position))

    def exit_event(self, event: ExitEvent) -> None:
        self.event("exit", event)

    def _pair_order_row(self, order: PairOrder) -> dict[str, Any]:
        payload = order.model_dump(mode="json")
        btc = payload["btc_leg"]
        eth = payload["eth_leg"]
        return {
            "order_number": payload["order_number"], "order_id": payload["order_id"],
            "interval_key": payload["interval_key"],
            "direction": payload["direction"], "opened_at": payload["opened_at"],
            "start_time": payload["start_time"], "end_time": payload["end_time"],
            "btc_slug": btc["market_slug"], "btc_token_id": btc["token_id"],
            "btc_direction": btc["direction"], "btc_avg_price": btc["avg_price"],
            "btc_quantity": btc["quantity"], "btc_quote": btc["quote"],
            "btc_fee_usd": btc["fee_usd"], "eth_slug": eth["market_slug"],
            "eth_token_id": eth["token_id"], "eth_direction": eth["direction"],
            "eth_avg_price": eth["avg_price"], "eth_quantity": eth["quantity"],
            "eth_quote": eth["quote"], "eth_fee_usd": eth["fee_usd"],
            "spread_cents": payload["spread_cents"], "total_cost_usd": payload["total_cost_usd"],
            "status": payload["status"], "btc_outcome": payload["btc_outcome"],
            "eth_outcome": payload["eth_outcome"], "payout_usd": payload["payout_usd"],
            "realized_pnl": payload["realized_pnl"], "settled_at": payload["settled_at"],
        }

    def pair_order(self, order: PairOrder) -> None:
        self.pair_orders.write(self._pair_order_row(order))
        self.event("pair_order", order)

    def pair_result(self, order: PairOrder) -> None:
        self.pair_results.write(self._pair_order_row(order))
        self.event("pair_settlement", order)

    def pair_market(self, summary: dict[str, Any]) -> None:
        self.pair_markets.write(summary)
        self.event("pair_market", summary)

    def latency_row(self, source: str, operation: str, ok: bool, latency_ms: float | None, detail: str = "") -> None:
        self.latency.write(
            {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source": source,
                "operation": operation,
                "ok": ok,
                "latency_ms": latency_ms,
                "detail": detail,
            }
        )

    def summary(self, payload: dict[str, Any]) -> None:
        (self.run_dir / "summary.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=json_default), encoding="utf-8")
