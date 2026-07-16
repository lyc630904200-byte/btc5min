from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any, Iterable


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def compact_stats(values: Iterable[float]) -> dict[str, float | int | None]:
    items = [value for value in values if value is not None]
    if not items:
        return {"count": 0, "min": None, "max": None, "avg": None}
    return {"count": len(items), "min": min(items), "max": max(items), "avg": mean(items)}


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                yield payload


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def latest_run_dir(data_dir: Path) -> Path | None:
    if not data_dir.exists():
        return None
    candidates = [path for path in data_dir.iterdir() if path.is_dir() and any((path / name).exists() for name in ("events.jsonl", "markets.jsonl", "fills.csv"))]
    if not candidates:
        return None
    return max(candidates, key=run_activity_time)


def run_activity_time(run_dir: Path) -> float:
    mtimes = [run_dir.stat().st_mtime]
    for name in ("events.jsonl", "markets.jsonl", "ticks.jsonl", "fills.csv", "positions.csv", "signals.jsonl", "summary.json"):
        path = run_dir / name
        if path.exists():
            mtimes.append(path.stat().st_mtime)
    return max(mtimes)


def summarize_markets(run_dir: Path) -> dict[str, Any]:
    updates = list(iter_jsonl(run_dir / "markets.jsonl"))
    latest_by_market: dict[str, dict[str, Any]] = {}
    for market in updates:
        condition_id = market.get("condition_id") or market.get("slug")
        if condition_id:
            latest_by_market[str(condition_id)] = market

    markets = list(latest_by_market.values())
    threshold_sources = Counter(str(market.get("threshold_source") or "unknown") for market in markets)
    threshold_lags: list[float] = []
    observed_markets = 0
    for market in markets:
        if market.get("threshold_price") is not None:
            observed_markets += 1
        start = parse_datetime(market.get("start_time"))
        observed_at = parse_datetime(market.get("threshold_observed_at"))
        if start and observed_at:
            threshold_lags.append((observed_at - start).total_seconds())

    return {
        "updates": len(updates),
        "unique_markets": len(markets),
        "markets_with_threshold": observed_markets,
        "threshold_source_counts": dict(threshold_sources),
        "threshold_lag_seconds": compact_stats(threshold_lags),
        "latest_market": updates[-1] if updates else None,
    }


def summarize_signals(run_dir: Path) -> dict[str, Any]:
    signals = list(iter_jsonl(run_dir / "signals.jsonl"))
    edges = [as_float(signal.get("edge_usd")) for signal in signals]
    asks = [as_float(signal.get("ask_price")) for signal in signals]
    by_direction = Counter(str(signal.get("direction") or "unknown") for signal in signals)
    by_reason = Counter(str(signal.get("reason") or "unknown") for signal in signals)
    return {
        "count": len(signals),
        "by_direction": dict(by_direction),
        "by_reason": dict(by_reason),
        "edge_usd": compact_stats([value for value in edges if value is not None]),
        "ask_price": compact_stats([value for value in asks if value is not None]),
    }


def summarize_fills(run_dir: Path) -> dict[str, Any]:
    fills = read_csv_rows(run_dir / "fills.csv")
    by_side = Counter(row.get("side") or "unknown" for row in fills)
    by_reason = Counter(row.get("reason") or "unknown" for row in fills)
    by_position: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in fills:
        position_id = row.get("position_id") or row.get("fill_id") or "unknown"
        by_position[position_id].append(row)

    buy_quotes: list[float] = []
    sell_quotes: list[float] = []
    buy_fees: list[float] = []
    sell_fees: list[float] = []
    buy_prices: list[float] = []
    sell_prices: list[float] = []
    round_trip_pnls: list[float] = []
    hold_seconds: list[float] = []
    open_positions = 0

    for rows in by_position.values():
        buys = [row for row in rows if row.get("side") == "BUY"]
        sells = [row for row in rows if row.get("side") == "SELL"]
        buy_quote = sum(as_float(row.get("quote")) or 0.0 for row in buys)
        sell_quote = sum(as_float(row.get("quote")) or 0.0 for row in sells)
        buy_fee = sum(as_float(row.get("fee_usd")) or 0.0 for row in buys)
        sell_fee = sum(as_float(row.get("fee_usd")) or 0.0 for row in sells)
        buy_qty = sum(as_float(row.get("quantity")) or 0.0 for row in buys)
        sell_qty = sum(as_float(row.get("quantity")) or 0.0 for row in sells)
        buy_quotes.extend(as_float(row.get("quote")) or 0.0 for row in buys)
        sell_quotes.extend(as_float(row.get("quote")) or 0.0 for row in sells)
        buy_fees.extend(as_float(row.get("fee_usd")) or 0.0 for row in buys)
        sell_fees.extend(as_float(row.get("fee_usd")) or 0.0 for row in sells)
        buy_prices.extend(as_float(row.get("avg_price")) or 0.0 for row in buys)
        sell_prices.extend(as_float(row.get("avg_price")) or 0.0 for row in sells)
        if buys and sells and sell_qty >= buy_qty - 1e-9:
            round_trip_pnls.append(sell_quote - sell_fee - buy_quote - buy_fee)
            opened = parse_datetime(buys[0].get("created_at"))
            closed = parse_datetime(sells[-1].get("created_at"))
            if opened and closed:
                hold_seconds.append((closed - opened).total_seconds())
        elif buys and buy_qty > sell_qty + 1e-9:
            open_positions += 1

    winners = len([pnl for pnl in round_trip_pnls if pnl > 0])
    losers = len([pnl for pnl in round_trip_pnls if pnl < 0])
    closed_positions = len(round_trip_pnls)

    return {
        "count": len(fills),
        "by_side": dict(by_side),
        "by_reason": dict(by_reason),
        "positions": len(by_position),
        "closed_positions_from_fills": closed_positions,
        "open_positions_from_fills": open_positions,
        "buy_quote": sum(buy_quotes),
        "sell_quote": sum(sell_quotes),
        "buy_fee": sum(buy_fees),
        "sell_fee": sum(sell_fees),
        "total_fee": sum(buy_fees) + sum(sell_fees),
        "realized_pnl_from_fills": sum(round_trip_pnls),
        "win_rate_from_fills": winners / closed_positions if closed_positions else None,
        "winners_from_fills": winners,
        "losers_from_fills": losers,
        "buy_price": compact_stats(buy_prices),
        "sell_price": compact_stats(sell_prices),
        "hold_seconds": compact_stats(hold_seconds),
    }


def summarize_events(run_dir: Path) -> dict[str, Any]:
    event_counts: Counter[str] = Counter()
    exit_reason_counts: Counter[str] = Counter()
    exit_pnls: list[float] = []
    first_at: datetime | None = None
    last_at: datetime | None = None

    for event in iter_jsonl(run_dir / "events.jsonl"):
        event_type = str(event.get("type") or "unknown")
        event_counts[event_type] += 1
        created_at = parse_datetime(event.get("created_at"))
        if created_at and (first_at is None or created_at < first_at):
            first_at = created_at
        if created_at and (last_at is None or created_at > last_at):
            last_at = created_at

        if event_type == "exit":
            payload = event.get("payload") or {}
            exit_reason_counts[str(payload.get("reason") or "unknown")] += 1
            pnl = as_float(payload.get("pnl"))
            if pnl is not None:
                exit_pnls.append(pnl)

    observed_seconds = (last_at - first_at).total_seconds() if first_at and last_at else None
    return {
        "event_counts": dict(event_counts),
        "first_event_at": first_at.isoformat() if first_at else None,
        "last_event_at": last_at.isoformat() if last_at else None,
        "observed_seconds": observed_seconds,
        "exit_reason_counts": dict(exit_reason_counts),
        "realized_pnl_from_exits": sum(exit_pnls),
        "exit_pnl": compact_stats(exit_pnls),
    }


def summarize_ticks(run_dir: Path) -> dict[str, Any]:
    prices: list[float] = []
    first_at: datetime | None = None
    last_at: datetime | None = None
    count = 0

    for tick in iter_jsonl(run_dir / "ticks.jsonl"):
        if tick.get("type") == "book":
            continue
        price = as_float(tick.get("price"))
        if price is not None:
            prices.append(price)
            count += 1
        received_at = parse_datetime(tick.get("received_at") or tick.get("timestamp"))
        if received_at and (first_at is None or received_at < first_at):
            first_at = received_at
        if received_at and (last_at is None or received_at > last_at):
            last_at = received_at

    return {
        "count": count,
        "first_tick_at": first_at.isoformat() if first_at else None,
        "last_tick_at": last_at.isoformat() if last_at else None,
        "price": compact_stats(prices),
    }


def build_report(run_dir: Path) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    if not run_dir.exists() or not run_dir.is_dir():
        raise FileNotFoundError(f"run directory not found: {run_dir}")

    events = summarize_events(run_dir)
    signals = summarize_signals(run_dir)
    fills = summarize_fills(run_dir)
    realized_from_exits = events["realized_pnl_from_exits"]
    realized_from_fills = fills["realized_pnl_from_fills"]

    return {
        "run_dir": str(run_dir),
        "events": events,
        "markets": summarize_markets(run_dir),
        "ticks": summarize_ticks(run_dir),
        "signals": signals,
        "fills": fills,
        "summary": {
            "observed_minutes": (events["observed_seconds"] / 60) if events["observed_seconds"] is not None else None,
            "signals": signals["count"],
            "closed_positions": fills["closed_positions_from_fills"],
            "open_positions": fills["open_positions_from_fills"],
            "realized_pnl": realized_from_exits if events["event_counts"].get("exit", 0) else realized_from_fills,
            "realized_pnl_source": "exit_events" if events["event_counts"].get("exit", 0) else "fills",
            "total_buy_quote": fills["buy_quote"],
            "win_rate": fills["win_rate_from_fills"],
        },
    }
