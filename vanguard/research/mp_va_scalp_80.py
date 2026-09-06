"""The 80% rule under a STRICTER setup definition, plus the power of the test.

mp_intraday flags opened_outside_py from the first bar's CLOSE, not its OPEN, and
the two agree only 67% of the time. If the 80% rule is real, the version keyed to
the literal OPEN -- price actually opening outside the prior value area -- should
look at least as good. This is a ROBUSTNESS CHECK ON A NEGATIVE, run after the
declared 20-rule set was scored; it is not a 21st candidate and no walk-forward
result is taken from it.

Also reported: what the far-edge target would have paid on a genuinely
path-aware exit, and how big an edge this sample could even have detected.
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd
import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research.mp_auction import dsn  # noqa: E402
from research.mp_intraday import load_intraday  # noqa: E402
from research.mp_va_scalp import BAR_HI, BAR_LO, COST, prep  # noqa: E402

start = date.today() - timedelta(days=int(5.4 * 365.25))
conn = psycopg2.connect(dsn())
try:
    raw = load_intraday(conn, ["BANKNIFTY"], start)
finally:
    conn.close()
f = prep(raw)
f = f[f["py_val"].notna()].reset_index(drop=True)

# side from the TRUE session open rather than the first bar's close
g = f.groupby(["underlying", "dt"], sort=False)
op = f[f["bar"] == 0][["underlying", "dt", "sess_open", "py_val", "py_vah"]].copy()
side_open = np.where(op["sess_open"] < op["py_val"], 1,
                     np.where(op["sess_open"] > op["py_vah"], -1, 0))
op = op[["underlying", "dt"]].assign(open_side_true=side_open)
f = f.merge(op, on=["underlying", "dt"], how="left")

print(f"sessions {f['dt'].nunique():,}")
print(f"opened outside prior value by the TRUE OPEN: "
      f"{(f[f['bar'] == 0]['open_side_true'] != 0).mean() * 100:.1f}%   "
      f"by the bar-0 CLOSE (what mp_intraday uses): "
      f"{f[f['bar'] == 0]['opened_outside_py'].mean() * 100:.1f}%\n")

elig = f["bar"].between(BAR_LO, BAR_HI)
for label, outflag in (("bar-0 close (declared rule)", f["opened_outside_py"]),
                       ("true open (stricter)", f["open_side_true"].ne(0))):
    cond = (outflag & f["in_py_value"] & f["prev_biv"]
            & f["open_side_true"].ne(0) & elig)
    if label.startswith("bar-0"):
        cond = (outflag & f["back_in_value"] & f["prev_biv"]
                & f["open_side"].ne(0) & elig)
        sd = f["open_side"]
    else:
        sd = f["open_side_true"]
    hits = f.loc[cond].groupby(["underlying", "dt"], sort=False).head(1)
    s = sd.loc[hits.index].values
    hi = hits["close"] * (1 + hits["mfe_eod"] / 100)
    lo = hits["close"] * (1 + hits["mae_eod"] / 100)
    trav = np.where(s > 0, hi >= hits["py_vah"], lo <= hits["py_val"])
    r = s * hits["r_eod"]
    sd_r = r.std(ddof=1)
    print(f"{label}")
    print(f"   setups {len(hits)}  ({len(hits) / f['dt'].nunique() * 100:.0f}% of sessions)"
          f"   TRAVERSE rate {trav.mean() * 100:.1f}%   (MP claims 80%)")
    print(f"   EOD {r.mean():+.3f}% gross / {r.mean() - COST:+.3f}% net   "
          f"t={r.mean() / (sd_r / np.sqrt(len(r))):+.2f}   win {(r > 0).mean() * 100:.0f}%")
    print(f"   sd {sd_r:.3f}%  -> smallest mean this n could show at t=2: "
          f"{2 * sd_r / np.sqrt(len(r)):.3f}%\n")

# how far does price get, unconditionally, versus the far edge it is meant to reach
m = f[elig & f["back_in_value"] & f["prev_biv"] & f["open_side"].ne(0)]
m = m.groupby(["underlying", "dt"], sort=False).head(1)
tgt = np.where(m["open_side"] > 0, m["py_vah"], m["py_val"])
need = np.abs(tgt / m["close"] - 1) * 100
got = np.where(m["open_side"] > 0, m["mfe_eod"], -m["mae_eod"])
print(f"distance to the far edge at entry: median {np.median(need):.3f}%   "
      f"favourable excursion actually achieved: median {np.median(got):.3f}%")
print(f"   share where the excursion covered the distance: "
      f"{np.mean(got >= need) * 100:.1f}%")
print(f"   adverse excursion first: median {m['mae_eod'].where(m['open_side'] > 0, -m['mfe_eod']).median():.3f}%")
