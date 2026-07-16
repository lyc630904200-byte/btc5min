import csv
import json
from datetime import datetime, timedelta, timezone

from polybtc.report import build_report, latest_run_dir


def write_jsonl(path, rows) -> None:
    path.write_text("\n".join(json.dumps(row, default=str) for row in rows), encoding="utf-8")


def test_build_report_summarizes_run_directory(tmp_path) -> None:
    run_dir = tmp_path / "20260711T020000Z"
    run_dir.mkdir()
    now = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
    market = {
        "condition_id": "m1",
        "slug": "btc-updown-5m-test",
        "threshold_price": 64000.0,
        "threshold_source": "polymarket_page_previous_close",
        "threshold_observed_at": now.isoformat(),
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(minutes=5)).isoformat(),
    }
    signal = {
        "signal_id": "s1",
        "market_id": "m1",
        "direction": "UP",
        "edge_usd": 55.5,
        "ask_price": 0.7,
        "reason": "entry_edge",
        "created_at": (now + timedelta(seconds=5)).isoformat(),
    }
    exit_event = {
        "type": "exit",
        "created_at": (now + timedelta(seconds=65)).isoformat(),
        "payload": {"position_id": "p1", "reason": "take_profit", "pnl": 1.5},
    }
    write_jsonl(run_dir / "markets.jsonl", [market])
    write_jsonl(run_dir / "signals.jsonl", [signal])
    write_jsonl(
        run_dir / "events.jsonl",
        [
            {"type": "market", "created_at": now.isoformat(), "payload": market},
            {"type": "signal", "created_at": signal["created_at"], "payload": signal},
            exit_event,
        ],
    )
    write_jsonl(
        run_dir / "ticks.jsonl",
        [
            {"price": 64010, "received_at": now.isoformat()},
            {"price": 64080, "received_at": (now + timedelta(seconds=65)).isoformat()},
            {"type": "book", "timestamp": now.isoformat()},
        ],
    )
    with (run_dir / "fills.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["fill_id", "position_id", "market_id", "token_id", "direction", "side", "avg_price", "quantity", "quote", "slippage", "created_at", "reason"],
        )
        writer.writeheader()
        writer.writerow(
            {
                "fill_id": "f1",
                "position_id": "p1",
                "market_id": "m1",
                "token_id": "up",
                "direction": "UP",
                "side": "BUY",
                "avg_price": "0.70",
                "quantity": "10",
                "quote": "7.0",
                "slippage": "0",
                "created_at": (now + timedelta(seconds=5)).isoformat(),
                "reason": "entry_edge",
            }
        )
        writer.writerow(
            {
                "fill_id": "f2",
                "position_id": "p1",
                "market_id": "m1",
                "token_id": "up",
                "direction": "UP",
                "side": "SELL",
                "avg_price": "0.85",
                "quantity": "10",
                "quote": "8.5",
                "slippage": "0",
                "created_at": (now + timedelta(seconds=65)).isoformat(),
                "reason": "take_profit",
            }
        )

    report = build_report(run_dir)

    assert report["summary"]["signals"] == 1
    assert report["summary"]["closed_positions"] == 1
    assert report["summary"]["realized_pnl"] == 1.5
    assert report["summary"]["realized_pnl_source"] == "exit_events"
    assert report["fills"]["realized_pnl_from_fills"] == 1.5
    assert report["fills"]["hold_seconds"]["avg"] == 60
    assert report["markets"]["threshold_source_counts"] == {"polymarket_page_previous_close": 1}
    assert report["ticks"]["price"]["max"] == 64080


def test_latest_run_dir_uses_mtime(tmp_path) -> None:
    older = tmp_path / "older"
    newer = tmp_path / "newer"
    container = tmp_path / "replay"
    older.mkdir()
    newer.mkdir()
    container.mkdir()
    (container / "child").mkdir()
    (older / "events.jsonl").write_text("", encoding="utf-8")
    (newer / "events.jsonl").write_text("", encoding="utf-8")
    older.touch()
    newer.touch()

    assert latest_run_dir(tmp_path) == newer


def test_build_report_treats_partial_sell_as_open(tmp_path) -> None:
    run_dir = tmp_path / "20260711T030000Z"
    run_dir.mkdir()
    with (run_dir / "fills.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["fill_id", "position_id", "market_id", "token_id", "direction", "side", "avg_price", "quantity", "quote", "slippage", "created_at", "reason"],
        )
        writer.writeheader()
        writer.writerow({"fill_id": "f1", "position_id": "p1", "side": "BUY", "avg_price": "0.5", "quantity": "10", "quote": "5"})
        writer.writerow({"fill_id": "f2", "position_id": "p1", "side": "SELL", "avg_price": "0.6", "quantity": "5", "quote": "3"})

    report = build_report(run_dir)

    assert report["fills"]["closed_positions_from_fills"] == 0
    assert report["fills"]["open_positions_from_fills"] == 1


def test_build_report_subtracts_fill_fees_from_realized_pnl(tmp_path) -> None:
    run_dir = tmp_path / "20260711T040000Z"
    run_dir.mkdir()
    now = datetime(2026, 7, 11, 4, 0, tzinfo=timezone.utc)
    path = run_dir / "fills.csv"
    fieldnames = [
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
        "created_at",
        "reason",
    ]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow(
            {
                "fill_id": "buy",
                "position_id": "p1",
                "market_id": "m1",
                "token_id": "up",
                "direction": "UP",
                "side": "BUY",
                "avg_price": 0.60,
                "quantity": 10 / 0.60,
                "quote": 10,
                "slippage": 0,
                "fee_usd": 0.28,
                "created_at": now.isoformat(),
                "reason": "entry_edge",
            }
        )
        writer.writerow(
            {
                "fill_id": "sell",
                "position_id": "p1",
                "market_id": "m1",
                "token_id": "up",
                "direction": "UP",
                "side": "SELL",
                "avg_price": 0.70,
                "quantity": 10 / 0.60,
                "quote": (10 / 0.60) * 0.70,
                "slippage": 0,
                "fee_usd": 0.245,
                "created_at": (now + timedelta(seconds=10)).isoformat(),
                "reason": "take_profit",
            }
        )

    report = build_report(run_dir)

    assert round(report["fills"]["total_fee"], 6) == 0.525
    assert round(report["fills"]["realized_pnl_from_fills"], 6) == round((10 / 0.60) * 0.70 - 10 - 0.525, 6)
