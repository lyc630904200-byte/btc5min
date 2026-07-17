from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import SourceConfig
from .models import MarketState


THRESHOLD_RE = re.compile(r"(?<![\d.])(?:\$\s*)?([1-9]\d{1,2}(?:,?\d{3})+(?:\.\d+)?)")
BTC_FIVE_MINUTE_SLUG_RE = re.compile(r"^(?:bitcoin-|btc-)updown-5m-(?P<start>\d+)$")
FIVE_MINUTES = 5 * 60
VERIFIED_DYNAMIC_THRESHOLD_SOURCES = {
    "polymarket_page_verified_open_price",
    "gamma_page_verified_price_to_beat",
    "polymarket_page_rtds_verified_open_price",
}


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def parse_json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not value:
        return []
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
            return loaded if isinstance(loaded, list) else []
        except json.JSONDecodeError:
            return []
    return []


def market_interval_from_slug(slug: str) -> tuple[datetime, datetime] | None:
    match = BTC_FIVE_MINUTE_SLUG_RE.fullmatch(slug)
    if not match:
        return None
    try:
        timestamp = int(match.group("start"))
        if timestamp % FIVE_MINUTES != 0:
            return None
        start = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return start, start + timedelta(seconds=FIVE_MINUTES)


def market_is_active(market: MarketState, now: datetime) -> bool:
    return market.start_time is not None and market.start_time <= now < market.end_time


def markets_are_adjacent(current: MarketState, candidate: MarketState) -> bool:
    return (
        current.start_time is not None
        and candidate.start_time is not None
        and current.end_time - current.start_time == timedelta(seconds=FIVE_MINUTES)
        and candidate.end_time - candidate.start_time == timedelta(seconds=FIVE_MINUTES)
        and candidate.start_time == current.end_time
    )


def threshold_is_tradable(market: MarketState) -> bool:
    if market.threshold_price is None or not market.threshold_verified:
        return False
    interval = market_interval_from_slug(market.slug)
    if interval is None:
        # Fixed-threshold markets without the canonical dynamic five-minute
        # slug retain their existing structured Gamma verification path.
        return True
    common_verified = bool(
        market.threshold_source in VERIFIED_DYNAMIC_THRESHOLD_SOURCES
        and market.start_time is not None
        and interval == (market.start_time, market.end_time)
        and market.threshold_observed_at == market.start_time
        and market.threshold_fetched_at is not None
        and market.threshold_fetched_at >= market.start_time
        and not market.threshold_candidate_conflicted
    )
    if not common_verified:
        return False
    candidate_fields_present = any(
        value is not None
        for value in (
            market.threshold_candidate_price,
            market.threshold_candidate_source,
            market.threshold_candidate_observed_at,
            market.threshold_candidate_received_at,
        )
    )
    candidate_is_valid = bool(
        market.threshold_candidate_price is not None
        and abs(market.threshold_candidate_price - market.threshold_price) <= 0.01
        and market.threshold_candidate_source == "polymarket_rtds_start_tick"
        and market.threshold_candidate_observed_at == market.start_time
        and market.threshold_candidate_received_at is not None
        and market.start_time - timedelta(seconds=1)
        <= market.threshold_candidate_received_at
        <= market.start_time + timedelta(seconds=2)
    )
    if candidate_fields_present and not candidate_is_valid:
        return False
    if market.threshold_source == "polymarket_page_rtds_verified_open_price":
        return candidate_is_valid
    return True


def parse_threshold(payload: dict[str, Any]) -> float | None:
    for key in ("groupItemThreshold", "threshold", "strikePrice", "targetPrice"):
        value = payload.get(key)
        try:
            parsed = float(str(value).replace(",", ""))
        except (TypeError, ValueError):
            continue
        if parsed >= 1000:
            return parsed

    text = " ".join(
        str(payload.get(key) or "") for key in ("question", "title", "slug", "description", "market_slug")
    )
    candidates = [float(match.group(1).replace(",", "")) for match in THRESHOLD_RE.finditer(text)]
    # Ignore Unix-like event timestamps embedded in slugs such as btc-updown-5m-1783704300.
    btc_like = [candidate for candidate in candidates if 1000 <= candidate <= 1_000_000]
    return btc_like[0] if btc_like else None


def is_btc_five_min_candidate(payload: dict[str, Any], config: SourceConfig, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    text = " ".join(
        str(payload.get(key) or "") for key in ("question", "title", "slug", "description", "market_slug")
    ).lower()
    if not any(pattern.lower() in text for pattern in config.market_slug_patterns):
        return False
    if not ("bitcoin" in text or "btc" in text):
        return False
    if "5m" not in text and "5 min" not in text and "5 minute" not in text and "five minute" not in text:
        if config.market_slug and str(payload.get("slug") or payload.get("market_slug") or "") == config.market_slug:
            pass
        else:
            return False
    if payload.get("closed") is True or payload.get("active") is False:
        return False
    if payload.get("enableOrderBook") is False and payload.get("enable_order_book") is False:
        return False
    end_time = parse_datetime(payload.get("endDate") or payload.get("end_date_iso"))
    if end_time is None:
        return False
    delta = (end_time - now).total_seconds()
    return -60 <= delta <= 12 * 60


def settlement_is_comparable(payload: dict[str, Any]) -> bool:
    text = " ".join(str(payload.get(key) or "") for key in ("description", "resolutionSource", "question", "slug"))
    lowered = text.lower()
    if "bitcoin" not in lowered and "btc" not in lowered:
        return False
    if "binance" in lowered or "chain.link" in lowered or "chainlink" in lowered or "btc" in lowered or "bitcoin" in lowered:
        return True
    return False


def has_dynamic_start_threshold(payload: dict[str, Any]) -> bool:
    text = " ".join(str(payload.get(key) or "") for key in ("description", "question", "title", "slug"))
    lowered = text.lower()
    return "beginning of" in lowered or "beginning price" in lowered or "start price" in lowered


def parse_market(payload: dict[str, Any], config: SourceConfig, now: datetime | None = None) -> MarketState | None:
    if not is_btc_five_min_candidate(payload, config, now=now):
        return None

    outcomes = [str(item) for item in parse_json_list(payload.get("outcomes"))]
    token_ids = [str(item) for item in parse_json_list(payload.get("clobTokenIds"))]
    if not outcomes and payload.get("tokens"):
        tokens = payload.get("tokens") or []
        outcomes = [str(token.get("outcome")) for token in tokens]
        token_ids = [str(token.get("token_id")) for token in tokens]
    if len(outcomes) < 2 or len(token_ids) < 2:
        return None

    up_index = None
    down_index = None
    for idx, outcome in enumerate(outcomes):
        normalized = outcome.strip().lower()
        if normalized in {"yes", "up", "higher", "above"}:
            up_index = idx
        if normalized in {"no", "down", "lower", "below"}:
            down_index = idx
    if up_index is None:
        up_index = 0
    if down_index is None:
        down_index = 1 if up_index == 0 else 0
    if up_index >= len(token_ids) or down_index >= len(token_ids) or up_index == down_index:
        return None

    end_time = parse_datetime(payload.get("endDate") or payload.get("end_date_iso"))
    if end_time is None:
        return None
    slug = str(payload.get("slug") or payload.get("market_slug") or "")
    canonical_interval = market_interval_from_slug(slug)
    explicit_start = parse_datetime(
        payload.get("eventStartTime") or payload.get("startTime") or payload.get("start_time")
    )
    if canonical_interval is not None:
        canonical_start, canonical_end = canonical_interval
        if end_time != canonical_end or (explicit_start is not None and explicit_start != canonical_start):
            return None
        start_time = canonical_start
    else:
        start_time = explicit_start or end_time - timedelta(seconds=FIVE_MINUTES)
        if end_time - start_time != timedelta(seconds=FIVE_MINUTES):
            return None

    dynamic_threshold = has_dynamic_start_threshold(payload)
    # Five-minute beginning-price markets must be verified against the exact
    # Chainlink interval after it starts.  Gamma fields and numbers embedded in
    # text are not a safe final threshold for these markets.
    threshold = None if dynamic_threshold else parse_threshold(payload)
    settlement_verified = (threshold is not None or dynamic_threshold) and settlement_is_comparable(payload)
    observe_only = config.observe_only_on_unverified_settlement and not settlement_verified

    return MarketState(
        condition_id=str(payload.get("conditionId") or payload.get("condition_id") or payload.get("id") or ""),
        slug=slug,
        question=str(payload.get("question") or payload.get("title") or ""),
        threshold_price=threshold,
        threshold_source="gamma" if threshold is not None else "dynamic_start_price" if dynamic_threshold else None,
        threshold_verified=threshold is not None,
        start_time=start_time,
        end_time=end_time,
        up_token_id=token_ids[up_index],
        down_token_id=token_ids[down_index],
        min_order_size=float(payload.get("orderMinSize") or payload.get("minimum_order_size") or 5),
        tick_size=float(payload.get("orderPriceMinTickSize") or payload.get("minimum_tick_size") or 0.01),
        accepting_orders=bool(payload.get("acceptingOrders", payload.get("accepting_orders", True))),
        settlement_verified=settlement_verified,
        observe_only=observe_only,
        raw=payload,
    )


def choose_current_market(
    markets: list[MarketState],
    now: datetime | None = None,
    max_start_price_lag_ms: int = 0,
) -> MarketState | None:
    now = now or datetime.now(timezone.utc)
    # Kept for API compatibility.  A future market is never considered active,
    # regardless of the dynamic-threshold capture tolerance.
    _ = max_start_price_lag_ms
    candidates = [market for market in markets if market_is_active(market, now)]
    return sorted(candidates, key=lambda market: market.end_time)[0] if candidates else None
