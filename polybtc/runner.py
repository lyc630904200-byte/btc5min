from __future__ import annotations

import asyncio
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from .clients import BinanceClient, PolymarketClient
from .config import AppConfig
from .engine import PaperEngine
from .journal import RunJournal
from .market import choose_current_market
from .models import Direction, MarketState, OrderBookSnapshot, PriceTick, rest_request_started_at


UpdateCallback = Callable[[dict[str, Any]], Awaitable[None] | None]
BINANCE_TICK_EMIT_INTERVAL = timedelta(milliseconds=200)
# Keep a sub-second REST safety net when the CLOB WebSocket is quiet.  The
# strategy freshness limit is one second, so a two-second fallback was too slow.
BOOK_REST_FALLBACK_AFTER = timedelta(milliseconds=500)
LIVE_EVENT_COALESCE_SECONDS = 0.02
BOOK_PUBLISH_HEARTBEAT = timedelta(milliseconds=250)
PROVISIONAL_THRESHOLD_SOURCES = {"binance_first_tick_after_start", "polymarket_page_previous_close"}


def run_dir(base: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return base / stamp


def cleanup_expired_runs(
    data_dir: Path,
    active_run: Path,
    retention: timedelta,
    now: datetime | None = None,
) -> list[Path]:
    """Remove expired completed run directories while preserving the active run and unrelated files."""
    if not data_dir.exists():
        return []
    base_dir = data_dir.resolve()
    active_dir = active_run.resolve()
    current_time = now or datetime.now(timezone.utc)
    run_markers = ("events.jsonl", "markets.jsonl", "fills.csv", "summary.json")
    removed: list[Path] = []
    for candidate in base_dir.iterdir():
        if not candidate.is_dir() or candidate.resolve() == active_dir or candidate.resolve().parent != base_dir:
            continue
        if not any((candidate / marker).exists() for marker in run_markers):
            continue
        modified_at = datetime.fromtimestamp(candidate.stat().st_mtime, tz=timezone.utc)
        if current_time - modified_at < retention:
            continue
        shutil.rmtree(candidate)
        removed.append(candidate)
    return removed


async def data_cleanup_loop(config: AppConfig, active_run: Path, journal: RunJournal) -> None:
    retention = timedelta(hours=config.data_retention_hours)
    while True:
        try:
            cleanup_expired_runs(config.data_dir, active_run, retention)
        except OSError as exc:
            journal.latency_row("data_cleanup", "remove_expired_runs", False, None, str(exc))
        await asyncio.sleep(config.data_cleanup_interval_seconds)


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
        "polymarket_tick": engine.polymarket_tick.model_dump(mode="json") if engine.polymarket_tick else None,
        "books": books,
        "open_position": engine.open_position.model_dump(mode="json") if engine.open_position else None,
        "summary": engine.summary(),
        "strategy": {
            "edge_correction_usd": engine.edge_correction_usd(),
            "edge_correction_source": engine.edge_correction_source(),
        },
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


async def apply_polymarket_page_threshold(
    client: PolymarketClient,
    market: MarketState,
    timeout_seconds: float = 4.0,
) -> bool:
    if market.start_time is None:
        return False
    if market.threshold_price is not None and market.threshold_source not in PROVISIONAL_THRESHOLD_SOURCES:
        return False
    previous_value = market.threshold_price
    previous_source = market.threshold_source
    event_threshold = getattr(client, "event_threshold", None)
    if event_threshold is not None:
        try:
            threshold = await asyncio.wait_for(event_threshold(market.slug), timeout=timeout_seconds)
        except Exception:
            threshold = None
        if threshold is not None:
            market.threshold_price = threshold
            market.threshold_source = "gamma_event_price_to_beat"
            market.threshold_observed_at = market.start_time
            return market.threshold_price != previous_value or market.threshold_source != previous_source
    page_data = getattr(client, "market_page_data", None)
    if page_data is not None:
        outcome_price, results = await asyncio.wait_for(page_data(market.slug), timeout=timeout_seconds)
    else:
        outcome_result, results_result = await asyncio.gather(
            asyncio.wait_for(client.outcome_price(market.slug), timeout=timeout_seconds),
            asyncio.wait_for(client.past_results(market.slug), timeout=timeout_seconds),
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
        return market.threshold_price != previous_value or market.threshold_source != previous_source
    previous = [result for result in results if result.end_time == market.start_time]
    if not previous:
        return False
    latest = previous[-1]
    market.threshold_price = latest.close_price
    market.threshold_source = "polymarket_page_previous_close"
    market.threshold_observed_at = latest.end_time
    return market.threshold_price != previous_value or market.threshold_source != previous_source


async def prefetch_next_market_threshold(
    client: PolymarketClient,
    current_market: MarketState,
    config: AppConfig,
) -> MarketState | None:
    """Fetch the next 5-minute market and its threshold before the current market expires."""
    markets = await client.discover_markets()
    candidates = [market for market in markets if market.end_time > current_market.end_time]
    if not candidates:
        return None
    next_market = min(candidates, key=lambda market: market.end_time)
    if next_market.threshold_price is None:
        await apply_polymarket_page_threshold(
            client,
            next_market,
            timeout_seconds=config.sources.threshold_page_timeout_seconds,
        )
    return next_market


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
                await apply_polymarket_page_threshold(client, market, engine.config.sources.threshold_page_timeout_seconds)
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
    prefetched_market: MarketState | None = None
    prefetched_for_market_id: str | None = None
    next_prefetch_attempt = datetime.min.replace(tzinfo=timezone.utc)
    next_threshold_retry = datetime.min.replace(tzinfo=timezone.utc)
    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            current_market = engine.market
            needs_page_threshold = bool(
                current_market
                and current_market.end_time > now_utc
                and (current_market.threshold_price is None or current_market.threshold_source in PROVISIONAL_THRESHOLD_SOURCES)
            )
            if needs_page_threshold and should_retry_threshold(now_utc, next_threshold_retry):
                next_threshold_retry = now_utc + timedelta(seconds=engine.config.sources.threshold_page_retry_seconds)
                try:
                    if await apply_polymarket_page_threshold(client, current_market, engine.config.sources.threshold_page_timeout_seconds):
                        await queue.put(("market", current_market))
                        await asyncio.sleep(interval_seconds)
                        continue
                except Exception as exc:
                    await queue.put(("error", {"source": "polymarket_page", "error": str(exc)}))
            if should_keep_current_market(engine, now=now_utc):
                if prefetched_for_market_id != engine.market.condition_id and now_utc >= next_prefetch_attempt:
                    try:
                        prefetched_market = await prefetch_next_market_threshold(client, engine.market, engine.config)
                        prefetched_for_market_id = engine.market.condition_id
                    except Exception as exc:
                        next_prefetch_attempt = now_utc + timedelta(seconds=5)
                        await queue.put(("error", {"source": "polymarket_page_prefetch", "error": str(exc)}))
                await asyncio.sleep(interval_seconds)
                continue
            market = prefetched_market if prefetched_market and prefetched_market.end_time >= now_utc else None
            prefetched_market = None
            if market is None:
                markets = await client.discover_markets()
                market = choose_current_market(
                    markets,
                    now=now_utc,
                    max_start_price_lag_ms=engine.config.sources.max_start_price_lag_ms,
                )
            if market and (market.condition_id != last_market_id or market.threshold_price is None):
                last_market_id = market.condition_id
                try:
                    await apply_polymarket_page_threshold(client, market, engine.config.sources.threshold_page_timeout_seconds)
                except Exception as exc:
                    await queue.put(("error", {"source": "polymarket_page", "error": str(exc)}))
                next_threshold_retry = now_utc + timedelta(seconds=engine.config.sources.threshold_page_retry_seconds)
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


async def polymarket_price_loop(client: PolymarketClient, queue: asyncio.Queue) -> None:
    while True:
        try:
            async for tick in client.rtds_crypto_price_ticks("btc/usd"):
                await queue.put(("polymarket_tick", tick))
        except Exception as exc:
            await queue.put(("error", {"source": "polymarket_rtds", "error": str(exc)}))
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


def books_need_rest_refresh(
    engine: PaperEngine,
    market: MarketState,
    now: datetime,
    last_rest_refresh_at: datetime | None = None,
) -> bool:
    # Delayed WebSocket frames can keep received_at fresh while carrying an
    # older price.  Periodically reconcile against a complete REST snapshot
    # even while the WebSocket appears active.
    if last_rest_refresh_at is not None and now - last_rest_refresh_at >= BOOK_REST_FALLBACK_AFTER:
        return True
    for direction in (Direction.UP, Direction.DOWN):
        book = engine.books.get(direction)
        if not book or not book_matches_market(market, direction, book):
            return True
        if now - book.received_at >= BOOK_REST_FALLBACK_AFTER:
            return True
    return False


async def book_rest_loop(client: PolymarketClient, engine: PaperEngine, queue: asyncio.Queue, poll_ms: int) -> None:
    check_interval_seconds = max(poll_ms / 1000, 0.1)
    last_rest_refresh_at = datetime.min.replace(tzinfo=timezone.utc)
    while True:
        started_at = datetime.now(timezone.utc)
        market = engine.market
        if not market:
            await asyncio.sleep(0.2)
            continue
        if market.end_time <= started_at:
            await asyncio.sleep(0.05)
            continue
        if books_need_rest_refresh(engine, market, started_at, last_rest_refresh_at):
            try:
                await emit_rest_books(client, market, queue)
            except Exception as exc:
                await queue.put(("error", {"source": "clob_rest", "error": str(exc)}))
            finally:
                # Schedule from the request start, not its completion.  The
                # proxy round trip can take ~500 ms; completion-based timing
                # added another full fallback interval between snapshots.
                last_rest_refresh_at = started_at
        elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
        await asyncio.sleep(max(0.02, check_interval_seconds - elapsed))


async def book_loop(client: PolymarketClient, engine: PaperEngine, queue: asyncio.Queue, poll_ms: int) -> None:
    timeout_seconds = max(poll_ms / 1000, 0.1)
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
        try:
            next_book_task = asyncio.create_task(stream.__anext__())

            while True:
                current_market = engine.market
                now = datetime.now(timezone.utc)
                if not current_market or current_market.condition_id != current_market_id or current_market.end_time <= now:
                    break
                try:
                    done, _ = await asyncio.wait({next_book_task}, timeout=timeout_seconds)
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
            await stream.aclose()
        await asyncio.sleep(0.05)


def flush_engine_updates(engine: PaperEngine, journal: RunJournal, counters: dict[str, int]) -> list[tuple[str, Any]]:
    live_events: list[tuple[str, Any]] = []
    while counters["signals"] < len(engine.signals):
        signal = engine.signals[counters["signals"]]
        journal.signal(signal)
        live_events.append(("signal", signal))
        counters["signals"] += 1
    while counters["fills"] < len(engine.fills):
        fill = engine.fills[counters["fills"]]
        journal.fill(fill)
        live_events.append(("fill", fill))
        counters["fills"] += 1
    while counters["exits"] < len(engine.exit_events):
        exit_event = engine.exit_events[counters["exits"]]
        journal.exit_event(exit_event)
        live_events.append(("exit", exit_event))
        counters["exits"] += 1
    return live_events


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
        if event_type == "polymarket_tick":
            buffered[("polymarket_tick", "latest")] = (index, event)
            continue
        if event_type == "book" and isinstance(payload, tuple) and payload:
            direction = payload[0]
            direction_key = direction.value if isinstance(direction, Direction) else str(direction)
            key = ("book", direction_key)
            previous = buffered.get(key)
            if previous:
                previous_payload = previous[1][1]
                previous_book = previous_payload[1] if isinstance(previous_payload, tuple) and len(previous_payload) > 1 else None
                current_book = payload[1] if len(payload) > 1 else None
                if (
                    isinstance(previous_book, OrderBookSnapshot)
                    and isinstance(current_book, OrderBookSnapshot)
                ):
                    request_started_at = rest_request_started_at(current_book)
                    if (
                        request_started_at is not None
                        and current_book.timestamp <= previous_book.timestamp
                        and previous_book.received_at > request_started_at
                    ):
                        continue
                    if current_book.timestamp < previous_book.timestamp:
                        is_fresh_rest_fallback = (
                            isinstance(current_book.raw, dict)
                            and current_book.raw.get("_transport") == "rest"
                            and current_book.received_at > previous_book.received_at
                        )
                        if not is_fresh_rest_fallback:
                            continue
                        current_book.timestamp = previous_book.timestamp
                    elif (
                        current_book.timestamp == previous_book.timestamp
                        and current_book.received_at < previous_book.received_at
                    ):
                        continue
            buffered[key] = (index, event)
            continue
        flush_buffered()
        result.append(event)
    flush_buffered()
    return result


def should_publish_book_update(
    previous: OrderBookSnapshot | None,
    current: OrderBookSnapshot,
    last_published_at: datetime | None,
) -> bool:
    if previous is None:
        return True
    if (
        previous.token_id != current.token_id
        or previous.market_id != current.market_id
        or previous.best_bid != current.best_bid
        or previous.best_ask != current.best_ask
    ):
        return True
    return last_published_at is None or current.received_at - last_published_at >= BOOK_PUBLISH_HEARTBEAT


async def run_live(config: AppConfig, max_seconds: int | None = None, on_update: UpdateCallback | None = None) -> Path:
    output_dir = run_dir(config.data_dir)
    journal = RunJournal(output_dir)
    engine = PaperEngine(config)
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
    poly = PolymarketClient(config.sources)
    binance = BinanceClient(config.sources)
    counters = {"signals": 0, "fills": 0, "exits": 0}
    last_published_book_at: dict[Direction, datetime] = {}

    await initialize_current_market(poly, engine, journal, output_dir, on_update)

    tasks = [
        asyncio.create_task(data_cleanup_loop(config, output_dir, journal)),
        asyncio.create_task(market_loop(poly, engine, queue, config.sources.market_refresh_seconds)),
        asyncio.create_task(binance_loop(binance, queue)),
        asyncio.create_task(polymarket_price_loop(poly, queue)),
        asyncio.create_task(book_rest_loop(poly, engine, queue, config.sources.poly_book_poll_ms)),
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
            # Give simultaneous market frames a tiny window to accumulate so
            # hundreds of depth-only deltas collapse to the newest complete
            # UP/DOWN snapshots.  This bounds added latency at 20 ms.
            await asyncio.sleep(LIVE_EVENT_COALESCE_SECONDS)
            while not queue.empty() and len(pending_events) < 500:
                pending_events.append(queue.get_nowait())
            for event_type, payload in coalesce_live_events(pending_events):
                publish_update = True
                if event_type == "market":
                    market: MarketState = payload
                    engine.set_market(market)
                    journal.market(market)
                elif event_type == "tick":
                    tick: PriceTick = payload
                    engine.set_tick(tick)
                    journal.tick(tick)
                elif event_type == "polymarket_tick":
                    tick = payload
                    assert isinstance(tick, PriceTick)
                    engine.set_polymarket_tick(tick)
                    journal.event("polymarket_tick", tick)
                elif event_type == "book":
                    direction, book = payload
                    assert isinstance(book, OrderBookSnapshot)
                    previous_book = engine.books.get(direction)
                    engine.set_book(direction, book)
                    active_book = engine.books.get(direction)
                    publish_update = active_book is book and should_publish_book_update(
                        previous_book,
                        book,
                        last_published_book_at.get(direction),
                    )
                    if publish_update:
                        journal.book(direction.value, book)
                        last_published_book_at[direction] = book.received_at
                elif event_type == "error":
                    journal.latency_row(payload.get("source", "unknown"), "stream", False, None, payload.get("error", ""))
                live_events = flush_engine_updates(engine, journal, counters)
                for live_event_type, live_payload in live_events:
                    await emit_update(on_update, live_snapshot(engine, output_dir, live_event_type, live_payload))
                if publish_update:
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
