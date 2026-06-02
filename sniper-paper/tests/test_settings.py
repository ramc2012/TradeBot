from __future__ import annotations

from sniper_paper.common.settings import Settings


def test_paper_yaml_loads_and_has_three_instruments():
    s = Settings.load("configs/paper.yaml")
    names = [i.name for i in s.instruments]
    assert names == ["NIFTY", "SENSEX", "CRUDE"]


def test_only_nifty_is_in_distribution_v0():
    s = Settings.load("configs/paper.yaml")
    in_dist = {i.name for i in s.instruments if i.model_in_distribution}
    assert in_dist == {"NIFTY"}


def test_mcx_has_late_close():
    s = Settings.load("configs/paper.yaml")
    crude = s.instrument_by_name("CRUDE")
    assert crude.trading_hours_ist.close == "23:30"
    assert crude.exchange == "MCX"
