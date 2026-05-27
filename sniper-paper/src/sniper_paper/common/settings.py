from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class TradingHours(BaseModel):
    open: str
    close: str


class Instrument(BaseModel):
    name: str
    exchange: Literal["NSE", "BSE", "MCX"]
    near_month_symbol: str
    spot_symbol: str | None = None
    lot_size: int
    tick_size: float
    trading_hours_ist: TradingHours
    model_in_distribution: bool


class Risk(BaseModel):
    daily_loss_cap_inr: float
    max_open_positions_total: int
    max_open_positions_per_instrument: int
    consecutive_loss_kill_switch: int
    max_signals_per_day: int
    reject_signals_outside_trading_hours: bool
    allow_ood_paper_trades: bool


class SignalConfig(BaseModel):
    decision_cadence_seconds: int
    ev_threshold_R: float
    min_p_win: float
    min_liquidity_lots: int
    setup_families: list[str]


class Costs(BaseModel):
    brokerage_per_order_inr: float
    exchange_txn_charge_bps: dict[str, float]
    sebi_charge_bps: float
    stt_bps_sell_side: dict[str, float]
    stamp_duty_bps_buy_side: float
    gst_on_brokerage_pct: float
    gst_on_exchange_pct: float
    slippage_bps_default: float
    slippage_bps_event_day: float


class ModelConfig(BaseModel):
    artifact_dir: str
    active_model_pointer: str
    predict_features: list[str]


class FyersConfig(BaseModel):
    daily_auth_at_ist: str
    reconnect_backoff_seconds: list[int]
    websocket_lite_mode: bool


class DatabaseConfig(BaseModel):
    dsn_env: str
    dsn_default: str
    schema: str = Field(default="public")
    table_prefix: str = "paper_"


class RedisConfig(BaseModel):
    url_env: str
    url_default: str
    tick_channel: str


class Deployment(BaseModel):
    host: str
    api_port: int
    static_ip_registered_with_fyers: bool


class Settings(BaseModel):
    env: Literal["production", "replay", "local"]
    deployment: Deployment
    database: DatabaseConfig
    redis: RedisConfig
    fyers: FyersConfig
    instruments: list[Instrument]
    risk: Risk
    signal: SignalConfig
    costs: Costs
    model: ModelConfig

    @classmethod
    def load(cls, path: str | Path = "configs/paper.yaml") -> "Settings":
        with open(path) as f:
            raw = yaml.safe_load(f)
        return cls(**raw)

    def db_dsn(self) -> str:
        return os.environ.get(self.database.dsn_env, self.database.dsn_default)

    def redis_url(self) -> str:
        return os.environ.get(self.redis.url_env, self.redis.url_default)

    def instrument_by_name(self, name: str) -> Instrument:
        for inst in self.instruments:
            if inst.name == name:
                return inst
        raise KeyError(f"Unknown instrument {name!r}")

    def instrument_by_symbol(self, symbol: str) -> Instrument:
        for inst in self.instruments:
            if inst.near_month_symbol == symbol or inst.spot_symbol == symbol:
                return inst
        raise KeyError(f"Unknown symbol {symbol!r}")


class Secrets(BaseModel):
    fyers: dict
    database: dict | None = None
    redis: dict | None = None

    @classmethod
    def load(cls, path: str | Path = "configs/secrets.yaml") -> "Secrets":
        with open(path) as f:
            raw = yaml.safe_load(f)
        return cls(**raw)
