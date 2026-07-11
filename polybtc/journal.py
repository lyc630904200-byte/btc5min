from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .models import ExitEvent, Fill, MarketState, OrderBookSnapshot, Position, PriceTick, Signal


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
            ["fill_id", "position_id", "market_id", "token_id", "direction", "side", "avg_price", "quantity", "quote", "slippage", "created_at", "reason"],
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
                "opened_at",
                "entry_edge_usd",
                "status",
                "exit_price",
                "exit_quote",
                "realized_pnl",
                "exit_reason",
                "closed_at",
            ],
        )
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
        payload = serialize(book)
        payload["direction"] = direction
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
