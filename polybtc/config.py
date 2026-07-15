from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class SourceConfig(BaseModel):
    proxy_url: str | None = "http://127.0.0.1:10808"
    market_slug: str | None = None
    binance_symbol: str = "BTCUSDT"
    binance_rest_url: str = "https://api.binance.com"
    binance_ws_url: str = "wss://stream.binance.com:9443/ws/btcusdt@trade"
    gamma_url: str = "https://gamma-api.polymarket.com"
    clob_url: str = "https://clob.polymarket.com"
    clob_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    rtds_ws_url: str = "wss://ws-live-data.polymarket.com"
    threshold_page_timeout_seconds: float = 4.0
    threshold_page_retry_seconds: float = 2.0
    poly_book_poll_ms: int = 200
    market_refresh_seconds: float = 0.5
    max_start_price_lag_ms: int = 2000
    market_slug_patterns: list[str] = Field(default_factory=lambda: ["bitcoin", "btc", "up-or-down", "updown"])
    observe_only_on_unverified_settlement: bool = True

    @field_validator("poly_book_poll_ms", "market_refresh_seconds", "max_start_price_lag_ms", "threshold_page_timeout_seconds", "threshold_page_retry_seconds")
    @classmethod
    def positive_interval(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("interval values must be positive")
        return value


class StrategyConfig(BaseModel):
    min_entry_edge_usd: float = 15.0
    stop_edge_usd: float = 15.0
    min_buy_price: float = 0.10
    max_buy_price: float = 0.75
    take_profit_ticks: float = 0.10
    min_profit_after_slippage: float = 0.04
    min_seconds_to_entry: float = 10.0
    max_seconds_to_entry: float = 240.0
    force_exit_seconds: float = 5.0

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


class RiskConfig(BaseModel):
    max_order_usd: float = 10.0
    max_market_usd: float = 30.0
    max_data_age_ms: int = 1000
    max_hold_seconds: float = 120.0

    @field_validator("max_order_usd", "max_market_usd", "max_data_age_ms", "max_hold_seconds")
    @classmethod
    def positive_value(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("risk values must be positive")
        return value


class AppConfig(BaseModel):
    data_dir: Path = Path("data")
    data_retention_hours: float = 24.0
    data_cleanup_interval_seconds: float = 300.0
    sources: SourceConfig = Field(default_factory=SourceConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)

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
