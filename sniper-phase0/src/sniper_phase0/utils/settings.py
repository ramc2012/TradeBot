from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class Paths(BaseModel):
    data_root: Path
    trade_log: Path
    underlying_ticks: Path
    book_snapshots: Path
    features_out: Path
    labels_out: Path
    model_out: Path
    reports_out: Path


class WalkForward(BaseModel):
    train_months: int
    validate_months: int
    test_months: int
    step_months: int
    start: str
    end: str
    purge_minutes: int = 90


class Costs(BaseModel):
    brokerage_per_order_inr: float
    exchange_txn_charge_bps: float
    sebi_charge_bps: float
    stt_bps_sell_side: float
    stamp_duty_bps_buy_side: float
    gst_on_brokerage_pct: float
    gst_on_exchange_pct: float
    slippage_bps_default: float
    slippage_bps_event_day: float


class Labeling(BaseModel):
    default_stop_pct: float
    default_target_pct: float
    max_hold_minutes: int


class DecisionGate(BaseModel):
    skip_accuracy_bottom_decile_min: float
    net_profit_factor_min_at_2x_slippage: float
    max_drawdown_pct_max: float


class Settings(BaseModel):
    paths: Paths
    instruments: list[str]
    walk_forward: WalkForward
    costs: Costs
    labeling: Labeling
    decision_gate: DecisionGate

    @classmethod
    def load(cls, path: str | Path = "configs/base.yaml") -> "Settings":
        with open(path) as f:
            raw = yaml.safe_load(f)
        return cls(**raw)


class FeatureToggles(BaseModel):
    market_profile: dict = Field(default_factory=dict)
    order_flow: dict = Field(default_factory=dict)
    context: dict = Field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path = "configs/features.yaml") -> "FeatureToggles":
        with open(path) as f:
            raw = yaml.safe_load(f)
        return cls(**raw)
