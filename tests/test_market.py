from datetime import datetime, timedelta, timezone

from polybtc.config import SourceConfig
from polybtc.market import choose_current_market, parse_market


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
        "slug": "btc-updown-5m-1783732200",
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
        "groupItemThreshold": "0",
    }

    market = parse_market(payload, SourceConfig(), now=now)

    assert market is not None
    assert market.threshold_price is None
    assert market.threshold_source == "dynamic_start_price"
    assert market.start_time == now
    assert market.settlement_verified is True
    assert market.observe_only is False


def test_choose_current_market_keeps_active_market_without_threshold() -> None:
    now = datetime(2026, 7, 11, 1, 2, tzinfo=timezone.utc)
    stale = parse_market(
        {
            "id": "1",
            "conditionId": "stale",
            "slug": "btc-updown-5m-1783732200",
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
            "slug": "btc-updown-5m-1783732500",
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
