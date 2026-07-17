from datetime import datetime, timedelta, timezone

from polybtc.config import SourceConfig
from polybtc.market import choose_current_market, market_interval_from_slug, parse_market
from polybtc.models import MarketState


def test_parse_btc_five_min_market() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    payload = {
        "id": "1",
        "conditionId": "0xabc",
        "slug": "bitcoin-up-or-down-july-11-0105",
        "question": "Bitcoin Up or Down above $118,000?",
        "description": "Bitcoin BTC five minute market.",
        "active": True,
        "closed": False,
        "enableOrderBook": True,
        "acceptingOrders": True,
        "endDate": (now + timedelta(minutes=5)).isoformat(),
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["yes-token", "no-token"]',
        "groupItemThreshold": "118000",
        "orderMinSize": 5,
    }

    market = parse_market(payload, SourceConfig(), now=now)

    assert market is not None
    assert market.threshold_price == 118000
    assert market.up_token_id == "yes-token"
    assert market.down_token_id == "no-token"
    assert market.observe_only is False


def test_parse_dynamic_start_threshold_market() -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    payload = {
        "id": "1",
        "conditionId": "0xabc",
        "slug": f"btc-updown-5m-{int(now.timestamp())}",
        "question": "Bitcoin Up or Down - July 10, 9:10PM-9:15PM ET",
        "description": "Resolves Up if the Bitcoin price at the end is greater than or equal to the price at the beginning of that range. Resolution source is Chainlink BTC/USD.",
        "resolutionSource": "https://data.chain.link/streams/btc-usd",
        "active": True,
        "closed": False,
        "enableOrderBook": True,
        "acceptingOrders": True,
        "eventStartTime": now.isoformat(),
        "endDate": (now + timedelta(minutes=5)).isoformat(),
        "outcomes": '["Up", "Down"]',
        "clobTokenIds": '["up-token", "down-token"]',
        "groupItemThreshold": "64714.03323555351",
    }

    market = parse_market(payload, SourceConfig(), now=now)

    assert market is not None
    assert market.threshold_price is None
    assert market.threshold_source == "dynamic_start_price"
    assert market.threshold_verified is False
    assert market.start_time == now
    assert market.settlement_verified is True
    assert market.observe_only is False


def test_choose_current_market_keeps_active_market_without_threshold() -> None:
    now = datetime(2026, 7, 11, 1, 2, tzinfo=timezone.utc)
    stale = parse_market(
        {
            "id": "1",
            "conditionId": "stale",
            "slug": f"btc-updown-5m-{int((now - timedelta(minutes=2)).timestamp())}",
            "question": "Bitcoin Up or Down - stale",
            "description": "Resolves Up if Bitcoin at the end is greater than or equal to the price at the beginning of that range. Chainlink BTC/USD.",
            "resolutionSource": "https://data.chain.link/streams/btc-usd",
            "active": True,
            "closed": False,
            "enableOrderBook": True,
            "eventStartTime": (now - timedelta(minutes=2)).isoformat(),
            "endDate": (now + timedelta(minutes=3)).isoformat(),
            "outcomes": '["Up", "Down"]',
            "clobTokenIds": '["up-stale", "down-stale"]',
            "groupItemThreshold": "0",
        },
        SourceConfig(),
        now=now,
    )
    upcoming = parse_market(
        {
            "id": "2",
            "conditionId": "upcoming",
            "slug": f"btc-updown-5m-{int((now + timedelta(minutes=3)).timestamp())}",
            "question": "Bitcoin Up or Down - upcoming",
            "description": "Resolves Up if Bitcoin at the end is greater than or equal to the price at the beginning of that range. Chainlink BTC/USD.",
            "resolutionSource": "https://data.chain.link/streams/btc-usd",
            "active": True,
            "closed": False,
            "enableOrderBook": True,
            "eventStartTime": (now + timedelta(minutes=3)).isoformat(),
            "endDate": (now + timedelta(minutes=8)).isoformat(),
            "outcomes": '["Up", "Down"]',
            "clobTokenIds": '["up-next", "down-next"]',
            "groupItemThreshold": "0",
        },
        SourceConfig(),
        now=now,
    )
    assert stale is not None
    assert upcoming is not None

    selected = choose_current_market([stale, upcoming], now=now, max_start_price_lag_ms=2000)

    assert selected is stale


def test_parse_market_uses_slug_interval_instead_of_gamma_start_date() -> None:
    start = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    payload = {
        "id": "1",
        "conditionId": "m1",
        "slug": f"btc-updown-5m-{int(start.timestamp())}",
        "question": "Bitcoin Up or Down - five minute",
        "description": "Bitcoin price at the end versus the beginning of that range. Chainlink BTC/USD.",
        "resolutionSource": "https://data.chain.link/streams/btc-usd",
        "active": True,
        "closed": False,
        "enableOrderBook": True,
        "startDate": (start - timedelta(days=1)).isoformat(),
        "endDate": (start + timedelta(minutes=5)).isoformat(),
        "outcomes": '["Up", "Down"]',
        "clobTokenIds": '["up", "down"]',
        "groupItemThreshold": "0",
    }

    parsed = parse_market(payload, SourceConfig(), now=start)

    assert parsed is not None
    assert parsed.start_time == start
    assert parsed.end_time == start + timedelta(minutes=5)


def test_parse_market_rejects_slug_and_explicit_start_conflict() -> None:
    start = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    payload = {
        "id": "1",
        "conditionId": "m1",
        "slug": f"btc-updown-5m-{int(start.timestamp())}",
        "question": "Bitcoin Up or Down - five minute",
        "description": "Bitcoin price at the end versus the beginning of that range. Chainlink BTC/USD.",
        "resolutionSource": "https://data.chain.link/streams/btc-usd",
        "active": True,
        "closed": False,
        "enableOrderBook": True,
        "eventStartTime": (start + timedelta(seconds=1)).isoformat(),
        "endDate": (start + timedelta(minutes=5)).isoformat(),
        "outcomes": '["Up", "Down"]',
        "clobTokenIds": '["up", "down"]',
        "groupItemThreshold": "0",
    }

    assert parse_market(payload, SourceConfig(), now=start) is None


def test_choose_current_market_never_selects_future_and_switches_at_boundary() -> None:
    boundary = datetime(2026, 7, 11, 1, 5, tzinfo=timezone.utc)

    def make_market(start: datetime, condition_id: str) -> MarketState:
        return MarketState(
            condition_id=condition_id,
            slug=f"btc-updown-5m-{int(start.timestamp())}",
            question="Bitcoin Up or Down",
            threshold_price=None,
            start_time=start,
            end_time=start + timedelta(minutes=5),
            up_token_id=f"{condition_id}-up",
            down_token_id=f"{condition_id}-down",
        )

    old = make_market(boundary - timedelta(minutes=5), "old")
    current = make_market(boundary, "current")
    future = make_market(boundary + timedelta(minutes=5), "future")

    assert choose_current_market([future], now=boundary) is None
    assert choose_current_market([old, current, future], now=boundary) is current
    assert market_interval_from_slug(current.slug) == (current.start_time, current.end_time)
    assert market_interval_from_slug(f"btc-updown-5m-{int(boundary.timestamp()) + 1}") is None
