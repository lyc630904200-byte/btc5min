from datetime import datetime, timedelta, timezone

from polybtc.config import AppConfig
from polybtc.engine import PaperEngine
from polybtc.models import Direction, MarketState, OrderBookSnapshot
from polybtc.runner import (
    apply_polymarket_page_threshold,
    books_need_rest_refresh,
    coalesce_live_events,
    current_market_with_page_threshold,
    prefetch_next_market_threshold,
    should_keep_current_market,
    should_retry_threshold,
)


def market(now: datetime, threshold: float | None, end_delta: timedelta) -> MarketState:
    return MarketState(
        condition_id="m1",
        slug="btc-updown-5m-test",
        question="Bitcoin Up or Down",
        threshold_price=threshold,
        threshold_source="binance_first_tick_after_start" if threshold is not None else "dynamic_start_price",
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

    assert should_keep_current_market(engine, now=now) is False


def test_should_not_keep_current_market_after_expiry() -> None:
    now = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
    engine = PaperEngine(AppConfig())
    engine.set_market(market(now, threshold=64000, end_delta=timedelta(seconds=-1)))

    assert should_keep_current_market(engine, now=now) is False


class FakePolymarketClient:
    async def outcome_price(self, market_slug: str):
        return None

    async def past_results(self, market_slug: str):
        from polybtc.clients import PolymarketPastResult

        return [
            PolymarketPastResult(
                start_time=datetime(2026, 7, 11, 1, 55, tzinfo=timezone.utc),
                end_time=datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc),
                open_price=63972.0,
                close_price=64000.25,
                outcome="up",
            )
        ]


class FakeDiscoverClient(FakePolymarketClient):
    async def discover_markets(self):
        now = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
        stale_current = market(now, threshold=None, end_delta=timedelta(minutes=4))
        stale_current.start_time = now
        upcoming = market(now, threshold=None, end_delta=timedelta(minutes=9))
        upcoming.condition_id = "m2"
        upcoming.start_time = now + timedelta(minutes=5)
        return [stale_current, upcoming]


class FakeOutcomePriceClient(FakePolymarketClient):
    async def outcome_price(self, market_slug: str):
        from polybtc.clients import PolymarketOutcomePrice

        return PolymarketOutcomePrice(slug=market_slug, open_price=64001.5, close_price=None)


class FakeEventThresholdClient(FakePolymarketClient):
    async def event_threshold(self, market_slug: str):
        return 64002.25


class FakePrefetchClient(FakeOutcomePriceClient):
    async def discover_markets(self):
        now = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
        current = market(now, threshold=64000, end_delta=timedelta(minutes=5))
        upcoming = market(now, threshold=None, end_delta=timedelta(minutes=10))
        upcoming.condition_id = "m2"
        upcoming.slug = "btc-updown-5m-next"
        upcoming.start_time = now + timedelta(minutes=5)
        return [current, upcoming]


def test_apply_polymarket_page_threshold_uses_previous_close() -> None:
    now = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
    current = market(now, threshold=None, end_delta=timedelta(minutes=5))
    current.start_time = now

    applied = __import__("asyncio").run(apply_polymarket_page_threshold(FakePolymarketClient(), current))

    assert applied is True
    assert current.threshold_price == 64000.25
    assert current.threshold_source == "polymarket_page_previous_close"
    assert current.threshold_observed_at == now


def test_apply_polymarket_page_threshold_prefers_current_open_price() -> None:
    now = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
    current = market(now, threshold=None, end_delta=timedelta(minutes=5))
    current.start_time = now

    applied = __import__("asyncio").run(apply_polymarket_page_threshold(FakeOutcomePriceClient(), current))

    assert applied is True
    assert current.threshold_price == 64001.5
    assert current.threshold_source == "polymarket_page_open_price"
    assert current.threshold_observed_at == now


def test_apply_polymarket_page_threshold_prefers_gamma_event_price_to_beat() -> None:
    now = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
    current = market(now, threshold=None, end_delta=timedelta(minutes=5))
    current.start_time = now

    applied = __import__("asyncio").run(apply_polymarket_page_threshold(FakeEventThresholdClient(), current))

    assert applied is True
    assert current.threshold_price == 64002.25
    assert current.threshold_source == "gamma_event_price_to_beat"


def test_current_market_with_page_threshold_keeps_current_after_lag() -> None:
    now = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
    selected = __import__("asyncio").run(
        current_market_with_page_threshold(FakeDiscoverClient(), max_start_price_lag_ms=2000, now=now)
    )

    assert selected is not None
    assert selected.condition_id == "m1"
    assert selected.threshold_price == 64000.25


def test_prefetch_next_market_threshold_fetches_upcoming_market() -> None:
    now = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
    current = market(now, threshold=64000, end_delta=timedelta(minutes=5))

    prefetched = __import__("asyncio").run(prefetch_next_market_threshold(FakePrefetchClient(), current, AppConfig()))

    assert prefetched is not None
    assert prefetched.condition_id == "m2"
    assert prefetched.threshold_price == 64001.5


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


def test_rest_book_fallback_only_runs_when_books_are_missing_or_stale() -> None:
    now = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)
    engine = PaperEngine(AppConfig())
    current_market = market(now, threshold=64000, end_delta=timedelta(minutes=3))
    engine.set_market(current_market)

    assert books_need_rest_refresh(engine, current_market, now) is True

    engine.set_book(
        Direction.UP,
        OrderBookSnapshot(token_id="up", market_id="m1", timestamp=now),
    )
    engine.set_book(
        Direction.DOWN,
        OrderBookSnapshot(token_id="down", market_id="m1", timestamp=now),
    )
    assert books_need_rest_refresh(engine, current_market, now) is False

    engine.books[Direction.DOWN].timestamp = now - timedelta(seconds=2)
    assert books_need_rest_refresh(engine, current_market, now) is True
