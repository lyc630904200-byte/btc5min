from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from .config import load_config
from .dashboard import run_dashboard
from .journal import RunJournal
from .replay import replay_events
from .report import build_report, latest_run_dir
from .runner import check_connectivity, run_live, run_dir


app = typer.Typer(help="Polymarket 5 minute BTC/ETH paper trading tool")
console = Console()


def print_json(payload: object) -> None:
    console.print_json(json.dumps(payload, ensure_ascii=False, default=str))


def format_value(value: object, digits: int = 4) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


@app.command()
def check(config: Optional[Path] = typer.Option(None, "--config", "-c", help="YAML config path")) -> None:
    """Check Binance, Gamma, CLOB, and local clock connectivity."""
    cfg = load_config(config)
    result = asyncio.run(check_connectivity(cfg))
    table = Table(title="polybtc check")
    table.add_column("source")
    table.add_column("ok")
    table.add_column("detail")
    for key, value in result.items():
        ok = bool(value.get("ok")) if isinstance(value, dict) else False
        detail = json.dumps(value, ensure_ascii=False, default=str)[:500]
        table.add_row(key, "yes" if ok else "no", detail)
    console.print(table)


@app.command()
def run(
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="YAML config path"),
    max_seconds: Optional[int] = typer.Option(None, "--max-seconds", help="Optional run duration for smoke tests"),
) -> None:
    """Run live paper trading."""
    cfg = load_config(config)
    output_dir = asyncio.run(run_live(cfg, max_seconds=max_seconds))
    console.print(f"run output: {output_dir}")


@app.command()
def dashboard(
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="YAML config path"),
    host: str = typer.Option("127.0.0.1", "--host", help="Dashboard host"),
    port: int = typer.Option(8765, "--port", help="HTTP port"),
    ws_port: int = typer.Option(8766, "--ws-port", help="WebSocket port"),
    max_seconds: Optional[int] = typer.Option(None, "--max-seconds", help="Optional run duration for smoke tests"),
) -> None:
    """Run live paper trading with a local realtime dashboard."""
    cfg = load_config(config)
    def show_started(payload: dict[str, object]) -> None:
        console.print(f"dashboard: {payload['url']}")

    result = asyncio.run(run_dashboard(cfg, host=host, port=port, ws_port=ws_port, max_seconds=max_seconds, on_started=show_started))
    print_json(result)


@app.command()
def replay(
    input: Path = typer.Option(..., "--input", "-i", help="events.jsonl path"),
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="YAML config path"),
    out: Optional[Path] = typer.Option(None, "--out", help="Replay output directory"),
) -> None:
    """Replay recorded events deterministically without network access."""
    cfg = load_config(config)
    engine = replay_events(input, cfg)
    output_dir = out or run_dir(cfg.data_dir / "replay")
    journal = RunJournal(output_dir)
    for signal in engine.signals:
        journal.signal(signal)
    for fill in engine.fills:
        journal.fill(fill)
    for event in engine.exit_events:
        journal.exit_event(event)
    for position in engine.positions:
        journal.position(position)
    journal.summary(engine.summary())
    print_json({"output_dir": str(output_dir), **engine.summary()})


@app.command()
def report(
    input: Optional[Path] = typer.Option(None, "--input", "-i", help="Run directory under data/; defaults to the latest run"),
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="YAML config path"),
    out: Optional[Path] = typer.Option(None, "--out", help="Optional JSON report output path"),
    json_output: bool = typer.Option(False, "--json", help="Print the full report as JSON"),
) -> None:
    """Summarize a recorded paper-trading run."""
    cfg = load_config(config)
    run_path = input or latest_run_dir(cfg.data_dir)
    if run_path is None:
        raise typer.BadParameter(f"no run directories found under {cfg.data_dir}")

    payload = build_report(run_path)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    if json_output:
        print_json(payload)
        return

    summary = payload["summary"]
    table = Table(title=f"polybtc report: {Path(payload['run_dir']).name}")
    table.add_column("metric")
    table.add_column("value")
    table.add_row("observed minutes", format_value(summary["observed_minutes"], 2))
    table.add_row("signals", format_value(summary["signals"], 0))
    table.add_row("closed positions", format_value(summary["closed_positions"], 0))
    table.add_row("open positions", format_value(summary["open_positions"], 0))
    table.add_row("realized pnl", format_value(summary["realized_pnl"], 4))
    table.add_row("pnl source", str(summary["realized_pnl_source"]))
    table.add_row("total buy quote", format_value(summary["total_buy_quote"], 2))
    table.add_row("win rate", format_value(summary["win_rate"], 2))
    console.print(table)

    markets = payload["markets"]
    market_table = Table(title="markets")
    market_table.add_column("metric")
    market_table.add_column("value")
    market_table.add_row("unique markets", format_value(markets["unique_markets"], 0))
    market_table.add_row("markets with threshold", format_value(markets["markets_with_threshold"], 0))
    market_table.add_row("threshold sources", json.dumps(markets["threshold_source_counts"], ensure_ascii=False))
    market_table.add_row("threshold lag avg sec", format_value(markets["threshold_lag_seconds"]["avg"], 2))
    console.print(market_table)


if __name__ == "__main__":
    app()
