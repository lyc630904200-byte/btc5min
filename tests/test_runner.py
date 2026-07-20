from datetime import datetime, timedelta, timezone

from polybtc.config import AppConfig
from polybtc.engine import PaperEngine
from polybtc.models import BookLevel, Direction, MarketState, OrderBookSnapshot
from polybtc.runner import (
    apply_polymarket_page_threshold,
    books_need_rest_refresh,
    coalesce_live_events,
    current_market_with_page_threshold,
    live_book_payload,
    prefetch_next_market_threshold,
    should_keep_current_market,
    should_retry_threshold,
    should_publish_book_update,
)


def market(now: datetime, threshold: float | None, end_delta: timedelta) -> MarketState:
    return MarketState(
        condition_id="m1",
        slug="btc-updown-5m-test",
        question="Bitcoin Up or Down",
        threshold_price=threshold,
        threshold_source="binance_first_tick_after_start" if threshold is not None else "dynamic_start_price",
        threshold_verified=threshold is not None,
        start_time=now - timedelta(minutes=1),
        end_time=now + end_delta,
        up_token_id="up",
        down_token_id="down",
    )


def test_should_keep_current_market_with_captured_threshold_before_expiry() -> None:
    now = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
    engine = PaperEngine(AppConfig())
    engine.set_market(market(now, threshold=64000, end_delta=timedelta(minutes=3)))

    assert should_keep_current_market(engine, now=now) is True


def test_should_not_keep_current_market_without_threshold() -> None:
    now = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
    engine = PaperEngine(AppConfig())
    engine.set_market(market(now, threshold=None, end_delta=timedelta(minutes=3)))

    assert should_keep_current_market(engine, now=now) is True


def test_should_not_keep_current_market_after_expiry() -> None:
    now = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
    engine = PaperEngine(AppConfig())
    engine.set_market(market(now, threshold=64000, end_delta=timedelta(seconds=-1)))

    assert should_keep_current_market(engine, now=now) is False


def interval_market(start: datetime, *, condition_id: str = "m1", threshold: float | None = None) -> MarketState:
    return MarketState(
        condition_id=condition_id,
        slug=f"btc-updown-5m-{int(start.timestamp())}",
        question="Bitcoin Up or Down",
        threshold_price=threshold,
        threshold_source="binance_first_tick_after_start" if threshold is not None else "dynamic_start_price",
        threshold_verified=False,
        start_time=start,
        end_time=start + timedelta(minutes=5),
        up_token_id=f"{condition_id}-up",
        down_token_id=f"{condition_id}-down",
    )


class FakePolymarketClient:
    def __init__(self, price: float = 64001.5, *, include_outcome: bool = True, previous_close: float | None = None):
        self.price = price
        self.include_outcome = include_outcome
        self.previous_close = price if previous_close is None else previous_close
        self.outcome_calls = 0

    async def outcome_price(self, market_slug: str):
        from polybtc.clients import PolymarketOutcomePrice

        self.outcome_calls += 1
        if not self.include_outcome:
            return None
        start = datetime.fromtimestamp(int(market_slug.rsplit("-", 1)[1]), tz=timezone.utc)
        return PolymarketOutcomePrice(
            slug=market_slug,
            open_price=self.price,
            close_price=None,
            start_time=start,
            end_time=start + timedelta(minutes=5),
        )

    async def past_results(self, market_slug: str):
        from polybtc.clients import PolymarketPastResult

        start = datetime.fromtimestamp(int(market_slug.rsplit("-", 1)[1]), tz=timezone.utc)
        return [
            PolymarketPastResult(
                start_time=start - timedelta(minutes=5),
                end_time=start,
                open_price=63972.0,
                close_price=self.previous_close,
                outcome="up",
            )
        ]


class FakeDiscoverClient(FakePolymarketClient):
    async def discover_markets(self):
        now = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
        return [interval_market(now), interval_market(now + timedelta(minutes=5), condition_id="m2")]


class FakeEventThresholdClient(FakePolymarketClient):
    def __init__(self, price: float = 64001.5, *, event_price: float = 64001.5):
        super().__init__(price)
        self.event_price = event_price

    async def event_threshold(self, market_slug: str):
        return self.event_price


class FakePrefetchClient(FakePolymarketClient):
    async def discover_markets(self):
        now = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
        return [
            interval_market(now, threshold=64000),
            interval_market(now + timedelta(minutes=5), condition_id="m2", threshold=64999),
            interval_market(now + timedelta(minutes=15), condition_id="m4"),
        ]


def test_apply_polymarket_page_threshold_rejects_previous_close_without_current_open() -> None:
    now = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
    current = interval_market(now)

    applied = __import__("asyncio").run(
        apply_polymarket_page_threshold(
            FakePolymarketClient(include_outcome=False), current, now=now + timedelta(seconds=5)
        )
    )

    assert applied is True
    assert current.threshold_price is None
    assert current.threshold_verified is False
    assert current.threshold_source == "threshold_verification_failed"


def test_apply_polymarket_page_threshold_rejects_current_open_without_previous_page_close() -> None:
    class MissingPreviousClient(FakePolymarketClient):
        async def past_results(self, market_slug: str):
            return []

    now = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
    current = interval_market(now)

    __import__("asyncio").run(
        apply_polymarket_page_threshold(
            MissingPreviousClient(), current, now=now + timedelta(seconds=5)
        )
    )

    assert current.threshold_price is None
    assert current.threshold_verified is False


def test_apply_polymarket_page_threshold_fast_verifies_page_with_exact_rtds_start_tick() -> None:
    class MissingPreviousClient(FakePolymarketClient):
        async def past_results(self, market_slug: str):
            return []

    start = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
    current = interval_market(start)
    current.threshold_candidate_price = 64001.5
    current.threshold_candidate_source = "polymarket_rtds_start_tick"
    current.threshold_candidate_observed_at = start
    current.threshold_candidate_received_at = start - timedelta(milliseconds=500)

    __import__("asyncio").run(
        apply_polymarket_page_threshold(
            MissingPreviousClient(), current, now=start + timedelta(seconds=1)
        )
    )

    assert current.threshold_price == 64001.5
    assert current.threshold_source == "polymarket_page_rtds_verified_open_price"
    assert current.threshold_verified is True


def test_apply_polymarket_page_threshold_rejects_rtds_page_mismatch() -> None:
    class MissingPreviousClient(FakePolymarketClient):
        async def past_results(self, market_slug: str):
            return []

    start = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
    current = interval_market(start)
    current.threshold_candidate_price = 64010
    current.threshold_candidate_source = "polymarket_rtds_start_tick"
    current.threshold_candidate_observed_at = start
    current.threshold_candidate_received_at = start

    __import__("asyncio").run(
        apply_polymarket_page_threshold(
            MissingPreviousClient(), current, now=start + timedelta(seconds=1)
        )
    )

    assert current.threshold_price is None
    assert current.threshold_verified is False


def test_apply_polymarket_page_threshold_retries_until_page_matches_rtds() -> None:
    class MissingPreviousClient(FakePolymarketClient):
        async def past_results(self, market_slug: str):
            return []

    start = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
    current = interval_market(start)
    current.threshold_candidate_price = 64001.5
    current.threshold_candidate_source = "polymarket_rtds_start_tick"
    current.threshold_candidate_observed_at = start
    current.threshold_candidate_received_at = start - timedelta(milliseconds=500)

    __import__("asyncio").run(
        apply_polymarket_page_threshold(
            MissingPreviousClient(price=63999), current, now=start + timedelta(seconds=1)
        )
    )
    assert current.threshold_verified is False

    __import__("asyncio").run(
        apply_polymarket_page_threshold(
            MissingPreviousClient(price=64001.5), current, now=start + timedelta(seconds=3)
        )
    )
    assert current.threshold_price == 64001.5
    assert current.threshold_verified is True


def test_apply_polymarket_page_threshold_rechecks_late_mismatching_rtds_candidate() -> None:
    start = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
    current = interval_market(start)
    current.threshold_price = 64001.5
    current.threshold_source = "polymarket_page_verified_open_price"
    current.threshold_observed_at = start
    current.threshold_verified = True
    current.threshold_fetched_at = start + timedelta(seconds=5)
    current.threshold_candidate_price = 64010
    current.threshold_candidate_source = "polymarket_rtds_start_tick"
    current.threshold_candidate_observed_at = start
    current.threshold_candidate_received_at = start

    changed = __import__("asyncio").run(
        apply_polymarket_page_threshold(
            FakePolymarketClient(price=64001.5), current, now=start + timedelta(seconds=10)
        )
    )

    assert changed is True
    assert current.threshold_price is None
    assert current.threshold_verified is False


def test_apply_polymarket_page_threshold_rejects_conflicted_rtds_candidate() -> None:
    start = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
    current = interval_market(start)
    current.threshold_candidate_source = "polymarket_rtds_conflict"
    current.threshold_candidate_observed_at = start
    current.threshold_candidate_conflicted = True

    __import__("asyncio").run(
        apply_polymarket_page_threshold(
            FakePolymarketClient(), current, now=start + timedelta(seconds=1)
        )
    )

    assert current.threshold_price is None
    assert current.threshold_verified is False


def test_apply_polymarket_page_threshold_verifies_matching_open_and_previous_close() -> None:
    now = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
    current = interval_market(now)

    applied = __import__("asyncio").run(
        apply_polymarket_page_threshold(FakePolymarketClient(), current, now=now + timedelta(seconds=5))
    )

    assert applied is True
    assert current.threshold_price == 64001.5
    assert current.threshold_source == "polymarket_page_verified_open_price"
    assert current.threshold_observed_at == now
    assert current.threshold_verified is True
    assert current.threshold_fetched_at == now + timedelta(seconds=5)


def test_apply_polymarket_page_threshold_rejects_open_close_mismatch() -> None:
    now = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
    current = interval_market(now)

    __import__("asyncio").run(
        apply_polymarket_page_threshold(
            FakePolymarketClient(price=64001.5, previous_close=64000.25),
            current,
            now=now + timedelta(seconds=5),
        )
    )

    assert current.threshold_price is None
    assert current.threshold_verified is False


def test_apply_polymarket_page_threshold_replaces_provisional_binance_tick() -> None:
    now = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
    current = interval_market(now, threshold=64099.0)

    __import__("asyncio").run(
        apply_polymarket_page_threshold(FakePolymarketClient(), current, now=now + timedelta(seconds=5))
    )

    assert current.threshold_price == 64001.5
    assert current.threshold_verified is True


def test_apply_polymarket_page_threshold_cross_checks_gamma_event_price() -> None:
    now = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
    current = interval_market(now)

    __import__("asyncio").run(
        apply_polymarket_page_threshold(FakeEventThresholdClient(), current, now=now + timedelta(seconds=5))
    )

    assert current.threshold_price == 64001.5
    assert current.threshold_source == "gamma_page_verified_price_to_beat"
    assert current.threshold_verified is True


def test_apply_polymarket_page_threshold_rejects_gamma_page_mismatch() -> None:
    now = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
    current = interval_market(now)

    __import__("asyncio").run(
        apply_polymarket_page_threshold(
            FakeEventThresholdClient(event_price=64010), current, now=now + timedelta(seconds=5)
        )
    )

    assert current.threshold_price is None
    assert current.threshold_verified is False


def test_apply_polymarket_page_threshold_does_not_fetch_before_start() -> None:
    now = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
    client = FakePolymarketClient()
    upcoming = interval_market(now + timedelta(seconds=10), threshold=64999)

    __import__("asyncio").run(apply_polymarket_page_threshold(client, upcoming, now=now))

    assert client.outcome_calls == 0
    assert upcoming.threshold_price is None
    assert upcoming.threshold_verified is False


def test_apply_polymarket_page_threshold_waits_one_second_after_start() -> None:
    start = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
    client = FakePolymarketClient()
    current = interval_market(start)

    __import__("asyncio").run(
        apply_polymarket_page_threshold(client, current, now=start + timedelta(milliseconds=999))
    )
    assert client.outcome_calls == 0
    assert current.threshold_verified is False

    __import__("asyncio").run(
        apply_polymarket_page_threshold(client, current, now=start + timedelta(seconds=1))
    )
    assert client.outcome_calls == 1
    assert current.threshold_verified is True


def test_apply_polymarket_page_threshold_rechecks_prestart_final_looking_value() -> None:
    start = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
    current = interval_market(start)
    current.threshold_price = 64714.03323555351
    current.threshold_source = "polymarket_page_verified_open_price"
    current.threshold_verified = True
    current.threshold_fetched_at = start - timedelta(seconds=10)
    client = FakePolymarketClient(price=64708.45856971535)

    __import__("asyncio").run(
        apply_polymarket_page_threshold(client, current, now=start + timedelta(seconds=5))
    )

    assert current.threshold_price == 64708.45856971535
    assert current.threshold_verified is True
    assert current.threshold_fetched_at == start + timedelta(seconds=5)


def test_apply_polymarket_page_threshold_rejects_wrong_outcome_interval() -> None:
    class WrongIntervalClient(FakePolymarketClient):
        async def outcome_price(self, market_slug: str):
            outcome = await super().outcome_price(market_slug)
            assert outcome is not None and outcome.start_time is not None and outcome.end_time is not None
            return outcome.__class__(
                slug=outcome.slug,
                open_price=outcome.open_price,
                start_time=outcome.start_time + timedelta(minutes=5),
                end_time=outcome.end_time + timedelta(minutes=5),
            )

    start = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
    current = interval_market(start)

    __import__("asyncio").run(
        apply_polymarket_page_threshold(
            WrongIntervalClient(), current, now=start + timedelta(seconds=5)
        )
    )

    assert current.threshold_price is None
    assert current.threshold_verified is False


def test_apply_polymarket_page_threshold_rejects_nonadjacent_previous_result() -> None:
    class WrongPreviousClient(FakePolymarketClient):
        async def past_results(self, market_slug: str):
            results = await super().past_results(market_slug)
            previous = results[0]
            return [
                previous.__class__(
                    start_time=previous.start_time - timedelta(seconds=1),
                    end_time=previous.end_time,
                    open_price=previous.open_price,
                    close_price=previous.close_price,
                )
            ]

    start = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
    current = interval_market(start)

    __import__("asyncio").run(
        apply_polymarket_page_threshold(
            WrongPreviousClient(), current, now=start + timedelta(seconds=5)
        )
    )

    assert current.threshold_price is None
    assert current.threshold_verified is False


def test_current_market_with_page_threshold_keeps_current_after_lag() -> None:
    now = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
    selected = __import__("asyncio").run(
        current_market_with_page_threshold(
            FakeDiscoverClient(), max_start_price_lag_ms=2000, now=now + timedelta(seconds=5)
        )
    )

    assert selected is not None
    assert selected.condition_id == "m1"
    assert selected.threshold_price == 64001.5
    assert selected.threshold_verified is True


def test_prefetch_next_market_threshold_only_caches_adjacent_metadata() -> None:
    now = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
    current = interval_market(now, threshold=64000)

    prefetched = __import__("asyncio").run(prefetch_next_market_threshold(FakePrefetchClient(), current, AppConfig()))

    assert prefetched is not None
    assert prefetched.condition_id == "m2"
    assert prefetched.threshold_price is None
    assert prefetched.threshold_verified is False


def test_prefetch_next_market_threshold_rejects_gap() -> None:
    class GapClient(FakePolymarketClient):
        async def discover_markets(self):
            start = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
            return [
                interval_market(start),
                interval_market(start + timedelta(minutes=10), condition_id="m3"),
            ]

    start = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)

    prefetched = __import__("asyncio").run(
        prefetch_next_market_threshold(GapClient(), interval_market(start), AppConfig())
    )

    assert prefetched is None


def test_threshold_retry_is_throttled_by_refresh_interval() -> None:
    now = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
    next_retry = now

    assert should_retry_threshold(now, next_retry) is True
    next_retry = now + timedelta(seconds=5)
    assert should_retry_threshold(now + timedelta(seconds=1), next_retry) is False
    assert should_retry_threshold(now + timedelta(seconds=5), next_retry) is True


def test_coalesce_live_events_keeps_latest_tick_and_books() -> None:
    events = [
        ("tick", {"price": 1}),
        ("book", (Direction.UP, "old-up")),
        ("book", (Direction.DOWN, "old-down")),
        ("tick", {"price": 2}),
        ("book", (Direction.UP, "new-up")),
        ("market", {"slug": "m1"}),
        ("tick", {"price": 3}),
        ("book", (Direction.DOWN, "new-down")),
    ]

    assert coalesce_live_events(events) == [
        ("book", (Direction.DOWN, "old-down")),
        ("tick", {"price": 2}),
        ("book", (Direction.UP, "new-up")),
        ("market", {"slug": "m1"}),
        ("tick", {"price": 3}),
        ("book", (Direction.DOWN, "new-down")),
    ]


def test_coalesce_live_events_keeps_newest_book_timestamp() -> None:
    now = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
    newer = OrderBookSnapshot(token_id="up", market_id="m1", timestamp=now + timedelta(milliseconds=2))
    older = OrderBookSnapshot(token_id="up", market_id="m1", timestamp=now + timedelta(milliseconds=1))

    events = [("book", (Direction.UP, newer)), ("book", (Direction.UP, older))]

    assert coalesce_live_events(events) == [("book", (Direction.UP, newer))]


def test_coalesce_live_events_keeps_last_snapshot_for_equal_timestamp() -> None:
    now = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
    partial = OrderBookSnapshot(
        token_id="up",
        market_id="m1",
        timestamp=now,
        received_at=now,
        bids=[{"price": 0.40, "size": 10}],
        asks=[{"price": 0.45, "size": 10}],
    )
    final = OrderBookSnapshot(
        token_id="up",
        market_id="m1",
        timestamp=now,
        received_at=now + timedelta(microseconds=1),
        bids=[{"price": 0.42, "size": 10}],
        asks=[{"price": 0.43, "size": 10}],
    )

    events = [("book", (Direction.UP, partial)), ("book", (Direction.UP, final))]

    assert coalesce_live_events(events) == [("book", (Direction.UP, final))]


def test_coalesce_live_events_keeps_fresh_rest_snapshot_with_older_source_time() -> None:
    now = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
    websocket_book = OrderBookSnapshot(token_id="up", market_id="m1", timestamp=now, received_at=now)
    rest_book = OrderBookSnapshot(
        token_id="up",
        market_id="m1",
        timestamp=now - timedelta(seconds=1),
        received_at=now + timedelta(milliseconds=1),
        raw={"_transport": "rest", "_request_started_at": (now + timedelta(microseconds=1)).isoformat()},
    )

    events = [("book", (Direction.UP, websocket_book)), ("book", (Direction.UP, rest_book))]

    assert coalesce_live_events(events) == [("book", (Direction.UP, rest_book))]
    assert rest_book.timestamp == websocket_book.timestamp


def test_coalesce_live_events_drops_rest_snapshot_superseded_during_request() -> None:
    now = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
    websocket_book = OrderBookSnapshot(
        token_id="up",
        market_id="m1",
        timestamp=now,
        received_at=now + timedelta(milliseconds=10),
    )
    rest_book = OrderBookSnapshot(
        token_id="up",
        market_id="m1",
        timestamp=now - timedelta(seconds=1),
        received_at=now + timedelta(milliseconds=20),
        raw={"_transport": "rest", "_request_started_at": now.isoformat()},
    )

    events = [("book", (Direction.UP, websocket_book)), ("book", (Direction.UP, rest_book))]

    assert coalesce_live_events(events) == [("book", (Direction.UP, websocket_book))]


def test_book_publication_is_immediate_on_top_change_and_rate_limited_otherwise() -> None:
    now = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
    previous = OrderBookSnapshot(
        token_id="up",
        market_id="m1",
        received_at=now,
        bids=[{"price": 0.40, "size": 10}],
        asks=[{"price": 0.41, "size": 10}],
    )
    depth_only = previous.model_copy(deep=True)
    depth_only.received_at = now + timedelta(milliseconds=20)
    depth_only.bids.append(BookLevel(price=0.39, size=5))
    changed_top = depth_only.model_copy(deep=True)
    changed_top.bids.append(BookLevel(price=0.42, size=5))
    heartbeat = depth_only.model_copy(deep=True)
    heartbeat.received_at = now + timedelta(milliseconds=250)

    assert should_publish_book_update(None, previous, None) is True
    assert should_publish_book_update(previous, depth_only, now) is False
    assert should_publish_book_update(previous, changed_top, now) is True
    assert should_publish_book_update(previous, heartbeat, now) is True


def test_live_book_payload_keeps_only_top_levels_without_raw_depth() -> None:
    now = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
    snapshot = OrderBookSnapshot(
        token_id="up",
        market_id="m1",
        timestamp=now,
        received_at=now,
        bids=[BookLevel(price=0.39, size=5), BookLevel(price=0.40, size=10)],
        asks=[BookLevel(price=0.42, size=7), BookLevel(price=0.41, size=8)],
        depth_trusted=True,
        raw={"large": ["unused"] * 100},
    )

    payload = live_book_payload(snapshot)

    assert payload["bids"] == [{"price": 0.40, "size": 10.0}]
    assert payload["asks"] == [{"price": 0.41, "size": 8.0}]
    assert payload["depth_trusted"] is True
    assert "raw" not in payload


def test_rest_book_fallback_only_runs_when_books_are_missing_or_stale() -> None:
    now = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
    engine = PaperEngine(AppConfig())
    current_market = market(now, threshold=64000, end_delta=timedelta(minutes=3))
    engine.set_market(current_market)

    assert books_need_rest_refresh(engine, current_market, now) is True

    engine.set_book(
        Direction.UP,
        OrderBookSnapshot(
            token_id="up", market_id="m1", timestamp=now, received_at=now, depth_trusted=True
        ),
    )
    engine.set_book(
        Direction.DOWN,
        OrderBookSnapshot(
            token_id="down", market_id="m1", timestamp=now, received_at=now, depth_trusted=True
        ),
    )
    assert books_need_rest_refresh(engine, current_market, now) is False

    engine.books[Direction.UP].depth_trusted = False
    assert books_need_rest_refresh(engine, current_market, now) is True
    engine.books[Direction.UP].depth_trusted = True

    engine.books[Direction.DOWN].received_at = now - timedelta(seconds=2)
    assert books_need_rest_refresh(engine, current_market, now) is True


def test_rest_book_reconciliation_runs_even_when_websocket_arrivals_are_fresh() -> None:
    now = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
    engine = PaperEngine(AppConfig())
    current_market = market(now, threshold=64000, end_delta=timedelta(minutes=3))
    engine.set_market(current_market)
    engine.set_book(
        Direction.UP,
        OrderBookSnapshot(
            token_id="up", market_id="m1", timestamp=now, received_at=now, depth_trusted=True
        ),
    )
    engine.set_book(
        Direction.DOWN,
        OrderBookSnapshot(
            token_id="down", market_id="m1", timestamp=now, received_at=now, depth_trusted=True
        ),
    )

    assert books_need_rest_refresh(engine, current_market, now, last_rest_refresh_at=now) is False

    websocket_arrival = now + timedelta(milliseconds=400)
    engine.books[Direction.UP].received_at = websocket_arrival
    engine.books[Direction.DOWN].received_at = websocket_arrival

    assert books_need_rest_refresh(
        engine,
        current_market,
        now + timedelta(milliseconds=500),
        last_rest_refresh_at=now,
    ) is False
    assert books_need_rest_refresh(
        engine,
        current_market,
        now + timedelta(seconds=2),
        last_rest_refresh_at=now,
    ) is True
