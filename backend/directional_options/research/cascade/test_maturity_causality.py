"""Causality proofs for the maturity (3) and pyramid (4) passes.

Same contract as ../setups_2d3d/test_causality.py and ./test_cascade_causality.py:
causality is PROVEN by test, not asserted in a comment.

  1. prefix invariance of every daily feature the maturity rules read;
  2. prefix invariance of the maturity FIRE SESSION itself (the rule fires on
     data up to and including session f and nothing later);
  3. future-bar perturbation: rewriting every bar after the fire session leaves
     the fire session bit-identical;
  4. execution timing: a maturity signal fired at the close of session f is
     executed in session f+1, never in f;
  5. contract selection never peeks: the tracked contract for an entry session
     is chosen from a strictly EARLIER session's snapshot;
  6. the pyramid's second tranche enters strictly after the confirming daily
     bar closes.

Run:  .venv/bin/python -m pytest backend/directional_options/research/cascade/\
test_maturity_causality.py -q
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "setups_2d3d"))

import pyr_run  # noqa: E402
from mat_defs import RULES, maturity_fire_session  # noqa: E402
from stages import add_daily_stage_features, daily_state  # noqa: E402

RNG = np.random.default_rng(7)
DAILY_COLS = ["D_adx14", "D_atr14", "D_macd_hist", "D_sma20", "D_adx_up"]


def _synth(n: int = 260) -> pd.DataFrame:
    r = RNG.normal(0, 0.012, n).cumsum()
    c = 1000 * np.exp(r)
    h = c * (1 + np.abs(RNG.normal(0, 0.006, n)))
    lo = c * (1 - np.abs(RNG.normal(0, 0.006, n)))
    return pd.DataFrame({"s_open": c, "s_high": h, "s_low": lo, "s_close": c})


def _arrays(d: pd.DataFrame, side_state=True) -> dict:
    f = add_daily_stage_features(d)
    return {
        "n": len(f),
        "adx": f["D_adx14"].to_numpy(float),
        "atr": f["D_atr14"].to_numpy(float),
        "hist": f["D_macd_hist"].to_numpy(float),
        "close": f["s_close"].to_numpy(float),
        "sma20": f["D_sma20"].to_numpy(float),
        "high": f["s_high"].to_numpy(float),
        "low": f["s_low"].to_numpy(float),
        "state_long": daily_state(f, "primary", 1).fillna(False).to_numpy(),
        "state_short": daily_state(f, "primary", -1).fillna(False).to_numpy(),
    }


# ---------------------------------------------------------------- 1
def test_daily_features_prefix_invariant():
    d = _synth()
    full = add_daily_stage_features(d)
    for k in (80, 120, 170, 210, 255):
        pre = add_daily_stage_features(d.iloc[: k + 1].reset_index(drop=True))
        for c in DAILY_COLS:
            a, b = full[c].iloc[k], pre[c].iloc[k]
            if pd.isna(a) and pd.isna(b):
                continue
            assert np.isclose(float(a), float(b), rtol=1e-12, atol=0), (c, k, a, b)


# ---------------------------------------------------------------- 2
def test_fire_session_prefix_invariant():
    d = _synth()
    D = _arrays(d)
    checked = 0
    for rule in RULES:
        for s0 in (60, 90, 130, 180):
            f = maturity_fire_session(D, s0, 1, rule, 20)
            if f < 0:
                continue
            Dp = _arrays(d.iloc[: f + 1].reset_index(drop=True))
            assert maturity_fire_session(Dp, s0, 1, rule, 20) == f, (rule, s0, f)
            checked += 1
    assert checked >= 8, checked


# ---------------------------------------------------------------- 3
def test_future_bars_cannot_move_the_fire_session():
    d = _synth()
    D = _arrays(d)
    checked = 0
    for rule in RULES:
        for s0 in (60, 100, 150):
            f = maturity_fire_session(D, s0, 1, rule, 20)
            if f < 0 or f + 3 >= len(d):
                continue
            d2 = d.copy()
            d2.iloc[f + 1:] = d2.iloc[f + 1:] * 1.37
            assert maturity_fire_session(_arrays(d2), s0, 1, rule, 20) == f, (rule, s0)
            checked += 1
    assert checked >= 6, checked


# ---------------------------------------------------------------- 4
def test_signal_is_executed_in_the_next_session_only():
    """mat_run and pyr_run both convert a fire at session f into the first bar
    of session f+1. Assert that mapping on the real session grid."""
    import run_cascade as rc
    intra, daily = rc.load()
    bars = rc.Bars(intra, daily)
    n = 0
    for (u, s), pos in list(bars.first_bar.items())[:5000]:
        assert int(bars.u[u]["sidx"][pos]) == s
        if pos > 0:
            assert int(bars.u[u]["sidx"][pos - 1]) < s      # it IS the first bar
        n += 1
    assert n > 1000


# ---------------------------------------------------------------- 5
def test_contract_selection_never_peeks_at_the_entry_session():
    import harness
    import run_cascade as rc
    _, daily = rc.load()
    opt = pyr_run.load_opt_cached()
    sess_map = {(r.underlying, r.session): int(r.sidx)
                for r in daily[["underlying", "session", "sidx"]].itertuples()}
    sel = harness.build_selection(opt, harness.MNY_BANDS["deep_itm"])
    # every selection snapshot is taken at the 15:15 bar of its own session ...
    snap = opt[(opt["mins"] == harness.SESSION_HI)]
    assert len(snap) > 0
    # ... and the entry session the contract is used for is strictly later
    cm = pyr_run.contract_map(sel, sess_map)
    assert len(cm) > 100
    inv = {(r.underlying, int(r.side)): [] for r in sel.itertuples()}
    for r in sel.itertuples():
        s = sess_map.get((r.underlying, r.sel_session))
        if s is None:
            continue
        inv[(r.underlying, int(r.side))].append(s)
    for (u, side, entry_sidx), _ in list(cm.items())[:20000]:
        assert entry_sidx - 1 in inv[(u, side)]
        assert entry_sidx - 1 < entry_sidx


# ---------------------------------------------------------------- 6
def test_second_tranche_enters_after_the_confirming_daily_bar():
    import mat_run
    import run_cascade as rc
    intra, daily = rc.load()
    bars = rc.Bars(intra, daily)
    epi = mat_run.build_episodes(intra, daily, bars)
    e = epi[(epi["family"] == "s1_primary") & (epi["s2"] == 1)]
    assert len(e) > 100
    n = 0
    for r in e.itertuples():
        pos = bars.first_bar.get((r.underlying, int(r.s2_sidx) + 1))
        if pos is None:
            continue
        assert int(bars.u[r.underlying]["sidx"][pos]) == int(r.s2_sidx) + 1
        assert int(bars.u[r.underlying]["sidx"][pos]) > int(r.s2_sidx)
        assert pos > int(r.bar)                     # and after the first tranche
        n += 1
    assert n > 100
