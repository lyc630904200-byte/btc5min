from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class SourceConfig(BaseModel):
    enabled_assets: list[str] = Field(default_factory=lambda: ["BTC", "ETH"])
    proxy_url: str | None = "http://127.0.0.1:10808"
    market_slug: str | None = None
    binance_symbol: str = "BTCUSDT"
    binance_rest_url: str = "https://api.binance.com"
    binance_ws_url: str = "wss://stream.binance.com:9443/ws/btcusdt@trade"
    gamma_url: str = "https://gamma-api.polymarket.com"
    clob_url: str = "https://clob.polymarket.com"
    clob_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    rtds_ws_url: str = "wss://ws-live-data.polymarket.com"
    rtds_stale_seconds: float = 10.0
    threshold_page_timeout_seconds: float = 4.0
    threshold_page_retry_seconds: float = 2.0
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

class AppConfig(BaseModel):
    data_dir: Path = Path("data")
    data_cleanup_enabled: bool = True
    data_retention_hours: float = 24.0
    data_cleanup_interval_seconds: float = 300.0
    sources: SourceConfig = Field(default_factory=SourceConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    pair_match: PairMatchConfig = Field(default_factory=PairMatchConfig)

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
