from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from .clients import BinanceClient, PolymarketClient
from .config import AppConfig
from .engine import PaperEngine
from .journal import RunJournal
from .market import choose_current_market
from .models import Direction, MarketState, OrderBookSnapshot, PriceTick


UpdateCallback = Callable[[dict[str, Any]], Awaitable[None] | None]
BINANCE_TICK_EMIT_INTERVAL = timedelta(milliseconds=200)


def run_dir(base: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return base / stamp


def model_payload(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return value
    return value


def live_snapshot(engine: PaperEngine, output_dir: Path, event_type: str, payload: Any) -> dict[str, Any]:
    books = {direction.value: book.model_dump(mode="json") for direction, book in engine.books.items()}
    event_payload: Any
    if event_type == "book" and isinstance(payload, tuple):
        direction, book = payload
        event_payload = {"direction": direction.value, **book.model_dump(mode="json")}
    else:
        event_payload = model_payload(payload)
    return {
        "type": "snapshot",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "output_dir": str(output_dir),
        "event": {"type": event_type, "payload": event_payload},
        "market": engine.market.model_dump(mode="json") if engine.market else None,
        "tick": engine.tick.model_dump(mode="json") if engine.tick else None,
        "books": books,
        "open_position": engine.open_position.model_dump(mode="json") if engine.open_position else None,
        "summary": engine.summary(),
        "strategy": {"edge_correction_usd": engine.config.strategy.edge_correction_usd},
        "last_rejection": engine.rejections[-1] if engine.rejections else None,
    }


async def emit_update(callback: UpdateCallback | None, snapshot: dict[str, Any]) -> None:
    if callback is None:
        return
    result = callback(snapshot)
    if result is not None:
        await result


async def check_connectivity(config: AppConfig) -> dict[str, Any]:
    binance = BinanceClient(config.sources)
    polymarket = PolymarketClient(config.sources)
    result: dict[str, Any] = {}

    try:
        result["binance_time"] = await binance.server_time()
    except Exception as exc:  # pragma: no cover - network dependent
        result["binance_time"] = {"ok": False, "error": str(exc)}

    try:
        markets = await polymarket.discover_markets()
        current = choose_current_market(markets, max_start_price_lag_ms=config.sources.max_start_price_lag_ms)
        result["gamma"] = {"ok": True, "market_count": len(markets), "current_market": current.model_dump(mode="json") if current else None}
    except Exception as exc:  # pragma: no cover - network dependent
        markets = []
        current = None
        result["gamma"] = {"ok": False, "error": str(exc)}

    if current:
        try:
            result["clob"] = await polymarket.price_probe(current.up_token_id)
        except Exception as exc:  # pragma: no cover - network dependent
            result["clob"] = {"ok": False, "error": str(exc)}
    else:
        result["clob"] = {"ok": False, "error": "no candidate BTC 5 minute market discovered"}
    return result


def should_keep_current_market(engine: PaperEngine, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    market = engine.market
    return bool(market and market.threshold_price is not None and market.end_time > now)


def should_retry_threshold(now: datetime, next_retry_at: datetime) -> bool:
    return now >= next_retry_at


async def apply_polymarket_page_threshold(client: PolymarketClient, market: MarketState) -> bool:
    if market.start_time is None:
        return False
    if market.threshold_price is not None and market.threshold_source not in {"binance_first_tick_after_start", "polymarket_page_previous_close"}:
        return False
    page_data = getattr(client, "market_page_data", None)
    if page_data is not None:
        outcome_price, results = await page_data(market.slug)
    else:
        outcome_result, results_result = await asyncio.gather(
            client.outcome_price(market.slug),
            client.past_results(market.slug),
            return_exceptions=True,
        )
        if isinstance(outcome_result, Exception) and isinstance(results_result, Exception):
            raise outcome_result
        outcome_price = None if isinstance(outcome_result, Exception) else outcome_result
        results = [] if isinstance(results_result, Exception) else results_result
    if outcome_price is not None:
        market.threshold_price = outcome_price.open_price
        market.threshold_source = "polymarket_page_open_price"
        market.threshold_observed_at = market.start_time
        return True
    previous = [result for result in results if result.end_time == market.start_time]
    if not previous:
        return False
    latest = previous[-1]
    market.threshold_price = latest.close_price
    market.threshold_source = "polymarket_page_previous_close"
    market.threshold_observed_at = latest.end_time
    return True


async def current_market_with_page_threshold(
    client: PolymarketClient,
    max_start_price_lag_ms: int,
    now: datetime | None = None,
) -> MarketState | None:
    markets = await client.discover_markets()
    market = choose_current_market(markets, now=now, max_start_price_lag_ms=max_start_price_lag_ms)
    if market and market.threshold_price is None:
        try:
            await apply_polymarket_page_threshold(client, market)
        except Exception:
            pass
    return market


async def initialize_current_market(
    client: PolymarketClient,
    engine: PaperEngine,
    journal: RunJournal,
    output_dir: Path,
    on_update: UpdateCallback | None,
) -> None:
    try:
        now = datetime.now(timezone.utc)
        markets = await asyncio.wait_for(client.discover_markets(), timeout=5)
        market = choose_current_market(
            markets,
            now=now,
            max_start_price_lag_ms=engine.config.sources.max_start_price_lag_ms,
        )
        if market and market.threshold_price is None:
            try:
                await apply_polymarket_page_threshold(client, market)
            except Exception as exc:
                journal.latency_row("polymarket_page", "initial_threshold", False, None, str(exc))
    except Exception as exc:
        journal.latency_row("gamma", "initial_market", False, None, str(exc))
        return
    if market is None:
        return
    engine.set_market(market)
    journal.market(market)
    await emit_update(on_update, live_snapshot(engine, output_dir, "market", market))


async def market_loop(client: PolymarketClient, engine: PaperEngine, queue: asyncio.Queue, interval_seconds: float) -> None:
    last_market_id = engine.market.condition_id if engine.market else None
    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            if engine.market and engine.market.threshold_price is None and engine.market.end_time > now_utc:
                try:
                    if await apply_polymarket_page_threshold(client, engine.market):
                        await queue.put(("market", engine.market))
                        await asyncio.sleep(interval_seconds)
                        continue
                except Exception as exc:
                    await queue.put(("error", {"source": "polymarket_page", "error": str(exc)}))
            if should_keep_current_market(engine, now=now_utc):
                await asyncio.sleep(interval_seconds)
                continue
            markets = await client.discover_markets()
            market = choose_current_market(
                markets,
                now=now_utc,
                max_start_price_lag_ms=engine.config.sources.max_start_price_lag_ms,
            )
            if market and (market.condition_id != last_market_id or market.threshold_price is None):
                last_market_id = market.condition_id
                try:
                    await apply_polymarket_page_threshold(client, market)
                except Exception as exc:
                    await queue.put(("error", {"source": "polymarket_page", "error": str(exc)}))
                await queue.put(("market", market))
        except Exception as exc:
            await queue.put(("error", {"source": "gamma", "error": str(exc)}))
        await asyncio.sleep(interval_seconds)


async def binance_loop(client: BinanceClient, queue: asyncio.Queue) -> None:
    last_emit_at = datetime.min.replace(tzinfo=timezone.utc)
    while True:
        try:
            async for tick in client.trades():
                if tick.received_at - last_emit_at < BINANCE_TICK_EMIT_INTERVAL:
                    continue
                last_emit_at = tick.received_at
                await queue.put(("tick", tick))
        except Exception as exc:
            await queue.put(("error", {"source": "binance", "error": str(exc)}))
            await asyncio.sleep(1)


def book_matches_market(market: MarketState, direction: Direction, book: OrderBookSnapshot) -> bool:
    expected_token = market.up_token_id if direction == Direction.UP else market.down_token_id
    if book.token_id != expected_token:
        return False
    if book.market_id and book.market_id != market.condition_id:
        return False
    return True


async def emit_rest_books(client: PolymarketClient, market: MarketState, queue: asyncio.Queue) -> None:
    up_book, down_book = await asyncio.gather(client.book(market.up_token_id), client.book(market.down_token_id))
    current_market = market
    if book_matches_market(current_market, Direction.UP, up_book):
        await queue.put(("book", (Direction.UP, up_book)))
    if book_matches_market(current_market, Direction.DOWN, down_book):
        await queue.put(("book", (Direction.DOWN, down_book)))


async def book_loop(client: PolymarketClient, engine: PaperEngine, queue: asyncio.Queue, poll_ms: int) -> None:
    timeout_seconds = max(poll_ms / 1000, 0.2)
    rest_refresh_seconds = timeout_seconds
    while True:
        market = engine.market
        if not market:
            await asyncio.sleep(0.2)
            continue
        if market.end_time <= datetime.now(timezone.utc):
            await asyncio.sleep(0.05)
            continue

        current_market_id = market.condition_id
        websocket_active = False
        stream = client.book_stream((market.up_token_id, market.down_token_id))
        next_book_task: asyncio.Task[tuple[str, OrderBookSnapshot]] | None = None
        rest_task: asyncio.Task[None] | None = None
        try:
            next_book_task = asyncio.create_task(stream.__anext__())
            rest_task = asyncio.create_task(emit_rest_books(client, market, queue))
            next_rest_refresh_at = datetime.now(timezone.utc) + timedelta(seconds=rest_refresh_seconds)

            while True:
                current_market = engine.market
                now = datetime.now(timezone.utc)
                if not current_market or current_market.condition_id != current_market_id or current_market.end_time <= now:
                    break
                try:
                    wait_tasks = {task for task in (next_book_task, rest_task) if task is not None}
                    done, _ = await asyncio.wait(wait_tasks, timeout=timeout_seconds)
                    if rest_task and rest_task in done:
                        try:
                            rest_task.result()
                        except Exception as exc:
                            await queue.put(("error", {"source": "clob_rest", "error": str(exc)}))
                        rest_task = None
                        next_rest_refresh_at = datetime.now(timezone.utc) + timedelta(seconds=rest_refresh_seconds)
                    if rest_task is None and now >= next_rest_refresh_at:
                        rest_task = asyncio.create_task(emit_rest_books(client, current_market, queue))
                    if not done:
                        continue
                    if next_book_task not in done:
                        continue
                    token_id, book = next_book_task.result()
                    next_book_task = asyncio.create_task(stream.__anext__())
                except StopAsyncIteration:
                    break
                except Exception:
                    if next_book_task and next_book_task.done():
                        next_book_task = None
                    raise
                websocket_active = True
                current_market = engine.market
                if not current_market or current_market.condition_id != current_market_id:
                    break
                if token_id == current_market.up_token_id:
                    direction = Direction.UP
                elif token_id == current_market.down_token_id:
                    direction = Direction.DOWN
                else:
                    continue
                if book_matches_market(current_market, direction, book):
                    await queue.put(("book", (direction, book)))
        except Exception as exc:
            source = "clob_ws" if websocket_active else "clob"
            await queue.put(("error", {"source": source, "error": str(exc)}))
            await asyncio.sleep(timeout_seconds)
        finally:
            if next_book_task and not next_book_task.done():
                next_book_task.cancel()
                try:
                    await next_book_task
                except (asyncio.CancelledError, StopAsyncIteration):
                    pass
            if rest_task and not rest_task.done():
                rest_task.cancel()
                try:
                    await rest_task
                except asyncio.CancelledError:
                    pass
            await stream.aclose()
        await asyncio.sleep(0.05)


def flush_engine_updates(engine: PaperEngine, journal: RunJournal, counters: dict[str, int]) -> None:
    while counters["signals"] < len(engine.signals):
        journal.signal(engine.signals[counters["signals"]])
        counters["signals"] += 1
    while counters["fills"] < len(engine.fills):
        journal.fill(engine.fills[counters["fills"]])
        counters["fills"] += 1
    while counters["exits"] < len(engine.exit_events):
        journal.exit_event(engine.exit_events[counters["exits"]])
        counters["exits"] += 1


def coalesce_live_events(events: list[tuple[str, Any]]) -> list[tuple[str, Any]]:
    result: list[tuple[str, Any]] = []
    buffered: dict[tuple[str, str], tuple[int, tuple[str, Any]]] = {}

    def flush_buffered() -> None:
        for _, event in sorted(buffered.values(), key=lambda item: item[0]):
            result.append(event)
        buffered.clear()

    for index, event in enumerate(events):
        event_type, payload = event
        if event_type == "tick":
            buffered[("tick", "latest")] = (index, event)
            continue
        if event_type == "book" and isinstance(payload, tuple) and payload:
            direction = payload[0]
            direction_key = direction.value if isinstance(direction, Direction) else str(direction)
            buffered[("book", direction_key)] = (index, event)
            continue
        flush_buffered()
        result.append(event)
    flush_buffered()
    return result


async def run_live(config: AppConfig, max_seconds: int | None = None, on_update: UpdateCallback | None = None) -> Path:
    output_dir = run_dir(config.data_dir)
    journal = RunJournal(output_dir)
    engine = PaperEngine(config)
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
    poly = PolymarketClient(config.sources)
    binance = BinanceClient(config.sources)
    counters = {"signals": 0, "fills": 0, "exits": 0}

    await initialize_current_market(poly, engine, journal, output_dir, on_update)

    tasks = [
        asyncio.create_task(market_loop(poly, engine, queue, config.sources.market_refresh_seconds)),
        asyncio.create_task(binance_loop(binance, queue)),
        asyncio.create_task(book_loop(poly, engine, queue, config.sources.poly_book_poll_ms)),
    ]
    started = datetime.now(timezone.utc)
    try:
        while True:
            if max_seconds is not None and (datetime.now(timezone.utc) - started).total_seconds() >= max_seconds:
                break
            try:
                pending_events = [await asyncio.wait_for(queue.get(), timeout=1)]
            except asyncio.TimeoutError:
                continue
            while not queue.empty() and len(pending_events) < 500:
                pending_events.append(queue.get_nowait())
            for event_type, payload in coalesce_live_events(pending_events):
                if event_type == "market":
                    market: MarketState = payload
                    engine.set_market(market)
                    journal.market(market)
                elif event_type == "tick":
                    tick: PriceTick = payload
                    engine.set_tick(tick)
                    journal.tick(tick)
                elif event_type == "book":
                    direction, book = payload
                    assert isinstance(book, OrderBookSnapshot)
                    engine.set_book(direction, book)
                    journal.book(direction.value, book)
                elif event_type == "error":
                    journal.latency_row(payload.get("source", "unknown"), "stream", False, None, payload.get("error", ""))
                flush_engine_updates(engine, journal, counters)
                await emit_update(on_update, live_snapshot(engine, output_dir, event_type, payload))
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for position in engine.positions:
            journal.position(position)
        journal.summary(engine.summary())
        await emit_update(on_update, live_snapshot(engine, output_dir, "summary", engine.summary()))
    return output_dir
