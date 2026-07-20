from __future__ import annotations

import asyncio
import math
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from .clients import BinanceClient, PolymarketClient
from .config import AppConfig
from .engine import PaperEngine
from .entry_registry import SqliteMarketEntryRegistry, historical_market_entry_counts
from .journal import RunJournal
from .market import (
    choose_current_market,
    market_interval_from_slug,
    market_is_active,
    markets_are_adjacent,
    threshold_is_tradable,
)
from .models import Direction, MarketState, OrderBookSnapshot, PriceTick, rest_request_started_at
from .pair_match import PairMatchEngine, PairMatchRegistry


UpdateCallback = Callable[[dict[str, Any]], Awaitable[None] | None]
BINANCE_TICK_EMIT_INTERVAL = timedelta(milliseconds=200)
# Keep a sub-second REST safety net when the CLOB WebSocket is quiet.  The
# strategy freshness limit is one second, so a two-second fallback was too slow.
BOOK_REST_FALLBACK_AFTER = timedelta(milliseconds=500)
BOOK_REST_RECONCILE_AFTER = timedelta(seconds=2)
LIVE_EVENT_COALESCE_SECONDS = 0.02
BOOK_PUBLISH_HEARTBEAT = timedelta(milliseconds=250)
THRESHOLD_MATCH_TOLERANCE_USD = 0.01
THRESHOLD_FINALIZATION_DELAY = timedelta(seconds=1)


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
    if not config.data_cleanup_enabled:
        return
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


def live_book_payload(book: OrderBookSnapshot) -> dict[str, Any]:
    best_bid = max(book.bids, key=lambda level: level.price, default=None)
    best_ask = min(book.asks, key=lambda level: level.price, default=None)
    return {
        "token_id": book.token_id,
        "market_id": book.market_id,
        "timestamp": book.timestamp.isoformat(),
        "received_at": book.received_at.isoformat(),
        "bids": [best_bid.model_dump(mode="json")] if best_bid is not None else [],
        "asks": [best_ask.model_dump(mode="json")] if best_ask is not None else [],
        "depth_trusted": book.depth_trusted,
        "min_order_size": book.min_order_size,
        "tick_size": book.tick_size,
    }


def live_snapshot(
    engine: PaperEngine,
    output_dir: Path,
    event_type: str,
    payload: Any,
    pair_match: dict[str, Any] | None = None,
) -> dict[str, Any]:
    books = {direction.value: live_book_payload(book) for direction, book in engine.books.items()}
    event_payload: Any
    if event_type == "book" and isinstance(payload, tuple):
        direction, book = payload
        event_payload = {"direction": direction.value, **live_book_payload(book)}
    else:
        event_payload = model_payload(payload)
    return {
        "type": "snapshot",
        "asset": engine.asset,
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
            "edge_correction_usd": engine.edge_correction_usd(datetime.now(timezone.utc)),
            "edge_correction_source": engine.edge_correction_source(datetime.now(timezone.utc)),
        },
        "last_rejection": engine.rejections[-1] if engine.rejections else None,
        "pair_match": pair_match or {},
    }


async def emit_update(callback: UpdateCallback | None, snapshot: dict[str, Any]) -> None:
    if callback is None:
        return
    result = callback(snapshot)
    if result is not None:
        await result


async def check_connectivity(config: AppConfig) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for asset in config.sources.enabled_assets:
        binance = BinanceClient(config.sources, asset)
        polymarket = PolymarketClient(config.sources, asset)
        asset_result: dict[str, Any] = {}
        try:
            asset_result["binance_time"] = await binance.server_time()
        except Exception as exc:  # pragma: no cover - network dependent
            asset_result["binance_time"] = {"ok": False, "error": str(exc)}
        try:
            markets = await polymarket.discover_markets()
            current = choose_current_market(markets, max_start_price_lag_ms=config.sources.max_start_price_lag_ms)
            asset_result["gamma"] = {
                "ok": True,
                "market_count": len(markets),
                "current_market": current.model_dump(mode="json") if current else None,
            }
        except Exception as exc:  # pragma: no cover - network dependent
            current = None
            asset_result["gamma"] = {"ok": False, "error": str(exc)}
        if current:
            try:
                asset_result["clob"] = await polymarket.price_probe(current.up_token_id)
            except Exception as exc:  # pragma: no cover - network dependent
                asset_result["clob"] = {"ok": False, "error": str(exc)}
        else:
            asset_result["clob"] = {"ok": False, "error": f"no candidate {asset} 5 minute market discovered"}
        asset_result["ok"] = all(
            bool(asset_result.get(source, {}).get("ok"))
            for source in ("binance_time", "gamma", "clob")
        )
        result[asset] = asset_result
    return result


def should_keep_current_market(engine: PaperEngine, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    market = engine.market
    return bool(market and market_is_active(market, now))


def should_retry_threshold(now: datetime, next_retry_at: datetime) -> bool:
    return now >= next_retry_at


async def apply_polymarket_page_threshold(
    client: PolymarketClient,
    market: MarketState,
    timeout_seconds: float = 4.0,
    now: datetime | None = None,
) -> bool:
    previous_state = (market.threshold_price, market.threshold_source, market.threshold_verified)

    def state_changed() -> bool:
        return previous_state != (market.threshold_price, market.threshold_source, market.threshold_verified)

    if market.start_time is None:
        return False
    checked_at = now or datetime.now(timezone.utc)
    if checked_at < market.start_time:
        market.threshold_price = None
        market.threshold_source = "dynamic_start_price"
        market.threshold_observed_at = None
        market.threshold_verified = False
        market.threshold_fetched_at = None
        return state_changed()
    if checked_at < market.start_time + THRESHOLD_FINALIZATION_DELAY:
        market.threshold_price = None
        market.threshold_source = "threshold_verification_pending"
        market.threshold_observed_at = None
        market.threshold_verified = False
        market.threshold_fetched_at = None
        return state_changed()
    if checked_at >= market.end_time:
        market.threshold_price = None
        market.threshold_source = "threshold_verification_expired"
        market.threshold_observed_at = None
        market.threshold_verified = False
        market.threshold_fetched_at = checked_at
        return state_changed()
    if threshold_is_tradable(market):
        return False

    # Clear every unverified/provisional value before network I/O.  If any
    # source fails or disagrees, the strategy sees no tradable threshold.
    market.threshold_price = None
    market.threshold_source = "threshold_verification_pending"
    market.threshold_observed_at = None
    market.threshold_verified = False
    market.threshold_fetched_at = None

    event_threshold = getattr(client, "event_threshold", None)
    gamma_threshold: float | None = None
    if event_threshold is not None:
        try:
            gamma_threshold = await asyncio.wait_for(event_threshold(market.slug), timeout=timeout_seconds)
        except Exception:
            gamma_threshold = None
    page_data = getattr(client, "market_page_data", None)
    try:
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
    except Exception:
        outcome_price, results = None, []

    completed_at = now or datetime.now(timezone.utc)
    market.threshold_fetched_at = completed_at
    interval = market_interval_from_slug(market.slug)
    exact_previous = [
        result
        for result in results
        if result.start_time == market.start_time - timedelta(minutes=5)
        and result.end_time == market.start_time
        and result.end_time - result.start_time == timedelta(minutes=5)
    ]
    valid_outcome = bool(
        outcome_price is not None
        and outcome_price.slug == market.slug
        and outcome_price.start_time == market.start_time
        and outcome_price.end_time == market.end_time
    )
    previous_closes = [result.close_price for result in exact_previous]
    previous_is_consistent = bool(
        previous_closes
        and max(previous_closes) - min(previous_closes) <= THRESHOLD_MATCH_TOLERANCE_USD
    )
    interval_is_exact = bool(interval and interval == (market.start_time, market.end_time))
    market_still_active = market_is_active(market, completed_at)

    if not (valid_outcome and interval_is_exact and market_still_active):
        market.threshold_source = "threshold_verification_failed"
        return state_changed()

    assert outcome_price is not None
    candidate_fields_present = any(
        value is not None
        for value in (
            market.threshold_candidate_price,
            market.threshold_candidate_source,
            market.threshold_candidate_observed_at,
            market.threshold_candidate_received_at,
        )
    )
    candidate_is_exact = bool(
        not market.threshold_candidate_conflicted
        and market.threshold_candidate_price is not None
        and math.isfinite(market.threshold_candidate_price)
        and market.threshold_candidate_source == "polymarket_rtds_start_tick"
        and market.threshold_candidate_observed_at == market.start_time
        and market.threshold_candidate_received_at is not None
        and market.start_time - timedelta(seconds=1)
        <= market.threshold_candidate_received_at
        <= market.start_time + timedelta(seconds=2)
    )
    if market.threshold_candidate_conflicted or (candidate_fields_present and not candidate_is_exact):
        market.threshold_source = "threshold_verification_failed"
        return state_changed()
    candidate_matches = bool(
        candidate_is_exact
        and abs((market.threshold_candidate_price or 0.0) - outcome_price.open_price)
        <= THRESHOLD_MATCH_TOLERANCE_USD
    )
    if candidate_is_exact and not candidate_matches:
        market.threshold_source = "threshold_verification_failed"
        return state_changed()

    previous_matches = bool(
        previous_is_consistent
        and abs(previous_closes[-1] - outcome_price.open_price) <= THRESHOLD_MATCH_TOLERANCE_USD
    )
    if previous_is_consistent and not previous_matches:
        market.threshold_source = "threshold_verification_failed"
        return state_changed()
    if not (candidate_matches or previous_matches):
        market.threshold_source = "threshold_verification_failed"
        return state_changed()
    if gamma_threshold is not None and abs(gamma_threshold - outcome_price.open_price) > THRESHOLD_MATCH_TOLERANCE_USD:
        market.threshold_source = "threshold_verification_failed"
        return state_changed()

    market.threshold_price = outcome_price.open_price
    if candidate_matches:
        market.threshold_source = "polymarket_page_rtds_verified_open_price"
    elif gamma_threshold is not None:
        market.threshold_source = "gamma_page_verified_price_to_beat"
    else:
        market.threshold_source = "polymarket_page_verified_open_price"
    market.threshold_observed_at = market.start_time
    market.threshold_verified = True
    return state_changed()


async def prefetch_next_market_threshold(
    client: PolymarketClient,
    current_market: MarketState,
    config: AppConfig,
) -> MarketState | None:
    """Fetch only the next adjacent market's metadata before it starts."""
    _ = config
    markets = await client.discover_markets()
    candidates = [market for market in markets if markets_are_adjacent(current_market, market)]
    if not candidates:
        return None
    next_market = min(candidates, key=lambda market: market.end_time)
    next_market.threshold_price = None
    next_market.threshold_source = "dynamic_start_price"
    next_market.threshold_observed_at = None
    next_market.threshold_verified = False
    next_market.threshold_fetched_at = None
    return next_market


async def current_market_with_page_threshold(
    client: PolymarketClient,
    max_start_price_lag_ms: int,
    now: datetime | None = None,
) -> MarketState | None:
    markets = await client.discover_markets()
    selection_now = now or datetime.now(timezone.utc)
    market = choose_current_market(markets, now=selection_now, max_start_price_lag_ms=max_start_price_lag_ms)
    if market and not threshold_is_tradable(market):
        try:
            await apply_polymarket_page_threshold(client, market, now=now)
        except Exception:
            pass
    if market and not market_is_active(market, now or datetime.now(timezone.utc)):
        return None
    return market


async def initialize_current_market(
    client: PolymarketClient,
    engine: PaperEngine,
    journal: RunJournal,
    output_dir: Path,
    on_update: UpdateCallback | None,
) -> None:
    try:
        markets = await asyncio.wait_for(client.discover_markets(), timeout=5)
        now = datetime.now(timezone.utc)
        market = choose_current_market(
            markets,
            now=now,
            max_start_price_lag_ms=engine.config.sources.max_start_price_lag_ms,
        )
        if market and not threshold_is_tradable(market):
            try:
                await apply_polymarket_page_threshold(client, market, engine.config.sources.threshold_page_timeout_seconds)
            except Exception as exc:
                journal.latency_row("polymarket_page", "initial_threshold", False, None, str(exc))
    except Exception as exc:
        journal.latency_row("gamma", "initial_market", False, None, str(exc))
        return
    if market is None or not market_is_active(market, datetime.now(timezone.utc)):
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
                and market_is_active(current_market, now_utc)
                and not threshold_is_tradable(current_market)
            )
            if needs_page_threshold and should_retry_threshold(now_utc, next_threshold_retry):
                next_threshold_retry = now_utc + timedelta(seconds=engine.config.sources.threshold_page_retry_seconds)
                try:
                    target_market_id = current_market.condition_id
                    if await apply_polymarket_page_threshold(client, current_market, engine.config.sources.threshold_page_timeout_seconds):
                        fresh_now = datetime.now(timezone.utc)
                        if (
                            engine.market is None
                            or engine.market.condition_id != target_market_id
                            or not market_is_active(current_market, fresh_now)
                        ):
                            await asyncio.sleep(interval_seconds)
                            continue
                        await queue.put(("market", current_market))
                        await asyncio.sleep(interval_seconds)
                        continue
                except Exception as exc:
                    await queue.put(("error", {"source": "polymarket_page", "error": str(exc)}))
            if should_keep_current_market(engine, now=now_utc):
                if prefetched_for_market_id != engine.market.condition_id and now_utc >= next_prefetch_attempt:
                    try:
                        prefetched_market = await prefetch_next_market_threshold(client, engine.market, engine.config)
                        if prefetched_market is not None:
                            prefetched_for_market_id = engine.market.condition_id
                        else:
                            next_prefetch_attempt = now_utc + timedelta(seconds=5)
                    except Exception as exc:
                        next_prefetch_attempt = now_utc + timedelta(seconds=5)
                        await queue.put(("error", {"source": "polymarket_page_prefetch", "error": str(exc)}))
                await asyncio.sleep(interval_seconds)
                continue
            market = prefetched_market if prefetched_market and market_is_active(prefetched_market, now_utc) else None
            prefetched_market = None
            if market is None:
                markets = await client.discover_markets()
                now_utc = datetime.now(timezone.utc)
                market = choose_current_market(
                    markets,
                    now=now_utc,
                    max_start_price_lag_ms=engine.config.sources.max_start_price_lag_ms,
                )
            if market and (market.condition_id != last_market_id or not threshold_is_tradable(market)):
                last_market_id = market.condition_id
                try:
                    await apply_polymarket_page_threshold(client, market, engine.config.sources.threshold_page_timeout_seconds)
                except Exception as exc:
                    await queue.put(("error", {"source": "polymarket_page", "error": str(exc)}))
                fresh_now = datetime.now(timezone.utc)
                next_threshold_retry = fresh_now + timedelta(seconds=engine.config.sources.threshold_page_retry_seconds)
                if market_is_active(market, fresh_now):
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
            async for tick in client.rtds_crypto_price_ticks():
                await queue.put(("polymarket_tick", tick))
        except Exception as exc:
            await queue.put(("error", {"source": "polymarket_rtds", "error": str(exc)}))
            await asyncio.sleep(1)


async def pair_resolution_loop(
    btc_client: PolymarketClient,
    eth_client: PolymarketClient,
    pair_engine: PairMatchEngine,
    queue: asyncio.Queue[tuple[str, str, Any]],
) -> None:
    while True:
        for btc_slug, eth_slug in pair_engine.pending_market_pairs():
            try:
                btc_outcome, eth_outcome = await asyncio.gather(
                    btc_client.resolved_outcome(btc_slug),
                    eth_client.resolved_outcome(eth_slug),
                )
                if btc_outcome is not None and eth_outcome is not None:
                    await queue.put(("PAIR", "pair_resolution", (btc_slug, eth_slug, btc_outcome, eth_outcome)))
            except Exception as exc:
                await queue.put(("PAIR", "pair_error", {"source": "gamma_resolution", "error": str(exc)}))
        await asyncio.sleep(2)


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
    for direction in (Direction.UP, Direction.DOWN):
        book = engine.books.get(direction)
        if not book or not book_matches_market(market, direction, book):
            return True
        if not book.depth_trusted:
            return True
        if now - book.received_at >= BOOK_REST_FALLBACK_AFTER:
            return True
    return bool(
        last_rest_refresh_at is not None
        and now - last_rest_refresh_at >= BOOK_REST_RECONCILE_AFTER
    )


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


class AssetEventQueue:
    def __init__(self, queue: asyncio.Queue[tuple[str, str, Any]], asset: str):
        self.queue = queue
        self.asset = asset

    async def put(self, event: tuple[str, Any]) -> None:
        event_type, payload = event
        await self.queue.put((self.asset, event_type, payload))


def aggregate_engine_summaries(engines: dict[str, PaperEngine]) -> dict[str, float | int]:
    summaries = [engine.summary() for engine in engines.values()]
    additive = {
        "total_positions",
        "closed_positions",
        "open_positions",
        "fills",
        "signals",
        "rejections",
        "realized_pnl",
        "normal_realized_pnl",
        "reverse_realized_pnl",
        "take_profit_pnl",
        "risk_exit_pnl",
        "settlement_pnl",
        "total_quote",
        "fees_paid_usd",
        "max_loss_exit_count",
        "entry_confirmation_updates",
        "current_market_trade_count",
    }
    combined = {key: sum(summary.get(key, 0) for summary in summaries) for key in additive}
    if engines:
        config = next(iter(engines.values())).config
        combined["max_trades_per_market"] = config.risk.max_trades_per_market
        combined["max_loss_usd"] = config.risk.max_loss_usd
    return combined


async def run_live(config: AppConfig, max_seconds: int | None = None, on_update: UpdateCallback | None = None) -> Path:
    output_dir = run_dir(config.data_dir)
    journal = RunJournal(output_dir)
    entry_registry = SqliteMarketEntryRegistry(config.data_dir / "market-entry-ledger.sqlite3")
    pair_registry = PairMatchRegistry(config.data_dir / "pair-match-ledger.sqlite3")
    clients: dict[str, PolymarketClient] = {}
    try:
        entry_registry.seed(historical_market_entry_counts(config.data_dir))
        assets = config.sources.enabled_assets
        engines = {
            asset: PaperEngine(config, entry_registry=entry_registry, run_id=output_dir.name, asset=asset)
            for asset in assets
        }
        queue: asyncio.Queue[tuple[str, str, Any]] = asyncio.Queue()
        clients = {asset: PolymarketClient(config.sources, asset) for asset in assets}
        binance_clients = {asset: BinanceClient(config.sources, asset) for asset in assets}
        counters = {asset: {"signals": 0, "fills": 0, "exits": 0} for asset in assets}
        last_published_book_at: dict[tuple[str, Direction], datetime] = {}
        pair_engine = PairMatchEngine(config, pair_registry)
        await asyncio.gather(
            *(
                initialize_current_market(clients[asset], engines[asset], journal, output_dir, on_update)
                for asset in assets
            )
        )
    except BaseException:
        try:
            await asyncio.wait_for(
                asyncio.gather(*(client.aclose() for client in clients.values()), return_exceptions=True),
                timeout=5,
            )
        except asyncio.TimeoutError:
            pass
        entry_registry.close()
        pair_registry.close()
        raise

    tasks = [asyncio.create_task(data_cleanup_loop(config, output_dir, journal))]
    for asset in assets:
        asset_queue = AssetEventQueue(queue, asset)
        engine = engines[asset]
        poly = clients[asset]
        tasks.extend(
            [
                asyncio.create_task(market_loop(poly, engine, asset_queue, config.sources.market_refresh_seconds)),
                asyncio.create_task(binance_loop(binance_clients[asset], asset_queue)),
                asyncio.create_task(polymarket_price_loop(poly, asset_queue)),
                asyncio.create_task(book_rest_loop(poly, engine, asset_queue, config.sources.poly_book_poll_ms)),
                asyncio.create_task(book_loop(poly, engine, asset_queue, config.sources.poly_book_poll_ms)),
            ]
        )
    if "BTC" in clients and "ETH" in clients:
        tasks.append(
            asyncio.create_task(pair_resolution_loop(clients["BTC"], clients["ETH"], pair_engine, queue))
        )
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
            pair_input_changed = False
            for asset in assets:
                asset_events = [(event_type, payload) for event_asset, event_type, payload in pending_events if event_asset == asset]
                if not asset_events:
                    continue
                engine = engines[asset]
                for event_type, payload in coalesce_live_events(asset_events):
                    engine.entry_enabled = not config.pair_match.enabled
                    publish_update = True
                    if event_type == "market":
                        pair_input_changed = True
                        market: MarketState = payload
                        if not market_is_active(market, datetime.now(timezone.utc)):
                            continue
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
                        pair_input_changed = True
                        direction, book = payload
                        assert isinstance(book, OrderBookSnapshot)
                        previous_book = engine.books.get(direction)
                        engine.set_book(direction, book)
                        active_book = engine.books.get(direction)
                        publish_update = active_book is book and should_publish_book_update(
                            previous_book,
                            book,
                            last_published_book_at.get((asset, direction)),
                        )
                        if publish_update:
                            journal.book(direction.value, book)
                            last_published_book_at[(asset, direction)] = book.received_at
                    elif event_type == "error":
                        source = f"{asset.lower()}_{payload.get('source', 'unknown')}"
                        journal.latency_row(source, "stream", False, None, payload.get("error", ""))
                    live_events = flush_engine_updates(engine, journal, counters[asset])
                    for live_event_type, live_payload in live_events:
                        await emit_update(
                            on_update,
                            live_snapshot(
                                engine, output_dir, live_event_type, live_payload, pair_engine.dashboard_state()
                            ),
                        )
                    if publish_update:
                        await emit_update(
                            on_update,
                            live_snapshot(engine, output_dir, event_type, payload, pair_engine.dashboard_state()),
                        )

            primary_engine = engines.get("BTC") or next(iter(engines.values()))
            pair_events = [
                (event_type, payload)
                for event_asset, event_type, payload in pending_events
                if event_asset == "PAIR"
            ]
            for event_type, payload in pair_events:
                if event_type == "pair_resolution":
                    btc_slug, eth_slug, btc_outcome, eth_outcome = payload
                    settled = pair_engine.settle(btc_slug, eth_slug, btc_outcome, eth_outcome)
                    for order in settled:
                        journal.pair_result(order)
                        await emit_update(
                            on_update,
                            live_snapshot(
                                primary_engine,
                                output_dir,
                                "pair_settlement",
                                order,
                                pair_engine.dashboard_state(),
                            ),
                        )
                    if settled:
                        matching_summary = next(
                            (
                                summary
                                for summary in pair_engine.dashboard_state()["recent_markets"]
                                if summary["btc_slug"] == btc_slug and summary["eth_slug"] == eth_slug
                            ),
                            None,
                        )
                        if matching_summary:
                            journal.pair_market(matching_summary)
                elif event_type == "pair_error":
                    journal.latency_row(
                        payload.get("source", "pair_match"),
                        "resolution",
                        False,
                        None,
                        payload.get("error", ""),
                    )

            for engine in engines.values():
                engine.entry_enabled = not config.pair_match.enabled
            if pair_input_changed:
                order = pair_engine.evaluate(engines, datetime.now(timezone.utc))
                if order is not None:
                    journal.pair_order(order)
                    event_type: str = "pair_order"
                    event_payload: Any = order
                else:
                    event_type = "pair_state"
                    event_payload = pair_engine.dashboard_state()
                await emit_update(
                    on_update,
                    live_snapshot(
                        primary_engine,
                        output_dir,
                        event_type,
                        event_payload,
                        pair_engine.dashboard_state(),
                    ),
                )
    finally:
        try:
            for task in tasks:
                task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=5,
                )
            except asyncio.TimeoutError:
                pass
            for asset, engine in engines.items():
                for position in engine.positions:
                    journal.position(position)
                await emit_update(
                    on_update,
                    live_snapshot(engine, output_dir, "summary", engine.summary(), pair_engine.dashboard_state()),
                )
            summary = aggregate_engine_summaries(engines)
            pair_summary = pair_engine.dashboard_state()["summary"]
            summary.update(
                {
                    "pair_orders": pair_summary["orders"],
                    "pair_pending_orders": pair_summary["pending_orders"],
                    "pair_realized_pnl": pair_summary["realized_pnl"],
                }
            )
            journal.summary(summary)
        finally:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(client.aclose() for client in clients.values()), return_exceptions=True),
                    timeout=5,
                )
            except asyncio.TimeoutError:
                pass
            entry_registry.close()
            pair_registry.close()
    return output_dir
