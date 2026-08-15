from __future__ import annotations

import asyncio
import math
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from queue import Empty as ThreadQueueEmpty
from queue import Queue as ThreadQueue
from typing import Any, Awaitable, Callable

from .btc_recovery import BtcRecoveryEngine, BtcRecoveryRegistry
from .btc_dynamic import BtcDynamicEngine, BtcDynamicRegistry
from .btc_lead_prediction import BtcLeadPredictionEngine, BtcLeadPredictionRegistry
from .btc_maker_arbitrage import BtcMakerArbitrageEngine, BtcMakerArbitrageRegistry
from .btc_weighted import BtcWeightedEngine, BtcWeightedRegistry
from .btc_v8 import BtcV8Engine, BtcV8Registry, V8Trade
from .clients import BinanceClient, PolymarketClient
from .config import AppConfig
from .engine import PaperEngine
from .entry_registry import SqliteMarketEntryRegistry, historical_market_entry_counts
from .journal import RunJournal
from .market import (
    TWAP_PAGE_VERIFIED_THRESHOLD_SOURCE,
    choose_current_market,
    market_interval_from_slug,
    market_is_active,
    markets_are_adjacent,
    supported_market_twap_lookback_seconds,
    threshold_is_tradable,
    threshold_needs_page_confirmation,
    twap_rtds_candidate_source,
)
from .models import Direction, MarketState, OrderBookSnapshot, PriceTick, rest_request_started_at
from .orderbook_chase import OrderbookChaseEngine, OrderbookChaseRegistry
from .pair_match import PairMatchEngine, PairMatchRegistry
from .real_trading import (
    RealTradingCredentials,
    RealTradingEngine,
    RealTradingRegistry,
)
from .signal_sources import (
    BinanceFuturesSignalClient,
    BinanceSpotSignalClient,
    CoinbaseSignalClient,
    KrakenSignalClient,
    SignalEvent,
)


UpdateCallback = Callable[[dict[str, Any]], Awaitable[None] | None]
BeforeMarketUpdatesCallback = Callable[[dict[str, MarketState]], bool | None]
BINANCE_TICK_EMIT_INTERVAL = timedelta(milliseconds=200)
# The CLOB WebSocket is the primary order-book source.  REST is only a safety
# net after a genuinely quiet stream and a slower periodic depth reconciliation.
BOOK_REST_FALLBACK_AFTER = timedelta(seconds=1)
BOOK_REST_RECONCILE_AFTER = timedelta(seconds=5)
CLOB_WS_INITIAL_DATA_TIMEOUT_SECONDS = 6.0
CLOB_WS_DATA_STALE_SECONDS = 3.0
LIVE_EVENT_COALESCE_SECONDS = 0.02
BOOK_PUBLISH_HEARTBEAT = timedelta(milliseconds=250)
MAKER_STATE_HEARTBEAT = timedelta(milliseconds=250)
THRESHOLD_MATCH_TOLERANCE_USD = 0.01
THRESHOLD_FINALIZATION_DELAY = timedelta(milliseconds=250)
TWAP_SUBSCRIPTION_RECHECK_SECONDS = 0.05


def standard_entry_enabled(config: AppConfig, asset: str) -> bool:
    if config.btc_v8.enabled or config.btc_lead_prediction.enabled:
        return False
    return not (
        config.pair_match.enabled
        or config.btc_recovery.enabled
        or (config.btc_dynamic.enabled and asset.upper() == "BTC")
    )


def btc_v8_signal_source_enabled(config: AppConfig, source: str) -> bool:
    if not config.btc_v8.enabled:
        return False
    return source != "binance_futures"


def btc_signal_source_enabled(config: AppConfig, source: str) -> bool:
    if btc_v8_signal_source_enabled(config, source):
        return True
    settings = config.btc_lead_prediction
    if not settings.enabled:
        return False
    if source == "binance_futures":
        return settings.futures_diagnostics_enabled
    return source in settings.spot_sources


def btc_dynamic_runtime_enabled(config: AppConfig) -> bool:
    return bool(config.btc_dynamic.enabled)


def btc_v8_runtime_enabled(config: AppConfig) -> bool:
    return bool(config.btc_v8.enabled)


def btc_lead_prediction_runtime_enabled(config: AppConfig) -> bool:
    return bool(config.btc_lead_prediction.enabled)


def btc_maker_arbitrage_runtime_enabled(config: AppConfig) -> bool:
    return bool(config.btc_maker_arbitrage.enabled)


def orderbook_chase_runtime_enabled(config: AppConfig) -> bool:
    return bool(config.orderbook_chase.enabled)


def real_trading_runtime_enabled(config: AppConfig) -> bool:
    return bool(config.real_trading.enabled)


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


async def btc_v8_data_cleanup_loop(
    config: AppConfig,
    registry: BtcV8Registry,
    journal: RunJournal,
    settings_name: str = "btc_v8",
) -> None:
    if not config.data_cleanup_enabled:
        return
    snapshot_batch_size = 2_000
    raw_batch_size = 5_000
    while True:
        try:
            settings = getattr(config, settings_name)
            retention_hours = (
                config.data_retention_hours
                if settings_name == "btc_v8"
                else settings.snapshot_retention_hours
            )
            total_removed = 0
            while True:
                removed = registry.cleanup_expired_snapshots(
                    retention_hours,
                    datetime.now(timezone.utc),
                    batch_size=snapshot_batch_size,
                )
                total_removed += removed
                if removed < snapshot_batch_size:
                    break
                await asyncio.sleep(0)
            raw_retention_hours = 0.0 if settings_name == "btc_v8" else settings.raw_retention_hours
            while True:
                removed = registry.cleanup_expired_raw_events(
                    raw_retention_hours,
                    datetime.now(timezone.utc),
                    batch_size=raw_batch_size,
                )
                total_removed += removed
                if removed < raw_batch_size:
                    break
                await asyncio.sleep(0)
            if total_removed:
                registry.checkpoint_wal()
        except sqlite3.Error as exc:
            journal.latency_row(
                f"{settings_name}_cleanup",
                "remove_expired_v8_data",
                False,
                None,
                str(exc),
            )
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
    btc_recovery: dict[str, Any] | None = None,
    real_trading: dict[str, Any] | None = None,
    btc_dynamic: dict[str, Any] | None = None,
    btc_v8: dict[str, Any] | None = None,
    orderbook_chase: dict[str, Any] | None = None,
    btc_weighted: dict[str, Any] | None = None,
    btc_lead_prediction: dict[str, Any] | None = None,
    btc_maker_arbitrage: dict[str, Any] | None = None,
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
        "polymarket_twap_tick": (
            engine.polymarket_twap_tick.model_dump(mode="json") if engine.polymarket_twap_tick else None
        ),
        "settlement_tick": (
            engine.settlement_price_tick().model_dump(mode="json") if engine.settlement_price_tick() else None
        ),
        "books": books,
        "open_position": engine.open_position.model_dump(mode="json") if engine.open_position else None,
        "summary": engine.summary(),
        "strategy": {
            "edge_correction_usd": engine.edge_correction_usd(datetime.now(timezone.utc)),
            "edge_correction_source": engine.edge_correction_source(datetime.now(timezone.utc)),
        },
        "last_rejection": engine.rejections[-1] if engine.rejections else None,
        "pair_match": pair_match or {},
        "btc_recovery": btc_recovery or {},
        "real_trading": real_trading or {},
        "btc_dynamic": btc_dynamic or {},
        "btc_v8": btc_v8 or {},
        "orderbook_chase": orderbook_chase or {},
        "btc_weighted": btc_weighted or {},
        "btc_lead_prediction": btc_lead_prediction or {},
        "btc_maker_arbitrage": btc_maker_arbitrage or {},
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
    fast_twap_active = threshold_needs_page_confirmation(market)
    if threshold_is_tradable(market) and not fast_twap_active:
        return False

    if not fast_twap_active:
        # Unverified values must never survive a failed page lookup. The exact
        # TWAP stream is the only exception; it stays live while the slower
        # page independently confirms it.
        market.threshold_price = None
        market.threshold_source = "threshold_verification_pending"
        market.threshold_observed_at = None
        market.threshold_verified = False
        market.threshold_fetched_at = None

    page_data = getattr(client, "market_page_data", None)

    async def fetch_gamma_threshold() -> float | None:
        event_threshold = getattr(client, "event_threshold", None)
        if event_threshold is None:
            return None
        try:
            return await asyncio.wait_for(event_threshold(market.slug), timeout=timeout_seconds)
        except Exception:
            return None

    async def fetch_page_data():
        if page_data is not None:
            return await asyncio.wait_for(page_data(market.slug), timeout=timeout_seconds)
        outcome_result, results_result = await asyncio.gather(
            asyncio.wait_for(client.outcome_price(market.slug), timeout=timeout_seconds),
            asyncio.wait_for(client.past_results(market.slug), timeout=timeout_seconds),
            return_exceptions=True,
        )
        if isinstance(outcome_result, Exception) and isinstance(results_result, Exception):
            raise outcome_result
        outcome_price = None if isinstance(outcome_result, Exception) else outcome_result
        results = [] if isinstance(results_result, Exception) else results_result
        return outcome_price, results

    gamma_result, page_result = await asyncio.gather(
        fetch_gamma_threshold(),
        fetch_page_data(),
        return_exceptions=True,
    )
    gamma_threshold = None if isinstance(gamma_result, Exception) else gamma_result
    if isinstance(page_result, Exception):
        outcome_price, results = None, []
    else:
        outcome_price, results = page_result

    completed_at = now or datetime.now(timezone.utc)
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

    def fail_verification(*, conflict: bool = False) -> bool:
        market.threshold_price = None
        market.threshold_source = "threshold_verification_conflict" if conflict else "threshold_verification_failed"
        market.threshold_observed_at = None
        market.threshold_verified = False
        market.threshold_fetched_at = completed_at
        if conflict:
            market.threshold_candidate_conflicted = True
        return state_changed()

    if outcome_price is None and fast_twap_active:
        return state_changed()
    if not (valid_outcome and interval_is_exact and market_still_active):
        return fail_verification(conflict=fast_twap_active and outcome_price is not None)

    assert outcome_price is not None
    is_twap_market = outcome_price.twap_lookback_seconds is not None
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
    market_twap_window = supported_market_twap_lookback_seconds(market)
    twap_candidate_is_exact = bool(
        not market.threshold_candidate_conflicted
        and market_twap_window is not None
        and outcome_price.twap_lookback_seconds == market_twap_window
        and market.threshold_candidate_price is not None
        and math.isfinite(market.threshold_candidate_price)
        and market.threshold_candidate_source == twap_rtds_candidate_source(market_twap_window)
        and market.threshold_candidate_observed_at == market.start_time
        and market.threshold_candidate_received_at is not None
        and market.start_time - timedelta(seconds=1)
        <= market.threshold_candidate_received_at
        <= market.start_time + timedelta(seconds=3)
    )
    twap_candidate_matches = bool(
        twap_candidate_is_exact
        and abs((market.threshold_candidate_price or 0.0) - outcome_price.open_price)
        <= THRESHOLD_MATCH_TOLERANCE_USD
    )
    if fast_twap_active and not (is_twap_market and twap_candidate_matches):
        return fail_verification(conflict=True)
    if not is_twap_market and (
        market.threshold_candidate_conflicted or (candidate_fields_present and not candidate_is_exact)
    ):
        return fail_verification()
    candidate_matches = bool(
        not is_twap_market
        and candidate_is_exact
        and abs((market.threshold_candidate_price or 0.0) - outcome_price.open_price)
        <= THRESHOLD_MATCH_TOLERANCE_USD
    )
    if not is_twap_market and candidate_is_exact and not candidate_matches:
        return fail_verification()

    if is_twap_market and not twap_candidate_is_exact and candidate_fields_present:
        # A point-price RTDS candidate is not evidence for a TWAP market.
        market.threshold_candidate_price = None
        market.threshold_candidate_source = None
        market.threshold_candidate_observed_at = None
        market.threshold_candidate_received_at = None
        market.threshold_candidate_conflicted = False

    previous_matches = bool(
        previous_is_consistent
        and abs(previous_closes[-1] - outcome_price.open_price) <= THRESHOLD_MATCH_TOLERANCE_USD
    )
    if previous_is_consistent and not previous_matches:
        return fail_verification(conflict=fast_twap_active)
    if is_twap_market and not previous_matches:
        if fast_twap_active and not previous_is_consistent:
            return state_changed()
        return fail_verification()
    if not is_twap_market and not (candidate_matches or previous_matches):
        return fail_verification()
    if gamma_threshold is not None and abs(gamma_threshold - outcome_price.open_price) > THRESHOLD_MATCH_TOLERANCE_USD:
        return fail_verification(conflict=fast_twap_active)

    market.threshold_price = outcome_price.open_price
    market.threshold_fetched_at = completed_at
    if twap_candidate_matches:
        market.threshold_source = TWAP_PAGE_VERIFIED_THRESHOLD_SOURCE
    elif candidate_matches:
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
                and (
                    not threshold_is_tradable(current_market)
                    or threshold_needs_page_confirmation(current_market)
                )
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


async def polymarket_twap_price_loop(
    client: PolymarketClient,
    engine: PaperEngine,
    queue: asyncio.Queue,
) -> None:
    while True:
        window_seconds = supported_market_twap_lookback_seconds(engine.market)
        if window_seconds is None:
            await asyncio.sleep(TWAP_SUBSCRIPTION_RECHECK_SECONDS)
            continue
        stream = None
        next_tick_task: asyncio.Task | None = None
        try:
            stream = client.rtds_twap_price_ticks(window_seconds=window_seconds)
            iterator = stream.__aiter__()
            next_tick_task = asyncio.create_task(anext(iterator))
            while supported_market_twap_lookback_seconds(engine.market) == window_seconds:
                done, _ = await asyncio.wait(
                    {next_tick_task},
                    timeout=TWAP_SUBSCRIPTION_RECHECK_SECONDS,
                )
                if not done:
                    continue
                tick = next_tick_task.result()
                next_tick_task = asyncio.create_task(anext(iterator))
                if supported_market_twap_lookback_seconds(engine.market) != window_seconds:
                    break
                await queue.put(("polymarket_twap_tick", tick))
        except StopAsyncIteration:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await queue.put(("error", {"source": "polymarket_twap_rtds", "error": str(exc)}))
            await asyncio.sleep(1)
        finally:
            if next_tick_task is not None and not next_tick_task.done():
                next_tick_task.cancel()
                await asyncio.gather(next_tick_task, return_exceptions=True)
            close_stream = getattr(stream, "aclose", None)
            if close_stream is not None:
                await close_stream()


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


async def btc_recovery_resolution_loop(
    btc_client: PolymarketClient,
    recovery_engine: BtcRecoveryEngine,
    queue: asyncio.Queue[tuple[str, str, Any]],
) -> None:
    while True:
        for slug in recovery_engine.unresolved_slugs():
            try:
                outcome = await btc_client.resolved_outcome(slug)
                if outcome is not None:
                    await queue.put(("BTC_RECOVERY", "btc_recovery_resolution", (slug, outcome)))
            except Exception as exc:
                await queue.put(
                    (
                        "BTC_RECOVERY",
                        "btc_recovery_error",
                        {"source": "gamma_resolution", "error": str(exc)},
                    )
                )
        await asyncio.sleep(2)


async def btc_dynamic_resolution_loop(
    btc_client: PolymarketClient,
    dynamic_engine: BtcDynamicEngine,
    queue: asyncio.Queue[tuple[str, str, Any]],
) -> None:
    while True:
        for slug in dynamic_engine.unresolved_slugs():
            try:
                outcome = await btc_client.resolved_outcome(slug)
                if outcome is not None:
                    await queue.put(("BTC_DYNAMIC", "btc_dynamic_resolution", (slug, outcome)))
            except Exception as exc:
                await queue.put(
                    (
                        "BTC_DYNAMIC",
                        "btc_dynamic_error",
                        {"source": "gamma_resolution", "error": str(exc)},
                    )
                )
        await asyncio.sleep(2)


async def btc_v8_resolution_loop(
    btc_client: PolymarketClient,
    v8_engine: BtcV8Engine,
    queue: asyncio.Queue[tuple[str, str, Any]],
    orderbook_chase_engine: OrderbookChaseEngine | None = None,
    weighted_engine: BtcWeightedEngine | None = None,
    lead_engine: BtcLeadPredictionEngine | None = None,
    maker_engine: BtcMakerArbitrageEngine | None = None,
) -> None:
    while True:
        slugs = set(v8_engine.unresolved_slugs())
        if orderbook_chase_engine is not None:
            slugs.update(orderbook_chase_engine.unresolved_slugs())
        if weighted_engine is not None:
            slugs.update(weighted_engine.unresolved_slugs())
        if lead_engine is not None:
            slugs.update(lead_engine.unresolved_slugs())
        if maker_engine is not None:
            slugs.update(maker_engine.unresolved_slugs())
        for slug in sorted(slugs):
            try:
                outcome = await btc_client.resolved_outcome(slug)
                if outcome is not None:
                    await queue.put(("BTC_V8", "btc_v8_resolution", (slug, outcome)))
            except Exception as exc:
                await queue.put(
                    (
                        "BTC_V8",
                        "btc_v8_error",
                        {"source": "gamma_resolution", "error": str(exc)},
                    )
                )
        await asyncio.sleep(2)


async def btc_v8_signal_collector(
    source: str,
    client: Any,
    buffer: asyncio.Queue[SignalEvent],
    config: AppConfig,
) -> None:
    while True:
        while not btc_signal_source_enabled(config, source):
            await asyncio.sleep(0.25)
        async for event in client.events():
            if not btc_signal_source_enabled(config, source):
                break
            if buffer.full():
                try:
                    buffer.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            buffer.put_nowait(event)


async def btc_v8_signal_batch_loop(
    buffers: dict[str, asyncio.Queue[SignalEvent]],
    queue: asyncio.Queue[tuple[str, str, Any]],
    config: AppConfig,
) -> None:
    loop = asyncio.get_running_loop()
    last_heartbeat = loop.time()
    while True:
        if not (config.btc_v8.enabled or config.btc_lead_prediction.enabled):
            for buffer in buffers.values():
                while not buffer.empty():
                    buffer.get_nowait()
            last_heartbeat = loop.time()
            await asyncio.sleep(0.25)
            continue
        batch: list[SignalEvent] = []
        for source, buffer in buffers.items():
            if not btc_signal_source_enabled(config, source):
                while not buffer.empty():
                    buffer.get_nowait()
                continue
            while not buffer.empty() and len(batch) < 1_000:
                batch.append(buffer.get_nowait())
        current = loop.time()
        if batch or current - last_heartbeat >= 0.25:
            await queue.put(("BTC_V8", "btc_v8_signals", batch))
            last_heartbeat = current
        await asyncio.sleep(0.05)


def book_matches_market(market: MarketState, direction: Direction, book: OrderBookSnapshot) -> bool:
    expected_token = market.up_token_id if direction == Direction.UP else market.down_token_id
    if book.token_id != expected_token:
        return False
    if book.market_id and book.market_id != market.condition_id:
        return False
    return True


async def emit_rest_books(client: PolymarketClient, market: MarketState, queue: asyncio.Queue) -> None:
    results = await asyncio.gather(
        client.book(market.up_token_id),
        client.book(market.down_token_id),
        return_exceptions=True,
    )
    for direction, result in zip((Direction.UP, Direction.DOWN), results, strict=True):
        if isinstance(result, BaseException):
            await queue.put(
                (
                    "error",
                    {
                        "source": f"clob_rest_{direction.value.lower()}",
                        "error": f"{type(result).__name__}: {result}",
                    },
                )
            )
            continue
        if book_matches_market(market, direction, result):
            await queue.put(("book", (direction, result)))


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


def book_needs_rest_refresh(
    engine: PaperEngine,
    market: MarketState,
    direction: Direction,
    now: datetime,
    last_rest_refresh_at: datetime | None = None,
) -> bool:
    book = engine.books.get(direction)
    return bool(
        not book
        or not book_matches_market(market, direction, book)
        or not book.depth_trusted
        or now - book.received_at >= BOOK_REST_FALLBACK_AFTER
        or (
            last_rest_refresh_at is not None
            and now - last_rest_refresh_at >= BOOK_REST_RECONCILE_AFTER
        )
    )


async def book_rest_leg_loop(
    client: PolymarketClient,
    engine: PaperEngine,
    queue: asyncio.Queue,
    poll_ms: int,
    direction: Direction,
) -> None:
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
        if book_needs_rest_refresh(
            engine, market, direction, started_at, last_rest_refresh_at
        ):
            token_id = (
                market.up_token_id if direction == Direction.UP else market.down_token_id
            )
            try:
                book = await client.book(token_id)
                if book_matches_market(market, direction, book):
                    await queue.put(("book", (direction, book)))
            except Exception as exc:
                await queue.put(
                    (
                        "error",
                        {
                            "source": f"clob_rest_{direction.value.lower()}",
                            "error": f"{type(exc).__name__}: {exc}",
                        },
                    )
                )
            finally:
                last_rest_refresh_at = started_at
        elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
        await asyncio.sleep(max(0.02, check_interval_seconds - elapsed))


async def book_rest_loop(client: PolymarketClient, engine: PaperEngine, queue: asyncio.Queue, poll_ms: int) -> None:
    await asyncio.gather(
        book_rest_leg_loop(client, engine, queue, poll_ms, Direction.UP),
        book_rest_leg_loop(client, engine, queue, poll_ms, Direction.DOWN),
    )


async def book_loop(client: PolymarketClient, engine: PaperEngine, queue: asyncio.Queue, poll_ms: int) -> None:
    timeout_seconds = max(poll_ms / 1000, 0.1)
    loop = asyncio.get_running_loop()
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
        last_market_event_at = loop.time()
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
                        stale_after = (
                            CLOB_WS_DATA_STALE_SECONDS
                            if websocket_active
                            else CLOB_WS_INITIAL_DATA_TIMEOUT_SECONDS
                        )
                        if loop.time() - last_market_event_at >= stale_after:
                            raise TimeoutError(
                                f"CLOB market stream silent for {stale_after:g} seconds"
                            )
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
                last_market_event_at = loop.time()
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
        if event_type == "polymarket_twap_tick":
            buffered[("polymarket_twap_tick", "latest")] = (index, event)
            continue
        if event_type == "book" and isinstance(payload, tuple) and payload:
            direction = payload[0]
            current_book = payload[1] if len(payload) > 1 else None
            if (
                isinstance(current_book, OrderBookSnapshot)
                and isinstance(current_book.raw, dict)
                and current_book.raw.get("_last_trade")
            ):
                flush_buffered()
                result.append(event)
                continue
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


def maker_state_heartbeat_due(
    *,
    maker_enabled: bool,
    input_changed: bool,
    has_real_events: bool,
    now: datetime,
    last_emitted_at: datetime | None,
) -> bool:
    return bool(
        maker_enabled
        and input_changed
        and not has_real_events
        and (
            last_emitted_at is None
            or now - last_emitted_at >= MAKER_STATE_HEARTBEAT
        )
    )


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


async def run_live(
    config: AppConfig,
    max_seconds: int | None = None,
    on_update: UpdateCallback | None = None,
    before_market_updates: BeforeMarketUpdatesCallback | None = None,
    control_commands: ThreadQueue[tuple[str, Any]] | None = None,
    live_credentials: RealTradingCredentials | None = None,
) -> Path:
    output_dir = run_dir(config.data_dir)
    journal = RunJournal(output_dir)
    entry_registry = SqliteMarketEntryRegistry(config.data_dir / "market-entry-ledger.sqlite3")
    pair_registry = PairMatchRegistry(config.data_dir / "pair-match-ledger.sqlite3")
    recovery_registry = BtcRecoveryRegistry(config.data_dir / "btc-recovery-ledger.sqlite3")
    dynamic_registry = BtcDynamicRegistry(config.data_dir / "btc-dynamic-ledger.sqlite3")
    v8_registry = BtcV8Registry(config.data_dir / "btc-v8-ledger.sqlite3")
    chase_path = config.data_dir / "orderbook-chase-ledger.sqlite3"
    chase_registry = OrderbookChaseRegistry(chase_path)
    weighted_registry = BtcWeightedRegistry(config.data_dir / "btc-weighted-ledger.sqlite3")
    lead_registry = BtcLeadPredictionRegistry(
        config.data_dir / "btc-lead-prediction-ledger.sqlite3"
    )
    maker_registry = BtcMakerArbitrageRegistry(
        config.data_dir / "btc-maker-arbitrage-ledger.sqlite3"
    )
    real_registry = RealTradingRegistry(config.data_dir / "real-trading-ledger.sqlite3")
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
        recovery_engine = BtcRecoveryEngine(config, recovery_registry)
        dynamic_engine = BtcDynamicEngine(config, dynamic_registry)
        v8_engine = BtcV8Engine(config, v8_registry)
        orderbook_chase_engine = OrderbookChaseEngine(
            config,
            chase_registry,
            v8_engine,
        )
        weighted_engine = BtcWeightedEngine(
            config,
            weighted_registry,
            orderbook_chase_engine.latency,
        )
        lead_engine = BtcLeadPredictionEngine(
            config,
            lead_registry,
            orderbook_chase_engine.latency,
        )
        maker_engine = BtcMakerArbitrageEngine(config, maker_registry)
        real_engine = RealTradingEngine(
            config,
            real_registry,
            credentials=live_credentials,
        )
        pair_enabled = bool(config.pair_match.enabled)
        recovery_enabled = bool(config.btc_recovery.enabled)
        dynamic_enabled = btc_dynamic_runtime_enabled(config)
        v8_enabled = btc_v8_runtime_enabled(config)
        lead_enabled = btc_lead_prediction_runtime_enabled(config)
        maker_enabled = btc_maker_arbitrage_runtime_enabled(config)
        chase_enabled = orderbook_chase_runtime_enabled(config)
        real_enabled = real_trading_runtime_enabled(config)
        await asyncio.gather(
            *(
                initialize_current_market(clients[asset], engines[asset], journal, output_dir, on_update)
                for asset in assets
            )
        )
        btc_engine = engines.get("BTC")
        if btc_engine and btc_engine.market:
            recovery_engine.set_market(btc_engine.market)
            if dynamic_enabled:
                dynamic_engine.set_market(btc_engine.market)
            if v8_enabled:
                v8_engine.set_market(btc_engine.market)
            if chase_enabled:
                orderbook_chase_engine.set_market(btc_engine.market)
            weighted_engine.set_market(btc_engine.market)
            lead_engine.set_market(btc_engine.market)
            maker_engine.set_market(btc_engine.market)
            settlement_tick = btc_engine.settlement_price_tick()
            if v8_enabled and settlement_tick is not None:
                v8_engine.add_chainlink_tick(settlement_tick)
            if settlement_tick is not None:
                weighted_engine.add_chainlink_tick(settlement_tick)
                lead_engine.add_chainlink_tick(settlement_tick)
            for direction, book in btc_engine.books.items():
                if v8_enabled:
                    v8_engine.add_polymarket_book(direction, book)
                weighted_engine.add_book(direction, book)
                lead_engine.add_book(direction, book)
                maker_engine.add_book(direction, book)
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
        recovery_registry.close()
        dynamic_registry.close()
        v8_registry.close()
        chase_registry.close()
        weighted_registry.close()
        lead_registry.close()
        maker_registry.close()
        real_registry.close()
        raise

    def recovery_dashboard_state() -> dict[str, Any]:
        btc_engine = engines.get("BTC")
        return recovery_engine.dashboard_state(
            btc_engine.books if btc_engine else {},
            pair_paused=bool(
                config.btc_recovery.enabled and config.pair_match.enabled
            ),
        )

    def real_dashboard_state() -> dict[str, Any]:
        return real_engine.dashboard_state()

    def dynamic_dashboard_state() -> dict[str, Any]:
        return dynamic_engine.dashboard_state()

    def v8_dashboard_state() -> dict[str, Any]:
        return v8_engine.dashboard_state()

    def orderbook_chase_dashboard_state() -> dict[str, Any]:
        return orderbook_chase_engine.dashboard_state()

    def weighted_dashboard_state() -> dict[str, Any]:
        return weighted_engine.dashboard_state()

    def lead_dashboard_state() -> dict[str, Any]:
        return lead_engine.dashboard_state()

    def maker_dashboard_state() -> dict[str, Any]:
        return maker_engine.dashboard_state()

    tasks = [
        asyncio.create_task(data_cleanup_loop(config, output_dir, journal)),
    ]
    if v8_enabled:
        tasks.append(asyncio.create_task(btc_v8_data_cleanup_loop(config, v8_registry, journal)))
    tasks.append(asyncio.create_task(orderbook_chase_engine.latency.run()))
    for asset in assets:
        asset_queue = AssetEventQueue(queue, asset)
        engine = engines[asset]
        poly = clients[asset]
        tasks.extend(
            [
                asyncio.create_task(market_loop(poly, engine, asset_queue, config.sources.market_refresh_seconds)),
                asyncio.create_task(binance_loop(binance_clients[asset], asset_queue)),
                asyncio.create_task(polymarket_price_loop(poly, asset_queue)),
                asyncio.create_task(polymarket_twap_price_loop(poly, engine, asset_queue)),
                asyncio.create_task(book_rest_loop(poly, engine, asset_queue, config.sources.poly_book_poll_ms)),
                asyncio.create_task(book_loop(poly, engine, asset_queue, config.sources.poly_book_poll_ms)),
            ]
        )
    if pair_enabled and "BTC" in clients and "ETH" in clients:
        tasks.append(
            asyncio.create_task(pair_resolution_loop(clients["BTC"], clients["ETH"], pair_engine, queue))
        )
    if "BTC" in clients:
        if recovery_enabled:
            tasks.append(
                asyncio.create_task(
                    btc_recovery_resolution_loop(clients["BTC"], recovery_engine, queue)
                )
            )
        if dynamic_enabled:
            tasks.append(
                asyncio.create_task(
                    btc_dynamic_resolution_loop(clients["BTC"], dynamic_engine, queue)
                )
            )
        tasks.append(
            asyncio.create_task(
                    btc_v8_resolution_loop(
                        clients["BTC"], v8_engine, queue, orderbook_chase_engine,
                    weighted_engine, lead_engine, maker_engine
                    )
            )
        )
    if "BTC" in clients:
        signal_clients: dict[str, Any] = {
            "binance": BinanceSpotSignalClient(config.sources),
            "coinbase": CoinbaseSignalClient(config.sources),
            "kraken": KrakenSignalClient(config.sources),
            "binance_futures": BinanceFuturesSignalClient(config.sources),
        }
        signal_buffers = {
            source: asyncio.Queue(maxsize=512) for source in signal_clients
        }
        for source, client in signal_clients.items():
            tasks.append(
                asyncio.create_task(
                    btc_v8_signal_collector(source, client, signal_buffers[source], config)
                )
            )
        tasks.append(
            asyncio.create_task(btc_v8_signal_batch_loop(signal_buffers, queue, config))
        )
    started = datetime.now(timezone.utc)
    last_maker_state_emitted_at: datetime | None = None
    try:
        while True:
            if max_seconds is not None and (datetime.now(timezone.utc) - started).total_seconds() >= max_seconds:
                break
            try:
                pending_events = [await asyncio.wait_for(queue.get(), timeout=1)]
            except asyncio.TimeoutError:
                pending_events = []
                timer_tick = True
            else:
                timer_tick = False
            recovery_control_changed = False
            dynamic_control_changed = False
            if control_commands is not None:
                while True:
                    try:
                        command, value = control_commands.get_nowait()
                    except ThreadQueueEmpty:
                        break
                    if command == "btc_recovery_orders_stopped":
                        recovery_engine.set_recovery_orders_stopped(bool(value))
                        recovery_control_changed = True
                    elif command == "btc_recovery_statistics_reset":
                        recovery_engine.reset_statistics(
                            value if isinstance(value, datetime) else None
                        )
                        recovery_control_changed = True
                    elif command == "btc_dynamic_statistics_reset":
                        dynamic_engine.reset_statistics(
                            value if isinstance(value, datetime) else None
                        )
                        dynamic_control_changed = True
                    elif command == "btc_dynamic_model_reset":
                        dynamic_engine.request_model_reset(
                            value if isinstance(value, datetime) else None
                        )
                        dynamic_control_changed = True
                    elif command == "real_trading_connect":
                        await real_engine.connect()
                    elif command == "real_trading_prepare_allowance":
                        await real_engine.prepare_allowance()
                    elif command == "real_trading_arm":
                        real_engine.arm()
                    elif command == "real_trading_disarm":
                        real_engine.disarm()
                    elif command == "real_trading_emergency_stop":
                        real_engine.emergency_stop()
                    elif command == "real_trading_resume":
                        real_engine.resume()
                    elif command == "real_trading_shadow_reset":
                        real_engine.reset_shadow()
                    elif command.startswith("orderbook_chase_"):
                        orderbook_chase_engine.control(
                            command.removeprefix("orderbook_chase_")
                        )
                    elif command == "btc_maker_pause":
                        maker_engine.pause()
                    elif command == "btc_maker_resume":
                        maker_engine.resume()
                    elif command == "btc_maker_cancel_quotes":
                        maker_engine.cancel_quotes("manual_cancel")
            # Give simultaneous market frames a tiny window to accumulate so
            # hundreds of depth-only deltas collapse to the newest complete
            # UP/DOWN snapshots.  This bounds added latency at 20 ms.
            if pending_events:
                await asyncio.sleep(LIVE_EVENT_COALESCE_SECONDS)
                while not queue.empty() and len(pending_events) < 500:
                    pending_events.append(queue.get_nowait())
            if before_market_updates is not None:
                market_updates = {
                    asset: payload
                    for asset, event_type, payload in pending_events
                    if event_type == "market" and isinstance(payload, MarketState)
                }
                if market_updates:
                    before_market_updates(market_updates)
            pair_input_changed = False
            recovery_input_changed = recovery_enabled and (timer_tick or recovery_control_changed)
            dynamic_input_changed = dynamic_enabled and (timer_tick or dynamic_control_changed)
            v8_input_changed = (v8_enabled or chase_enabled) and timer_tick
            weighted_input_changed = timer_tick
            lead_input_changed = timer_tick
            maker_input_changed = timer_tick
            for asset in assets:
                asset_events = [(event_type, payload) for event_asset, event_type, payload in pending_events if event_asset == asset]
                if not asset_events:
                    continue
                engine = engines[asset]
                for event_type, payload in coalesce_live_events(asset_events):
                    engine.entry_enabled = standard_entry_enabled(config, asset)
                    publish_update = True
                    if event_type == "market":
                        pair_input_changed = pair_input_changed or pair_enabled
                        recovery_input_changed = recovery_input_changed or (recovery_enabled and asset == "BTC")
                        dynamic_input_changed = dynamic_input_changed or (dynamic_enabled and asset == "BTC")
                        weighted_input_changed = weighted_input_changed or asset == "BTC"
                        lead_input_changed = lead_input_changed or asset == "BTC"
                        maker_input_changed = maker_input_changed or asset == "BTC"
                        market: MarketState = payload
                        if not market_is_active(market, datetime.now(timezone.utc)):
                            continue
                        engine.set_market(market)
                        if asset == "BTC":
                            if dynamic_enabled:
                                dynamic_engine.set_market(market)
                            if v8_enabled:
                                v8_engine.set_market(market)
                            if chase_enabled:
                                orderbook_chase_engine.set_market(market)
                            weighted_engine.set_market(market)
                            lead_engine.set_market(market)
                            maker_engine.set_market(market)
                            settlement_tick = engine.settlement_price_tick()
                            if dynamic_enabled and settlement_tick is not None:
                                dynamic_engine.add_chainlink_tick(settlement_tick)
                            if v8_enabled and settlement_tick is not None:
                                v8_engine.add_chainlink_tick(settlement_tick)
                            if settlement_tick is not None:
                                weighted_engine.add_chainlink_tick(settlement_tick)
                                lead_engine.add_chainlink_tick(settlement_tick)
                            v8_input_changed = v8_input_changed or v8_enabled or chase_enabled
                        journal.market(market)
                    elif event_type == "tick":
                        tick: PriceTick = payload
                        engine.set_tick(tick)
                        if dynamic_enabled and asset == "BTC":
                            dynamic_engine.add_binance_tick(tick)
                            dynamic_input_changed = True
                        journal.tick(tick)
                    elif event_type == "polymarket_tick":
                        tick = payload
                        assert isinstance(tick, PriceTick)
                        engine.set_polymarket_tick(tick)
                        if asset == "BTC" and engine.settlement_price_tick() is tick:
                            if dynamic_enabled:
                                dynamic_engine.add_chainlink_tick(tick)
                                dynamic_input_changed = True
                            if v8_enabled:
                                v8_engine.add_chainlink_tick(tick)
                                v8_input_changed = True
                            weighted_engine.add_chainlink_tick(tick)
                            lead_engine.add_chainlink_tick(tick)
                            weighted_input_changed = True
                            lead_input_changed = True
                        journal.event("polymarket_tick", tick)
                    elif event_type == "polymarket_twap_tick":
                        tick = payload
                        assert isinstance(tick, PriceTick)
                        threshold_changed = engine.set_polymarket_twap_tick(tick)
                        if asset == "BTC" and engine.settlement_price_tick() is tick:
                            if dynamic_enabled:
                                dynamic_engine.add_chainlink_tick(tick)
                                dynamic_input_changed = True
                            if v8_enabled:
                                v8_engine.add_chainlink_tick(tick)
                                v8_input_changed = True
                            weighted_engine.add_chainlink_tick(tick)
                            lead_engine.add_chainlink_tick(tick)
                            weighted_input_changed = True
                            lead_input_changed = True
                        if threshold_changed and engine.market is not None:
                            pair_input_changed = pair_input_changed or pair_enabled
                            dynamic_input_changed = dynamic_input_changed or (dynamic_enabled and asset == "BTC")
                            v8_input_changed = v8_input_changed or ((v8_enabled or chase_enabled) and asset == "BTC")
                            journal.market(engine.market)
                        journal.event("polymarket_twap_tick", tick)
                    elif event_type == "book":
                        pair_input_changed = pair_input_changed or pair_enabled
                        recovery_input_changed = recovery_input_changed or (recovery_enabled and asset == "BTC")
                        dynamic_input_changed = dynamic_input_changed or (dynamic_enabled and asset == "BTC")
                        direction, book = payload
                        assert isinstance(book, OrderBookSnapshot)
                        previous_book = engine.books.get(direction)
                        engine.set_book(direction, book)
                        active_book = engine.books.get(direction)
                        if v8_enabled and asset == "BTC" and active_book is book:
                            v8_engine.add_polymarket_book(direction, active_book)
                            v8_input_changed = True
                        if asset == "BTC" and active_book is book:
                            weighted_engine.add_book(direction, active_book)
                            weighted_input_changed = True
                            lead_engine.add_book(direction, active_book)
                            lead_input_changed = True
                            maker_engine.add_book(direction, active_book)
                            maker_input_changed = True
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
                        if asset == "BTC" and str(payload.get("source", "")).startswith("clob"):
                            maker_engine.mark_unmeasurable("clob_stream_interrupted")
                            maker_input_changed = True
                        journal.latency_row(source, "stream", False, None, payload.get("error", ""))
                    live_events = flush_engine_updates(engine, journal, counters[asset])
                    for live_event_type, live_payload in live_events:
                        await emit_update(
                            on_update,
                            live_snapshot(
                                engine,
                                output_dir,
                                live_event_type,
                                live_payload,
                                pair_engine.dashboard_state(),
                                recovery_dashboard_state(),
                                real_dashboard_state(),
                                dynamic_dashboard_state(),
                                v8_dashboard_state(),
                                orderbook_chase_dashboard_state(),
                                weighted_dashboard_state(),
                                lead_dashboard_state(),
                            ),
                        )
                    if publish_update:
                        await emit_update(
                            on_update,
                            live_snapshot(
                                engine,
                                output_dir,
                                event_type,
                                payload,
                                pair_engine.dashboard_state(),
                                recovery_dashboard_state(),
                                real_dashboard_state(),
                                dynamic_dashboard_state(),
                                v8_dashboard_state(),
                                orderbook_chase_dashboard_state(),
                                weighted_dashboard_state(),
                                lead_dashboard_state(),
                            ),
                        )

            primary_engine = engines.get("BTC") or next(iter(engines.values()))
            pair_events = [
                (event_type, payload)
                for event_asset, event_type, payload in pending_events
                if pair_enabled and event_asset == "PAIR"
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
                                recovery_dashboard_state(),
                                real_dashboard_state(),
                                dynamic_dashboard_state(),
                                v8_dashboard_state(),
                                orderbook_chase_dashboard_state(),
                                weighted_dashboard_state(),
                                lead_dashboard_state(),
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

            recovery_events = [
                (event_type, payload)
                for event_asset, event_type, payload in pending_events
                if recovery_enabled and event_asset == "BTC_RECOVERY"
            ]
            for event_type, payload in recovery_events:
                if event_type == "btc_recovery_resolution":
                    slug, outcome = payload
                    recovery_engine.settle(slug, outcome)
                    if real_enabled:
                        await real_engine.settle(slug, outcome)
                elif event_type == "btc_recovery_error":
                    journal.latency_row(
                        payload.get("source", "btc_recovery"),
                        "resolution",
                        False,
                        None,
                        payload.get("error", ""),
                    )

            dynamic_events = [
                (event_type, payload)
                for event_asset, event_type, payload in pending_events
                if dynamic_enabled and event_asset == "BTC_DYNAMIC"
            ]
            for event_type, payload in dynamic_events:
                if event_type == "btc_dynamic_resolution":
                    slug, outcome = payload
                    dynamic_engine.settle(slug, outcome)
                elif event_type == "btc_dynamic_error":
                    journal.latency_row(
                        payload.get("source", "btc_dynamic"),
                        "resolution",
                        False,
                        None,
                        payload.get("error", ""),
                    )

            v8_events = [
                (event_type, payload)
                for event_asset, event_type, payload in pending_events
                if event_asset == "BTC_V8"
            ]
            for event_type, payload in v8_events:
                if event_type == "btc_v8_signals":
                    for signal_event in payload:
                        if btc_v8_signal_source_enabled(config, signal_event.source):
                            v8_engine.add_signal(signal_event)
                    lead_engine.add_signals(payload)
                    v8_input_changed = True
                    lead_input_changed = True
                elif event_type == "btc_v8_resolution":
                    slug, outcome = payload
                    v8_engine.settle(slug, outcome)
                    orderbook_chase_engine.settle(slug, outcome)
                    weighted_engine.settle(slug, outcome)
                    lead_engine.settle(slug, outcome)
                    maker_engine.settle(slug, outcome)
                    v8_input_changed = True
                    lead_input_changed = True
                elif event_type == "btc_v8_error":
                    journal.latency_row(
                        payload.get("source", "btc_v8"),
                        "stream",
                        False,
                        None,
                        payload.get("error", ""),
                    )

            for engine in engines.values():
                engine.entry_enabled = standard_entry_enabled(config, engine.asset)
            btc_engine = engines.get("BTC")
            if recovery_input_changed and btc_engine is not None:
                if real_enabled:
                    real_engine.last_market = btc_engine.market
                recovery_engine.evaluate(
                    btc_engine.market,
                    btc_engine.books,
                    datetime.now(timezone.utc),
                )
            for recovery_event_type, recovery_payload in recovery_engine.drain_events():
                if recovery_event_type == "btc_recovery_fill":
                    journal.btc_recovery_order(recovery_payload)
                    if (
                        recovery_payload.stage == "initial"
                        and recovery_payload.side.value == "BUY"
                        and btc_engine is not None
                        and btc_engine.market is not None
                        and real_enabled
                    ):
                        await real_engine.on_initial_signal(
                            recovery_payload,
                            btc_engine.market,
                            btc_engine.books,
                            datetime.now(timezone.utc),
                        )
                elif recovery_event_type == "btc_recovery_round":
                    journal.btc_recovery_round(recovery_payload)
                elif recovery_event_type == "btc_recovery_result":
                    journal.btc_recovery_result(recovery_payload)
                else:
                    journal.event(recovery_event_type, recovery_payload)
                await emit_update(
                    on_update,
                    live_snapshot(
                        primary_engine,
                        output_dir,
                        recovery_event_type,
                        recovery_payload,
                        pair_engine.dashboard_state(),
                        recovery_dashboard_state(),
                        real_dashboard_state(),
                        dynamic_dashboard_state(),
                        v8_dashboard_state(),
                        orderbook_chase_dashboard_state(),
                        weighted_dashboard_state(),
                        lead_dashboard_state(),
                    ),
                )
            if dynamic_enabled and dynamic_input_changed and btc_engine is not None:
                dynamic_engine.evaluate(
                    btc_engine.market,
                    btc_engine.books,
                    datetime.now(timezone.utc),
                )
            if dynamic_enabled:
                for dynamic_event_type, dynamic_payload in dynamic_engine.drain_events():
                    journal.event(dynamic_event_type, dynamic_payload)
                    await emit_update(
                        on_update,
                        live_snapshot(
                            primary_engine,
                            output_dir,
                            dynamic_event_type,
                            dynamic_payload,
                            pair_engine.dashboard_state(),
                            recovery_dashboard_state(),
                            real_dashboard_state(),
                            dynamic_dashboard_state(),
                            v8_dashboard_state(),
                            orderbook_chase_dashboard_state(),
                            weighted_dashboard_state(),
                            lead_dashboard_state(),
                        ),
                    )
            if weighted_input_changed and btc_engine is not None:
                weighted_engine.evaluate(
                    btc_engine.market,
                    btc_engine.books,
                    datetime.now(timezone.utc),
                )
            for weighted_event_type, weighted_payload in weighted_engine.drain_events():
                journal.event(weighted_event_type, weighted_payload)
                await emit_update(
                    on_update,
                    live_snapshot(
                        primary_engine,
                        output_dir,
                        weighted_event_type,
                        weighted_payload,
                        pair_engine.dashboard_state(),
                        recovery_dashboard_state(),
                        real_dashboard_state(),
                        dynamic_dashboard_state(),
                        v8_dashboard_state(),
                        orderbook_chase_dashboard_state(),
                        weighted_dashboard_state(),
                        lead_dashboard_state(),
                    ),
                )
            if lead_input_changed and btc_engine is not None:
                lead_engine.evaluate(
                    datetime.now(timezone.utc),
                    update_key=(
                        f"{len(pending_events)}:{datetime.now(timezone.utc).timestamp()}"
                    ),
                )
            for lead_event_type, lead_payload in lead_engine.drain_events():
                journal.event(lead_event_type, lead_payload)
                await emit_update(
                    on_update,
                    live_snapshot(
                        primary_engine,
                        output_dir,
                        lead_event_type,
                        lead_payload,
                        pair_engine.dashboard_state(),
                        recovery_dashboard_state(),
                        real_dashboard_state(),
                        dynamic_dashboard_state(),
                        v8_dashboard_state(),
                        orderbook_chase_dashboard_state(),
                        weighted_dashboard_state(),
                        lead_dashboard_state(),
                    ),
                )
            if maker_input_changed and btc_engine is not None:
                maker_engine.evaluate(datetime.now(timezone.utc))
            maker_events = maker_engine.drain_events()
            maker_emit_at = datetime.now(timezone.utc)
            if maker_state_heartbeat_due(
                maker_enabled=maker_enabled,
                input_changed=maker_input_changed,
                has_real_events=bool(maker_events),
                now=maker_emit_at,
                last_emitted_at=last_maker_state_emitted_at,
            ):
                maker_events = [("btc_maker_state", maker_dashboard_state())]
            if maker_events:
                last_maker_state_emitted_at = maker_emit_at
            for maker_event_type, maker_payload in maker_events:
                journal.event(maker_event_type, maker_payload)
                await emit_update(
                    on_update,
                    live_snapshot(
                        primary_engine,
                        output_dir,
                        maker_event_type,
                        maker_payload,
                        pair_engine.dashboard_state(),
                        recovery_dashboard_state(),
                        real_dashboard_state(),
                        dynamic_dashboard_state(),
                        v8_dashboard_state(),
                        orderbook_chase_dashboard_state(),
                        weighted_dashboard_state(),
                        lead_dashboard_state(),
                        maker_dashboard_state(),
                    ),
                )
            v8_engine_events: list[tuple[str, Any]] = []
            if (v8_enabled or chase_enabled) and v8_input_changed and btc_engine is not None:
                evaluation_now = datetime.now(timezone.utc)
                if v8_enabled:
                    v8_engine.evaluate(
                        btc_engine.market,
                        btc_engine.books,
                        evaluation_now,
                    )
                    v8_engine_events = v8_engine.drain_events()
                confirmed_trades = [
                    payload
                    for event_type, payload in v8_engine_events
                    if event_type == "btc_v8_trade"
                    and isinstance(payload, V8Trade)
                    and payload.action == "BUY"
                ]
                if chase_enabled:
                    await orderbook_chase_engine.evaluate(
                        btc_engine.market,
                        btc_engine.books,
                        confirmed_trades,
                        evaluation_now,
                    )
            for v8_event_type, v8_payload in v8_engine_events:
                journal.event(v8_event_type, v8_payload)
                await emit_update(
                    on_update,
                    live_snapshot(
                        primary_engine,
                        output_dir,
                        v8_event_type,
                        v8_payload,
                        pair_engine.dashboard_state(),
                        recovery_dashboard_state(),
                        real_dashboard_state(),
                        dynamic_dashboard_state(),
                        v8_dashboard_state(),
                        orderbook_chase_dashboard_state(),
                        weighted_dashboard_state(),
                        lead_dashboard_state(),
                    ),
                )
            if chase_enabled:
                for chase_event_type, chase_payload in orderbook_chase_engine.drain_events():
                    journal.event(chase_event_type, chase_payload)
                    await emit_update(
                        on_update,
                        live_snapshot(
                            primary_engine,
                            output_dir,
                            chase_event_type,
                            chase_payload,
                            pair_engine.dashboard_state(),
                            recovery_dashboard_state(),
                            real_dashboard_state(),
                            dynamic_dashboard_state(),
                            v8_dashboard_state(),
                            orderbook_chase_dashboard_state(),
                            weighted_dashboard_state(),
                            lead_dashboard_state(),
                        ),
                    )
            if real_enabled:
                await real_engine.reconcile()
                for real_event_type, real_payload in real_engine.drain_events():
                    await emit_update(
                        on_update,
                        live_snapshot(
                            primary_engine,
                            output_dir,
                            real_event_type,
                            real_payload,
                            pair_engine.dashboard_state(),
                            recovery_dashboard_state(),
                            real_dashboard_state(),
                            dynamic_dashboard_state(),
                            v8_dashboard_state(),
                            orderbook_chase_dashboard_state(),
                            weighted_dashboard_state(),
                            lead_dashboard_state(),
                        ),
                    )
            if pair_input_changed:
                if config.btc_v8.enabled:
                    pair_engine.status = "paused_by_btc_v8"
                    pair_engine.last_reason = pair_engine.status
                    order = None
                elif config.btc_dynamic.enabled:
                    pair_engine.status = "paused_by_btc_dynamic"
                    pair_engine.last_reason = pair_engine.status
                    order = None
                elif config.btc_recovery.enabled:
                    pair_engine.status = "paused_by_btc_recovery"
                    pair_engine.last_reason = pair_engine.status
                    order = None
                else:
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
                        recovery_dashboard_state(),
                        real_dashboard_state(),
                        dynamic_dashboard_state(),
                        v8_dashboard_state(),
                        orderbook_chase_dashboard_state(),
                        weighted_dashboard_state(),
                        lead_dashboard_state(),
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
                    live_snapshot(
                        engine,
                        output_dir,
                        "summary",
                        engine.summary(),
                        pair_engine.dashboard_state(),
                        recovery_dashboard_state(),
                        real_dashboard_state(),
                        dynamic_dashboard_state(),
                        v8_dashboard_state(),
                        orderbook_chase_dashboard_state(),
                        weighted_dashboard_state(),
                        lead_dashboard_state(),
                    ),
                )
            summary = aggregate_engine_summaries(engines)
            pair_summary = pair_engine.dashboard_state()["summary"]
            recovery_summary = recovery_dashboard_state()["summary"]
            summary.update(
                {
                    "pair_orders": pair_summary["orders"],
                    "pair_pending_orders": pair_summary["pending_orders"],
                    "pair_realized_pnl": pair_summary["realized_pnl"],
                    "btc_recovery_markets": recovery_summary["observed_markets"],
                    "btc_recovery_pending_settlements": recovery_summary["pending_settlements"],
                    "btc_recovery_realized_pnl": recovery_summary["realized_pnl"],
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
            await real_engine.close()
            await orderbook_chase_engine.close()
            entry_registry.close()
            pair_registry.close()
            recovery_registry.close()
            dynamic_registry.close()
            v8_registry.close()
            chase_registry.close()
            weighted_registry.close()
            lead_registry.close()
            maker_registry.close()
            real_registry.close()
    return output_dir
