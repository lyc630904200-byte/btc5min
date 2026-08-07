from __future__ import annotations

import json
import math
import sqlite3
import statistics
import uuid
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .btc_dynamic import clamp, dynamic_max_price, logit, normal_cdf, sigmoid
from .config import AppConfig, BtcV8Config
from .models import Direction, MarketState, OrderBookSnapshot, PriceTick
from .orderbook import simulate_buy, simulate_sell, taker_fee_usd
from .signal_sources import SignalEvent, iso_time


SPOT_EXCHANGES = ("binance", "coinbase", "kraken")
RETURN_WINDOWS = (1, 3, 5, 10, 30)
V8_MODEL_KEY = "v8"
V8_FEATURE_SCHEMA_VERSION = 1
AUTO_BUY_CONFIRMATION_SECONDS = 2.0
AUTO_BUY_CONFIRMATION_UPDATES = 2
AUTO_SELL_CONFIRMATION_SECONDS = 1.0
AUTO_SELL_CONFIRMATION_UPDATES = 2
AUTO_EXIT_LOSS_FRACTION = 0.50
AUTO_EXIT_FULL_CONFIDENCE_MARKETS = 100
AUTO_EXIT_RISK_THRESHOLD = 0.58
AUTO_EXIT_TERMINAL_SECONDS = 10.0
CHASE_BUY_CONFIRMATION_SECONDS = 0.25
CHASE_BUY_CONFIRMATION_UPDATES = 2
CHASE_SELL_CONFIRMATION_SECONDS = 0.25
CHASE_SELL_CONFIRMATION_UPDATES = 2
CHASE_MAX_HOLD_SECONDS = 5.0
CHASE_MIN_SIGNAL_SIGMA = 1.0
CHASE_MIN_PROBABILITY_MOVE = 0.03
CHASE_MIN_NET_EDGE = 0.01
CHASE_MIN_PROFIT_USD = 0.02
CHASE_MAX_CHAINLINK_AGE_SECONDS = 2.0


def v8_decision_policy(settings: BtcV8Config) -> dict[str, float | int | bool]:
    if settings.orderbook_chase_mode:
        return {
            "auto_decision_mode": False,
            "orderbook_chase_mode": True,
            "automated_mode": True,
            "buy_edge_cents": CHASE_MIN_NET_EDGE * 100.0,
            "sell_edge_cents": 0.0,
            "buy_confirmation_seconds": CHASE_BUY_CONFIRMATION_SECONDS,
            "buy_confirmation_updates": CHASE_BUY_CONFIRMATION_UPDATES,
            "sell_confirmation_seconds": CHASE_SELL_CONFIRMATION_SECONDS,
            "sell_confirmation_updates": CHASE_SELL_CONFIRMATION_UPDATES,
            "use_fixed_max_loss": False,
            "emergency_loss_fraction": AUTO_EXIT_LOSS_FRACTION,
            "emergency_loss_enabled": False,
        }
    if settings.auto_decision_mode:
        return {
            "auto_decision_mode": True,
            "orderbook_chase_mode": False,
            "automated_mode": True,
            "buy_edge_cents": 0.0,
            "sell_edge_cents": 0.0,
            "buy_confirmation_seconds": AUTO_BUY_CONFIRMATION_SECONDS,
            "buy_confirmation_updates": AUTO_BUY_CONFIRMATION_UPDATES,
            "sell_confirmation_seconds": AUTO_SELL_CONFIRMATION_SECONDS,
            "sell_confirmation_updates": AUTO_SELL_CONFIRMATION_UPDATES,
            "use_fixed_max_loss": False,
            "emergency_loss_fraction": AUTO_EXIT_LOSS_FRACTION,
            "emergency_loss_enabled": settings.auto_emergency_loss_enabled,
        }
    return {
        "auto_decision_mode": False,
        "orderbook_chase_mode": False,
        "automated_mode": False,
        "buy_edge_cents": settings.buy_edge_cents,
        "sell_edge_cents": settings.sell_edge_cents,
        "buy_confirmation_seconds": settings.buy_confirmation_seconds,
        "buy_confirmation_updates": settings.buy_confirmation_updates,
        "sell_confirmation_seconds": settings.sell_confirmation_seconds,
        "sell_confirmation_updates": settings.sell_confirmation_updates,
        "use_fixed_max_loss": True,
        "emergency_loss_fraction": 0.0,
        "emergency_loss_enabled": False,
    }


def _feature_names() -> tuple[str, ...]:
    names = [
        "remaining_time",
        "volatility_ratio",
        "open_crossings",
        "poly_up_market_gap",
        "poly_up_depth_imbalance",
        "poly_up_spread",
        "poly_up_microprice",
        "poly_up_ofi_5s",
        "poly_up_trade_flow_5s",
        "poly_down_depth_imbalance",
        "poly_down_spread",
        "poly_down_microprice",
        "poly_down_ofi_5s",
        "poly_down_trade_flow_5s",
    ]
    for source in SPOT_EXCHANGES:
        names.extend(f"{source}_return_{seconds}s" for seconds in RETURN_WINDOWS)
        names.extend(f"{source}_cvd_{seconds}s" for seconds in RETURN_WINDOWS)
        names.extend(
            [
                f"{source}_trade_intensity",
                f"{source}_book_imbalance",
                f"{source}_microprice",
                f"{source}_spread",
                f"{source}_ofi_5s",
                f"{source}_chainlink_gap_1s",
                f"{source}_chainlink_gap_5s",
                f"{source}_latency",
                f"{source}_anomaly",
                f"{source}_health_failure",
                f"{source}_missing",
            ]
        )
    for seconds in RETURN_WINDOWS:
        names.extend(
            [
                f"cross_median_return_{seconds}s",
                f"cross_direction_agreement_{seconds}s",
                f"cross_return_dispersion_{seconds}s",
                f"cross_cvd_consensus_{seconds}s",
            ]
        )
    names.extend(
        [
            "cross_price_dispersion",
            "fresh_spot_fraction",
            "futures_return_1s",
            "futures_return_5s",
            "futures_return_30s",
            "futures_cvd_5s",
            "futures_cvd_30s",
            "futures_book_imbalance",
            "futures_microprice",
            "futures_spread",
            "futures_ofi_5s",
            "futures_basis",
            "futures_basis_change_30s",
            "futures_open_interest_change_30s",
            "futures_funding",
            "futures_liquidation_5s",
            "futures_liquidation_30s",
            "futures_missing",
        ]
    )
    return tuple(names)


V8_FEATURE_NAMES = _feature_names()


def normalized_remaining_time(
    start_time: datetime, end_time: datetime, now: datetime
) -> float:
    duration = max(0.001, (_ensure_utc(end_time) - _ensure_utc(start_time)).total_seconds())
    remaining = (_ensure_utc(end_time) - _ensure_utc(now)).total_seconds()
    return clamp(2.0 * remaining / duration - 1.0, -1.0, 1.0)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _window_values(
    rows: list[tuple[datetime, float]], now: datetime, seconds: float
) -> list[tuple[datetime, float]]:
    cutoff = now - timedelta(seconds=seconds)
    return [(timestamp, value) for timestamp, value in rows if cutoff <= timestamp <= now]


def _return_for_window(
    rows: list[tuple[datetime, float]], now: datetime, seconds: int
) -> float | None:
    current = next((value for timestamp, value in reversed(rows) if timestamp <= now), None)
    prior = next(
        (
            value
            for timestamp, value in reversed(rows)
            if timestamp <= now - timedelta(seconds=seconds)
        ),
        None,
    )
    if current is None or prior is None or current <= 0 or prior <= 0:
        return None
    return math.log(current / prior)


def _volatility(rows: list[tuple[datetime, float]]) -> float:
    values: list[float] = []
    for (left_time, left), (right_time, right) in zip(rows, rows[1:]):
        elapsed = (right_time - left_time).total_seconds()
        if left > 0 and right > 0 and elapsed > 0:
            values.append(math.log(right / left) / math.sqrt(elapsed))
    return math.sqrt(sum(value * value for value in values) / len(values)) if values else 0.0


def _book_metrics(book: OrderBookSnapshot | SignalEvent | None) -> dict[str, float | None]:
    if book is None:
        return {"imbalance": 0.0, "spread": 1.0, "midpoint": None, "microprice": 0.0}
    bids = sorted(book.bids, key=lambda level: level.price, reverse=True)
    asks = sorted(book.asks, key=lambda level: level.price)
    if not bids or not asks:
        return {"imbalance": 0.0, "spread": 1.0, "midpoint": None, "microprice": 0.0}
    bid_depth = sum(level.size for level in bids[:5])
    ask_depth = sum(level.size for level in asks[:5])
    total = bid_depth + ask_depth
    imbalance = (bid_depth - ask_depth) / total if total > 0 else 0.0
    best_bid, best_ask = bids[0], asks[0]
    midpoint = (best_bid.price + best_ask.price) / 2.0
    spread_bps = (best_ask.price - best_bid.price) / max(midpoint, 1e-12) * 10_000
    top_total = best_bid.size + best_ask.size
    microprice = (
        (best_ask.price * best_bid.size + best_bid.price * best_ask.size) / top_total
        if top_total > 0
        else midpoint
    )
    micro_offset = (microprice - midpoint) / max(best_ask.price - best_bid.price, 1e-12)
    return {
        "imbalance": clamp(imbalance, -1.0, 1.0),
        "spread": clamp(spread_bps / 20.0, 0.0, 1.0),
        "midpoint": midpoint,
        "microprice": clamp(micro_offset, -1.0, 1.0),
    }


def _top_of_book_ofi(previous: SignalEvent | OrderBookSnapshot | None, current: SignalEvent | OrderBookSnapshot) -> float:
    if previous is None or not previous.bids or not previous.asks or not current.bids or not current.asks:
        return 0.0
    old_bid = max(previous.bids, key=lambda level: level.price)
    old_ask = min(previous.asks, key=lambda level: level.price)
    new_bid = max(current.bids, key=lambda level: level.price)
    new_ask = min(current.asks, key=lambda level: level.price)
    value = 0.0
    if new_bid.price >= old_bid.price:
        value += new_bid.size
    if new_bid.price <= old_bid.price:
        value -= old_bid.size
    if new_ask.price <= old_ask.price:
        value -= new_ask.size
    if new_ask.price >= old_ask.price:
        value += old_ask.size
    scale = old_bid.size + old_ask.size + new_bid.size + new_ask.size
    return clamp(value / scale * 4.0 if scale > 0 else 0.0, -1.0, 1.0)


class V8Model(BaseModel):
    model_key: str = V8_MODEL_KEY
    feature_schema_version: int = V8_FEATURE_SCHEMA_VERSION
    bias: float = 0.0
    weights: dict[str, float] = Field(
        default_factory=lambda: {name: 0.0 for name in V8_FEATURE_NAMES}
    )
    trained_markets: int = 0
    version: int = 1
    updated_at: datetime = Field(default_factory=utc_now)

    def probability(
        self,
        formula_probability: float,
        features: dict[str, float],
        maximum_correction: float,
    ) -> float:
        score = logit(formula_probability) + self.bias
        score += sum(
            self.weights.get(name, 0.0) * clamp(features.get(name, 0.0), -1.0, 1.0)
            for name in V8_FEATURE_NAMES
        )
        raw = sigmoid(score)
        return clamp(
            raw,
            max(0.01, formula_probability - maximum_correction),
            min(0.99, formula_probability + maximum_correction),
        )


class V8Trade(BaseModel):
    trade_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    trade_number: int | None = None
    market_id: str
    market_slug: str
    position_id: str
    action: Literal["BUY", "SELL", "SETTLE"]
    direction: Direction
    model_probability: float
    formula_probability: float
    avg_price: float
    quantity: float
    quote: float
    fee_usd: float
    edge_per_share: float | None = None
    reason: str
    realized_pnl: float | None = None
    decision_details: dict[str, Any] = Field(default_factory=dict)
    strategy_mode: Literal["manual", "auto", "orderbook_chase"] = "manual"
    created_at: datetime


class V8Position(BaseModel):
    position_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    market_id: str
    market_slug: str
    direction: Direction
    quantity: float
    entry_price: float
    entry_quote: float
    entry_fee_usd: float
    entry_model_probability: float | None = None
    entry_formula_probability: float | None = None
    peak_unrealized_pnl: float | None = None
    strategy_mode: Literal["manual", "auto", "orderbook_chase"] = "manual"
    entry_target_probability: float | None = None
    entry_signal_strength: float | None = None
    opened_at: datetime
    status: Literal["OPEN", "CLOSED", "SETTLED"] = "OPEN"
    exit_price: float | None = None
    exit_quote: float = 0.0
    exit_fee_usd: float = 0.0
    realized_pnl: float | None = None
    closed_at: datetime | None = None
    exit_reason: str | None = None


def v8_auto_exit_loss_limit(position: V8Position) -> float:
    return max(0.01, position.entry_quote * AUTO_EXIT_LOSS_FRACTION)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _oriented_mean(
    features: dict[str, float], names: tuple[str, ...], direction: Direction
) -> float:
    orientation = 1.0 if direction == Direction.UP else -1.0
    return clamp(
        _mean([clamp(features.get(name, 0.0), -1.0, 1.0) for name in names])
        * orientation,
        -1.0,
        1.0,
    )


def v8_auto_exit_decision(
    position: V8Position,
    probability: float,
    formula_probability: float,
    net_value: float,
    pnl: float,
    features: dict[str, float],
    diagnostics: dict[str, Any],
    elapsed_seconds: float,
    sell_end_seconds: float,
    trained_markets: int,
    emergency_loss_enabled: bool = False,
) -> dict[str, Any]:
    direction = position.direction
    orientation = 1.0 if direction == Direction.UP else -1.0
    entry_probability = position.entry_model_probability or probability
    entry_formula = position.entry_formula_probability or formula_probability
    probability_decay = clamp(
        (entry_probability - probability) / max(entry_probability, 0.05), 0.0, 1.0
    )
    formula_decay = clamp(
        (entry_formula - formula_probability) / max(entry_formula, 0.05), 0.0, 1.0
    )

    windows = (1, 3, 5, 10)
    spot_momentum = _oriented_mean(
        features, tuple(f"cross_median_return_{seconds}s" for seconds in windows), direction
    )
    spot_agreement = _oriented_mean(
        features,
        tuple(f"cross_direction_agreement_{seconds}s" for seconds in windows),
        direction,
    )
    spot_cvd = _oriented_mean(
        features, tuple(f"cross_cvd_consensus_{seconds}s" for seconds in windows), direction
    )
    spot_support = clamp(
        0.45 * spot_momentum + 0.35 * spot_agreement + 0.20 * spot_cvd, -1.0, 1.0
    )
    futures_support = _oriented_mean(
        features,
        (
            "futures_return_1s",
            "futures_return_5s",
            "futures_return_30s",
            "futures_cvd_5s",
            "futures_book_imbalance",
            "futures_ofi_5s",
        ),
        direction,
    )
    prefix = "poly_up" if direction == Direction.UP else "poly_down"
    poly_support = clamp(
        _mean(
            [
                features.get(f"{prefix}_depth_imbalance", 0.0),
                features.get(f"{prefix}_microprice", 0.0),
                features.get(f"{prefix}_ofi_5s", 0.0),
                features.get(f"{prefix}_trade_flow_5s", 0.0),
            ]
        ),
        -1.0,
        1.0,
    )
    chainlink_support = clamp(
        orientation * float(diagnostics.get("z_score") or 0.0) / 2.0, -1.0, 1.0
    )
    signal_support = clamp(
        0.35 * chainlink_support
        + 0.30 * spot_support
        + 0.20 * futures_support
        + 0.15 * poly_support,
        -1.0,
        1.0,
    )
    adverse_signal = clamp(-signal_support, 0.0, 1.0)

    required_sources = max(1, int(diagnostics.get("required_fresh_spot_count") or 1))
    source_confidence = clamp(
        float(diagnostics.get("fresh_spot_count") or 0) / required_sources, 0.0, 1.0
    )
    model_confidence = clamp(
        trained_markets / AUTO_EXIT_FULL_CONFIDENCE_MARKETS, 0.0, 1.0
    )
    confidence = 0.65 * source_confidence + 0.35 * model_confidence
    uncertainty = 1.0 - confidence

    loss_limit = v8_auto_exit_loss_limit(position)
    loss_pressure = clamp(-pnl / loss_limit, 0.0, 1.0)
    capital = max(position.entry_quote + position.entry_fee_usd, 0.01)
    peak_pnl = max(position.peak_unrealized_pnl or pnl, pnl)
    drawdown = max(0.0, peak_pnl - pnl)
    drawdown_pressure = clamp(drawdown / capital, 0.0, 1.0)
    time_pressure = clamp(
        (elapsed_seconds - max(0.0, sell_end_seconds - 60.0)) / 60.0, 0.0, 1.0
    )
    risk_score = clamp(
        0.24 * probability_decay
        + 0.10 * formula_decay
        + 0.23 * adverse_signal
        + 0.18 * loss_pressure
        + 0.10 * drawdown_pressure
        + 0.10 * time_pressure * (1.0 - probability)
        + 0.05 * uncertainty,
        0.0,
        1.0,
    )
    risk_threshold = clamp(
        AUTO_EXIT_RISK_THRESHOLD - 0.08 * time_pressure - 0.05 * uncertainty,
        0.40,
        AUTO_EXIT_RISK_THRESHOLD,
    )
    uncertainty_discount = min(
        0.05,
        uncertainty * (0.01 + 0.08 * math.sqrt(max(0.0, probability * (1.0 - probability)))),
    )
    risk_adjusted_hold_value = clamp(
        probability - uncertainty_discount - 0.03 * time_pressure * adverse_signal,
        0.0,
        1.0,
    )
    risk_adjusted_value_edge = net_value - risk_adjusted_hold_value

    reason = None
    if emergency_loss_enabled and pnl <= -loss_limit + 1e-12:
        reason = "auto_emergency_loss"
    elif (
        elapsed_seconds >= max(0.0, sell_end_seconds - AUTO_EXIT_TERMINAL_SECONDS)
        and probability < 0.50
        and pnl < 0.0
    ):
        reason = "auto_terminal_exit"
    elif (
        peak_pnl >= 0.50
        and drawdown >= max(0.50, 0.55 * peak_pnl)
    ):
        reason = "auto_trailing_exit"
    elif (
        probability_decay >= 0.35
        and (formula_decay >= 0.20 or adverse_signal >= 0.25)
        and risk_score >= risk_threshold - 0.10
    ):
        reason = "auto_thesis_exit"
    elif (
        risk_score >= risk_threshold
        and (
            probability_decay >= 0.15
            or adverse_signal >= 0.40
            or drawdown_pressure >= 0.40
        )
    ):
        reason = "auto_risk_exit"
    elif risk_adjusted_value_edge >= -1e-12:
        reason = "auto_model_sell"

    return {
        "eligible": reason is not None,
        "reason": reason or "auto_hold",
        "risk_score": risk_score,
        "risk_threshold": risk_threshold,
        "signal_support": signal_support,
        "spot_support": spot_support,
        "futures_support": futures_support,
        "chainlink_support": chainlink_support,
        "poly_support": poly_support,
        "probability_decay": probability_decay,
        "formula_probability_decay": formula_decay,
        "loss_limit_usd": loss_limit,
        "emergency_loss_enabled": emergency_loss_enabled,
        "loss_pressure": loss_pressure,
        "peak_unrealized_pnl": peak_pnl,
        "drawdown_usd": drawdown,
        "time_pressure": time_pressure,
        "data_confidence": confidence,
        "risk_adjusted_hold_value": risk_adjusted_hold_value,
        "risk_adjusted_value_edge": risk_adjusted_value_edge,
    }


def v8_orderbook_chase_signal(
    formula_probability_up: float,
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    lead_return = diagnostics.get("spot_chainlink_lead_return_1s")
    chainlink_age = diagnostics.get("chainlink_age_seconds")
    max_chainlink_age = float(
        diagnostics.get("chase_chainlink_max_age_seconds")
        or CHASE_MAX_CHAINLINK_AGE_SECONDS
    )
    sigma = max(float(diagnostics.get("sigma") or 0.0), 1e-12)
    remaining = max(float(diagnostics.get("remaining_seconds") or 0.0), 0.001)
    current_price = float(diagnostics.get("chainlink_current_price") or 0.0)
    open_price = float(diagnostics.get("chainlink_open_price") or 0.0)
    required_sources = max(
        1, int(diagnostics.get("required_fresh_spot_count") or 1)
    )
    positive_sources = int(diagnostics.get("spot_positive_return_sources_1s") or 0)
    negative_sources = int(diagnostics.get("spot_negative_return_sources_1s") or 0)
    supporting_sources = max(positive_sources, negative_sources)
    base = {
        "eligible": False,
        "reason": "chase_waiting_history",
        "direction": None,
        "target_probability_up": formula_probability_up,
        "target_probability": None,
        "lead_return_1s": lead_return,
        "signal_strength": 0.0,
        "supporting_sources": supporting_sources,
        "required_sources": required_sources,
        "chainlink_age_seconds": chainlink_age,
        "max_chainlink_age_seconds": max_chainlink_age,
    }
    if (
        chainlink_age is None
        or float(chainlink_age) < 0
        or float(chainlink_age) > max_chainlink_age
    ):
        base["reason"] = "chase_chainlink_stale"
        return base
    if lead_return is None or current_price <= 0 or open_price <= 0:
        return base

    lead_return = float(lead_return)
    direction = Direction.UP if lead_return > 0 else Direction.DOWN
    same_direction_sources = positive_sources if direction == Direction.UP else negative_sources
    signal_strength = abs(lead_return) / sigma
    projected_return = clamp(lead_return, -3.0 * sigma, 3.0 * sigma)
    projected_price = current_price * math.exp(projected_return)
    projected_z = math.log(projected_price / open_price) / (sigma * math.sqrt(remaining))
    target_up = clamp(normal_cdf(projected_z), 0.01, 0.99)
    probability_move = abs(target_up - formula_probability_up)
    target_probability = target_up if direction == Direction.UP else 1.0 - target_up
    base.update(
        {
            "direction": direction.value,
            "target_probability_up": target_up,
            "target_probability": target_probability,
            "projected_chainlink_price": projected_price,
            "lead_return_1s": lead_return,
            "lead_bps_1s": lead_return * 10_000.0,
            "signal_strength": signal_strength,
            "probability_move": probability_move,
            "supporting_sources": same_direction_sources,
        }
    )
    if same_direction_sources < required_sources:
        base["reason"] = "chase_consensus_insufficient"
    elif signal_strength + 1e-12 < CHASE_MIN_SIGNAL_SIGMA:
        base["reason"] = "chase_signal_weak"
    elif probability_move + 1e-12 < CHASE_MIN_PROBABILITY_MOVE:
        base["reason"] = "chase_probability_move_small"
    else:
        base["eligible"] = True
        base["reason"] = "chase_signal"
    return base


def v8_orderbook_chase_exit_decision(
    position: V8Position,
    net_value: float,
    pnl: float,
    diagnostics: dict[str, Any],
    now: datetime,
    take_profit_arm_usd: float = 0.25,
    take_profit_drawdown_usd: float = 0.15,
    take_profit_drawdown_fraction: float = 0.35,
) -> dict[str, Any]:
    chase = diagnostics.get("orderbook_chase") or {}
    held_seconds = max(0.0, (_ensure_utc(now) - _ensure_utc(position.opened_at)).total_seconds())
    target = position.entry_target_probability or 1.0
    entry_strength = max(position.entry_signal_strength or 0.0, CHASE_MIN_SIGNAL_SIGMA)
    current_strength = float(chase.get("signal_strength") or 0.0)
    current_direction = chase.get("direction")
    signal_reversed = bool(
        chase.get("eligible")
        and current_direction
        and current_direction != position.direction.value
    )
    signal_decayed = bool(
        held_seconds >= 1.0
        and (
            not chase.get("eligible")
            or current_strength < max(CHASE_MIN_SIGNAL_SIGMA, entry_strength * 0.40)
        )
    )
    peak_pnl = max(
        position.peak_unrealized_pnl
        if position.peak_unrealized_pnl is not None
        else pnl,
        pnl,
    )
    take_profit_armed = peak_pnl + 1e-12 >= take_profit_arm_usd
    profit_drawdown = max(0.0, peak_pnl - pnl)
    required_profit_drawdown = max(
        take_profit_drawdown_usd,
        peak_pnl * take_profit_drawdown_fraction,
    )
    trailing_profit_exit = bool(
        take_profit_armed
        and profit_drawdown + 1e-12 >= required_profit_drawdown
    )
    target_reached = net_value + 0.005 >= target
    reason = None
    if pnl + 1e-12 >= CHASE_MIN_PROFIT_USD and target_reached:
        reason = "chase_caught_up"
    elif held_seconds + 1e-12 >= CHASE_MAX_HOLD_SECONDS:
        reason = "chase_timeout"
    elif signal_reversed:
        reason = "chase_signal_reversed"
    elif trailing_profit_exit:
        reason = "chase_profit_trailing"
    return {
        "eligible": reason is not None,
        "reason": reason or "chase_holding",
        "held_seconds": held_seconds,
        "max_hold_seconds": CHASE_MAX_HOLD_SECONDS,
        "entry_target_probability": target,
        "exit_net_value_per_share": net_value,
        "target_reached": target_reached,
        "signal_reversed": signal_reversed,
        "signal_decayed": signal_decayed,
        "take_profit_armed": take_profit_armed,
        "take_profit_arm_usd": take_profit_arm_usd,
        "peak_unrealized_pnl": peak_pnl,
        "profit_drawdown_usd": profit_drawdown,
        "required_profit_drawdown_usd": required_profit_drawdown,
        "take_profit_drawdown_usd": take_profit_drawdown_usd,
        "take_profit_drawdown_fraction": take_profit_drawdown_fraction,
        "entry_signal_strength": entry_strength,
        "current_signal_strength": current_strength,
        "current_chase_direction": current_direction,
        "pnl": pnl,
    }


class V8Round(BaseModel):
    round_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    market_id: str
    market_slug: str
    model_key: str = V8_MODEL_KEY
    model_version: int = 1
    start_time: datetime
    end_time: datetime
    settings: BtcV8Config
    entry_count: int = 0
    current_position_id: str | None = None
    official_outcome: Direction | None = None
    trained: bool = False
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None


class V8Snapshot(BaseModel):
    market_id: str
    model_key: str = V8_MODEL_KEY
    model_version: int = 1
    snapshot_second: int
    formula_probability: float
    model_probability: float
    features: dict[str, float]
    fresh_spot_exchanges: list[str]
    source_health: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


def _update_v8_model(
    model: V8Model,
    samples: list[V8Snapshot],
    label: float,
    probabilities: list[float],
    updated_at: datetime,
) -> None:
    if not samples or len(samples) != len(probabilities):
        return
    rate = 0.05 / math.sqrt(1.0 + model.trained_markets / 500.0)
    weight = 1.0 / len(samples)
    errors = [probability - label for probability in probabilities]
    bias_gradient = weight * sum(errors)
    gradients = {name: 0.0 for name in V8_FEATURE_NAMES}
    for sample, error in zip(samples, errors):
        for name in V8_FEATURE_NAMES:
            gradients[name] += weight * error * sample.features.get(name, 0.0)
    model.bias = clamp(model.bias - rate * bias_gradient, -3.0, 3.0)
    for name in V8_FEATURE_NAMES:
        old = model.weights.get(name, 0.0)
        model.weights[name] = clamp(
            old - rate * (gradients[name] + 0.001 * old), -3.0, 3.0
        )
    model.trained_markets += 1
    model.version += 1
    model.updated_at = _ensure_utc(updated_at)


class BtcV8Registry:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS btc_v8_rounds (
                market_id TEXT PRIMARY KEY,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS btc_v8_snapshots (
                market_id TEXT NOT NULL,
                snapshot_second INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY(market_id, snapshot_second)
            );
            CREATE INDEX IF NOT EXISTS btc_v8_snapshot_age_idx
                ON btc_v8_snapshots(created_at);
            CREATE TABLE IF NOT EXISTS btc_v8_positions (
                position_id TEXT PRIMARY KEY,
                market_id TEXT NOT NULL,
                status TEXT NOT NULL,
                opened_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS btc_v8_positions_market_idx
                ON btc_v8_positions(market_id, status);
            CREATE TABLE IF NOT EXISTS btc_v8_trades (
                trade_id TEXT PRIMARY KEY,
                trade_number INTEGER NOT NULL UNIQUE,
                market_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS btc_v8_models (
                model_key TEXT NOT NULL,
                version INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY(model_key, version)
            );
            CREATE TABLE IF NOT EXISTS btc_v8_raw_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                kind TEXT NOT NULL,
                received_at TEXT NOT NULL,
                payload_json BLOB NOT NULL
            );
            CREATE INDEX IF NOT EXISTS btc_v8_raw_age_idx
                ON btc_v8_raw_events(received_at);
            CREATE TABLE IF NOT EXISTS btc_v8_controls (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        self.connection.commit()
        self._raw_buffer: list[tuple[str, str, str, dict[str, Any]]] = []
        self._raw_book_buffer: dict[
            tuple[str, str, int], tuple[str, str, str, dict[str, Any]]
        ] = {}
        self._raw_last_flush = utc_now()

    @staticmethod
    def _payload(value: BaseModel) -> str:
        return json.dumps(value.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def decode_raw_payload(value: str | bytes | memoryview) -> dict[str, Any]:
        if isinstance(value, memoryview):
            value = value.tobytes()
        if isinstance(value, bytes):
            try:
                value = zlib.decompress(value)
            except zlib.error:
                pass
            value = value.decode("utf-8")
        return json.loads(value)

    def close(self) -> None:
        self.flush_raw_events(force=True)
        self.connection.close()

    def load_model(self, model_key: str = V8_MODEL_KEY) -> V8Model:
        row = self.connection.execute(
            "SELECT payload_json FROM btc_v8_models WHERE model_key=? ORDER BY version DESC LIMIT 1",
            (model_key,),
        ).fetchone()
        return (
            V8Model.model_validate_json(row["payload_json"])
            if row
            else V8Model(
                model_key=model_key,
                updated_at=datetime(1970, 1, 1, tzinfo=timezone.utc),
            )
        )

    def load_model_version(self, model_key: str, version: int) -> V8Model | None:
        row = self.connection.execute(
            "SELECT payload_json FROM btc_v8_models WHERE model_key=? AND version=?",
            (model_key, version),
        ).fetchone()
        return V8Model.model_validate_json(row["payload_json"]) if row else None

    def load_model_before(self, model_key: str, not_after: datetime) -> V8Model:
        row = self.connection.execute(
            """
            SELECT payload_json FROM btc_v8_models
            WHERE model_key=? AND updated_at<=?
            ORDER BY version DESC LIMIT 1
            """,
            (model_key, _ensure_utc(not_after).isoformat()),
        ).fetchone()
        if row:
            return V8Model.model_validate_json(row["payload_json"])
        return V8Model(
            model_key=model_key,
            updated_at=datetime(1970, 1, 1, tzinfo=timezone.utc),
        )

    def save_model(self, model: V8Model) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO btc_v8_models(model_key,version,updated_at,payload_json) VALUES(?,?,?,?)",
            (model.model_key, model.version, model.updated_at.isoformat(), self._payload(model)),
        )
        self.connection.commit()

    def get_control(self, key: str) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM btc_v8_controls WHERE key=?", (key,)
        ).fetchone()
        return str(row["value"]) if row else None

    def set_control(self, key: str, value: str | None, now: datetime | None = None) -> None:
        if value is None:
            self.connection.execute("DELETE FROM btc_v8_controls WHERE key=?", (key,))
        else:
            self.connection.execute(
                """
                INSERT INTO btc_v8_controls(key,value,updated_at) VALUES(?,?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
                """,
                (key, value, (now or utc_now()).isoformat()),
            )
        self.connection.commit()

    def active_model_key(self) -> str:
        return self.get_control("active_model_key") or V8_MODEL_KEY

    def pending_model(self) -> tuple[str | None, datetime | None]:
        key = self.get_control("pending_model_key")
        raw_time = self.get_control("pending_model_not_before")
        return key, iso_time(raw_time) if raw_time else None

    def schedule_model(self, model_key: str, not_before: datetime) -> None:
        if self.load_model_version(model_key, self.load_model(model_key).version) is None:
            raise ValueError(f"BTC V8 model does not exist: {model_key}")
        self.set_control("pending_model_key", model_key, not_before)
        self.set_control(
            "pending_model_not_before", _ensure_utc(not_before).isoformat(), not_before
        )

    def activate_pending_for_market(self, market_start: datetime) -> str:
        pending_key, not_before = self.pending_model()
        if pending_key and not_before and _ensure_utc(market_start) > not_before:
            self.set_control("active_model_key", pending_key, market_start)
            self.set_control("pending_model_key", None)
            self.set_control("pending_model_not_before", None)
            return pending_key
        return self.active_model_key()

    def get_round(self, market_id: str) -> V8Round | None:
        row = self.connection.execute(
            "SELECT payload_json FROM btc_v8_rounds WHERE market_id=?", (market_id,)
        ).fetchone()
        return V8Round.model_validate_json(row["payload_json"]) if row else None

    def save_round(self, round_: V8Round) -> None:
        self.connection.execute(
            """
            INSERT INTO btc_v8_rounds(market_id,start_time,end_time,payload_json)
            VALUES(?,?,?,?) ON CONFLICT(market_id) DO UPDATE SET
                start_time=excluded.start_time,end_time=excluded.end_time,payload_json=excluded.payload_json
            """,
            (
                round_.market_id,
                round_.start_time.isoformat(),
                round_.end_time.isoformat(),
                self._payload(round_),
            ),
        )
        self.connection.commit()

    def save_snapshot(self, snapshot: V8Snapshot) -> bool:
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO btc_v8_snapshots(market_id,snapshot_second,created_at,payload_json) VALUES(?,?,?,?)",
            (
                snapshot.market_id,
                snapshot.snapshot_second,
                snapshot.created_at.isoformat(),
                self._payload(snapshot),
            ),
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def snapshots(self, market_id: str) -> list[V8Snapshot]:
        rows = self.connection.execute(
            "SELECT payload_json FROM btc_v8_snapshots WHERE market_id=? ORDER BY snapshot_second",
            (market_id,),
        ).fetchall()
        return [V8Snapshot.model_validate_json(row["payload_json"]) for row in rows]

    def save_position(self, position: V8Position) -> None:
        self.connection.execute(
            """
            INSERT INTO btc_v8_positions(position_id,market_id,status,opened_at,payload_json)
            VALUES(?,?,?,?,?) ON CONFLICT(position_id) DO UPDATE SET
                status=excluded.status,payload_json=excluded.payload_json
            """,
            (
                position.position_id,
                position.market_id,
                position.status,
                position.opened_at.isoformat(),
                self._payload(position),
            ),
        )
        self.connection.commit()

    def get_position(self, position_id: str | None) -> V8Position | None:
        if not position_id:
            return None
        row = self.connection.execute(
            "SELECT payload_json FROM btc_v8_positions WHERE position_id=?", (position_id,)
        ).fetchone()
        return V8Position.model_validate_json(row["payload_json"]) if row else None

    def save_trade(self, trade: V8Trade) -> V8Trade:
        if trade.trade_number is None:
            row = self.connection.execute(
                "SELECT COALESCE(MAX(trade_number),0)+1 AS value FROM btc_v8_trades"
            ).fetchone()
            trade.trade_number = int(row["value"])
        self.connection.execute(
            "INSERT OR REPLACE INTO btc_v8_trades(trade_id,trade_number,market_id,created_at,payload_json) VALUES(?,?,?,?,?)",
            (
                trade.trade_id,
                trade.trade_number,
                trade.market_id,
                trade.created_at.isoformat(),
                self._payload(trade),
            ),
        )
        self.connection.commit()
        return trade

    def recent_trades(self, limit: int = 300) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT payload_json FROM btc_v8_trades ORDER BY trade_number DESC LIMIT ?", (limit,)
        ).fetchall()
        return [V8Trade.model_validate_json(row["payload_json"]).model_dump(mode="json") for row in rows]

    def recent_positions(self, limit: int = 300) -> list[V8Position]:
        rows = self.connection.execute(
            "SELECT payload_json FROM btc_v8_positions ORDER BY opened_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [V8Position.model_validate_json(row["payload_json"]) for row in rows]

    def latest_closed_position(self, market_id: str) -> V8Position | None:
        rows = self.connection.execute(
            "SELECT payload_json FROM btc_v8_positions WHERE market_id=? ORDER BY opened_at DESC",
            (market_id,),
        ).fetchall()
        for row in rows:
            position = V8Position.model_validate_json(row["payload_json"])
            if position.closed_at is not None:
                return position
        return None

    def save_raw_event(self, event: SignalEvent) -> None:
        payload = {
            "source": event.source,
            "market_type": event.market_type,
            "kind": event.kind,
            "symbol": event.symbol,
            "price": event.price,
            "quantity": event.quantity,
            "taker_side": event.taker_side,
            "value": event.value,
            "sequence": event.sequence,
            "exchange_timestamp": event.exchange_timestamp.isoformat(),
            "received_at": event.received_at.isoformat(),
            "processed_at": event.processed_at.isoformat(),
            "valid": event.valid,
            "reason": event.reason,
            "raw": event.raw,
        }
        row = (
            event.source,
            event.kind,
            event.received_at.isoformat(),
            payload,
        )
        if event.kind == "book":
            bucket = int(event.received_at.timestamp() * 4)
            self._raw_book_buffer[(event.source, event.symbol, bucket)] = row
        else:
            self._raw_buffer.append(row)
        self.flush_raw_events()

    def flush_raw_events(self, force: bool = False) -> None:
        now = utc_now()
        row_count = len(self._raw_buffer) + len(self._raw_book_buffer)
        if not row_count:
            return
        if not force and row_count < 100 and (now - self._raw_last_flush).total_seconds() < 1:
            return
        rows = [*self._raw_buffer, *self._raw_book_buffer.values()]
        self._raw_buffer = []
        self._raw_book_buffer = {}
        encoded_rows = [
            (
                source,
                kind,
                received_at,
                zlib.compress(
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                        "utf-8"
                    ),
                    level=3,
                ),
            )
            for source, kind, received_at, payload in rows
        ]
        self.connection.executemany(
            "INSERT INTO btc_v8_raw_events(source,kind,received_at,payload_json) VALUES(?,?,?,?)",
            encoded_rows,
        )
        self.connection.commit()
        self._raw_last_flush = now

    def cleanup_raw_events(self, retention_hours: float, now: datetime) -> None:
        cutoff = now - timedelta(hours=retention_hours)
        self.connection.execute(
            "DELETE FROM btc_v8_raw_events WHERE received_at < ?", (cutoff.isoformat(),)
        )
        self.connection.commit()

    def cleanup_expired_raw_events(
        self,
        retention_hours: float,
        now: datetime,
        batch_size: int = 5_000,
    ) -> int:
        if batch_size <= 0:
            raise ValueError("raw event cleanup batch size must be positive")
        cutoff = _ensure_utc(now) - timedelta(hours=retention_hours)
        cursor = self.connection.execute(
            """
            DELETE FROM btc_v8_raw_events
            WHERE id IN (
                SELECT id
                FROM btc_v8_raw_events
                WHERE received_at < ?
                ORDER BY received_at
                LIMIT ?
            )
            """,
            (cutoff.isoformat(), batch_size),
        )
        self.connection.commit()
        return max(cursor.rowcount, 0)

    def discard_raw_buffer(self) -> None:
        self._raw_buffer = []
        self._raw_book_buffer = {}

    def cleanup_expired_snapshots(
        self,
        retention_hours: float,
        now: datetime,
        batch_size: int = 2_000,
    ) -> int:
        if batch_size <= 0:
            raise ValueError("snapshot cleanup batch size must be positive")
        cutoff = _ensure_utc(now) - timedelta(hours=retention_hours)
        cursor = self.connection.execute(
            """
            DELETE FROM btc_v8_snapshots
            WHERE rowid IN (
                SELECT rowid
                FROM btc_v8_snapshots
                WHERE created_at < ?
                ORDER BY created_at
                LIMIT ?
            )
            """,
            (cutoff.isoformat(), batch_size),
        )
        self.connection.commit()
        return max(cursor.rowcount, 0)

    def checkpoint_wal(self) -> None:
        self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()

    def settle(
        self, market_slug: str, outcome: Direction, now: datetime
    ) -> tuple[V8Round, V8Model, list[V8Trade], bool] | None:
        rows = self.connection.execute(
            "SELECT payload_json FROM btc_v8_rounds ORDER BY start_time DESC"
        ).fetchall()
        round_ = next(
            (
                candidate
                for row in rows
                if (candidate := V8Round.model_validate_json(row["payload_json"])).market_slug
                == market_slug
            ),
            None,
        )
        if round_ is None:
            return None
        model = (
            V8Model()
            if round_.settings.orderbook_chase_mode
            else self.load_model(round_.model_key)
        )
        if round_.official_outcome is not None:
            return round_, model, [], False
        settlement_trades: list[V8Trade] = []
        position = self.get_position(round_.current_position_id)
        if position is not None and position.status == "OPEN":
            payout = position.quantity if position.direction == outcome else 0.0
            position.status = "SETTLED"
            position.exit_price = 1.0 if position.direction == outcome else 0.0
            position.exit_quote = payout
            position.realized_pnl = payout - position.entry_quote - position.entry_fee_usd
            position.closed_at = now
            position.exit_reason = "official_settlement"
            self.save_position(position)
            trade = V8Trade(
                market_id=round_.market_id,
                market_slug=round_.market_slug,
                position_id=position.position_id,
                action="SETTLE",
                direction=position.direction,
                model_probability=1.0 if position.direction == outcome else 0.0,
                formula_probability=1.0 if position.direction == outcome else 0.0,
                avg_price=position.exit_price,
                quantity=position.quantity,
                quote=payout,
                fee_usd=0.0,
                reason="official_settlement",
                realized_pnl=position.realized_pnl,
                strategy_mode=position.strategy_mode,
                created_at=now,
            )
            settlement_trades.append(self.save_trade(trade))
            round_.current_position_id = None
        round_.official_outcome = outcome
        round_.closed_at = now
        round_.updated_at = now
        if not round_.trained:
            if not round_.settings.orderbook_chase_mode:
                samples = self.snapshots(round_.market_id)
                if samples:
                    label = 1.0 if outcome == Direction.UP else 0.0
                    _update_v8_model(
                        model,
                        samples,
                        label,
                        [sample.model_probability for sample in samples],
                        now,
                    )
                    self.save_model(model)
            round_.trained = True
        self.save_round(round_)
        return round_, model, settlement_trades, True

    def summary(self, include_model_metrics: bool = True) -> dict[str, Any]:
        positions = self.recent_positions(100_000)
        completed = [position for position in positions if position.realized_pnl is not None]
        wins = [position for position in completed if (position.realized_pnl or 0.0) > 0]
        fees = sum(position.entry_fee_usd + position.exit_fee_usd for position in positions)
        snapshot_rows = (
            self.connection.execute(
                """
                SELECT snapshots.payload_json AS snapshot_json, rounds.payload_json AS round_json
                FROM btc_v8_snapshots AS snapshots
                JOIN btc_v8_rounds AS rounds ON rounds.market_id=snapshots.market_id
                ORDER BY snapshots.created_at
                """
            ).fetchall()
            if include_model_metrics
            else []
        )
        brier: list[float] = []
        accuracy: list[bool] = []
        by_time: dict[str, list[float]] = {
            "0-60": [], "60-120": [], "120-180": [], "180-240": [], "240-285": []
        }
        by_source_availability: dict[str, dict[str, dict[str, list[Any]]]] = {
            source: {
                "available": {"errors": [], "accuracy": []},
                "missing": {"errors": [], "accuracy": []},
            }
            for source in SPOT_EXCHANGES
        }
        calibration: dict[str, dict[str, float]] = {}
        for row in snapshot_rows:
            snapshot = V8Snapshot.model_validate_json(row["snapshot_json"])
            round_ = V8Round.model_validate_json(row["round_json"])
            if round_.official_outcome is None:
                continue
            label = 1.0 if round_.official_outcome == Direction.UP else 0.0
            error = (snapshot.model_probability - label) ** 2
            brier.append(error)
            accuracy.append((snapshot.model_probability >= 0.5) == bool(label))
            second = snapshot.snapshot_second
            bucket = (
                "0-60" if second < 60 else "60-120" if second < 120 else
                "120-180" if second < 180 else "180-240" if second < 240 else "240-285"
            )
            by_time[bucket].append(error)
            for source in SPOT_EXCHANGES:
                state = "available" if source in snapshot.fresh_spot_exchanges else "missing"
                by_source_availability[source][state]["errors"].append(error)
                by_source_availability[source][state]["accuracy"].append(
                    (snapshot.model_probability >= 0.5) == bool(label)
                )
            lower = int(snapshot.model_probability * 10) * 10
            label_key = f"{lower}-{min(100, lower + 10)}"
            item = calibration.setdefault(label_key, {"count": 0.0, "probability": 0.0, "outcome": 0.0})
            item["count"] += 1
            item["probability"] += snapshot.model_probability
            item["outcome"] += label
        calibration_rows = []
        for bucket, item in sorted(calibration.items()):
            count = item["count"]
            calibration_rows.append(
                {
                    "bucket": bucket,
                    "count": int(count),
                    "mean_probability": item["probability"] / count,
                    "outcome_rate": item["outcome"] / count,
                }
            )
        return {
            "positions": len(positions),
            "completed_positions": len(completed),
            "wins": len(wins),
            "win_rate": len(wins) / len(completed) if completed else 0.0,
            "realized_pnl": sum(position.realized_pnl or 0.0 for position in completed),
            "fees_usd": fees,
            "brier": sum(brier) / len(brier) if brier else None,
            "accuracy": sum(accuracy) / len(accuracy) if accuracy else None,
            "evaluated_snapshots": len(brier),
            "brier_by_time": {
                bucket: sum(values) / len(values) if values else None
                for bucket, values in by_time.items()
            },
            "by_source_availability": {
                source: {
                    state: {
                        "count": len(values["errors"]),
                        "brier": (
                            sum(values["errors"]) / len(values["errors"])
                            if values["errors"] else None
                        ),
                        "accuracy": (
                            sum(values["accuracy"]) / len(values["accuracy"])
                            if values["accuracy"] else None
                        ),
                    }
                    for state, values in states.items()
                }
                for source, states in by_source_availability.items()
            },
            "calibration": calibration_rows,
        }

    def retrain_candidate(
        self,
        now: datetime | None = None,
        model_key: str | None = None,
    ) -> dict[str, Any]:
        completed_at = _ensure_utc(now or utc_now())
        pending_key, _ = self.pending_model()
        if pending_key:
            raise ValueError(f"BTC V8 candidate is already pending: {pending_key}")
        candidate_key = model_key or f"v8_candidate_{completed_at.strftime('%Y%m%dT%H%M%SZ')}"
        if not candidate_key or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            for character in candidate_key
        ):
            raise ValueError("BTC V8 model key may contain only letters, digits, underscores, and hyphens")
        row = self.connection.execute(
            "SELECT 1 FROM btc_v8_models WHERE model_key=? LIMIT 1", (candidate_key,)
        ).fetchone()
        if row:
            raise ValueError(f"BTC V8 model already exists: {candidate_key}")
        rows = self.connection.execute(
            "SELECT payload_json FROM btc_v8_rounds ORDER BY start_time, market_id"
        ).fetchall()
        model = V8Model(
            model_key=candidate_key,
            updated_at=datetime(1970, 1, 1, tzinfo=timezone.utc),
        )
        market_count = 0
        snapshot_count = 0
        market_order: list[str] = []
        brier: list[float] = []
        accuracy: list[bool] = []
        for row in rows:
            round_ = V8Round.model_validate_json(row["payload_json"])
            if round_.official_outcome is None:
                continue
            samples = self.snapshots(round_.market_id)
            if not samples:
                continue
            label = 1.0 if round_.official_outcome == Direction.UP else 0.0
            correction = round_.settings.max_probability_correction_points / 100.0
            probabilities = [
                model.probability(
                    sample.formula_probability,
                    sample.features,
                    correction,
                )
                for sample in samples
            ]
            brier.extend((probability - label) ** 2 for probability in probabilities)
            accuracy.extend((probability >= 0.5) == bool(label) for probability in probabilities)
            _update_v8_model(
                model,
                samples,
                label,
                probabilities,
                round_.closed_at or round_.end_time,
            )
            market_count += 1
            snapshot_count += len(samples)
            market_order.append(round_.market_slug)
        if not market_count:
            raise ValueError("BTC V8 has no settled markets with snapshots to retrain")
        model.updated_at = completed_at
        self.save_model(model)
        self.schedule_model(candidate_key, completed_at)
        return {
            "model_key": candidate_key,
            "model_version": model.version,
            "trained_markets": market_count,
            "snapshots": snapshot_count,
            "market_order": market_order,
            "walk_forward_brier": sum(brier) / len(brier),
            "walk_forward_accuracy": sum(accuracy) / len(accuracy),
            "pending_model_not_before": completed_at.isoformat(),
            "active_model_key": self.active_model_key(),
        }


class BtcV8Engine:
    def __init__(self, config: AppConfig, registry: BtcV8Registry):
        self.config = config
        self.registry = registry
        self.model = (
            V8Model()
            if config.btc_v8.orderbook_chase_mode
            else registry.load_model(registry.active_model_key())
        )
        if not config.btc_v8.orderbook_chase_mode and self.model.updated_at.year == 1970:
            registry.save_model(self.model)
        self.current_round: V8Round | None = None
        self.position: V8Position | None = None
        self.chainlink_ticks: list[tuple[datetime, float]] = []
        self.trades: dict[str, list[SignalEvent]] = {
            source: [] for source in (*SPOT_EXCHANGES, "binance_futures")
        }
        self.books: dict[str, SignalEvent] = {}
        self.health: dict[str, SignalEvent] = {}
        self.book_ofi: dict[str, list[tuple[datetime, float]]] = {}
        self.values: dict[str, list[tuple[datetime, float]]] = {}
        self.polymarket_books: dict[Direction, OrderBookSnapshot] = {}
        self.polymarket_ofi: dict[Direction, list[tuple[datetime, float]]] = {
            Direction.UP: [], Direction.DOWN: []
        }
        self.polymarket_trades: dict[Direction, list[tuple[datetime, float]]] = {
            Direction.UP: [], Direction.DOWN: []
        }
        self._polymarket_trade_keys: set[tuple[str, str, str, str]] = set()
        self.last_evaluated_at: datetime | None = None
        self.last_snapshot_second: int | None = None
        self.last_exit_at: datetime | None = None
        self.confirmations: dict[str, dict[str, Any]] = {}
        self.status = "disabled" if not config.btc_v8.enabled else "waiting_for_btc_market"
        self.last_reason = self.status
        self.diagnostics: dict[str, Any] = {}
        self.candidates: dict[str, Any] = {}
        self.events: list[tuple[str, Any]] = []
        self._recent_trades: list[dict[str, Any]] = registry.recent_trades()
        self._summary: dict[str, Any] = registry.summary(
            include_model_metrics=not config.btc_v8.orderbook_chase_mode
        )

    def _orderbook_chase_mode(self) -> bool:
        settings = self.current_round.settings if self.current_round else self.config.btc_v8
        return settings.orderbook_chase_mode

    def _refresh_views(self) -> None:
        self._recent_trades = self.registry.recent_trades()
        self._summary = self.registry.summary(
            include_model_metrics=not self._orderbook_chase_mode()
        )

    def _record_trade_view(
        self,
        trade: V8Trade,
        *,
        opened_position: bool = False,
        completed_position: bool = False,
    ) -> None:
        payload = trade.model_dump(mode="json")
        self._recent_trades = [
            payload,
            *(item for item in self._recent_trades if item.get("trade_id") != trade.trade_id),
        ][:300]
        if opened_position:
            self._summary["positions"] = int(self._summary.get("positions") or 0) + 1
        if completed_position:
            completed = int(self._summary.get("completed_positions") or 0) + 1
            wins = int(self._summary.get("wins") or 0)
            if (trade.realized_pnl or 0.0) > 0:
                wins += 1
            self._summary["completed_positions"] = completed
            self._summary["wins"] = wins
            self._summary["win_rate"] = wins / completed
            self._summary["realized_pnl"] = float(
                self._summary.get("realized_pnl") or 0.0
            ) + float(trade.realized_pnl or 0.0)
        self._summary["fees_usd"] = float(self._summary.get("fees_usd") or 0.0) + float(
            trade.fee_usd
        )

    def set_market(self, market: MarketState, now: datetime | None = None) -> None:
        now = now or utc_now()
        if market.asset.upper() != "BTC" or market.start_time is None:
            return
        if self.current_round and self.current_round.market_id == market.condition_id:
            return
        if not self.config.btc_v8.enabled:
            self.current_round = None
            self.position = None
            self.status = self.last_reason = "disabled"
            return
        existing = self.registry.get_round(market.condition_id)
        if existing is None:
            if self.config.btc_v8.orderbook_chase_mode:
                market_model = self.model
            else:
                active_model_key = self.registry.activate_pending_for_market(market.start_time)
                market_model = self.registry.load_model_before(active_model_key, market.start_time)
            self.current_round = V8Round(
                market_id=market.condition_id,
                market_slug=market.slug,
                model_key=market_model.model_key,
                model_version=market_model.version,
                start_time=market.start_time,
                end_time=market.end_time,
                settings=self.config.btc_v8.model_copy(deep=True),
                created_at=now,
                updated_at=now,
            )
        else:
            self.current_round = existing
        if existing is None:
            self.registry.save_round(self.current_round)
        self.position = self.registry.get_position(self.current_round.current_position_id)
        latest_closed = self.registry.latest_closed_position(market.condition_id)
        self.last_exit_at = latest_closed.closed_at if latest_closed else None
        if not self.current_round.settings.orderbook_chase_mode:
            self.model = (
                self.registry.load_model_version(
                    self.current_round.model_key, self.current_round.model_version
                )
                or self.registry.load_model_before(
                    self.current_round.model_key, self.current_round.start_time
                )
            )
        self.last_snapshot_second = None
        self.confirmations.clear()
        self.status = self.last_reason = "collecting_signals"
        if self.current_round.settings.orderbook_chase_mode:
            self.registry.discard_raw_buffer()
        else:
            self.registry.cleanup_raw_events(self.current_round.settings.raw_retention_hours, now)
        self.events.append(("btc_v8_round", self.current_round))

    def add_chainlink_tick(self, tick: PriceTick) -> None:
        if tick.price <= 0:
            return
        self.chainlink_ticks.append((_ensure_utc(tick.received_at), tick.price))
        retention_seconds = (
            max(self.config.btc_v8.long_volatility_window_seconds + 5, 65)
            if self._orderbook_chase_mode()
            else 310
        )
        cutoff = tick.received_at - timedelta(seconds=retention_seconds)
        self.chainlink_ticks = [row for row in self.chainlink_ticks if row[0] >= cutoff]

    def add_polymarket_book(self, direction: Direction, book: OrderBookSnapshot) -> None:
        chase_mode = self._orderbook_chase_mode()
        if not chase_mode:
            self.registry.save_raw_event(
                SignalEvent(
                    source="polymarket",
                    market_type="polymarket",
                    kind="book",
                    symbol=direction.value,
                    bids=book.bids,
                    asks=book.asks,
                    exchange_timestamp=_ensure_utc(book.timestamp),
                    received_at=_ensure_utc(book.received_at),
                    valid=book.depth_trusted,
                    reason=None if book.depth_trusted else "depth_untrusted",
                    raw=book.raw,
                )
            )
        previous = self.polymarket_books.get(direction)
        self.polymarket_books[direction] = book
        if chase_mode:
            return
        value = _top_of_book_ofi(previous, book)
        history = self.polymarket_ofi[direction]
        history.append((_ensure_utc(book.received_at), value))
        cutoff = book.received_at - timedelta(seconds=65)
        self.polymarket_ofi[direction] = [row for row in history if row[0] >= cutoff]
        raw_trade = book.raw.get("_last_trade") if isinstance(book.raw, dict) else None
        if isinstance(raw_trade, dict):
            key = (
                direction.value,
                str(raw_trade.get("timestamp") or ""),
                str(raw_trade.get("price") or ""),
                str(raw_trade.get("size") or ""),
            )
            if key not in self._polymarket_trade_keys:
                self._polymarket_trade_keys.add(key)
                if len(self._polymarket_trade_keys) > 10_000:
                    self._polymarket_trade_keys = {key}
                side = str(raw_trade.get("side") or "").upper()
                try:
                    size = max(0.0, float(raw_trade.get("size") or 1.0))
                    price = float(raw_trade.get("price") or 0.0)
                except (TypeError, ValueError):
                    size, price = 0.0, 0.0
                signed_size = size if side == "BUY" else -size
                self.polymarket_trades[direction].append((book.received_at, signed_size))
                self.polymarket_trades[direction] = [
                    row for row in self.polymarket_trades[direction] if row[0] >= cutoff
                ]
                if price > 0 and size > 0:
                    self.registry.save_raw_event(
                        SignalEvent(
                            source="polymarket",
                            market_type="polymarket",
                            kind="trade",
                            symbol=direction.value,
                            price=price,
                            quantity=size,
                            taker_side="buy" if side == "BUY" else "sell",
                            exchange_timestamp=_ensure_utc(book.timestamp),
                            received_at=_ensure_utc(book.received_at),
                            raw=raw_trade,
                        )
                    )

    def add_signal(self, event: SignalEvent) -> None:
        event.processed_at = utc_now()
        source = event.source
        chase_mode = self._orderbook_chase_mode()
        if chase_mode and source == "binance_futures":
            return
        if not chase_mode:
            self.registry.save_raw_event(event)
        if event.kind in {"trade", "liquidation"}:
            if event.kind == "trade":
                self.health[source] = event
            target = self.trades.setdefault(source, [])
            if event.valid and event.price is not None and event.price > 0:
                target.append(event)
            cutoff = event.received_at - timedelta(seconds=5 if chase_mode else 65)
            self.trades[source] = [item for item in target if item.received_at >= cutoff]
            return
        if event.kind == "book":
            self.health[source] = event
            previous = self.books.get(source)
            self.books[source] = event
            if chase_mode:
                return
            history = self.book_ofi.setdefault(source, [])
            history.append((event.received_at, _top_of_book_ofi(previous, event)))
            cutoff = event.received_at - timedelta(seconds=65)
            self.book_ofi[source] = [row for row in history if row[0] >= cutoff]
            return
        if event.kind == "health":
            self.health[source] = event
            return
        if event.kind in {"mark_price", "open_interest", "funding"}:
            value = event.price if event.kind == "mark_price" else event.value
            if value is None or not math.isfinite(value):
                return
            key = f"{source}:{event.kind}"
            target = self.values.setdefault(key, [])
            target.append((event.received_at, value))
            cutoff = event.received_at - timedelta(seconds=310)
            self.values[key] = [row for row in target if row[0] >= cutoff]

    def _source_fresh(self, source: str, now: datetime) -> bool:
        settings = self.current_round.settings if self.current_round else self.config.btc_v8
        trade = next((item for item in reversed(self.trades.get(source, [])) if item.valid), None)
        book = self.books.get(source)
        health = self.health.get(source)
        return bool(
            trade is not None
            and book is not None
            and book.valid
            and (health is None or health.valid)
            and 0 <= (now - trade.received_at).total_seconds() <= settings.spot_stale_seconds
            and 0 <= (now - book.received_at).total_seconds() <= settings.spot_stale_seconds
        )

    def _cvd(self, source: str, now: datetime, seconds: int) -> float | None:
        rows = [
            event for event in self.trades.get(source, [])
            if event.kind == "trade"
            and now - timedelta(seconds=seconds) <= event.received_at <= now
        ]
        if not rows:
            return None
        signed = sum(
            (1.0 if event.taker_side == "buy" else -1.0)
            * (event.quantity or 0.0)
            * (event.price or 0.0)
            for event in rows
        )
        total = sum((event.quantity or 0.0) * (event.price or 0.0) for event in rows)
        return clamp(signed / total if total > 0 else 0.0, -1.0, 1.0)

    def _trade_prices(self, source: str) -> list[tuple[datetime, float]]:
        return [
            (event.received_at, event.price)
            for event in self.trades.get(source, [])
            if event.kind == "trade"
            and event.valid
            and event.price is not None
            and event.price > 0
        ]

    def _ofi(self, source: str, now: datetime, seconds: int = 5) -> float:
        rows = _window_values(self.book_ofi.get(source, []), now, seconds)
        return clamp(sum(value for _, value in rows) / len(rows), -1.0, 1.0) if rows else 0.0

    def _chase_probabilities(
        self,
        market: MarketState,
        now: datetime,
        settings: BtcV8Config,
        current_at: datetime,
        current_price: float,
        age: float,
        remaining: float,
        sigma: float,
        z_score: float,
        formula_up: float,
    ) -> tuple[float, float, dict[str, float], dict[str, Any]]:
        raw_fresh_sources = [
            source for source in settings.spot_exchanges if self._source_fresh(source, now)
        ]
        raw_midpoints = {
            source: float(midpoint)
            for source in raw_fresh_sources
            if (midpoint := _book_metrics(self.books.get(source))["midpoint"]) is not None
        }
        anomalous_sources: set[str] = set()
        if len(raw_midpoints) >= 2:
            median_midpoint = statistics.median(raw_midpoints.values())
            anomalous_sources = {
                source
                for source, midpoint in raw_midpoints.items()
                if abs(math.log(midpoint / median_midpoint)) > 0.005
            }
        fresh_sources = [
            source for source in raw_fresh_sources if source not in anomalous_sources
        ]
        spot_returns_1s = {
            source: value
            for source in fresh_sources
            if (value := _return_for_window(self._trade_prices(source), now, 1)) is not None
        }
        chainlink_return_1s = _return_for_window(self.chainlink_ticks, now, 1)
        lead_returns_1s = [
            value - chainlink_return_1s
            for value in spot_returns_1s.values()
            if chainlink_return_1s is not None
        ]
        source_health = self._source_health(now)
        for source in anomalous_sources:
            source_health[source]["fresh"] = False
            source_health[source]["last_reason"] = "price_outlier"
        diagnostics = {
            "runtime_profile": "orderbook_chase_lightweight",
            "raw_event_archive_enabled": False,
            "futures_features_enabled": False,
            "model_metrics_enabled": False,
            "chainlink_open_price": market.threshold_price,
            "chainlink_current_price": current_price,
            "chainlink_tick_at": current_at.isoformat(),
            "chainlink_age_seconds": age,
            "chase_chainlink_max_age_seconds": min(
                CHASE_MAX_CHAINLINK_AGE_SECONDS,
                settings.spot_stale_seconds,
                settings.chainlink_stale_seconds,
            ),
            "remaining_seconds": remaining,
            "sigma": sigma,
            "z_score": z_score,
            "formula_probability_up": formula_up,
            "model_probability_up": formula_up,
            "residual_model_enabled": False,
            "fresh_spot_exchanges": fresh_sources,
            "anomalous_spot_exchanges": sorted(anomalous_sources),
            "fresh_spot_count": len(fresh_sources),
            "required_fresh_spot_count": settings.min_fresh_spot_exchanges,
            "timestamp_basis": "received_at",
            "spot_returns_1s": spot_returns_1s,
            "chainlink_return_1s": chainlink_return_1s,
            "spot_chainlink_lead_return_1s": (
                statistics.median(lead_returns_1s) if lead_returns_1s else None
            ),
            "spot_positive_return_sources_1s": sum(
                1 for value in spot_returns_1s.values() if value > 0
            ),
            "spot_negative_return_sources_1s": sum(
                1 for value in spot_returns_1s.values() if value < 0
            ),
            "features": {},
            "source_health": source_health,
        }
        return formula_up, formula_up, {}, diagnostics

    def _probabilities(
        self, market: MarketState, books: dict[Direction, OrderBookSnapshot], now: datetime
    ) -> tuple[float, float, dict[str, float], dict[str, Any]] | None:
        if not market.threshold_verified or market.threshold_price is None or market.threshold_price <= 0:
            self.last_reason = "chainlink_open_unverified"
            return None
        if not self.chainlink_ticks:
            self.last_reason = "chainlink_current_unavailable"
            return None
        current_at, current_price = self.chainlink_ticks[-1]
        settings = self.current_round.settings
        age = (now - current_at).total_seconds()
        if age < 0 or age > settings.chainlink_stale_seconds:
            self.last_reason = "chainlink_stale"
            return None
        short_rows = _window_values(
            self.chainlink_ticks, now, settings.short_volatility_window_seconds
        )
        long_rows = _window_values(
            self.chainlink_ticks, now, settings.long_volatility_window_seconds
        )
        short_volatility = _volatility(short_rows)
        long_volatility = _volatility(long_rows)
        floor = settings.volatility_floor_bps / 10_000.0
        sigma = max(short_volatility, long_volatility, floor)
        remaining = max(0.001, (market.end_time - now).total_seconds())
        log_return = math.log(current_price / market.threshold_price)
        z_score = log_return / (sigma * math.sqrt(remaining))
        formula_up = clamp(normal_cdf(z_score), 0.01, 0.99)
        if settings.orderbook_chase_mode:
            return self._chase_probabilities(
                market,
                now,
                settings,
                current_at,
                current_price,
                age,
                remaining,
                sigma,
                z_score,
                formula_up,
            )
        crossings = 0
        prior_side: bool | None = None
        for _, price in long_rows:
            side = price >= market.threshold_price
            if prior_side is not None and side != prior_side:
                crossings += 1
            prior_side = side
        features = {name: 0.0 for name in V8_FEATURE_NAMES}
        features.update(
            {
                "remaining_time": normalized_remaining_time(
                    market.start_time, market.end_time, now
                ),
                "volatility_ratio": clamp(
                    (short_volatility / max(long_volatility, floor) - 1.0) / 2.0,
                    -1.0,
                    1.0,
                ),
                "open_crossings": clamp(crossings / 10.0, 0.0, 1.0),
            }
        )
        up_metrics = _book_metrics(books.get(Direction.UP))
        down_metrics = _book_metrics(books.get(Direction.DOWN))
        features.update(
            {
                "poly_up_market_gap": clamp(
                    ((up_metrics["midpoint"] if up_metrics["midpoint"] is not None else formula_up) - formula_up)
                    / 0.2,
                    -1.0,
                    1.0,
                ),
                "poly_up_depth_imbalance": float(up_metrics["imbalance"]),
                "poly_up_spread": float(up_metrics["spread"]),
                "poly_up_microprice": float(up_metrics["microprice"]),
                "poly_up_ofi_5s": self._polymarket_ofi(Direction.UP, now),
                "poly_up_trade_flow_5s": self._polymarket_trade_flow(Direction.UP, now),
                "poly_down_depth_imbalance": float(down_metrics["imbalance"]),
                "poly_down_spread": float(down_metrics["spread"]),
                "poly_down_microprice": float(down_metrics["microprice"]),
                "poly_down_ofi_5s": self._polymarket_ofi(Direction.DOWN, now),
                "poly_down_trade_flow_5s": self._polymarket_trade_flow(Direction.DOWN, now),
            }
        )
        chainlink_returns = {
            seconds: _return_for_window(self.chainlink_ticks, now, seconds)
            for seconds in RETURN_WINDOWS
        }
        raw_fresh_sources = [
            source for source in settings.spot_exchanges if self._source_fresh(source, now)
        ]
        source_metrics = {
            source: _book_metrics(self.books.get(source)) for source in SPOT_EXCHANGES
        }
        raw_midpoints = {
            source: float(source_metrics[source]["midpoint"])
            for source in raw_fresh_sources
            if source_metrics[source]["midpoint"] is not None
        }
        anomalous_sources: set[str] = set()
        if len(raw_midpoints) >= 2:
            median_midpoint = statistics.median(raw_midpoints.values())
            anomalous_sources = {
                source
                for source, midpoint in raw_midpoints.items()
                if abs(math.log(midpoint / median_midpoint)) > 0.005
            }
        fresh_sources = [
            source for source in raw_fresh_sources if source not in anomalous_sources
        ]
        spot_returns: dict[str, dict[int, float | None]] = {}
        spot_cvds: dict[str, dict[int, float | None]] = {}
        spot_midpoints: list[float] = []
        for source in SPOT_EXCHANGES:
            source_is_fresh = source in fresh_sources
            source_rows = self._trade_prices(source) if source_is_fresh else []
            returns = {
                seconds: _return_for_window(source_rows, now, seconds)
                for seconds in RETURN_WINDOWS
            }
            cvds = {
                seconds: self._cvd(source, now, seconds) if source_is_fresh else None
                for seconds in RETURN_WINDOWS
            }
            spot_returns[source] = returns
            spot_cvds[source] = cvds
            for seconds in RETURN_WINDOWS:
                standard = (
                    returns[seconds] / (sigma * math.sqrt(seconds))
                    if returns[seconds] is not None
                    else 0.0
                )
                features[f"{source}_return_{seconds}s"] = clamp(standard / 3.0, -1.0, 1.0)
                features[f"{source}_cvd_{seconds}s"] = cvds[seconds] or 0.0
            recent_count = len(_window_values(source_rows, now, 5))
            long_count = len(_window_values(source_rows, now, 30))
            ratio = recent_count / max(long_count / 6.0, 1.0)
            metrics = source_metrics[source] if source_is_fresh else _book_metrics(None)
            latest_trade = next(
                (
                    event
                    for event in reversed(self.trades.get(source, []))
                    if event.kind == "trade" and event.valid
                ),
                None,
            )
            latest_book = self.books.get(source)
            latency_seconds = max(
                0.0,
                (latest_trade.received_at - latest_trade.exchange_timestamp).total_seconds()
                if latest_trade else 0.0,
                (latest_book.received_at - latest_book.exchange_timestamp).total_seconds()
                if latest_book else 0.0,
            )
            source_health = self.health.get(source)
            features.update(
                {
                    f"{source}_trade_intensity": clamp((ratio - 1.0) / 2.0, -1.0, 1.0),
                    f"{source}_book_imbalance": float(metrics["imbalance"]),
                    f"{source}_microprice": float(metrics["microprice"]),
                    f"{source}_spread": float(metrics["spread"]),
                    f"{source}_ofi_5s": self._ofi(source, now) if source_is_fresh else 0.0,
                    f"{source}_latency": clamp(latency_seconds / 2.0, 0.0, 1.0),
                    f"{source}_anomaly": 1.0 if source in anomalous_sources else 0.0,
                    f"{source}_health_failure": 1.0 if source_health and not source_health.valid else 0.0,
                    f"{source}_missing": 0.0 if source_is_fresh else 1.0,
                }
            )
            if metrics["midpoint"] is not None and source_is_fresh:
                spot_midpoints.append(float(metrics["midpoint"]))
            for seconds in (1, 5):
                chain = chainlink_returns[seconds]
                gap = (
                    (returns[seconds] - chain) / (sigma * math.sqrt(seconds))
                    if returns[seconds] is not None and chain is not None
                    else 0.0
                )
                features[f"{source}_chainlink_gap_{seconds}s"] = clamp(gap / 3.0, -1.0, 1.0)
        spot_returns_1s = {
            source: float(spot_returns[source][1])
            for source in fresh_sources
            if spot_returns[source][1] is not None
        }
        chainlink_return_1s = chainlink_returns[1]
        lead_returns_1s = [
            value - chainlink_return_1s
            for value in spot_returns_1s.values()
            if chainlink_return_1s is not None
        ]
        spot_chainlink_lead_return_1s = (
            statistics.median(lead_returns_1s) if lead_returns_1s else None
        )
        for seconds in RETURN_WINDOWS:
            values = [
                spot_returns[source][seconds] / (sigma * math.sqrt(seconds))
                for source in fresh_sources
                if spot_returns[source][seconds] is not None
            ]
            cvd_values = [
                spot_cvds[source][seconds]
                for source in fresh_sources
                if spot_cvds[source][seconds] is not None
            ]
            if values:
                features[f"cross_median_return_{seconds}s"] = clamp(statistics.median(values) / 3.0, -1.0, 1.0)
                features[f"cross_direction_agreement_{seconds}s"] = clamp(
                    sum(1.0 if value > 0 else -1.0 if value < 0 else 0.0 for value in values)
                    / len(values),
                    -1.0,
                    1.0,
                )
                features[f"cross_return_dispersion_{seconds}s"] = clamp(
                    (max(values) - min(values)) / 6.0, 0.0, 1.0
                )
            if cvd_values:
                features[f"cross_cvd_consensus_{seconds}s"] = clamp(
                    statistics.median(cvd_values), -1.0, 1.0
                )
        if len(spot_midpoints) >= 2:
            features["cross_price_dispersion"] = clamp(
                math.log(max(spot_midpoints) / min(spot_midpoints)) / 0.001, 0.0, 1.0
            )
        features["fresh_spot_fraction"] = len(fresh_sources) / max(1, len(settings.spot_exchanges))
        self._futures_features(features, now, sigma, spot_midpoints)
        residual_model_enabled = not settings.orderbook_chase_mode
        if residual_model_enabled:
            maximum_correction = settings.max_probability_correction_points / 100.0
            model_up = self.model.probability(formula_up, features, maximum_correction)
        else:
            model_up = formula_up
        source_health = self._source_health(now)
        for source in anomalous_sources:
            source_health[source]["fresh"] = False
            source_health[source]["last_reason"] = "price_outlier"
        diagnostics = {
            "chainlink_open_price": market.threshold_price,
            "chainlink_current_price": current_price,
            "chainlink_tick_at": current_at.isoformat(),
            "chainlink_age_seconds": age,
            "chase_chainlink_max_age_seconds": min(
                CHASE_MAX_CHAINLINK_AGE_SECONDS,
                settings.spot_stale_seconds,
                settings.chainlink_stale_seconds,
            ),
            "remaining_seconds": remaining,
            "sigma": sigma,
            "z_score": z_score,
            "formula_probability_up": formula_up,
            "model_probability_up": model_up,
            "residual_model_enabled": residual_model_enabled,
            "fresh_spot_exchanges": fresh_sources,
            "anomalous_spot_exchanges": sorted(anomalous_sources),
            "fresh_spot_count": len(fresh_sources),
            "required_fresh_spot_count": settings.min_fresh_spot_exchanges,
            "timestamp_basis": "received_at",
            "spot_returns_1s": spot_returns_1s,
            "chainlink_return_1s": chainlink_return_1s,
            "spot_chainlink_lead_return_1s": spot_chainlink_lead_return_1s,
            "spot_positive_return_sources_1s": sum(
                1 for value in spot_returns_1s.values() if value > 0
            ),
            "spot_negative_return_sources_1s": sum(
                1 for value in spot_returns_1s.values() if value < 0
            ),
            "features": features,
            "source_health": source_health,
        }
        return formula_up, model_up, features, diagnostics

    def _polymarket_ofi(self, direction: Direction, now: datetime) -> float:
        rows = _window_values(self.polymarket_ofi[direction], now, 5)
        return clamp(sum(value for _, value in rows) / len(rows), -1.0, 1.0) if rows else 0.0

    def _polymarket_trade_flow(self, direction: Direction, now: datetime) -> float:
        rows = _window_values(self.polymarket_trades[direction], now, 5)
        total = sum(abs(value) for _, value in rows)
        return clamp(sum(value for _, value in rows) / total, -1.0, 1.0) if total > 0 else 0.0

    def _futures_features(
        self,
        features: dict[str, float],
        now: datetime,
        sigma: float,
        spot_midpoints: list[float],
    ) -> None:
        source = "binance_futures"
        last_trade = next(
            (
                event
                for event in reversed(self.trades.get(source, []))
                if event.kind == "trade" and event.valid
            ),
            None,
        )
        book = self.books.get(source)
        health = self.health.get(source)
        fresh = bool(
            last_trade is not None
            and book is not None
            and book.valid
            and (health is None or health.valid)
            and 0 <= (now - last_trade.received_at).total_seconds() <= 5
            and 0 <= (now - book.received_at).total_seconds() <= 5
        )
        rows = self._trade_prices(source) if fresh else []
        for seconds in (1, 5, 30):
            value = _return_for_window(rows, now, seconds)
            features[f"futures_return_{seconds}s"] = clamp(
                value / (sigma * math.sqrt(seconds)) / 3.0 if value is not None else 0.0,
                -1.0,
                1.0,
            )
        features["futures_cvd_5s"] = (self._cvd(source, now, 5) or 0.0) if fresh else 0.0
        features["futures_cvd_30s"] = (self._cvd(source, now, 30) or 0.0) if fresh else 0.0
        metrics = _book_metrics(book if fresh else None)
        features.update(
            {
                "futures_book_imbalance": float(metrics["imbalance"]),
                "futures_microprice": float(metrics["microprice"]),
                "futures_spread": float(metrics["spread"]),
                "futures_ofi_5s": self._ofi(source, now),
                "futures_missing": 0.0 if fresh else 1.0,
            }
        )
        marks = self.values.get(f"{source}:mark_price", [])
        current_mark = (
            marks[-1][1]
            if fresh and marks and 0 <= (now - marks[-1][0]).total_seconds() <= 5
            else None
        )
        if current_mark and spot_midpoints:
            spot = statistics.median(spot_midpoints)
            features["futures_basis"] = clamp(math.log(current_mark / spot) / 0.002, -1.0, 1.0)
            old_mark = next(
                (value for timestamp, value in reversed(marks) if timestamp <= now - timedelta(seconds=30)),
                None,
            )
            if old_mark:
                features["futures_basis_change_30s"] = clamp(
                    math.log(current_mark / old_mark) / 0.002, -1.0, 1.0
                )
        open_interest = self.values.get(f"{source}:open_interest", [])
        if fresh and open_interest and 0 <= (now - open_interest[-1][0]).total_seconds() <= 10:
            current = open_interest[-1][1]
            old = next(
                (value for timestamp, value in reversed(open_interest) if timestamp <= now - timedelta(seconds=30)),
                None,
            )
            if old and current > 0 and old > 0:
                features["futures_open_interest_change_30s"] = clamp(
                    math.log(current / old) / 0.005, -1.0, 1.0
                )
        funding = self.values.get(f"{source}:funding", [])
        if fresh and funding and 0 <= (now - funding[-1][0]).total_seconds() <= 10:
            features["futures_funding"] = clamp(funding[-1][1] / 0.001, -1.0, 1.0)
        liquidations = [
            event
            for event in self.trades.get(source, [])
            if fresh and event.kind == "liquidation"
        ]
        for seconds in (5, 30):
            rows_liq = [
                event for event in liquidations
                if now - timedelta(seconds=seconds) <= event.received_at <= now
            ]
            signed = sum(
                (1.0 if event.taker_side == "buy" else -1.0)
                * (event.quantity or 0.0) * (event.price or 0.0)
                for event in rows_liq
            )
            total = sum((event.quantity or 0.0) * (event.price or 0.0) for event in rows_liq)
            features[f"futures_liquidation_{seconds}s"] = clamp(
                signed / total if total > 0 else 0.0, -1.0, 1.0
            )

    def _source_health(self, now: datetime) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for source in (*SPOT_EXCHANGES, "binance_futures"):
            trade = next(
                (
                    event
                    for event in reversed(self.trades.get(source, []))
                    if event.kind == "trade" and event.valid
                ),
                None,
            )
            book = self.books.get(source)
            health = self.health.get(source)
            trade_age = (now - trade.received_at).total_seconds() if trade else None
            book_age = (now - book.received_at).total_seconds() if book else None
            futures_fresh = bool(
                trade
                and book
                and book.valid
                and trade_age is not None
                and book_age is not None
                and 0 <= trade_age <= 5
                and 0 <= book_age <= 5
            )
            result[source] = {
                "fresh": self._source_fresh(source, now) if source in SPOT_EXCHANGES else futures_fresh,
                "trade_at": trade.received_at.isoformat() if trade else None,
                "book_at": book.received_at.isoformat() if book else None,
                "trade_age_ms": round(trade_age * 1000) if trade_age is not None else None,
                "book_age_ms": round(book_age * 1000) if book_age is not None else None,
                "book_valid": bool(book and book.valid),
                "last_reason": (
                    health.reason
                    if health is not None and not health.valid
                    else book.reason if book else "not_connected"
                ),
            }
        return result

    def _book_ready(
        self, market: MarketState, direction: Direction, book: OrderBookSnapshot | None, now: datetime
    ) -> str | None:
        if book is None:
            return "book_missing"
        expected = market.up_token_id if direction == Direction.UP else market.down_token_id
        if book.token_id != expected or book.market_id not in {None, market.condition_id}:
            return "book_market_mismatch"
        if not book.depth_trusted:
            return "book_depth_untrusted"
        if not 0 <= (now - book.received_at).total_seconds() * 1000 <= self.config.risk.max_data_age_ms:
            return "book_stale"
        return None

    def _buy_candidate(
        self,
        market: MarketState,
        direction: Direction,
        probability: float,
        formula_probability: float,
        books: dict[Direction, OrderBookSnapshot],
        now: datetime,
        *,
        valuation_probability: float | None = None,
        strategy_mode: Literal["manual", "auto", "orderbook_chase"] | None = None,
        decision_details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        settings = self.current_round.settings
        policy = v8_decision_policy(settings)
        valuation = probability if valuation_probability is None else valuation_probability
        mode = strategy_mode or ("auto" if policy["auto_decision_mode"] else "manual")
        estimated_exit_fee_per_share = (
            taker_fee_usd(1.0, valuation, self.config.strategy.taker_fee_rate)
            if mode == "orderbook_chase"
            else 0.0
        )
        book = books.get(direction)
        reason = self._book_ready(market, direction, book, now)
        limit = dynamic_max_price(
            max(0.01, valuation - estimated_exit_fee_per_share),
            self.config.strategy.taker_fee_rate,
            settings.slippage_reserve_cents / 100.0,
            float(policy["buy_edge_cents"]) / 100.0,
            book.tick_size if book else market.tick_size,
        )
        result: dict[str, Any] = {
            "direction": direction.value,
            "probability": probability,
            "formula_probability": formula_probability,
            "valuation_probability": valuation,
            "limit": limit,
            "eligible": False,
            "reason": reason,
            "auto_decision_mode": policy["auto_decision_mode"],
            "orderbook_chase_mode": policy["orderbook_chase_mode"],
            "strategy_mode": mode,
            "required_edge_cents": policy["buy_edge_cents"],
            "estimated_exit_fee_per_share": estimated_exit_fee_per_share,
            "decision_details": decision_details or {},
        }
        if reason is not None or limit is None or book is None:
            result["reason"] = reason or "model_edge_below_threshold"
            return result
        limited = book.model_copy(deep=True)
        limited.asks = [level for level in limited.asks if level.price <= limit + 1e-12]
        execution = simulate_buy(
            limited, settings.quote_amount_usd, self.config.strategy.taker_fee_rate
        )
        result.update(execution.model_dump())
        if not execution.complete:
            result["reason"] = "depth_below_limit"
            return result
        if execution.quantity + 1e-12 < max(market.min_order_size, book.min_order_size):
            result["reason"] = "quantity_below_market_minimum"
            return result
        edge = (
            valuation
            - execution.avg_price
            - execution.fee_usd / execution.quantity
            - estimated_exit_fee_per_share
            - settings.slippage_reserve_cents / 100.0
        )
        result["edge_per_share"] = edge
        if edge + 1e-12 < float(policy["buy_edge_cents"]) / 100.0:
            result["reason"] = "actual_edge_below_threshold"
            return result
        if policy["automated_mode"]:
            liquidation = simulate_sell(
                book, execution.quantity, self.config.strategy.taker_fee_rate
            )
            result["immediate_liquidation_complete"] = liquidation.complete
            if not liquidation.complete:
                result["reason"] = "auto_exit_depth_unavailable"
                return result
            immediate_pnl = (
                liquidation.quote
                - liquidation.fee_usd
                - execution.quote
                - execution.fee_usd
            )
            loss_limit = max(0.01, execution.quote * AUTO_EXIT_LOSS_FRACTION)
            result["immediate_liquidation_pnl"] = immediate_pnl
            result["auto_loss_limit_usd"] = loss_limit
            if immediate_pnl <= -loss_limit + 1e-12:
                result["reason"] = "auto_liquidation_risk"
                return result
        result["eligible"] = True
        result["reason"] = "eligible"
        return result

    def _confirmation(
        self,
        key: str,
        candidate_key: str | None,
        update_key: str,
        now: datetime,
        seconds: float,
        updates: int,
    ) -> bool:
        if candidate_key is None:
            self.confirmations.pop(key, None)
            return False
        state = self.confirmations.get(key)
        if state is None or state["candidate"] != candidate_key:
            state = {"candidate": candidate_key, "started_at": now, "updates": set()}
            self.confirmations[key] = state
        state["updates"].add(update_key)
        return (
            (now - state["started_at"]).total_seconds() + 1e-12 >= seconds
            and len(state["updates"]) >= updates
        )

    def _update_key(self, books: dict[Direction, OrderBookSnapshot]) -> str:
        parts = [self.chainlink_ticks[-1][0].isoformat() if self.chainlink_ticks else ""]
        parts.extend(
            books[direction].received_at.isoformat() if direction in books else ""
            for direction in (Direction.UP, Direction.DOWN)
        )
        for source in SPOT_EXCHANGES:
            trade = self.trades.get(source, [])[-1] if self.trades.get(source) else None
            parts.append(trade.received_at.isoformat() if trade else "")
        return "|".join(parts)

    def _open_position(
        self, candidate: dict[str, Any], now: datetime
    ) -> None:
        direction = Direction(candidate["direction"])
        probability = float(candidate["probability"])
        formula = float(candidate["formula_probability"])
        strategy_mode = candidate.get("strategy_mode", "manual")
        details = dict(candidate.get("decision_details") or {})
        position = V8Position(
            market_id=self.current_round.market_id,
            market_slug=self.current_round.market_slug,
            direction=direction,
            quantity=float(candidate["quantity"]),
            entry_price=float(candidate["avg_price"]),
            entry_quote=float(candidate["quote"]),
            entry_fee_usd=float(candidate["fee_usd"]),
            entry_model_probability=probability,
            entry_formula_probability=formula,
            strategy_mode=strategy_mode,
            entry_target_probability=(
                float(candidate["valuation_probability"])
                if strategy_mode == "orderbook_chase"
                else None
            ),
            entry_signal_strength=(
                float(details.get("signal_strength") or 0.0)
                if strategy_mode == "orderbook_chase"
                else None
            ),
            opened_at=now,
        )
        self.registry.save_position(position)
        trade = self.registry.save_trade(
            V8Trade(
                market_id=position.market_id,
                market_slug=position.market_slug,
                position_id=position.position_id,
                action="BUY",
                direction=direction,
                model_probability=probability,
                formula_probability=formula,
                avg_price=position.entry_price,
                quantity=position.quantity,
                quote=position.entry_quote,
                fee_usd=position.entry_fee_usd,
                edge_per_share=float(candidate["edge_per_share"]),
                reason=("chase_buy" if strategy_mode == "orderbook_chase" else "model_buy"),
                decision_details=details,
                strategy_mode=strategy_mode,
                created_at=now,
            )
        )
        self.position = position
        self.current_round.current_position_id = position.position_id
        self.current_round.entry_count += 1
        self.current_round.updated_at = now
        self.registry.save_round(self.current_round)
        self.confirmations.clear()
        self.events.append(("btc_v8_trade", trade))
        self._record_trade_view(trade, opened_position=True)

    def _evaluate_sell(
        self,
        market: MarketState,
        model_up: float,
        formula_up: float,
        features: dict[str, float],
        diagnostics: dict[str, Any],
        elapsed: float,
        books: dict[Direction, OrderBookSnapshot],
        now: datetime,
        update_key: str,
    ) -> bool:
        position = self.position
        if position is None:
            return False
        settings = self.current_round.settings
        policy = v8_decision_policy(settings)
        book = books.get(position.direction)
        reason = self._book_ready(market, position.direction, book, now)
        if reason is not None or book is None:
            self.last_reason = reason or "book_missing"
            return False
        execution = simulate_sell(book, position.quantity, self.config.strategy.taker_fee_rate)
        if not execution.complete:
            self.last_reason = "sell_depth_unavailable"
            return False
        probability = model_up if position.direction == Direction.UP else 1.0 - model_up
        formula = formula_up if position.direction == Direction.UP else 1.0 - formula_up
        net_value = (
            execution.avg_price
            - execution.fee_usd / execution.quantity
            - settings.slippage_reserve_cents / 100.0
        )
        value_edge = net_value - probability
        pnl = execution.quote - execution.fee_usd - position.entry_quote - position.entry_fee_usd
        exit_reason = None
        decision_details: dict[str, Any] = {}
        if position.strategy_mode == "orderbook_chase":
            if position.peak_unrealized_pnl is None or pnl > position.peak_unrealized_pnl:
                position.peak_unrealized_pnl = pnl
                self.registry.save_position(position)
            decision_details = v8_orderbook_chase_exit_decision(
                position=position,
                net_value=net_value,
                pnl=pnl,
                diagnostics=diagnostics,
                now=now,
                take_profit_arm_usd=settings.chase_take_profit_arm_usd,
                take_profit_drawdown_usd=settings.chase_take_profit_drawdown_usd,
                take_profit_drawdown_fraction=settings.chase_take_profit_drawdown_fraction,
            )
            if decision_details["eligible"]:
                exit_reason = str(decision_details["reason"])
        elif policy["auto_decision_mode"]:
            position_changed = False
            if position.entry_model_probability is None:
                position.entry_model_probability = probability
                position_changed = True
            if position.entry_formula_probability is None:
                position.entry_formula_probability = formula
                position_changed = True
            if position.peak_unrealized_pnl is None or pnl > position.peak_unrealized_pnl:
                position.peak_unrealized_pnl = pnl
                position_changed = True
            if position_changed:
                self.registry.save_position(position)
            decision_details = v8_auto_exit_decision(
                position=position,
                probability=probability,
                formula_probability=formula,
                net_value=net_value,
                pnl=pnl,
                features=features,
                diagnostics=diagnostics,
                elapsed_seconds=elapsed,
                sell_end_seconds=settings.sell_end_seconds,
                trained_markets=self.model.trained_markets,
                emergency_loss_enabled=settings.auto_emergency_loss_enabled,
            )
            if decision_details["eligible"]:
                exit_reason = str(decision_details["reason"])
        else:
            if value_edge + 1e-12 >= float(policy["sell_edge_cents"]) / 100.0:
                exit_reason = "model_value_sell"
            elif policy["use_fixed_max_loss"] and pnl <= -settings.max_loss_usd + 1e-12:
                exit_reason = "max_loss"
        self.candidates["SELL"] = {
            "direction": position.direction.value,
            "probability": probability,
            "formula_probability": formula,
            "avg_price": execution.avg_price,
            "quantity": execution.quantity,
            "quote": execution.quote,
            "fee_usd": execution.fee_usd,
            "value_edge_per_share": value_edge,
            "pnl": pnl,
            "eligible": exit_reason is not None,
            "reason": exit_reason or "hold_value_higher",
            "auto_decision_mode": policy["auto_decision_mode"],
            "orderbook_chase_mode": position.strategy_mode == "orderbook_chase",
            "strategy_mode": position.strategy_mode,
            "required_edge_cents": policy["sell_edge_cents"],
            **decision_details,
        }
        confirmation_seconds = float(policy["sell_confirmation_seconds"])
        confirmation_updates = int(policy["sell_confirmation_updates"])
        if exit_reason == "auto_emergency_loss":
            confirmation_seconds = 0.0
            confirmation_updates = 1
        elif exit_reason == "chase_timeout":
            confirmation_seconds = 0.0
            confirmation_updates = 1
        confirmed = self._confirmation(
            "sell",
            exit_reason,
            update_key,
            now,
            confirmation_seconds,
            confirmation_updates,
        )
        if not confirmed:
            self.status = self.last_reason = "confirming_sell" if exit_reason else "holding"
            return False
        position.status = "CLOSED"
        position.exit_price = execution.avg_price
        position.exit_quote = execution.quote
        position.exit_fee_usd = execution.fee_usd
        position.realized_pnl = pnl
        position.closed_at = now
        position.exit_reason = exit_reason
        self.registry.save_position(position)
        trade = self.registry.save_trade(
            V8Trade(
                market_id=position.market_id,
                market_slug=position.market_slug,
                position_id=position.position_id,
                action="SELL",
                direction=position.direction,
                model_probability=probability,
                formula_probability=formula,
                avg_price=execution.avg_price,
                quantity=execution.quantity,
                quote=execution.quote,
                fee_usd=execution.fee_usd,
                edge_per_share=value_edge,
                reason=exit_reason or "model_sell",
                realized_pnl=pnl,
                decision_details=decision_details,
                strategy_mode=position.strategy_mode,
                created_at=now,
            )
        )
        self.current_round.current_position_id = None
        self.current_round.updated_at = now
        self.registry.save_round(self.current_round)
        self.position = None
        self.last_exit_at = now
        self.confirmations.clear()
        self.status = self.last_reason = "sold"
        self.events.append(("btc_v8_trade", trade))
        self._record_trade_view(trade, completed_position=True)
        return True

    def evaluate(
        self,
        market: MarketState | None,
        books: dict[Direction, OrderBookSnapshot],
        now: datetime | None = None,
        force: bool = False,
    ) -> None:
        now = now or utc_now()
        if not self.config.btc_v8.enabled:
            self.status = self.last_reason = "disabled"
            return
        if market is None or market.asset.upper() != "BTC" or market.start_time is None:
            self.status = self.last_reason = "waiting_for_btc_market"
            return
        self.set_market(market, now)
        if self.current_round is None:
            return
        interval = self.current_round.settings.evaluation_interval_ms / 1000.0
        if not force and self.last_evaluated_at and (now - self.last_evaluated_at).total_seconds() < interval:
            return
        self.last_evaluated_at = now
        probabilities = self._probabilities(market, books, now)
        if probabilities is None:
            self.status = self.last_reason
            return
        formula_up, model_up, features, diagnostics = probabilities
        decision_policy = v8_decision_policy(self.current_round.settings)
        diagnostics["orderbook_chase"] = v8_orderbook_chase_signal(
            formula_up, diagnostics
        )
        self.diagnostics = {
            **diagnostics,
            "model_probability_down": 1.0 - model_up,
            "formula_probability_down": 1.0 - formula_up,
            "decision_policy": decision_policy,
        }
        if not decision_policy["orderbook_chase_mode"]:
            self.diagnostics["model"] = self.model.model_dump(mode="json")
            self.diagnostics["feature_contributions"] = sorted(
                (
                    {
                        "feature": name,
                        "value": features.get(name, 0.0),
                        "weight": self.model.weights.get(name, 0.0),
                        "contribution": features.get(name, 0.0)
                        * self.model.weights.get(name, 0.0),
                    }
                    for name in V8_FEATURE_NAMES
                ),
                key=lambda item: abs(item["contribution"]),
                reverse=True,
            )[:12]
        elapsed = max(0.0, (now - market.start_time).total_seconds())
        snapshot_second = int(elapsed // self.current_round.settings.snapshot_interval_seconds) * self.current_round.settings.snapshot_interval_seconds
        if (
            not decision_policy["orderbook_chase_mode"]
            and snapshot_second != self.last_snapshot_second
            and elapsed <= 300
        ):
            if self.registry.save_snapshot(
                V8Snapshot(
                    market_id=market.condition_id,
                    model_key=self.current_round.model_key,
                    model_version=self.current_round.model_version,
                    snapshot_second=snapshot_second,
                    formula_probability=formula_up,
                    model_probability=model_up,
                    features=features,
                    fresh_spot_exchanges=diagnostics["fresh_spot_exchanges"],
                    source_health=diagnostics["source_health"],
                    created_at=now,
                )
            ):
                self.last_snapshot_second = snapshot_second
        update_key = self._update_key(books)
        if self.position is not None:
            if elapsed < self.current_round.settings.sell_end_seconds:
                self._evaluate_sell(
                    market,
                    model_up,
                    formula_up,
                    features,
                    diagnostics,
                    elapsed,
                    books,
                    now,
                    update_key,
                )
            else:
                self.status = self.last_reason = "holding_for_settlement"
            return
        settings = self.current_round.settings
        if elapsed >= settings.entry_end_seconds:
            self.status = self.last_reason = "entry_window_closed"
            return
        if self.current_round.entry_count >= settings.max_entries_per_market:
            self.status = self.last_reason = "market_entry_limit"
            return
        if self.last_exit_at and (now - self.last_exit_at).total_seconds() < settings.reentry_cooldown_seconds:
            self.status = self.last_reason = "reentry_cooldown"
            return
        if diagnostics["fresh_spot_count"] < settings.min_fresh_spot_exchanges:
            self.status = self.last_reason = "insufficient_fresh_spot_exchanges"
            return
        chase = diagnostics["orderbook_chase"]
        if decision_policy["orderbook_chase_mode"]:
            chase_direction = (
                Direction(chase["direction"]) if chase.get("eligible") else None
            )
            candidates = []
            for direction in (Direction.UP, Direction.DOWN):
                probability = model_up if direction == Direction.UP else 1.0 - model_up
                formula = formula_up if direction == Direction.UP else 1.0 - formula_up
                if direction != chase_direction:
                    candidates.append(
                        {
                            "direction": direction.value,
                            "probability": probability,
                            "formula_probability": formula,
                            "eligible": False,
                            "reason": (
                                str(chase.get("reason") or "chase_waiting_history")
                                if chase_direction is None
                                else "chase_opposite_direction"
                            ),
                            "orderbook_chase_mode": True,
                            "strategy_mode": "orderbook_chase",
                            "decision_details": chase,
                        }
                    )
                    continue
                target = float(chase["target_probability"])
                candidates.append(
                    self._buy_candidate(
                        market,
                        direction,
                        probability,
                        formula,
                        books,
                        now,
                        valuation_probability=target,
                        strategy_mode="orderbook_chase",
                        decision_details=chase,
                    )
                )
        else:
            candidates = [
                self._buy_candidate(market, Direction.UP, model_up, formula_up, books, now),
                self._buy_candidate(
                    market, Direction.DOWN, 1.0 - model_up, 1.0 - formula_up, books, now
                ),
            ]
        self.candidates = {candidate["direction"]: candidate for candidate in candidates}
        eligible = [candidate for candidate in candidates if candidate.get("eligible")]
        choice = max(eligible, key=lambda item: item["edge_per_share"], default=None)
        confirmed = self._confirmation(
            "buy",
            choice["direction"] if choice else None,
            update_key,
            now,
            float(decision_policy["buy_confirmation_seconds"]),
            int(decision_policy["buy_confirmation_updates"]),
        )
        if choice is not None and confirmed:
            self._open_position(choice, now)
            self.status = self.last_reason = f"bought_{choice['direction'].lower()}"
        elif choice is not None:
            self.status = self.last_reason = "confirming_buy"
        else:
            self.status = "waiting_for_edge"
            self.last_reason = next(
                (candidate.get("reason") for candidate in candidates if candidate.get("reason") != "eligible"),
                self.status,
            )

    def settle(
        self, market_slug: str, outcome: Direction, now: datetime | None = None
    ) -> V8Round | None:
        result = self.registry.settle(market_slug, outcome, now or utc_now())
        if result is None:
            return None
        round_, model, trades, newly_settled = result
        if self.current_round and self.current_round.market_slug == market_slug:
            self.current_round = round_
            self.position = None
            self.model = model
            self.status = self.last_reason = "official_settlement"
        if newly_settled:
            for trade in trades:
                self.events.append(("btc_v8_trade", trade))
            self.events.append(("btc_v8_settlement", round_))
        self._refresh_views()
        return round_

    def unresolved_slugs(self) -> list[str]:
        rows = self.registry.connection.execute(
            "SELECT payload_json FROM btc_v8_rounds ORDER BY end_time"
        ).fetchall()
        result = []
        for row in rows:
            round_ = V8Round.model_validate_json(row["payload_json"])
            if round_.official_outcome is None and round_.end_time <= utc_now():
                result.append(round_.market_slug)
        return result

    def drain_events(self) -> list[tuple[str, Any]]:
        events, self.events = self.events, []
        return events

    def dashboard_state(self) -> dict[str, Any]:
        chase_mode = self._orderbook_chase_mode()
        if chase_mode:
            pending_model_key, pending_not_before = None, None
        else:
            pending_model_key, pending_not_before = self.registry.pending_model()
        return {
            "enabled": self.config.btc_v8.enabled,
            "status": self.status,
            "last_reason": self.last_reason,
            "config": (
                self.current_round.settings.model_dump(mode="json")
                if self.current_round else self.config.btc_v8.model_dump(mode="json")
            ),
            "model": None if chase_mode else self.model.model_dump(mode="json"),
            "model_control": {
                "active_model_key": None if chase_mode else self.registry.active_model_key(),
                "pending_model_key": pending_model_key,
                "pending_model_not_before": (
                    pending_not_before.isoformat() if pending_not_before else None
                ),
            },
            "round": self.current_round.model_dump(mode="json") if self.current_round else None,
            "position": self.position.model_dump(mode="json") if self.position else None,
            "diagnostics": self.diagnostics,
            "candidates": self.candidates,
            "recent_trades": self._recent_trades,
            "summary": self._summary,
        }
