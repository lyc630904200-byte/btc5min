from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class SourceConfig(BaseModel):
    enabled_assets: list[str] = Field(default_factory=lambda: ["BTC", "ETH"])
    proxy_url: str | None = None
    market_slug: str | None = None
    binance_symbol: str = "BTCUSDT"
    binance_rest_url: str = "https://api.binance.com"
    binance_ws_url: str = "wss://stream.binance.com:9443/ws/btcusdt@trade"
    binance_signal_ws_url: str = "wss://stream.binance.com:9443/stream"
    binance_futures_rest_url: str = "https://fapi.binance.com"
    binance_futures_ws_url: str = "wss://fstream.binance.com/stream"
    coinbase_rest_url: str = "https://api.exchange.coinbase.com"
    coinbase_ws_url: str = "wss://ws-feed.exchange.coinbase.com"
    kraken_ws_url: str = "wss://ws.kraken.com/v2"
    gamma_url: str = "https://gamma-api.polymarket.com"
    clob_url: str = "https://clob.polymarket.com"
    clob_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    rtds_ws_url: str = "wss://ws-live-data.polymarket.com"
    rtds_stale_seconds: float = 10.0
    threshold_page_timeout_seconds: float = 4.0
    threshold_page_retry_seconds: float = 0.75
    poly_book_poll_ms: int = 200
    market_refresh_seconds: float = 0.5
    max_start_price_lag_ms: int = 2000
    market_slug_patterns: list[str] = Field(default_factory=lambda: ["bitcoin", "btc", "up-or-down", "updown"])
    observe_only_on_unverified_settlement: bool = True

    @field_validator("enabled_assets")
    @classmethod
    def valid_enabled_assets(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            asset = str(value).strip().upper()
            if asset not in {"BTC", "ETH"}:
                raise ValueError(f"unsupported five-minute asset: {asset}")
            if asset not in normalized:
                normalized.append(asset)
        if not normalized:
            raise ValueError("enabled_assets must contain at least one asset")
        return normalized

    @field_validator("poly_book_poll_ms", "market_refresh_seconds", "max_start_price_lag_ms", "threshold_page_timeout_seconds", "threshold_page_retry_seconds", "rtds_stale_seconds")
    @classmethod
    def positive_interval(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("interval values must be positive")
        return value


class StrategyConfig(BaseModel):
    min_entry_edge_usd: float = 10.0
    stop_edge_usd: float = 10.0
    min_buy_price: float = 0.10
    max_buy_price: float = 0.75
    take_profit_ticks: float = 0.10
    min_profit_after_slippage: float = 0.04
    min_seconds_to_entry: float = 10.0
    max_seconds_to_entry: float = 240.0
    force_exit_seconds: float = 5.0
    book_direction_exit_delay_seconds: float = 10.0
    reverse_entry_enabled: bool = False
    entry_confirmation_enabled: bool = True
    entry_confirmation_seconds: float = 1.0
    entry_confirmation_updates: int = 3
    taker_fee_rate: float = 0.07

    @field_validator("min_buy_price", "max_buy_price")
    @classmethod
    def valid_probability(cls, value: float) -> float:
        if not 0 < value < 1:
            raise ValueError("buy prices must be between 0 and 1")
        return value

    @model_validator(mode="after")
    def valid_buy_price_range(self) -> "StrategyConfig":
        if self.min_buy_price >= self.max_buy_price:
            raise ValueError("min_buy_price must be lower than max_buy_price")
        if self.min_seconds_to_entry > self.max_seconds_to_entry:
            raise ValueError("min_seconds_to_entry must not exceed max_seconds_to_entry")
        return self

    @field_validator("min_seconds_to_entry", "max_seconds_to_entry")
    @classmethod
    def valid_entry_window(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("entry window values must be positive")
        return value

    @field_validator("book_direction_exit_delay_seconds", "entry_confirmation_seconds")
    @classmethod
    def positive_book_direction_exit_delay(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("confirmation and exit delay values must be positive")
        return value

    @field_validator("entry_confirmation_updates")
    @classmethod
    def positive_entry_confirmation_updates(cls, value: int) -> int:
        if value < 1:
            raise ValueError("entry confirmation updates must be at least one")
        return value

    @field_validator("taker_fee_rate")
    @classmethod
    def valid_taker_fee_rate(cls, value: float) -> float:
        if not 0 <= value < 1:
            raise ValueError("taker fee rate must be between zero and one")
        return value


class RiskConfig(BaseModel):
    max_order_usd: float = 10.0
    max_market_usd: float = 30.0
    max_data_age_ms: int = 1000
    max_hold_seconds: float = 120.0
    max_loss_usd: float = 2.5
    max_trades_per_market: int = 1

    @field_validator("max_order_usd", "max_market_usd", "max_data_age_ms", "max_hold_seconds", "max_loss_usd")
    @classmethod
    def positive_value(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("risk values must be positive")
        return value

    @field_validator("max_trades_per_market")
    @classmethod
    def positive_trade_limit(cls, value: int) -> int:
        if value < 1:
            raise ValueError("max_trades_per_market must be at least one")
        return value


class PairMatchConfig(BaseModel):
    enabled: bool = False
    leg_quote_usd: float = 10.0
    min_spread_cents: float = 0.0
    second_order_min_spread_cents: float = 0.0
    min_leg_price_gap_cents: float = 0.0
    start_seconds_after_open: float = 20.0
    end_seconds_after_open: float = 280.0
    max_pairs_per_market: int = 1
    alternate_directions: bool = True
    alternation_mode: Literal[
        "per_market",
        "continuous_abab",
        "always_a",
        "always_b",
        "per_market_ab",
        "per_market_ba",
        "per_market_two_stage",
    ] = "per_market"

    @model_validator(mode="before")
    @classmethod
    def default_sequence_mode_pair_limit(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        mode = value.get("alternation_mode")
        if mode == "per_market_two_stage":
            return {
                **value,
                "alternate_directions": True,
                "max_pairs_per_market": 2,
            }
        if (
            mode in {"per_market_ab", "per_market_ba"}
            and "max_pairs_per_market" not in value
        ):
            return {**value, "max_pairs_per_market": 2}
        return value

    @field_validator("leg_quote_usd")
    @classmethod
    def positive_leg_quote(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("leg_quote_usd must be positive")
        return value

    @field_validator("min_spread_cents")
    @classmethod
    def valid_spread_cents(cls, value: float) -> float:
        if not -100 <= value <= 100:
            raise ValueError("min_spread_cents must be between -100 and 100")
        return value

    @field_validator("second_order_min_spread_cents")
    @classmethod
    def valid_second_order_spread_cents(cls, value: float) -> float:
        if not -100 <= value <= 100:
            raise ValueError("second_order_min_spread_cents must be between -100 and 100")
        return value

    @field_validator("min_leg_price_gap_cents")
    @classmethod
    def valid_leg_price_gap_cents(cls, value: float) -> float:
        if not 0 <= value <= 100:
            raise ValueError("min_leg_price_gap_cents must be between 0 and 100")
        return value

    @field_validator("start_seconds_after_open", "end_seconds_after_open")
    @classmethod
    def valid_market_second(cls, value: float) -> float:
        if not 0 <= value <= 300:
            raise ValueError("pair match market seconds must be between 0 and 300")
        return value

    @field_validator("max_pairs_per_market")
    @classmethod
    def positive_pair_limit(cls, value: int) -> int:
        if value < 1:
            raise ValueError("max_pairs_per_market must be at least one")
        return value

    @model_validator(mode="after")
    def valid_pair_window(self) -> "PairMatchConfig":
        if self.start_seconds_after_open >= self.end_seconds_after_open:
            raise ValueError("start_seconds_after_open must be lower than end_seconds_after_open")
        return self


class BtcRecoveryConfig(BaseModel):
    enabled: bool = False
    entry_price_cents: float = 70.0
    max_entry_price_cents: float = 100.0
    target_price_cents: float = 80.0
    recovery_target_price_cents: float = 80.0
    recovery_trigger_cents: float = 40.0
    stop_price_cents: float = 30.0
    initial_quantity: float = 5.0
    recovery_quantity: float = 15.0
    entry_seconds_after_open: float = 0.0
    exit_seconds_after_open: float = 300.0

    @field_validator(
        "entry_price_cents",
        "recovery_target_price_cents",
        "stop_price_cents",
    )
    @classmethod
    def valid_open_price_cents(cls, value: float) -> float:
        if not 0 < value < 100:
            raise ValueError("BTC recovery prices must be between 0 and 100 cents")
        return value

    @field_validator("max_entry_price_cents")
    @classmethod
    def valid_max_entry_price_cents(cls, value: float) -> float:
        if not 0 < value <= 100:
            raise ValueError(
                "BTC recovery maximum entry price must be above 0 and at most 100 cents"
            )
        return value

    @field_validator("target_price_cents")
    @classmethod
    def valid_initial_target_price_cents(cls, value: float) -> float:
        if not 0 < value <= 100:
            raise ValueError(
                "BTC recovery initial target must be above 0 and at most 100 cents"
            )
        return value

    @field_validator("recovery_trigger_cents")
    @classmethod
    def valid_recovery_trigger_cents(cls, value: float) -> float:
        if not 0 <= value < 100:
            raise ValueError(
                "BTC recovery trigger must be at least 0 and below 100 cents"
            )
        return value

    @field_validator("initial_quantity", "recovery_quantity")
    @classmethod
    def positive_quantity(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("BTC recovery quantities must be positive")
        return value

    @field_validator("entry_seconds_after_open", "exit_seconds_after_open")
    @classmethod
    def valid_market_second(cls, value: float) -> float:
        if not 0 <= value <= 300:
            raise ValueError("BTC recovery market seconds must be between 0 and 300")
        return value

    @model_validator(mode="after")
    def valid_strategy_shape(self) -> "BtcRecoveryConfig":
        if self.entry_seconds_after_open >= self.exit_seconds_after_open:
            raise ValueError(
                "BTC recovery entry_seconds_after_open must be lower than exit_seconds_after_open"
            )
        if self.max_entry_price_cents <= self.entry_price_cents:
            raise ValueError(
                "BTC recovery max_entry_price_cents must be above entry_price_cents"
            )
        return self


class BtcDynamicConfig(BaseModel):
    enabled: bool = False
    sizing_mode: Literal["quantity", "quote"] = "quantity"
    quantity: float = 10.0
    quote_amount_usd: float = 5.0
    entry_seconds_after_open: float = 270.0
    exit_seconds_after_open: float = 290.0
    min_net_edge_cents: float = 3.0
    slippage_reserve_cents: float = 1.35
    confirmation_seconds: float = 2.0
    confirmation_updates: int = 2
    loss_streak_limit: int = 5
    loss_cooldown_minutes: float = 30.0
    short_volatility_window_seconds: int = 10
    long_volatility_window_seconds: int = 60
    volatility_floor_bps: float = 0.5
    max_probability_correction_points: float = 10.0

    @field_validator("quantity", "quote_amount_usd", "loss_cooldown_minutes")
    @classmethod
    def positive_dynamic_order_size(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("BTC dynamic order size and cooldown must be positive")
        return value

    @field_validator("entry_seconds_after_open", "exit_seconds_after_open")
    @classmethod
    def valid_dynamic_market_second(cls, value: float) -> float:
        if not 0 <= value <= 300:
            raise ValueError("BTC dynamic market seconds must be between 0 and 300")
        return value

    @field_validator(
        "min_net_edge_cents",
        "slippage_reserve_cents",
        "confirmation_seconds",
        "volatility_floor_bps",
        "max_probability_correction_points",
    )
    @classmethod
    def non_negative_dynamic_value(cls, value: float) -> float:
        if value < 0:
            raise ValueError("BTC dynamic thresholds must not be negative")
        return value

    @field_validator(
        "confirmation_updates",
        "loss_streak_limit",
        "short_volatility_window_seconds",
        "long_volatility_window_seconds",
    )
    @classmethod
    def positive_dynamic_count(cls, value: int) -> int:
        if value < 1:
            raise ValueError("BTC dynamic counts must be at least one")
        return value

    @model_validator(mode="after")
    def valid_dynamic_shape(self) -> "BtcDynamicConfig":
        if self.entry_seconds_after_open >= self.exit_seconds_after_open:
            raise ValueError(
                "BTC dynamic entry_seconds_after_open must be lower than exit_seconds_after_open"
            )
        if self.short_volatility_window_seconds >= self.long_volatility_window_seconds:
            raise ValueError(
                "BTC dynamic short volatility window must be lower than long window"
            )
        if self.max_probability_correction_points > 100:
            raise ValueError("BTC dynamic probability correction must be at most 100 points")
        return self


class BtcWeightedEntrySegment(BaseModel):
    id: Literal["early", "middle", "late"]
    enabled: bool = True
    duration_seconds: int
    quote_amount_usd: float

    @field_validator("duration_seconds")
    @classmethod
    def positive_segment_duration(cls, value: int) -> int:
        if value < 1:
            raise ValueError("BTC weighted segment durations must be positive")
        return value

    @field_validator("quote_amount_usd")
    @classmethod
    def valid_segment_quote(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("BTC weighted segment quote amounts must be positive")
        rounded = round(value, 2)
        if abs(value - rounded) > 1e-9:
            raise ValueError("BTC weighted segment quote amounts support at most two decimals")
        return rounded


def default_btc_weighted_entry_segments() -> list[BtcWeightedEntrySegment]:
    return [
        BtcWeightedEntrySegment(id="early", enabled=True, duration_seconds=80, quote_amount_usd=1.0),
        BtcWeightedEntrySegment(id="middle", enabled=True, duration_seconds=140, quote_amount_usd=3.0),
        BtcWeightedEntrySegment(id="late", enabled=True, duration_seconds=80, quote_amount_usd=5.0),
    ]


class BtcWeightedConfig(BaseModel):
    enabled: bool = False
    reversal_sequence_enabled: bool = False
    entry_segments: list[BtcWeightedEntrySegment] = Field(
        default_factory=default_btc_weighted_entry_segments
    )
    entry_score_threshold: float = 70.0
    entry_lead_points: float = 8.0
    entry_confirmation_seconds: float = 2.0
    entry_confirmation_updates: int = 3
    entry_start_seconds_after_open: float = 10.0
    metric_warmup_seconds: float = 10.0
    metric_min_samples: int = 8
    min_entry_remaining_seconds: float = 10.0
    min_hold_seconds: float = 15.0
    exit_score_threshold: float = 70.0
    exit_lead_points: float = 10.0
    exit_confirmation_seconds: float = 3.0
    exit_confirmation_updates: int = 3
    score_exit_end_seconds_after_open: float = 60.0
    min_score_exit_price_cents: float = 25.0
    exit_intent_ttl_seconds: float = 3.0
    min_buy_price_cents: float = 15.0
    max_buy_price_cents: float = 90.0
    max_spread_cents: float = 5.0
    chainlink_max_age_seconds: float = 2.0
    book_max_age_seconds: float = 1.0
    max_one_tick_loss_usd: float = 0.50
    short_volatility_window_seconds: int = 10
    long_volatility_window_seconds: int = 60
    volatility_floor_bps: float = 0.5
    book_std_floor_cents: float = 0.5
    gap_std_floor_bps: float = 0.1
    sample_tolerance_seconds: float = 1.0
    weight_ema_seconds: float = 3.0
    score_history_seconds: int = 300
    risk_pause_enabled: bool = True
    rolling_loss_window_minutes: float = 1440.0
    rolling_loss_limit_usd: float = 25.0
    loss_streak_pause_count: int = 3
    loss_streak_pause_minutes: float = 30.0

    @field_validator(
        "entry_confirmation_seconds",
        "entry_start_seconds_after_open",
        "metric_warmup_seconds",
        "min_entry_remaining_seconds",
        "min_hold_seconds",
        "exit_confirmation_seconds",
        "score_exit_end_seconds_after_open",
        "exit_intent_ttl_seconds",
        "max_spread_cents",
        "chainlink_max_age_seconds",
        "book_max_age_seconds",
        "max_one_tick_loss_usd",
        "volatility_floor_bps",
        "book_std_floor_cents",
        "gap_std_floor_bps",
        "sample_tolerance_seconds",
        "weight_ema_seconds",
        "rolling_loss_window_minutes",
        "rolling_loss_limit_usd",
        "loss_streak_pause_minutes",
    )
    @classmethod
    def positive_weighted_value(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("BTC weighted values must be positive")
        return value

    @field_validator(
        "entry_score_threshold",
        "entry_lead_points",
        "exit_score_threshold",
        "exit_lead_points",
    )
    @classmethod
    def weighted_score_range(cls, value: float) -> float:
        if not 0 <= value <= 100:
            raise ValueError("BTC weighted score thresholds must be within 0-100")
        return value

    @field_validator(
        "entry_confirmation_updates",
        "metric_min_samples",
        "exit_confirmation_updates",
        "short_volatility_window_seconds",
        "long_volatility_window_seconds",
        "score_history_seconds",
        "loss_streak_pause_count",
    )
    @classmethod
    def positive_weighted_count(cls, value: int) -> int:
        if value < 1:
            raise ValueError("BTC weighted counts must be at least one")
        return value

    @model_validator(mode="after")
    def valid_weighted_shape(self) -> "BtcWeightedConfig":
        segment_ids = [segment.id for segment in self.entry_segments]
        if segment_ids != ["early", "middle", "late"]:
            raise ValueError("BTC weighted entry segments must be early, middle, late in order")
        if sum(segment.duration_seconds for segment in self.entry_segments) != 300:
            raise ValueError("BTC weighted entry segment durations must total 300 seconds")
        if not 0 <= self.min_buy_price_cents < self.max_buy_price_cents <= 100:
            raise ValueError("BTC weighted buy price range must be within 0-100 cents")
        if not 0 <= self.min_score_exit_price_cents <= 100:
            raise ValueError("BTC weighted minimum score exit price must be within 0-100 cents")
        if (
            max(self.entry_start_seconds_after_open, self.metric_warmup_seconds)
            + self.min_entry_remaining_seconds
            >= 300
        ):
            raise ValueError("BTC weighted entry time window must leave a positive interval")
        if self.min_entry_remaining_seconds > 300:
            raise ValueError("BTC weighted entry remaining time must be at most 300 seconds")
        if self.score_exit_end_seconds_after_open > 300:
            raise ValueError("BTC weighted score exit window must be at most 300 seconds")
        if self.metric_warmup_seconds > self.score_history_seconds:
            raise ValueError("BTC weighted metric warmup exceeds score history")
        if self.short_volatility_window_seconds >= self.long_volatility_window_seconds:
            raise ValueError("BTC weighted short volatility window must be below long window")
        if self.long_volatility_window_seconds > self.score_history_seconds:
            raise ValueError("BTC weighted long volatility window exceeds score history")
        return self


class BtcLeadPredictionConfig(BaseModel):
    enabled: bool = False
    spot_sources: list[Literal["binance", "coinbase", "kraken"]] = Field(
        default_factory=lambda: ["binance", "coinbase", "kraken"]
    )
    futures_diagnostics_enabled: bool = True
    prediction_horizons_seconds: list[int] = Field(default_factory=lambda: [1, 3, 5, 8])
    primary_horizon_seconds: int = 5
    quote_amount_usd: float = 1.0
    buy_limit_buffer_cents: float = 0.0
    shock_threshold_bps: float = 1.5
    confirmation_seconds: float = 0.8
    confirmation_updates: int = 3
    min_healthy_sources: int = 2
    max_source_dispersion_bps: float = 2.0
    min_expected_reprice_cents: float = 6.0
    min_p95_net_edge_cents: float = 2.0
    min_buy_price_cents: float = 40.0
    max_buy_price_cents: float = 85.0
    max_spread_cents: float = 2.0
    chainlink_max_age_seconds: float = 1.0
    external_max_age_seconds: float = 0.5
    book_max_age_seconds: float = 0.5
    max_hold_seconds: float = 8.0
    exit_liquidity_retry_seconds: float = 3.0
    target_net_profit_cents: float = 3.0
    adverse_move_cents: float = 4.0
    cooldown_seconds: float = 5.0
    max_trades_per_market: int = 3
    stop_entry_remaining_seconds: float = 30.0
    force_exit_remaining_seconds: float = 20.0
    source_basis_window_seconds: float = 60.0
    calibration_window_minutes: float = 60.0
    calibration_min_samples: int = 30
    calibration_update_seconds: float = 5.0
    velocity_half_life_seconds: float = 2.0
    risk_pause_enabled: bool = True
    loss_streak_pause_count: int = 3
    loss_streak_pause_minutes: float = 30.0
    daily_p95_loss_limit_usd: float = 10.0

    @field_validator(
        "quote_amount_usd",
        "shock_threshold_bps",
        "confirmation_seconds",
        "max_source_dispersion_bps",
        "min_expected_reprice_cents",
        "min_p95_net_edge_cents",
        "max_spread_cents",
        "chainlink_max_age_seconds",
        "external_max_age_seconds",
        "book_max_age_seconds",
        "max_hold_seconds",
        "exit_liquidity_retry_seconds",
        "target_net_profit_cents",
        "adverse_move_cents",
        "cooldown_seconds",
        "stop_entry_remaining_seconds",
        "force_exit_remaining_seconds",
        "source_basis_window_seconds",
        "calibration_window_minutes",
        "calibration_update_seconds",
        "velocity_half_life_seconds",
        "loss_streak_pause_minutes",
        "daily_p95_loss_limit_usd",
    )
    @classmethod
    def positive_lead_value(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("BTC lead prediction values must be positive")
        return value

    @field_validator("buy_limit_buffer_cents")
    @classmethod
    def nonnegative_buy_limit_buffer(cls, value: float) -> float:
        if not 0 <= value <= 10:
            raise ValueError("BTC lead buy limit buffer must be within 0-10 cents")
        return value

    @field_validator(
        "confirmation_updates",
        "min_healthy_sources",
        "max_trades_per_market",
        "calibration_min_samples",
        "loss_streak_pause_count",
    )
    @classmethod
    def positive_lead_count(cls, value: int) -> int:
        if value < 1:
            raise ValueError("BTC lead prediction counts must be at least one")
        return value

    @field_validator("spot_sources")
    @classmethod
    def unique_lead_sources(cls, values: list[str]) -> list[str]:
        result = list(dict.fromkeys(values))
        if not result:
            raise ValueError("BTC lead prediction requires at least one spot source")
        return result

    @field_validator("prediction_horizons_seconds")
    @classmethod
    def valid_lead_horizons(cls, values: list[int]) -> list[int]:
        result = sorted(set(values))
        if not result or any(value < 1 for value in result):
            raise ValueError("BTC lead prediction horizons must be positive")
        return result

    @model_validator(mode="after")
    def valid_lead_shape(self) -> "BtcLeadPredictionConfig":
        if self.primary_horizon_seconds not in self.prediction_horizons_seconds:
            raise ValueError("BTC lead primary horizon must be in prediction horizons")
        if 3 not in self.prediction_horizons_seconds or 5 not in self.prediction_horizons_seconds:
            raise ValueError("BTC lead prediction horizons must include 3 and 5 seconds")
        if self.min_healthy_sources > len(self.spot_sources):
            raise ValueError("BTC lead healthy-source minimum exceeds configured sources")
        if not 0 <= self.min_buy_price_cents < self.max_buy_price_cents <= 100:
            raise ValueError("BTC lead buy price range must be within 0-100 cents")
        if self.stop_entry_remaining_seconds > 300:
            raise ValueError("BTC lead stop-entry time must be at most 300 seconds")
        if self.force_exit_remaining_seconds >= self.stop_entry_remaining_seconds:
            raise ValueError("BTC lead force-exit time must be below stop-entry time")
        return self


class RealTradingConfig(BaseModel):
    enabled: bool = False
    order_quantity: float = 1.0
    max_order_notional_usd: float = 5.0
    daily_loss_limit_usd: float = 10.0
    max_orders_per_day: int = 10
    auto_redeem: bool = True
    shadow_required_signals: int = 20

    @field_validator(
        "order_quantity",
        "max_order_notional_usd",
        "daily_loss_limit_usd",
    )
    @classmethod
    def positive_real_trading_value(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("real trading quantity and risk limits must be positive")
        return value

    @field_validator("max_orders_per_day", "shadow_required_signals")
    @classmethod
    def positive_real_trading_count(cls, value: int) -> int:
        if value < 1:
            raise ValueError("real trading counts must be at least one")
        return value


class BtcV8Config(BaseModel):
    enabled: bool = False
    quote_amount_usd: float = 5.0
    slippage_reserve_cents: float = 0.5
    exit_fee_reserve_fraction: float = 0.0
    min_direction_signal_bps: float = 0.03
    min_signal_sigma: float = 0.05
    min_probability_move_points: float = 0.2
    min_target_direction_probability: float = 0.40
    min_supporting_sources: int = 1
    min_buy_price_cents: float = 15.0
    max_buy_price_cents: float = 90.0
    min_effective_edge_cents: float = 0.5
    low_price_penalty_start_cents: float = 45.0
    low_price_penalty_weight: float = 1.5
    buy_score_price_weight: float = 1.0
    buy_score_edge_weight: float = 0.30
    max_one_tick_loss_usd: float = 0.50
    buy_confirmation_seconds: float = 1.0
    buy_confirmation_updates: int = 2
    sell_confirmation_seconds: float = 0.50
    sell_confirmation_updates: int = 1
    reversal_confirmation_seconds: float = 10.0
    reversal_confirmation_updates: int = 2
    reentry_cooldown_seconds: float = 3.0
    max_entries_per_market: int = 2
    min_entry_remaining_seconds: float = 60.0
    min_hold_seconds: float = 30.0
    max_hold_seconds: float = 90.0
    min_profit_usd: float = 0.05
    hard_stop_loss_usd: float = 1.50
    emergency_stop_loss_usd: float = 2.00
    chase_take_profit_arm_usd: float = 0.25
    chase_take_profit_drawdown_usd: float = 0.15
    chase_take_profit_drawdown_fraction: float = 0.35
    polymarket_trend_window_seconds: float = 3.0
    polymarket_trend_min_span_seconds: float = 1.0
    chainlink_max_age_seconds: float = 5.0
    signal_retention_seconds: float = 65.0
    direction_short_seconds: int = 10
    direction_long_seconds: int = 30
    direction_average_window_seconds: float = 30.0
    direction_average_min_span_seconds: float = 20.0
    direction_average_min_samples: int = 15
    direction_average_max_sample_gap_seconds: float = 2.5
    basis_window_seconds: float = 300.0
    basis_exclusion_seconds: float = 10.0
    basis_min_span_seconds: float = 60.0
    basis_min_samples: int = 30
    basis_max_pair_age_seconds: float = 1.0
    basis_mad_multiplier: float = 6.0
    basis_min_clip_bps: float = 2.0
    basis_max_clip_bps: float = 10.0
    evaluation_interval_ms: int = 250
    spot_exchanges: list[Literal["binance", "coinbase", "kraken"]] = Field(
        default_factory=lambda: ["binance", "coinbase", "kraken"]
    )
    min_fresh_spot_exchanges: int = 1
    spot_stale_seconds: float = 2.0
    short_volatility_window_seconds: int = 10
    long_volatility_window_seconds: int = 60
    volatility_floor_bps: float = 0.5

    @field_validator(
        "quote_amount_usd",
        "buy_confirmation_seconds",
        "sell_confirmation_seconds",
        "reversal_confirmation_seconds",
        "reentry_cooldown_seconds",
        "min_entry_remaining_seconds",
        "min_hold_seconds",
        "max_hold_seconds",
        "hard_stop_loss_usd",
        "emergency_stop_loss_usd",
        "chase_take_profit_arm_usd",
        "chase_take_profit_drawdown_usd",
        "polymarket_trend_window_seconds",
        "polymarket_trend_min_span_seconds",
        "chainlink_max_age_seconds",
        "signal_retention_seconds",
        "direction_average_window_seconds",
        "direction_average_min_span_seconds",
        "direction_average_max_sample_gap_seconds",
        "basis_window_seconds",
        "basis_min_span_seconds",
        "basis_max_pair_age_seconds",
        "max_one_tick_loss_usd",
        "spot_stale_seconds",
    )
    @classmethod
    def positive_v8_value(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("BTC V8 values must be positive")
        return value

    @field_validator(
        "slippage_reserve_cents",
        "min_direction_signal_bps",
        "min_signal_sigma",
        "min_probability_move_points",
        "min_effective_edge_cents",
        "low_price_penalty_weight",
        "buy_score_price_weight",
        "buy_score_edge_weight",
        "volatility_floor_bps",
        "min_profit_usd",
        "basis_exclusion_seconds",
        "basis_mad_multiplier",
        "basis_min_clip_bps",
        "basis_max_clip_bps",
    )
    @classmethod
    def non_negative_v8_value(cls, value: float) -> float:
        if value < 0:
            raise ValueError("BTC V8 thresholds must not be negative")
        return value

    @field_validator(
        "min_target_direction_probability",
        "chase_take_profit_drawdown_fraction",
    )
    @classmethod
    def valid_v8_probability(cls, value: float) -> float:
        if not 0 < value <= 1:
            raise ValueError("BTC V8 probabilities must be within (0, 1]")
        return value

    @field_validator("exit_fee_reserve_fraction")
    @classmethod
    def valid_v8_reserve_fraction(cls, value: float) -> float:
        if not 0 <= value <= 1:
            raise ValueError("BTC V8 exit fee reserve fraction must be within [0, 1]")
        return value

    @field_validator(
        "buy_confirmation_updates",
        "sell_confirmation_updates",
        "reversal_confirmation_updates",
        "max_entries_per_market",
        "min_supporting_sources",
        "min_fresh_spot_exchanges",
        "evaluation_interval_ms",
        "direction_short_seconds",
        "direction_long_seconds",
        "direction_average_min_samples",
        "basis_min_samples",
        "short_volatility_window_seconds",
        "long_volatility_window_seconds",
    )
    @classmethod
    def positive_v8_count(cls, value: int) -> int:
        if value < 1:
            raise ValueError("BTC V8 counts must be at least one")
        return value

    @field_validator("spot_exchanges")
    @classmethod
    def unique_v8_exchanges(cls, values: list[str]) -> list[str]:
        result = list(dict.fromkeys(values))
        if not result:
            raise ValueError("BTC V8 requires at least one spot exchange")
        return result

    @model_validator(mode="after")
    def valid_v8_shape(self) -> "BtcV8Config":
        if not 0 <= self.min_buy_price_cents < self.max_buy_price_cents <= 100:
            raise ValueError("BTC V8 buy price range must be within 0-100 cents")
        if not 0 <= self.low_price_penalty_start_cents <= 100:
            raise ValueError("BTC V8 low-price penalty start must be within 0-100 cents")
        if self.min_probability_move_points > 100:
            raise ValueError("BTC V8 probability move must be at most 100 points")
        if self.min_entry_remaining_seconds > 300:
            raise ValueError("BTC V8 entry remaining time must be at most 300 seconds")
        if self.min_hold_seconds > self.max_hold_seconds:
            raise ValueError("BTC V8 minimum hold must not exceed maximum hold")
        if self.polymarket_trend_min_span_seconds > self.polymarket_trend_window_seconds:
            raise ValueError("BTC V8 trend span must not exceed its window")
        if self.direction_short_seconds >= self.direction_long_seconds:
            raise ValueError("BTC V8 direction short window must be below long window")
        if self.direction_average_min_span_seconds > self.direction_average_window_seconds:
            raise ValueError("BTC V8 direction average span must not exceed its window")
        if self.basis_exclusion_seconds >= self.basis_window_seconds:
            raise ValueError("BTC V8 basis exclusion must be below its window")
        if self.basis_min_clip_bps > self.basis_max_clip_bps:
            raise ValueError("BTC V8 basis minimum clip must not exceed maximum clip")
        if self.short_volatility_window_seconds >= self.long_volatility_window_seconds:
            raise ValueError("BTC V8 short volatility window must be below long window")
        if self.min_fresh_spot_exchanges > len(self.spot_exchanges):
            raise ValueError("BTC V8 fresh exchange minimum exceeds configured exchanges")
        if self.min_supporting_sources > len(self.spot_exchanges):
            raise ValueError("BTC V8 supporting source minimum exceeds configured exchanges")
        return self

class OrderbookChaseConfig(BaseModel):
    """High-fidelity shadow execution settings; strategy decisions come from BTC V8."""

    enabled: bool = False
    latency_probe_interval_seconds: float = 2.0
    latency_window_minutes: float = 15.0
    latency_min_samples: int = 30
    latency_max_age_seconds: float = 5.0
    readiness_required_roundtrips: int = 200
    observed_survival_threshold: float = 0.90
    p95_survival_threshold: float = 0.80

    @field_validator(
        "latency_probe_interval_seconds",
        "latency_window_minutes",
        "latency_max_age_seconds",
    )
    @classmethod
    def positive_chase_latency_value(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("orderbook chase latency values must be positive")
        return value

    @field_validator(
        "latency_min_samples",
        "readiness_required_roundtrips",
    )
    @classmethod
    def positive_chase_count(cls, value: int) -> int:
        if value < 1:
            raise ValueError("orderbook chase counts must be at least one")
        return value

    @field_validator("observed_survival_threshold", "p95_survival_threshold")
    @classmethod
    def valid_chase_threshold(cls, value: float) -> float:
        if not 0 < value <= 1:
            raise ValueError("orderbook chase survival thresholds must be within (0, 1]")
        return value


class BtcMakerArbitrageConfig(BaseModel):
    """Conservative shadow-only UP/DOWN maker-arbitrage settings."""

    enabled: bool = False
    mode: Literal["SHADOW_ONLY"] = "SHADOW_ONLY"
    sizing_mode: Literal["quantity", "budget"] = "quantity"
    quantity_per_leg: float = 5.0
    pair_budget_usd: float = 5.0
    min_locked_profit_cents: float = 2.0
    maker_fee_reserve_cents: float = 0.0
    quote_ttl_seconds: float = 2.0
    reprice_cooldown_ms: int = 500
    max_unhedged_seconds: float = 5.0
    stop_new_quotes_remaining_seconds: float = 60.0
    emergency_exit_remaining_seconds: float = 30.0
    max_pairs_per_market: int = 3
    # CLOB updates are event-driven.  Allow enough time for the REST
    # reconciliation fallback to complete when a quiet WebSocket does not
    # publish a top-of-book change.
    book_max_age_seconds: float = 1.5
    trade_print_max_age_seconds: float = 1.0
    max_single_leg_loss_usd: float = 0.50
    daily_shadow_loss_limit_usd: float = 5.0

    @field_validator(
        "quantity_per_leg",
        "pair_budget_usd",
        "min_locked_profit_cents",
        "quote_ttl_seconds",
        "max_unhedged_seconds",
        "stop_new_quotes_remaining_seconds",
        "emergency_exit_remaining_seconds",
        "book_max_age_seconds",
        "trade_print_max_age_seconds",
        "max_single_leg_loss_usd",
        "daily_shadow_loss_limit_usd",
    )
    @classmethod
    def positive_maker_value(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("BTC maker arbitrage values must be positive")
        return value

    @field_validator("maker_fee_reserve_cents")
    @classmethod
    def nonnegative_maker_reserve(cls, value: float) -> float:
        if not 0 <= value < 100:
            raise ValueError("BTC maker fee reserve must be within [0, 100) cents")
        return value

    @field_validator("reprice_cooldown_ms", "max_pairs_per_market")
    @classmethod
    def positive_maker_count(cls, value: int) -> int:
        if value < 1:
            raise ValueError("BTC maker counts must be at least one")
        return value

    @model_validator(mode="after")
    def valid_maker_shape(self) -> "BtcMakerArbitrageConfig":
        if self.min_locked_profit_cents + self.maker_fee_reserve_cents >= 100:
            raise ValueError("BTC maker profit plus fee reserve must be below 100 cents")
        if self.stop_new_quotes_remaining_seconds > 300:
            raise ValueError("BTC maker stop-new-quotes time must be at most 300 seconds")
        if self.emergency_exit_remaining_seconds >= self.stop_new_quotes_remaining_seconds:
            raise ValueError("BTC maker emergency exit must be below stop-new-quotes time")
        return self

class AppConfig(BaseModel):
    data_dir: Path = Path("data")
    data_cleanup_enabled: bool = True
    data_retention_hours: float = 24.0
    data_cleanup_interval_seconds: float = 300.0
    sources: SourceConfig = Field(default_factory=SourceConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    pair_match: PairMatchConfig = Field(default_factory=PairMatchConfig)
    btc_recovery: BtcRecoveryConfig = Field(default_factory=BtcRecoveryConfig)
    btc_dynamic: BtcDynamicConfig = Field(default_factory=BtcDynamicConfig)
    btc_weighted: BtcWeightedConfig = Field(default_factory=BtcWeightedConfig)
    btc_lead_prediction: BtcLeadPredictionConfig = Field(
        default_factory=BtcLeadPredictionConfig
    )
    btc_v8: BtcV8Config = Field(default_factory=BtcV8Config)
    orderbook_chase: OrderbookChaseConfig = Field(default_factory=OrderbookChaseConfig)
    btc_maker_arbitrage: BtcMakerArbitrageConfig = Field(
        default_factory=BtcMakerArbitrageConfig
    )
    real_trading: RealTradingConfig = Field(default_factory=RealTradingConfig)

    @field_validator("data_retention_hours", "data_cleanup_interval_seconds")
    @classmethod
    def positive_data_cleanup_value(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("data cleanup values must be positive")
        return value


def load_config(path: str | Path | None) -> AppConfig:
    if path is None:
        return AppConfig()
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as fh:
        payload: dict[str, Any] = yaml.safe_load(fh) or {}
    return AppConfig.model_validate(payload)
