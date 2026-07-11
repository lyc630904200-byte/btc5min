from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from .config import SourceConfig
from .models import MarketState


THRESHOLD_RE = re.compile(r"(?<![\d.])(?:\$\s*)?([1-9]\d{1,2}(?:,?\d{3})+(?:\.\d+)?)")


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
    start_time = parse_datetime(
        payload.get("eventStartTime")
        or payload.get("startTime")
        or payload.get("start_time")
        or payload.get("startDate")
        or payload.get("start_date_iso")
    )
    threshold = parse_threshold(payload)
    dynamic_threshold = threshold is None and has_dynamic_start_threshold(payload)
    settlement_verified = (threshold is not None or dynamic_threshold) and settlement_is_comparable(payload)
    observe_only = config.observe_only_on_unverified_settlement and not settlement_verified

    return MarketState(
        condition_id=str(payload.get("conditionId") or payload.get("condition_id") or payload.get("id") or ""),
        slug=str(payload.get("slug") or payload.get("market_slug") or ""),
        question=str(payload.get("question") or payload.get("title") or ""),
        threshold_price=threshold,
        threshold_source="gamma" if threshold is not None else "dynamic_start_price" if dynamic_threshold else None,
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
    candidates = [market for market in markets if market.end_time >= now]
    return sorted(candidates, key=lambda market: market.end_time)[0] if candidates else None
