"""Guards the net-GEX fix in directional_options.chain_analytics.

The option-chain builder stores `gamma_exposure` as a PER-STRIKE dict
`{strike: sign·gamma·OI·spot}`. The old code did `_safe_float(dict)` which is
always None, so `gex_total` was null for every underlying (NIFTY/BANKNIFTY/
SENSEX) on the analytics panel AND as policy feature 30. Net GEX is the signed
sum across strikes.
"""
from __future__ import annotations

from directional_options.chain_analytics import _net_gex


def test_net_gex_sums_per_strike_dict_signed():
    # CE positive, PE negative (repo sign convention already baked in).
    gex = {"23400": 1000.0, "23450": -600.0, "23500": 250.5}
    assert _net_gex(gex) == 650.5


def test_net_gex_none_and_empty_are_none():
    assert _net_gex(None) is None
    assert _net_gex({}) is None


def test_net_gex_tolerates_nulls_in_dict():
    assert _net_gex({"a": None, "b": 5.0, "c": "x"}) == 5.0


def test_net_gex_scalar_passthrough():
    # Backward-compatible if a future builder emits a scalar.
    assert _net_gex(42.0) == 42.0
    assert _net_gex("17.5") == 17.5


def test_net_gex_sensex_small_but_nonzero():
    # SENSEX has thin OI → small gammas, but must still produce a number
    # (the bug made it None). Mirrors prod: ~32k net GEX.
    sensex_like = {str(73000 + 100 * i): (0.12 if i % 2 == 0 else -0.08) * 1000 for i in range(20)}
    out = _net_gex(sensex_like)
    assert out is not None and out != 0.0
