"""Black-76 dealer-positioning engine (GEX / DEX / gamma-flip / density / smile).

Pure, dependency-free port of the fyers-webapp options-analytics reference
(`reference/py/compute3.py` + `docs/ANALYTICS_METHODOLOGY.md`). Recomputes IV and
Greeks per strike from the raw chain (LTP/OI) under Black-76 with the **futures
forward** — implied from ATM put-call parity when no futures price is available —
and aggregates the US-style dealer-positioning metrics the platform's scalar GEX
lacked: a per-strike GEX profile (₹Cr / 1% move), Net GEX, the zero-gamma "gamma
flip" spot, gamma density, DEX, max pain, call/put walls, ATM IV and the IV smile.

This module is intentionally additive: the directional RL policy keeps consuming
the legacy `chain_analytics.fetch_chain_analytics` payload unchanged; this engine
feeds the UI panel only.

Conventions (match the methodology doc):
  F = forward (futures), K = strike, T = years to expiry, σ = IV, r = 0.065,
  S = spot, OI in units (contracts × lot). Dealers long calls (+), short puts (−).
    GEX_strike = (Γ_ce·OI_ce − Γ_pe·OI_pe) · S²·0.01 / 1e7   # ₹ Cr per 1% move
    DEX_strike = (Δ_ce·OI_ce + Δ_pe·OI_pe) · S / 1e7         # ₹ Cr delta-notional
    gamma density = Γ_ce·OI_ce + Γ_pe·OI_pe                  # unsigned concentration
"""
from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any, Optional

R_DEFAULT = 0.065
_YEAR_DAYS = 365.0
_IV_LO, _IV_HI, _IV_ITERS = 1e-4, 5.0, 80


# --------------------------------------------------------------------------- #
# Black-76 primitives (options on the forward)
# --------------------------------------------------------------------------- #
def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-x * x / 2.0) / math.sqrt(2.0 * math.pi)


def black76_price(F: float, K: float, T: float, sigma: float, r: float, cp: str) -> float:
    """Black-76 price. cp = 'C' or 'P'. Degenerate (σ≤0 or T≤0) → discounted intrinsic."""
    if sigma <= 0 or T <= 0:
        intrinsic = max(0.0, F - K) if cp == "C" else max(0.0, K - F)
        return math.exp(-r * T) * intrinsic
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    df = math.exp(-r * T)
    if cp == "C":
        return df * (F * _norm_cdf(d1) - K * _norm_cdf(d2))
    return df * (K * _norm_cdf(-d2) - F * _norm_cdf(-d1))


def implied_vol(price: float, F: float, K: float, T: float, r: float, cp: str) -> float:
    """Bisection IV. Returns NaN for deep-ITM (price ≤ intrinsic+ε) / expired."""
    intrinsic = math.exp(-r * T) * (max(0.0, F - K) if cp == "C" else max(0.0, K - F))
    if T <= 0 or price <= intrinsic + 1e-6:
        return float("nan")
    lo, hi = _IV_LO, _IV_HI
    for _ in range(_IV_ITERS):
        mid = (lo + hi) / 2.0
        if black76_price(F, K, T, mid, r, cp) > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2.0


def greeks(F: float, K: float, T: float, sigma: float, r: float, cp: str):
    """Return (delta, gamma, vega, theta). gamma is unrounded (used for GEX/flip)."""
    if sigma != sigma or sigma <= 0 or T <= 0:  # NaN or degenerate
        return (None, 0.0, 0.0, None)
    sqrtT = math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    df = math.exp(-r * T)
    delta = df * _norm_cdf(d1) if cp == "C" else -df * _norm_cdf(-d1)
    gamma = df * _norm_pdf(d1) / (F * sigma * sqrtT)
    vega = F * df * _norm_pdf(d1) * sqrtT / 100.0
    t1 = -F * df * _norm_pdf(d1) * sigma / (2.0 * sqrtT)
    if cp == "C":
        theta = (t1 - r * K * df * _norm_cdf(d2) + r * F * df * _norm_cdf(d1)) / 365.0
    else:
        theta = (t1 + r * K * df * _norm_cdf(-d2) - r * F * df * _norm_cdf(-d1)) / 365.0
    return (round(delta, 4), gamma, round(vega, 2), round(theta, 2))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def time_to_expiry_years(expiry: date, now: datetime) -> float:
    """Years to expiry, matching the selector convention (intraday fraction,
    floored at 0.25 calendar day so 0DTE never blows up the Greeks)."""
    days = (expiry - now.date()).days + (1.0 - now.hour / 24.0)
    days = max(days, 0.25)
    return max(days / _YEAR_DAYS, 1.0 / 3650.0)


def implied_forward(
    atm_call_ltp: Optional[float],
    atm_put_ltp: Optional[float],
    atm_strike: float,
    T: float,
    spot: float,
    r: float = R_DEFAULT,
) -> float:
    """Forward F from ATM put-call parity: F = K + e^(rT)·(C − P).

    The cached chain carries no futures price, so we imply the true forward
    (basis and all) from the ATM call/put. Falls back to the cost-of-carry
    forward S·e^(rT) when the ATM premia are missing.
    """
    if atm_call_ltp is not None and atm_put_ltp is not None:
        return atm_strike + math.exp(r * T) * (atm_call_ltp - atm_put_ltp)
    return spot * math.exp(r * T)


def chain_entries_by_strike(entries: list[dict]) -> dict[float, dict[str, Any]]:
    """Group a flat option-chain entry list into strike → {ce_*, pe_*}."""
    by_strike: dict[float, dict[str, Any]] = {}
    for e in entries:
        try:
            strike = float(e.get("strike"))
        except (TypeError, ValueError):
            continue
        otype = str(e.get("option_type") or "").upper()
        if otype not in ("CE", "PE"):
            continue
        row = by_strike.setdefault(strike, {})
        prefix = "ce" if otype == "CE" else "pe"
        row[f"{prefix}_ltp"] = _f(e.get("ltp"))
        row[f"{prefix}_oi"] = _f(e.get("oi")) or 0.0
        row[f"{prefix}_oich"] = _f(e.get("oi_change"))
    return by_strike


def _f(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Single-expiry analytics
# --------------------------------------------------------------------------- #
def compute_expiry_gex(
    chain_by_strike: dict[float, dict[str, Any]],
    spot: float,
    T: float,
    *,
    forward: Optional[float] = None,
    r: float = R_DEFAULT,
    totals: Optional[tuple[float, float]] = None,
    expiry_label: Optional[str] = None,
) -> dict[str, Any]:
    """Per-strike GEX profile + meta for one expiry.

    chain_by_strike: strike → {ce_ltp, ce_oi, pe_ltp, pe_oi[, ce_oich, pe_oich]}.
    forward: explicit F; if None, implied from ATM parity.
    totals: (Σ ce_oi, Σ pe_oi) full-chain totals for a true PCR; else summed here.
    Returns {meta: {...}, rows: [...]}.
    """
    strikes = sorted(k for k in chain_by_strike if chain_by_strike[k])
    if not strikes or spot <= 0 or T <= 0:
        return {"meta": _empty_meta(expiry_label, spot, T), "rows": []}

    # Forward: explicit or parity-implied off the strike nearest spot.
    if forward is None:
        atm_for_fwd = min(strikes, key=lambda k: abs(k - spot))
        a = chain_by_strike[atm_for_fwd]
        forward = implied_forward(a.get("ce_ltp"), a.get("pe_ltp"), atm_for_fwd, T, spot, r)
    F = float(forward)

    SC = spot * spot * 0.01 / 1e7  # ₹ Cr per 1% move scaling
    rows: list[dict[str, Any]] = []
    net_gex = net_dex = 0.0
    sum_ce_oi = sum_pe_oi = 0.0

    for K in strikes:
        c = chain_by_strike[K]
        cl, co = c.get("ce_ltp"), c.get("ce_oi") or 0.0
        pl, po = c.get("pe_ltp"), c.get("pe_oi") or 0.0
        sum_ce_oi += co
        sum_pe_oi += po

        ic = implied_vol(cl, F, K, T, r, "C") if cl is not None else float("nan")
        ip = implied_vol(pl, F, K, T, r, "P") if pl is not None else float("nan")
        cd, cg, cv, ct = greeks(F, K, T, ic, r, "C")
        pd, pg, pv, pt = greeks(F, K, T, ip, r, "P")

        gex = cg * co * SC - pg * po * SC
        dex = (cd or 0.0) * co * spot / 1e7 + (pd or 0.0) * po * spot / 1e7
        gdens = cg * co + pg * po
        net_gex += gex
        net_dex += dex

        rows.append({
            "strike": K,
            "ce_ltp": cl, "ce_oi": co, "ce_oich": c.get("ce_oich"),
            "ce_iv": None if ic != ic else round(ic * 100, 2),
            "ce_delta": cd, "ce_gamma": round(cg, 7), "ce_theta": ct,
            "pe_ltp": pl, "pe_oi": po, "pe_oich": c.get("pe_oich"),
            "pe_iv": None if ip != ip else round(ip * 100, 2),
            "pe_delta": pd, "pe_gamma": round(pg, 7), "pe_theta": pt,
            "gex": round(gex, 2), "dex": round(dex, 2), "gdens": round(gdens / 1e6, 3),
        })

    # Max pain / walls / ATM.
    def total_payout(E: float) -> float:
        return sum(
            (chain_by_strike[K].get("ce_oi") or 0.0) * max(0.0, E - K)
            + (chain_by_strike[K].get("pe_oi") or 0.0) * max(0.0, K - E)
            for K in strikes
        )

    max_pain = min(strikes, key=total_payout)
    call_wall = max(strikes, key=lambda K: chain_by_strike[K].get("ce_oi") or 0.0)
    put_wall = max(strikes, key=lambda K: chain_by_strike[K].get("pe_oi") or 0.0)
    atm = min(strikes, key=lambda K: abs(K - F))
    arow = next(x for x in rows if x["strike"] == atm)
    atm_iv = round(((arow["ce_iv"] or 0.0) + (arow["pe_iv"] or 0.0)) / 2.0, 2)

    tot_ce, tot_pe = totals if totals else (sum_ce_oi, sum_pe_oi)
    pcr = round(tot_pe / tot_ce, 3) if tot_ce else None

    meta = {
        "expiry": expiry_label,
        "days": round(T * _YEAR_DAYS, 2),
        "T": round(T, 6),
        "fp": round(F, 2),
        "spot": spot,
        "basis": round(F - spot, 2),
        "pcr": pcr,
        "atm": atm,
        "atm_iv": atm_iv,
        "max_pain": max_pain,
        "call_wall": call_wall,
        "put_wall": put_wall,
        "net_gex": round(net_gex, 2),
        "net_dex": round(net_dex, 2),
        "gamma_flip": gamma_flip(rows, chain_by_strike, T, r),
        "tot_ce_oi": tot_ce,
        "tot_pe_oi": tot_pe,
    }
    return {"meta": meta, "rows": rows}


def gamma_flip(
    rows: list[dict[str, Any]],
    chain_by_strike: dict[float, dict[str, Any]],
    T: float,
    r: float = R_DEFAULT,
    step: float = 10.0,
) -> Optional[float]:
    """Zero-gamma spot S*: scan S across the strike range, recomputing each
    strike's Γ at S (IV held per strike), and interpolate the first sign change
    of net dealer GEX(S). None if no crossing in range."""
    strikes = [x["strike"] for x in rows]
    if len(strikes) < 2:
        return None

    def gnet(Sx: float) -> float:
        g = 0.0
        for x in rows:
            K = x["strike"]
            ce_oi = chain_by_strike[K].get("ce_oi") or 0.0
            pe_oi = chain_by_strike[K].get("pe_oi") or 0.0
            ic = (x["ce_iv"] or 0.0) / 100.0
            ip = (x["pe_iv"] or 0.0) / 100.0
            if ic > 0:
                g += greeks(Sx, K, T, ic, r, "C")[1] * ce_oi * Sx * Sx * 0.01
            if ip > 0:
                g -= greeks(Sx, K, T, ip, r, "P")[1] * pe_oi * Sx * Sx * 0.01
        return g

    prev_s = strikes[0]
    prev_v = gnet(prev_s)
    s = strikes[0] + step
    while s <= strikes[-1]:
        v = gnet(s)
        if (prev_v < 0 <= v) or (prev_v > 0 >= v):
            return round(prev_s + (s - prev_s) * (0 - prev_v) / (v - prev_v)) if v != prev_v else s
        prev_s, prev_v = s, v
        s += step
    return None


def iv_smile(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Liquid-wing IV smile: OTM puts below spot-implied, OTM calls above.

    We expose both CE and PE IV per strike; the UI plots the OTM wing
    (PE for strikes ≤ ATM-ish, CE above). Kept simple — return all strikes
    with both IVs so the client can draw the smile/skew it wants."""
    return [
        {"strike": x["strike"], "ce_iv": x["ce_iv"], "pe_iv": x["pe_iv"]}
        for x in rows
    ]


def _empty_meta(expiry_label: Optional[str], spot: float, T: float) -> dict[str, Any]:
    return {
        "expiry": expiry_label, "days": round(T * _YEAR_DAYS, 2) if T else None,
        "T": round(T, 6) if T else None, "fp": None, "spot": spot, "basis": None,
        "pcr": None, "atm": None, "atm_iv": None, "max_pain": None,
        "call_wall": None, "put_wall": None, "net_gex": None, "net_dex": None,
        "gamma_flip": None, "tot_ce_oi": None, "tot_pe_oi": None,
    }


# --------------------------------------------------------------------------- #
# Term structure across expiries
# --------------------------------------------------------------------------- #
def compute_progression(
    series_by_strike: dict[float, dict[str, list]],
    times: list[str],
    underlying_px: list[float],
    T_by_bucket: list[float],
    *,
    r: float = R_DEFAULT,
) -> dict[str, Any]:
    """30-minute net-GEX / gamma-density / OI progression over a strike band.

    Mirrors the reference progression: per bucket t (underlying close S_t, T_t),
    recompute Black-76 IV+Γ from each strike's CE/PE close, then
      netGEX(t) = Σ_K [Γ_ce·OI_ce − Γ_pe·OI_pe]·S_t²·0.01/1e7
    plus per-strike gamma density (Γ_ce·OI_ce + Γ_pe·OI_pe) and OI series.

    series_by_strike: strike → {ce_close:[...], ce_oi:[...], pe_close:[...], pe_oi:[...]}
    (all lists aligned to `times`). Missing values may be None → treated as skip.
    Returns the matrices/series the heatmaps + progression chart consume.
    """
    strikes = sorted(series_by_strike)
    n = len(times)
    if not strikes or n == 0:
        return {"times": times, "strikes": [], "idx": underlying_px, "gdens": [],
                "netgex": [], "oi_call": [], "oi_put": [], "oi_change": [],
                "gex": [], "regime": [], "atm": None}

    gdens_mat: list[list[Optional[float]]] = []
    netgex_mat: list[list[Optional[float]]] = []
    oi_call_mat: list[list[Optional[float]]] = []
    oi_put_mat: list[list[Optional[float]]] = []
    for K in strikes:
        s = series_by_strike[K]
        g_row: list[Optional[float]] = []
        ng_row: list[Optional[float]] = []
        for j in range(n):
            S = _f(underlying_px[j]) if j < len(underlying_px) else None
            T = T_by_bucket[j] if j < len(T_by_bucket) else None
            cl = _f((s.get("ce_close") or [None] * n)[j])
            pl = _f((s.get("pe_close") or [None] * n)[j])
            co_raw = _f((s.get("ce_oi") or [None] * n)[j])
            po_raw = _f((s.get("pe_oi") or [None] * n)[j])
            co = co_raw or 0.0
            po = po_raw or 0.0
            if S is None or T is None or S <= 0 or T <= 0:
                g_row.append(None)
                ng_row.append(None)
                continue
            if cl is None and pl is None and co_raw is None and po_raw is None:
                # No data for this strike in this bucket — None, not a false
                # 0.0 (5 of 7 band strikes had no candles at all and the
                # heatmap painted them as zero gamma; 2026-06-10 audit).
                g_row.append(None)
                ng_row.append(None)
                continue
            ic = implied_vol(cl, S, K, T, r, "C") if cl is not None else float("nan")
            ip = implied_vol(pl, S, K, T, r, "P") if pl is not None else float("nan")
            gc = greeks(S, K, T, ic, r, "C")[1]
            gp = greeks(S, K, T, ip, r, "P")[1]
            g_row.append(round((gc * co + gp * po) / 1e6, 3))
            # Signed per-strike GEX (₹Cr per 1% move), same convention as the
            # snapshot rows[].gex (call gamma +, put gamma −) — the spec's
            # Net-GEX strike×time heatmap input; gdens above is unsigned.
            ng_row.append(round((gc * co - gp * po) * S * S * 0.01 / 1e7, 3))
        gdens_mat.append(g_row)
        netgex_mat.append(ng_row)
        oi_call_mat.append([_f((s.get("ce_oi") or [None] * n)[j]) for j in range(n)])
        oi_put_mat.append([_f((s.get("pe_oi") or [None] * n)[j]) for j in range(n)])

    gex_series: list[Optional[float]] = []
    for j in range(n):
        S = _f(underlying_px[j]) if j < len(underlying_px) else None
        T = T_by_bucket[j] if j < len(T_by_bucket) else None
        if S is None or T is None or S <= 0 or T <= 0:
            gex_series.append(None)
            continue
        SC = S * S * 0.01 / 1e7
        g = 0.0
        for K in strikes:
            s = series_by_strike[K]
            cl = _f((s.get("ce_close") or [None] * n)[j])
            pl = _f((s.get("pe_close") or [None] * n)[j])
            co = _f((s.get("ce_oi") or [None] * n)[j]) or 0.0
            po = _f((s.get("pe_oi") or [None] * n)[j]) or 0.0
            ic = implied_vol(cl, S, K, T, r, "C") if cl is not None else float("nan")
            ip = implied_vol(pl, S, K, T, r, "P") if pl is not None else float("nan")
            g += greeks(S, K, T, ic, r, "C")[1] * co * SC - greeks(S, K, T, ip, r, "P")[1] * po * SC
        gex_series.append(round(g, 2))

    # Bucket-over-bucket ΔOI across the band (spec: progression OI change).
    oi_totals: list[Optional[float]] = []
    for j in range(n):
        vals = [
            v
            for K in strikes
            for v in (oi_call_mat[strikes.index(K)][j], oi_put_mat[strikes.index(K)][j])
            if v is not None
        ]
        oi_totals.append(sum(vals) if vals else None)
    oi_change: list[Optional[float]] = [None]
    for j in range(1, n):
        cur, prev = oi_totals[j], oi_totals[j - 1]
        oi_change.append(round(cur - prev, 1) if cur is not None and prev is not None else None)

    regime = [("pos" if (g is not None and g >= 0) else ("neg" if g is not None else None)) for g in gex_series]
    last_spot = next((_f(s) for s in reversed(underlying_px) if _f(s)), None)
    atm = min(strikes, key=lambda K: abs(K - last_spot)) if last_spot else None
    return {
        "times": times, "strikes": strikes, "idx": underlying_px,
        "gdens": gdens_mat, "netgex": netgex_mat,
        "oi_call": oi_call_mat, "oi_put": oi_put_mat, "oi_change": oi_change,
        "gex": gex_series, "regime": regime, "atm": atm,
    }


def build_term_structure(per_expiry: list[dict[str, Any]]) -> dict[str, list]:
    """ATM IV / PCR / Net GEX / max pain / total OI across (ordered) expiries.
    `per_expiry` is a list of the `meta` dicts in expiry order."""
    return {
        "labels": [m.get("expiry") for m in per_expiry],
        "days": [m.get("days") for m in per_expiry],
        "pcr": [m.get("pcr") for m in per_expiry],
        "atm_iv": [m.get("atm_iv") for m in per_expiry],
        "net_gex": [m.get("net_gex") for m in per_expiry],
        "max_pain": [m.get("max_pain") for m in per_expiry],
        "tot_oi": [
            round(((m.get("tot_ce_oi") or 0) + (m.get("tot_pe_oi") or 0)) / 1e7, 2)
            for m in per_expiry
        ],
    }
