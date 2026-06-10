"""Sniper SCORER — turn logged shadow predictions into a learning signal + a dashboard.

The shadow sidecar logs predictions to sniper_shadow.jsonl. This scorer reads them, joins each
matured (symbol, decision_time, horizon) against REALIZED price from TimescaleDB (ATR-normalized
the same way the training labels are), and computes per-horizon directional accuracy + magnitude
rank-IC + time-to-peak correlation. It writes:

  sniper_scored.jsonl    one row per scored (symbol, decision_time, horizon) — the "trades" ledger
  sniper_metrics.json    aggregates: per-horizon dir_acc / mag_ic / ttp_corr / n, per-symbol, and a
                         per-day directional-accuracy series (the model's learning curve)
  sniper_signals.json    the latest prediction per symbol (all horizons) — the live "signals"
  sniper_dashboard.html  a SELF-CONTAINED page (data embedded inline; no server needed) with a
                         Signals/Trades view and a Model—Learning view

Runs in the isolated sidecar image (asyncpg + nomad_sniper available); zero load on the prod
backend. Idempotent: re-scans the whole log each run and re-scores anything newly matured.

Run:  python sniper_scorer.py
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone

import asyncpg
import numpy as np
import pandas as pd

from nomad_sniper.utils.normalize import atr_reference

DSN = os.environ.get("SNIPER_DB_DSN", "postgresql://nomadcurie:nomadcurie@db:5432/nomadcurie")
SNIPER_DIR = os.environ.get("SNIPER_DIR", "/sniper")
LOG = os.path.join(SNIPER_DIR, "sniper_shadow.jsonl")
SCORED = os.path.join(SNIPER_DIR, "sniper_scored.jsonl")
METRICS = os.path.join(SNIPER_DIR, "sniper_metrics.json")
SIGNALS = os.path.join(SNIPER_DIR, "sniper_signals.json")
RETRAIN = os.path.join(SNIPER_DIR, "sniper_retrain_report.json")
HTML = os.path.join(SNIPER_DIR, "sniper_dashboard.html")
SYMBOLS = ("NIFTY", "BANKNIFTY", "SENSEX")

# horizon -> (kind, n). intraday n=minutes; swing n=trading sessions forward (eod = same session).
HORIZONS = {
    "30m": ("min", 30), "60m": ("min", 60), "90m": ("min", 90), "120m": ("min", 120),
    "eod": ("day", 0), "1d": ("day", 1), "2d": ("day", 2), "3d": ("day", 3),
    "1w": ("day", 5), "1M": ("day", 21),
}


# ---------------------------------------------------------------- data -------
def _bars_from_rows(rows) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
    df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_convert("Asia/Kolkata")
    df = df.set_index("time").sort_index()
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    mins = df.index.hour * 60 + df.index.minute  # RTH only (09:15–15:30 IST)
    df = df[(mins >= 9 * 60 + 15) & (mins <= 15 * 60 + 30)]
    df = df[~df.index.duplicated(keep="last")]  # DB can carry duplicate (time,underlying) rows
    # Drop garbage prints (e.g. a NIFTY close of 53,362 when spot is ~23,300): rows whose o/h/l/c
    # deviate >20% from the per-session median close. Else one bad print explodes realized magnitude.
    if not df.empty:
        med = df.groupby(df.index.date)["close"].transform("median")
        lo, hi = med * 0.8, med * 1.2
        keep = (df["close"].between(lo, hi) & df["high"].between(lo, hi)
                & df["low"].between(lo, hi) & df["open"].between(lo, hi))
        df = df[keep.values]
    return df[["open", "high", "low", "close", "volume"]].astype(float)


async def _load_all_bars(conn) -> dict[str, pd.DataFrame]:
    out = {}
    for s in SYMBOLS:
        rows = await conn.fetch(
            "select time,open,high,low,close,volume from underlying_spot_candles "
            "where underlying=$1 and interval='1minute' order by time", s)
        out[s] = _bars_from_rows(rows) if rows else pd.DataFrame()
    return out


# ------------------------------------------------------------- realized ------
def _window_end(bars: pd.DataFrame, t: pd.Timestamp, kind: str, n: int):
    """Return (window_end_timestamp, matured?) for a horizon starting at t."""
    sessions = sorted({d.date() for d in bars.index})
    t_date = t.date()
    if kind == "min":
        end = t + pd.Timedelta(minutes=n)
        # intraday horizon must stay within the SAME session
        same = bars[(bars.index > t) & (bars.index <= end) & (bars.index.map(lambda x: x.date() == t_date))]
        last_same = bars[bars.index.map(lambda x: x.date() == t_date)].index.max()
        matured = bool(pd.notna(last_same) and last_same >= end)
        return same, matured
    # day horizons
    if n == 0:  # eod = same-session close
        win = bars[(bars.index > t) & (bars.index.map(lambda x: x.date() == t_date))]
        matured = any(d > t_date for d in sessions) or _is_session_complete(bars, t_date)
        return win, matured
    future = [d for d in sessions if d > t_date]
    if len(future) < n:
        return pd.DataFrame(), False
    end_date = future[n - 1]
    win = bars[(bars.index > t) & (bars.index.map(lambda x: x.date() <= end_date))]
    matured = len(future) >= n
    return win, matured


def _is_session_complete(bars, d):
    day = bars[bars.index.map(lambda x: x.date() == d)]
    return len(day) > 0 and (day.index.max().hour * 60 + day.index.max().minute) >= 15 * 60 + 25


def _realized(bars: pd.DataFrame, t: pd.Timestamp, win: pd.DataFrame, atr: float):
    """Realized magnitude (max excursion / ATR), dominant direction, signed move, time-to-peak."""
    at = bars[bars.index <= t]
    if at.empty or win.empty or not atr or atr <= 0:
        return None
    ref = float(at["close"].iloc[-1])
    up_exc = (float(win["high"].max()) - ref) / atr
    dn_exc = (ref - float(win["low"].min())) / atr
    mag = max(up_exc, dn_exc)
    dom_dir = 1 if up_exc >= dn_exc else -1
    signed = (float(win["close"].iloc[-1]) - ref) / atr
    # time-to-peak: positional index of the extreme bar within the window, as a fraction. Use
    # numpy argmax on values (the DB index can carry duplicate timestamps, so label-based get_loc
    # would return a slice).
    vals = win["high"].to_numpy() if dom_dir == 1 else win["low"].to_numpy()
    pos = int(np.argmax(vals)) if dom_dir == 1 else int(np.argmin(vals))
    frac = (pos + 1) / len(win)
    return {"mag": mag, "dom_dir": dom_dir, "signed": signed, "ttp_frac": float(frac), "ref": ref}


# --------------------------------------------------------------- score -------
async def score():
    if not os.path.exists(LOG):
        print("no shadow log yet"); return {}
    preds = [json.loads(l) for l in open(LOG) if l.strip()]
    conn = await asyncpg.connect(DSN)
    try:
        bars_by = await _load_all_bars(conn)
    finally:
        await conn.close()
    # ATR reference per (symbol, decision date) — same normalizer the model trained with
    atr_cache: dict[tuple, float] = {}

    def atr_for(sym, t):
        key = (sym, t.date())
        if key not in atr_cache:
            b = bars_by.get(sym)
            atr_cache[key] = float(atr_reference(b, t.date())) if b is not None and not b.empty else 0.0
        return atr_cache[key]

    scored: list[dict] = []
    for r in preds:
        sym = r.get("symbol")
        b = bars_by.get(sym)
        if b is None or b.empty:
            continue
        t = pd.Timestamp(r["decision_time"])
        if t.tzinfo is None:
            t = t.tz_localize("Asia/Kolkata")
        atr = atr_for(sym, t)
        for tf, pred in (r.get("prediction") or {}).items():
            if tf not in HORIZONS:
                continue
            kind, n = HORIZONS[tf]
            win, matured = _window_end(b, t, kind, n)
            if not matured or win.empty:
                continue
            rz = _realized(b, t, win, atr)
            if rz is None:
                continue
            p_up = pred.get("p_up")
            pred_dir_up = (p_up is not None and p_up >= 0.5)
            dir_hit = int(pred_dir_up == (rz["dom_dir"] == 1))
            scored.append({
                "symbol": sym, "decision_time": r["decision_time"], "horizon": tf,
                "pred_signed": pred.get("signed_move"), "pred_p_up": p_up,
                "pred_mag": pred.get("magnitude"), "pred_ttp": pred.get("time_to_peak"),
                "real_signed": rz["signed"], "real_mag": rz["mag"], "real_dom_dir": rz["dom_dir"],
                "real_ttp_frac": rz["ttp_frac"], "dir_hit": dir_hit,
                "has_live_of": r.get("has_live_of", False),
            })
    with open(SCORED, "w") as f:
        for s in scored:
            f.write(json.dumps(s) + "\n")
    print(f"scored {len(scored)} matured predictions across {len(preds)} records")
    return _aggregate(preds, scored)


def _ic(x, y):
    x, y = np.asarray(x, float), np.asarray(y, float)
    m = np.isfinite(x) & np.isfinite(y)
    if m.sum() < 5 or np.std(x[m]) == 0 or np.std(y[m]) == 0:
        return None
    return float(pd.Series(x[m]).rank().corr(pd.Series(y[m]).rank()))


def _avg_ic(per_h: dict, key: str):
    """Evidence-weighted mean of the PER-HORIZON ICs — the honest 'overall' magnitude skill.

    A single IC pooled over all horizons is spuriously high: longer horizons have larger moves in
    both prediction and realization, so the cross-horizon rank-correlation mostly reflects horizon
    length, not forecasting skill. Averaging within-horizon ICs (weighted by each horizon's sample
    count) removes that artifact.
    """
    pairs = [(v[key], v["n"]) for v in per_h.values()
             if v.get(key) is not None and v.get("n")]
    den = sum(n for _, n in pairs)
    return float(sum(ic * n for ic, n in pairs) / den) if den else None


def _aggregate(preds, scored):
    sdf = pd.DataFrame(scored)
    per_h = {}
    for tf in HORIZONS:
        d = sdf[sdf["horizon"] == tf] if not sdf.empty else pd.DataFrame()
        if len(d):
            per_h[tf] = {
                "n": int(len(d)),
                "dir_acc": float(d["dir_hit"].mean()),
                "mag_ic": _ic(d["pred_mag"], d["real_mag"]),
                "signed_ic": _ic(d["pred_signed"], d["real_signed"]),
                "ttp_corr": _ic(d["pred_ttp"], d["real_ttp_frac"]),
            }
        else:
            per_h[tf] = {"n": 0, "dir_acc": None, "mag_ic": None, "signed_ic": None, "ttp_corr": None}
    per_sym = {}
    if not sdf.empty:
        for sym, d in sdf.groupby("symbol"):
            per_sym[sym] = {"n": int(len(d)), "dir_acc": float(d["dir_hit"].mean())}
    # learning curve: directional accuracy by decision DATE (does it improve as data grows?)
    curve = []
    if not sdf.empty:
        sdf["date"] = pd.to_datetime(sdf["decision_time"]).dt.date.astype(str)
        for dt, d in sdf.groupby("date"):
            curve.append({"date": dt, "n": int(len(d)), "dir_acc": float(d["dir_hit"].mean())})
    # Overall: dir_acc is a scale-free hit rate → pooling is valid. Magnitude/signed IC are
    # scale-dependent → use the per-horizon evidence-weighted average, NOT a pooled correlation
    # (which is inflated by the horizon-length effect; see _avg_ic).
    overall = ({"n": int(len(sdf)), "dir_acc": float(sdf["dir_hit"].mean()),
                "mag_ic": _avg_ic(per_h, "mag_ic"), "signed_ic": _avg_ic(per_h, "signed_ic"),
                "mag_ic_method": "per_horizon_n_weighted_mean"} if not sdf.empty
               else {"n": 0, "dir_acc": None, "mag_ic": None, "signed_ic": None})
    metrics = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": os.path.basename(os.environ.get("SNIPER_MODEL", "excursion_estimator_sensex.joblib")),
        "total_predictions": len(preds), "total_scored": int(len(sdf)),
        "overall": overall, "per_horizon": per_h, "per_symbol": per_sym, "learning_curve": curve,
    }
    with open(METRICS, "w") as f:
        json.dump(metrics, f, indent=2)
    # latest signals: the most-recently-LOGGED record per symbol (last in append-order file). Using
    # decision_time max would tie — every post-close run today stamps the same 15:30 decision_time.
    latest = {}
    for r in preds:
        latest[r.get("symbol")] = r
    with open(SIGNALS, "w") as f:
        json.dump({"generated_at": metrics["generated_at"], "latest": list(latest.values()),
                   "recent_scored": scored[-200:]}, f, indent=2)
    return metrics


# ----------------------------------------------------------- dashboard -------
def build_html(metrics, signals, retrain=None):
    data = json.dumps({"metrics": metrics, "signals": signals, "retrain": retrain}).replace("</", "<\\/")
    return _HTML_TEMPLATE.replace("__DATA__", data)


_HTML_TEMPLATE = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Nomad Sniper — Live Shadow</title><style>
:root{--bg:#0b0e14;--panel:#141a24;--line:#222c3a;--fg:#e6edf3;--mut:#8b98a8;--grn:#3fb950;--red:#f85149;--amb:#d29922;--acc:#388bfd}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{padding:18px 24px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:16px;flex-wrap:wrap}
h1{font-size:18px;margin:0;font-weight:650}.muted{color:var(--mut)}.pill{background:var(--panel);border:1px solid var(--line);border-radius:999px;padding:3px 10px;font-size:12px}
.tabs{display:flex;gap:6px;padding:12px 24px 0}.tab{padding:8px 16px;border:1px solid var(--line);border-bottom:none;border-radius:8px 8px 0 0;background:var(--panel);cursor:pointer;color:var(--mut)}
.tab.on{color:var(--fg);background:var(--bg);border-color:var(--acc)}.wrap{padding:18px 24px}
.grid{display:grid;gap:14px}.cards{grid-template-columns:repeat(auto-fill,minmax(150px,1fr))}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px}
.card .k{color:var(--mut);font-size:12px}.card .v{font-size:22px;font-weight:650;margin-top:4px}
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{padding:9px 12px;text-align:right;border-bottom:1px solid var(--line);font-variant-numeric:tabular-nums}
th:first-child,td:first-child{text-align:left}th{color:var(--mut);font-weight:550;font-size:12px;background:#10161f}
tr:last-child td{border-bottom:none}.up{color:var(--grn)}.dn{color:var(--red)}.flat{color:var(--mut)}
.bdg{padding:1px 7px;border-radius:6px;font-size:12px;font-weight:600}.hit{background:rgba(63,185,80,.15);color:var(--grn)}.miss{background:rgba(248,81,73,.15);color:var(--red)}
.bar{height:9px;background:#0d1219;border-radius:5px;overflow:hidden}.bar>i{display:block;height:100%}
.sec{margin:8px 0 4px;font-weight:600}.hidden{display:none}.foot{padding:14px 24px;color:var(--mut);font-size:12px;border-top:1px solid var(--line)}
svg{display:block}.lgnd{font-size:12px;color:var(--mut)}
</style></head><body>
<header><h1>🎯 Nomad Sniper <span class=muted style=font-weight:400>· live shadow</span></h1>
<span class=pill id=p_model></span><span class=pill id=p_pred></span><span class=pill id=p_scored></span><span class=pill id=p_time></span></header>
<div class=tabs><div class="tab on" data-t=sig>Signals / Trades</div><div class=tab data-t=mdl>Model — Learning</div></div>
<div class=wrap id=v_sig>
  <div class=sec>Latest signals <span class=muted>(most recent decision per symbol — paper/shadow, no orders)</span></div>
  <div id=latest></div>
  <div class=sec style=margin-top:18px>Scored trades <span class=muted>(matured predictions vs realized)</span></div>
  <div id=trades></div>
</div>
<div class="wrap hidden" id=v_mdl>
  <div class=sec>Learning / retrain status</div>
  <div class=card id=retrain></div>
  <div class=sec style=margin-top:18px>Directional accuracy by horizon <span class=muted>(0.50 = coin flip)</span></div>
  <div class="grid cards" id=mcards></div>
  <div class=sec style=margin-top:18px>Learning curve <span class=muted>— directional accuracy by day (grows as the shadow set accumulates)</span></div>
  <div class=card id=curve></div>
  <div class=sec style=margin-top:18px>Per-horizon detail</div><div id=mtable></div>
</div>
<div class=foot id=foot></div>
<script>
const D=__DATA__;const M=D.metrics,S=D.signals;
const HS=["30m","60m","90m","120m","eod","1d","2d","3d","1w","1M"];
const f=(x,d=2)=>x==null?"–":(+x).toFixed(d);const cls=v=>v==null?"flat":v>0?"up":v<0?"dn":"flat";
const arrow=v=>v==null?"":v>0?"▲":v<0?"▼":"·";
document.getElementById("p_model").textContent="model: "+(M.model||"?");
document.getElementById("p_pred").textContent=(M.total_predictions||0)+" predictions";
document.getElementById("p_scored").textContent=(M.total_scored||0)+" scored";
document.getElementById("p_time").textContent="updated "+new Date(M.generated_at).toLocaleString();
// ---- latest signals table ----
function bdg(on){return on?"<span class='bdg hit'>✓</span>":"<span class='bdg miss'>–</span>";}
function sigTable(latest){
  let h="<table><tr><th>Symbol</th><th>Decision (IST)</th><th>OF</th><th>Opt</th>"+HS.map(x=>"<th>"+x+"</th>").join("")+"</tr>";
  latest.sort((a,b)=>a.symbol<b.symbol?-1:1).forEach(r=>{
    h+="<tr><td><b>"+r.symbol+"</b></td><td class=muted>"+r.decision_time.replace("T"," ").slice(0,16)+"</td>"+
       "<td title='live order flow'>"+bdg(r.has_live_of)+"</td><td title='option chain'>"+bdg(r.has_options)+"</td>";
    HS.forEach(tf=>{const p=(r.prediction||{})[tf];if(!p){h+="<td>–</td>";return;}
      const sm=p.signed_move;h+="<td class="+cls(sm)+" title='p_up="+f(p.p_up)+" mag="+f(p.magnitude)+"'>"+arrow(sm)+" "+f(sm)+"</td>";});
    h+="</tr>";});
  return h+"</table>";
}
document.getElementById("latest").innerHTML=sigTable(S.latest||[]);
// ---- scored trades table ----
function tradeTable(rows){
  if(!rows||!rows.length)return "<div class=muted>No matured predictions yet — intraday horizons score within the day; 1d+ as sessions complete.</div>";
  rows=rows.slice().reverse().slice(0,120);
  let h="<table><tr><th>Symbol</th><th>Decision</th><th>Horizon</th><th>Pred signed</th><th>Real signed</th><th>Pred mag</th><th>Real mag</th><th>Dir</th></tr>";
  rows.forEach(r=>{h+="<tr><td><b>"+r.symbol+"</b></td><td class=muted>"+r.decision_time.replace("T"," ").slice(5,16)+"</td><td>"+r.horizon+"</td>"+
    "<td class="+cls(r.pred_signed)+">"+f(r.pred_signed)+"</td><td class="+cls(r.real_signed)+">"+f(r.real_signed)+"</td>"+
    "<td>"+f(r.pred_mag)+"</td><td>"+f(r.real_mag)+"</td><td><span class='bdg "+(r.dir_hit?"hit":"miss")+"'>"+(r.dir_hit?"✓":"✗")+"</span></td></tr>";});
  return h+"</table>";
}
document.getElementById("trades").innerHTML=tradeTable(S.recent_scored||[]);
// ---- model cards ----
function accColor(a){return a==null?"#8b98a8":a>=0.55?"#3fb950":a<=0.45?"#f85149":"#d29922";}
let mc="";HS.forEach(tf=>{const m=M.per_horizon[tf]||{};const a=m.dir_acc;
  mc+="<div class=card><div class=k>"+tf+" · n="+(m.n||0)+"</div><div class=v style=color:"+accColor(a)+">"+(a==null?"–":(a*100).toFixed(0)+"%")+"</div>"+
  "<div class=bar style=margin-top:8px><i style='width:"+((a||0)*100)+"%;background:"+accColor(a)+"'></i></div>"+
  "<div class=k style=margin-top:6px>mag IC "+f(m.mag_ic)+"</div></div>";});
document.getElementById("mcards").innerHTML=mc;
// ---- learning curve (inline SVG) ----
function curveSVG(c){
  if(!c||!c.length)return "<div class=muted>Accumulating — the curve appears once ≥1 day of predictions has matured.</div>";
  const W=720,H=180,pad=30;const xs=c.map((_,i)=>pad+i*(W-2*pad)/Math.max(1,c.length-1));
  const ys=c.map(p=>H-pad-(p.dir_acc)*(H-2*pad));
  let pts=xs.map((x,i)=>x+","+ys[i]).join(" ");
  let dots=c.map((p,i)=>"<circle cx="+xs[i]+" cy="+ys[i]+" r=3 fill="+accColor(p.dir_acc)+"><title>"+p.date+": "+(p.dir_acc*100).toFixed(0)+"% (n="+p.n+")</title></circle>").join("");
  const y50=H-pad-0.5*(H-2*pad);
  return "<svg width="+W+" height="+H+" viewBox='0 0 "+W+" "+H+"'>"+
    "<line x1="+pad+" y1="+y50+" x2="+(W-pad)+" y2="+y50+" stroke=#2a3647 stroke-dasharray=4></line>"+
    "<text x="+(W-pad)+" y="+(y50-4)+" fill=#8b98a8 font-size=11 text-anchor=end>0.50</text>"+
    "<polyline points='"+pts+"' fill=none stroke=#388bfd stroke-width=2></polyline>"+dots+
    "<text x="+pad+" y=14 fill=#8b98a8 font-size=11>directional accuracy / day</text></svg>";
}
document.getElementById("curve").innerHTML=curveSVG(M.learning_curve);
// ---- per-horizon detail table ----
let dt="<table><tr><th>Horizon</th><th>n</th><th>Dir acc</th><th>Mag IC</th><th>Signed IC</th><th>TTP corr</th></tr>";
HS.forEach(tf=>{const m=M.per_horizon[tf]||{};dt+="<tr><td>"+tf+"</td><td>"+(m.n||0)+"</td><td style=color:"+accColor(m.dir_acc)+">"+(m.dir_acc==null?"–":(m.dir_acc*100).toFixed(0)+"%")+"</td><td>"+f(m.mag_ic)+"</td><td>"+f(m.signed_ic)+"</td><td>"+f(m.ttp_corr)+"</td></tr>";});
document.getElementById("mtable").innerHTML=dt+"</table>";
// ---- retrain status ----
(function(){const R=D.retrain;const el=document.getElementById("retrain");if(!el)return;
  if(!R){el.innerHTML="<span class=muted>No retrain run yet — accumulating feature-bearing records.</span>";return;}
  const stcol={accumulating:"#d29922",held_incumbent:"#388bfd",promoted:"#3fb950",ready_but_thin_holdout:"#d29922"}[R.status]||"#8b98a8";
  let h="<div style='display:flex;gap:22px;flex-wrap:wrap;align-items:flex-start'>"+
    "<div><span class=k>status</span><div class=v style='color:"+stcol+";font-size:18px'>"+(R.status||"?")+"</div></div>"+
    "<div><span class=k>order-flow-bearing samples</span><div class=v style=font-size:18px>"+(R.n_of_bearing||0)+" / "+(R.target_of_samples||"?")+"</div></div>"+
    "<div><span class=k>labeled rows</span><div class=v style=font-size:18px>"+(R.n_labeled||0)+"</div></div>";
  if(R.candidate_mag_ic!=null)h+="<div><span class=k>candidate vs incumbent mag-IC</span><div class=v style=font-size:18px>"+f(R.candidate_mag_ic)+" vs "+f(R.incumbent_mag_ic)+"</div></div>";
  if(R.n_uof_features!=null)h+="<div><span class=k>u_of_ features in candidate</span><div class=v style=font-size:18px>"+R.n_uof_features+"</div></div>";
  h+="</div>";
  const pct=Math.min(100,100*(R.n_of_bearing||0)/(R.target_of_samples||1));
  h+="<div class=bar style=margin-top:12px><i style='width:"+pct+"%;background:"+stcol+"'></i></div>";
  if(R.note)h+="<div class=k style=margin-top:8px>"+R.note+"</div>";
  if(R.last_retrain)h+="<div class=k style=margin-top:4px>last promotion: "+new Date(R.last_retrain).toLocaleString()+"</div>";
  el.innerHTML=h;})();
document.getElementById("foot").innerHTML="Paper/shadow only — predictions are observations, no orders placed. "+
  "Magnitude &amp; time-to-peak are the validated heads; direction (~0.50 AUC historically) is the open problem these scores track. "+
  "OF = live order flow now feeds the prediction context; Opt = live ATM option chain feeds the o_* features. "+
  "The B2 (u_of_*) family logs forward for a retrain — the current model has the o_* slots but not yet u_of_*.";
// ---- tabs ----
document.querySelectorAll(".tab").forEach(t=>t.onclick=()=>{
  document.querySelectorAll(".tab").forEach(x=>x.classList.remove("on"));t.classList.add("on");
  document.getElementById("v_sig").classList.toggle("hidden",t.dataset.t!="sig");
  document.getElementById("v_mdl").classList.toggle("hidden",t.dataset.t!="mdl");});
</script></body></html>"""


async def main():
    metrics = await score()
    if not metrics:
        return 1
    signals = json.load(open(SIGNALS))
    retrain = json.load(open(RETRAIN)) if os.path.exists(RETRAIN) else None
    open(HTML, "w").write(build_html(metrics, signals, retrain))
    print(f"wrote {METRICS}, {SIGNALS}, {HTML}")
    print(f"overall: scored={metrics['total_scored']} dir_acc={metrics['overall'].get('dir_acc')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
