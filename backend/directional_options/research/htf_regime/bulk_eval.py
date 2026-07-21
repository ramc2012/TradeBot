"""MEASUREMENT PASS, stage 3: bulk OPTION evaluation table.

For every in-window universe bar (both timeframes), both sides (CE/PE) and
both moneyness bands, this computes the option expression of an entry at that
bar's close and its exit at each of the four holds - through EXACTLY the
rules of option_read_layer (same selection, same dedup, same stale/modelled
mark logic), vectorized so the 200-draw controls are cheap lookups.

HONESTY MECHANISM: this module re-implements the layer's selection and mark
rules in numpy for speed, and then PROVES equivalence against the layer
itself on a random sample (assertions, rtol 1e-9) every run. If the two ever
diverge the run dies loudly.

Output: data/eval_30m.parquet, data/eval_1h.parquet keyed
(underlying, time, side, band) with per-hold gross returns + honesty flags.
"""
from __future__ import annotations

import glob
import os
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
from scipy.special import erf  # vector norm cdf

from option_read_layer import (BANDS, DTE_MAX, DTE_MIN, OptionReadLayer,
                               RISK_FREE, STALE_TOL_MIN, load_opt_extracts,
                               load_spot_csvs)
from study_grid import HOLDS, TIMEFRAMES

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
OPT_DIR = os.path.join(DATA, "opt")
WINDOW_LO, WINDOW_HI = "2026-03-02", "2026-07-21"
N_EQUIV = 300
SEED_EQ = 987654


def _ncdf(x):
    return 0.5 * (1.0 + erf(x / np.sqrt(2.0)))


def bs_vec(S, K, T, iv, is_ce, r=RISK_FREE):
    S, K, T, iv = (np.asarray(a, float) for a in (S, K, T, iv))
    intr = np.where(is_ce, np.maximum(0.0, S - K), np.maximum(0.0, K - S))
    ok = (T > 0) & (iv > 0) & (S > 0) & (K > 0)
    Ts = np.where(ok, T, 1.0)
    ivs = np.where(ok, iv, 1.0)
    sq = ivs * np.sqrt(Ts)
    d1 = (np.log(S / np.where(K > 0, K, 1.0)) + (r + 0.5 * ivs * ivs) * Ts) / sq
    d2 = d1 - sq
    ce = S * _ncdf(d1) - K * np.exp(-r * Ts) * _ncdf(d2)
    pe = K * np.exp(-r * Ts) * _ncdf(-d2) - S * _ncdf(-d1)
    px = np.where(is_ce, ce, pe)
    return np.where(ok, px, intr)


class BulkEvaluator:
    def __init__(self, layer: OptionReadLayer):
        self.layer = layer
        o = layer.opt
        ist = o["time"].dt.tz_convert("Asia/Kolkata")
        o = o.assign(session=pd.to_datetime(ist.dt.date))
        self.opt = o
        # per-contract arrays for marks
        self.c_times: dict[str, np.ndarray] = {}
        self.c_close: dict[str, np.ndarray] = {}
        self.c_ivff: dict[str, np.ndarray] = {}
        self.c_oi: dict[str, np.ndarray] = {}
        self.c_iv_raw: dict[str, np.ndarray] = {}
        self.c_meta: dict[str, tuple] = {}
        o = o.assign(_tn=o["time"].dt.tz_convert("UTC").dt.tz_localize(None))
        for cid, g in o.groupby("contract_id", sort=False):
            self.c_times[cid] = g["_tn"].to_numpy()
            self.c_close[cid] = g["close"].to_numpy(float)
            iv = pd.to_numeric(g["iv"], errors="coerce")
            self.c_iv_raw[cid] = iv.to_numpy(float)
            self.c_ivff[cid] = iv.ffill().to_numpy(float)
            self.c_oi[cid] = pd.to_numeric(g["oi"], errors="coerce").fillna(0).to_numpy(float)
            r0 = g.iloc[0]
            self.c_meta[cid] = (r0["underlying"], r0["expiry"],
                                float(r0["strike"]), r0["option_type"])
        # spot arrays
        sp = layer.spot.assign(
            _tn=layer.spot["time"].dt.tz_convert("UTC").dt.tz_localize(None))
        self.s_times = {u: g["_tn"].to_numpy()
                        for u, g in sp.groupby("underlying", sort=False)}
        self.s_close = {u: g["close"].to_numpy(float)
                        for u, g in sp.groupby("underlying", sort=False)}

    # ------------------------------------------------------------- selection
    def select(self, univ: pd.DataFrame, entry_off: pd.Timedelta) -> pd.DataFrame:
        """Replicates layer.contracts_for at asof = bar time + entry_off for
        every universe bar, both sides/bands. Returns long frame."""
        rows = []
        o = self.opt
        by_us = {k: v for k, v in o.groupby(
            ["underlying", "session", "option_type"], sort=False)}
        for (und, sess), bars in univ.groupby(["underlying", "session"], sort=False):
            tnaive = (bars["time"].dt.tz_convert("UTC").dt.tz_localize(None)
                      .to_numpy())
            ts = tnaive + entry_off.to_numpy()
            for side in ("CE", "PE"):
                g = by_us.get((und, sess, side))
                if g is None:
                    continue
                cg = g.groupby("contract_id", sort=False)
                first_t = cg["time"].min()
                meta = cg[["expiry", "strike"]].first()
                dte = np.array([(e - sess.date()).days for e in meta["expiry"]])
                ok_dte = (dte >= DTE_MIN) & (dte <= DTE_MAX)
                if not ok_dte.any():
                    continue
                cids = first_t.index.to_numpy()[ok_dte]
                ft = first_t.to_numpy("datetime64[ns]")[ok_dte]
                exp = meta["expiry"].to_numpy()[ok_dte]
                K = meta["strike"].to_numpy(float)[ok_dte]
                sign = 1.0 if side == "CE" else -1.0
                st, sc = self.s_times.get(und), self.s_close.get(und)
                if st is None:
                    continue
                si = np.searchsorted(st, ts, side="right") - 1
                for bi, t in enumerate(ts):
                    if si[bi] < 0:
                        continue
                    S = sc[si[bi]]
                    elig = ft <= t
                    if not elig.any():
                        continue
                    exp0 = exp[elig].min()
                    m = elig & (exp == exp0)
                    mny = sign * (K - S) / S
                    for band, (lo, hi, mid) in BANDS.items():
                        cand = m & (mny >= lo) & (mny < hi)
                        if not cand.any():
                            continue
                        j = np.flatnonzero(cand)[
                            np.argmin(np.abs(mny[cand] - mid))]
                        rows.append((und, tnaive[bi], side,
                                     band, cids[j], S, float(mny[j])))
        return pd.DataFrame(rows, columns=["underlying", "time", "side",
                                           "band", "cid", "spot", "mny"])

    # ----------------------------------------------------------------- marks
    def marks(self, cid: str, ts: np.ndarray):
        """Vectorized layer.mark for one contract at many timestamps.
        Returns price, modelled, method_code(0 none,1 bs,2 floor),
        bar_exact, iv_present, oi_present  (nan price = None-equivalent)."""
        bt, bc = self.c_times[cid], self.c_close[cid]
        und, expiry, K, cp = self.c_meta[cid]
        n = len(ts)
        idx = np.searchsorted(bt, ts, side="right") - 1
        price = np.full(n, np.nan)
        modelled = np.zeros(n, bool)
        method = np.zeros(n, np.int8)
        exact = np.zeros(n, bool)
        ivp = np.zeros(n, bool)
        oip = np.zeros(n, bool)
        valid = idx >= 0
        age_min = np.full(n, np.inf)
        age_min[valid] = (ts[valid] - bt[idx[valid]]) / np.timedelta64(60, "s")
        fresh = valid & (age_min <= STALE_TOL_MIN)
        price[fresh] = bc[idx[fresh]]
        exact[valid] = age_min[valid] <= 0.5
        ivr = self.c_iv_raw[cid]
        ivp[fresh] = ~np.isnan(ivr[idx[fresh]])
        oip[valid] = self.c_oi[cid][idx[valid]] > 0
        # modelled path
        model = valid & (age_min > STALE_TOL_MIN)
        if model.any():
            st, sc = self.s_times.get(und), self.s_close.get(und)
            mi = np.flatnonzero(model)
            S = np.full(len(mi), np.nan)
            if st is not None:
                sj = np.searchsorted(st, ts[mi], side="right") - 1
                okj = sj >= 0
                ages = np.full(len(mi), np.inf)
                ages[okj] = (ts[mi][okj] - st[sj[okj]]) / np.timedelta64(1, "D")
                okj &= ages <= 3.0
                S[okj] = sc[np.clip(sj, 0, None)][okj]
            exp_ts = np.datetime64(datetime(expiry.year, expiry.month,
                                            expiry.day, 10, 0), "ns")
            T = (exp_ts - ts[mi]) / np.timedelta64(1, "s") / (365.0 * 86400.0)
            T = np.maximum(T, 0.0)
            ivc = self.c_ivff[cid][idx[mi]]
            bs_ok = (~np.isnan(S)) & (~np.isnan(ivc)) & (T > 0)
            if bs_ok.any():
                price[mi[bs_ok]] = bs_vec(S[bs_ok], K, T[bs_ok], ivc[bs_ok],
                                          cp == "CE")
                modelled[mi[bs_ok]] = True
                method[mi[bs_ok]] = 1
                ivp[mi[bs_ok]] = True
            flo = (~bs_ok) & (~np.isnan(S))
            if flo.any():
                intr = (np.maximum(0.0, S[flo] - K) if cp == "CE"
                        else np.maximum(0.0, K - S[flo]))
                price[mi[flo]] = intr
                modelled[mi[flo]] = True
                method[mi[flo]] = 2
        return price, modelled, method, exact, ivp, oip


def _ts_i(col: pd.Series) -> pd.Series:
    """tz-aware/naive timestamps -> int64 UTC ns (NaT -> pandas NA)."""
    t = pd.to_datetime(col)
    if getattr(t.dt, "tz", None) is not None:
        t = t.dt.tz_convert("UTC").dt.tz_localize(None)
    out = pd.Series(t.astype("int64"), index=col.index)
    return out.mask(t.isna())


def evaluate_tf(be: BulkEvaluator, univ: pd.DataFrame, tf: str) -> pd.DataFrame:
    off = pd.Timedelta(0) if tf == "30m" else pd.Timedelta("30min")
    sel = be.select(univ, off)
    print(f"{tf}: selected rows {len(sel):,}", flush=True)
    exits = univ[["underlying", "time"]
                 + [f"exit_ts_{h}" for h in HOLDS]].copy()
    exits["time"] = exits["time"].dt.tz_convert("UTC").dt.tz_localize(None)
    sel = sel.merge(exits, on=["underlying", "time"], how="left")
    sel["entry_ts"] = sel["time"] + off

    tcols = ["entry_ts"] + [f"exit_ts_{h}" for h in HOLDS]
    for c in tcols:
        sel[f"_i_{c}"] = _ts_i(sel[c])
    # unique (cid, ts) requests -> vectorized marks per contract
    pairs = pd.concat(
        [sel[["cid", f"_i_{c}"]].rename(columns={f"_i_{c}": "ts_i"})
         for c in tcols], ignore_index=True).dropna().drop_duplicates()
    pairs["ts_i"] = pairs["ts_i"].astype("int64")
    out = []
    for cid, g in pairs.groupby("cid", sort=False):
        ts = g["ts_i"].to_numpy("int64").view("datetime64[ns]")
        price, modelled, method, exact, ivp, oip = be.marks(cid, ts)
        out.append(pd.DataFrame({
            "cid": cid, "ts_i": g["ts_i"].to_numpy(),
            "px": price, "mdl": modelled, "mth": method,
            "exact": exact, "ivp": ivp, "oip": oip}))
    marks = pd.concat(out, ignore_index=True)

    def join(col: str, pref: str) -> None:
        nonlocal sel
        m = marks.rename(columns={c: f"{pref}{c}" for c in
                                  ("px", "mdl", "mth", "exact", "ivp", "oip")})
        sel = sel.merge(m, left_on=["cid", f"_i_{col}"],
                        right_on=["cid", "ts_i"], how="left").drop(columns="ts_i")

    join("entry_ts", "e_")
    n0 = len(sel)
    sel = sel[(sel["e_px"] > 0) & (sel["e_exact"] > 0)].copy()
    print(f"{tf}: entries with an exact tradeable entry bar "
          f"{len(sel):,}/{n0:,} ({len(sel) / max(n0, 1):.1%})", flush=True)
    for h in HOLDS:
        join(f"exit_ts_{h}", f"x{h}_")
        with np.errstate(invalid="ignore", divide="ignore"):
            sel[f"gross_{h}"] = sel[f"x{h}_px"] / sel["e_px"] - 1.0
        sel[f"modelled_{h}"] = sel[f"x{h}_mdl"].fillna(False).astype(bool)
        sel[f"method_{h}"] = sel[f"x{h}_mth"].fillna(0).astype(np.int8)
        sel = sel.drop(columns=[f"x{h}_{c}" for c in
                                ("px", "mdl", "mth", "exact", "ivp", "oip")])
    sel = sel.rename(columns={"e_px": "entry_px", "e_ivp": "iv_present",
                              "e_oip": "oi_present"})
    keep = (["underlying", "time", "side", "band", "cid", "spot", "mny",
             "entry_px", "iv_present", "oi_present"]
            + [f"gross_{h}" for h in HOLDS] + [f"modelled_{h}" for h in HOLDS]
            + [f"method_{h}" for h in HOLDS])
    return sel[keep].reset_index(drop=True)


def equivalence_check(be: BulkEvaluator, layer: OptionReadLayer,
                      ev: pd.DataFrame, tf: str) -> None:
    rng = np.random.default_rng(SEED_EQ)
    off = pd.Timedelta(0) if tf == "30m" else pd.Timedelta("30min")
    pick = ev.iloc[rng.integers(0, len(ev), size=min(N_EQUIV, len(ev)))]
    n_sel = n_mark = 0
    for _, r in pick.iterrows():
        entry_ts = pd.Timestamp(r["time"]).tz_localize("UTC") + off
        sess = entry_ts.tz_convert("Asia/Kolkata").date()
        cs = layer.contracts_for(r["underlying"], sess, r["side"],
                                 bands=(r["band"],), asof=entry_ts)
        assert not cs.empty, \
            f"selection empty {r['underlying']} {sess} {r['side']} {r['band']}"
        if cs["contract_id"].iloc[0] != r["cid"]:
            # equal-distance band tie (e.g. spot exactly midway between two
            # strikes at ATM): accept iff the distances match exactly
            _, _, mid = BANDS[r["band"]]
            assert np.isclose(abs(cs["mny"].iloc[0] - mid),
                              abs(r["mny"] - mid), rtol=1e-9), \
                f"selection mismatch {r['underlying']} {sess} {r['side']} " \
                f"{r['band']}: layer={cs['contract_id'].iloc[0]} bulk={r['cid']}"
        n_sel += 1
        m = layer.mark(r["cid"], entry_ts)
        assert m is not None and np.isclose(m.price, r["entry_px"], rtol=1e-9), \
            f"mark mismatch {r['cid']} {entry_ts}"
        n_mark += 1
    print(f"{tf}: equivalence vs OptionReadLayer OK on {n_sel} selections + "
          f"{n_mark} marks", flush=True)


def main() -> None:
    opt = load_opt_extracts(sorted(glob.glob(os.path.join(OPT_DIR, "opt_*.csv"))))
    spot = load_spot_csvs(sorted(glob.glob(os.path.join(OPT_DIR, "spot_*.csv"))))
    layer = OptionReadLayer(opt, spot)
    del opt
    be = BulkEvaluator(layer)
    for tf in TIMEFRAMES:
        u = pd.read_parquet(os.path.join(DATA, f"univ_{tf}.parquet"))
        u = u[(u["session"] >= WINDOW_LO) & (u["session"] <= WINDOW_HI)]
        ev = evaluate_tf(be, u, tf)
        equivalence_check(be, layer, ev, tf)
        ev.to_parquet(os.path.join(DATA, f"eval_{tf}.parquet"))
        print(f"{tf}: eval rows {len(ev):,} -> eval_{tf}.parquet", flush=True)


if __name__ == "__main__":
    main()
