#!/usr/bin/env python3
"""Fundamentals screen for 10-year multibagger candidates.

Universe: Nifty 500 (large + Midcap 150 + Smallcap 250) plus Nifty Microcap 250,
i.e. the ~750 names of the Nifty Total Market ex-tail. Constituents come from
niftyindices.com (nsearchives.nseindia.com as fallback); fundamentals come from
the public screener.in company pages (consolidated, falling back to standalone).

Pipeline:
  1. fetch    — constituents + one screener.in page per symbol, cached to disk
  2. parse    — top ratios, annual P&L / balance sheet / cash flow / ratios,
                compounded-growth ranges and quarterly shareholding
  3. metrics  — 5y CAGRs computed from the annual series, ROCE level and floor,
                cash conversion, leverage, dilution, promoter trend, PEG
  4. gates    — hard quality filters (separate gate set for lenders)
  5. score    — percentile-ranked composite over the gated set

Usage:
  python3 tools/multibagger_screen.py fetch          # network: constituents + pages
  python3 tools/multibagger_screen.py screen         # offline: parse cache, score, report
  python3 tools/multibagger_screen.py all            # both

Fetching is polite (default 2s between requests, backoff on 429) and resumable:
pages already in the cache are not refetched unless --refresh is given.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data" / "fundamentals"
CACHE_DIR = DATA_DIR / "screener_cache"
UNIVERSE_FILE = DATA_DIR / "universe.csv"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Segment lists. Nifty 500 = Nifty 100 + Midcap 150 + Smallcap 250; Microcap 250
# sits outside it (ranks 501-750). The segment lists are fetched only to label rows.
INDEX_FILES: dict[str, str] = {
    "nifty500": "ind_nifty500list.csv",
    "midcap150": "ind_niftymidcap150list.csv",
    "smallcap250": "ind_niftysmallcap250list.csv",
    "microcap250": "ind_niftymicrocap250_list.csv",
}
INDEX_BASES = (
    "https://www.niftyindices.com/IndexConstituent/",
    "https://nsearchives.nseindia.com/content/indices/",
)
SEGMENT_ORDER = ("microcap250", "smallcap250", "midcap150", "nifty500")


# --------------------------------------------------------------------------- fetch


def _session():
    import requests

    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    return s


def _get(session, url: str, *, retries: int = 4, timeout: int = 30):
    delay = 5.0
    for attempt in range(retries):
        try:
            resp = session.get(url, timeout=timeout)
        except Exception:  # network blip
            if attempt == retries - 1:
                raise
            time.sleep(delay)
            delay *= 2
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            time.sleep(delay)
            delay *= 2
            continue
        return resp
    return resp


def fetch_universe(session) -> list[dict[str, str]]:
    members: dict[str, dict[str, str]] = {}
    for key in SEGMENT_ORDER[::-1]:  # nifty500 first so the broad list seeds the rows
        rows = None
        for base in INDEX_BASES:
            resp = _get(session, base + INDEX_FILES[key])
            if resp.status_code == 200 and "Symbol" in resp.text[:500]:
                rows = list(csv.DictReader(io.StringIO(resp.text)))
                break
        if rows is None:
            raise RuntimeError(f"could not download constituents for {key}")
        for r in rows:
            sym = (r.get("Symbol") or "").strip()
            if not sym:
                continue
            row = members.setdefault(
                sym,
                {
                    "symbol": sym,
                    "company": (r.get("Company Name") or "").strip(),
                    "industry": (r.get("Industry") or "").strip(),
                    "isin": (r.get("ISIN Code") or "").strip(),
                    "segment": "",
                },
            )
            if key != "nifty500":
                row["segment"] = key
    for row in members.values():
        if not row["segment"]:
            row["segment"] = "nifty100"  # in Nifty 500 but neither mid nor small
    out = sorted(members.values(), key=lambda r: r["symbol"])
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with UNIVERSE_FILE.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["symbol", "company", "industry", "isin", "segment"])
        w.writeheader()
        w.writerows(out)
    return out


def load_universe() -> list[dict[str, str]]:
    with UNIVERSE_FILE.open() as fh:
        return list(csv.DictReader(fh))


def _cache_path(symbol: str) -> Path:
    return CACHE_DIR / f"{re.sub(r'[^A-Za-z0-9_-]', '_', symbol)}.html"


def fetch_pages(session, symbols: list[str], *, pause: float, refresh: bool) -> dict[str, str]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    status: dict[str, str] = {}
    for i, sym in enumerate(symbols, 1):
        path = _cache_path(sym)
        if path.exists() and not refresh:
            status[sym] = "cached"
            continue
        html = None
        for suffix in ("consolidated/", ""):
            url = f"https://www.screener.in/company/{sym}/{suffix}"
            resp = _get(session, url)
            time.sleep(pause)
            if resp.status_code != 200:
                continue
            # A consolidated URL for a company without subsidiaries renders empty tables.
            if suffix and not _has_annual_data(resp.text):
                continue
            html = resp.text
            break
        if html is None:
            status[sym] = "missing"
        else:
            path.write_text(html)
            status[sym] = "fetched"
        if i % 25 == 0:
            done = sum(v != "missing" for v in status.values())
            print(f"  {i}/{len(symbols)} pages ({done} ok)", flush=True)
    return status


def _has_annual_data(html: str) -> bool:
    try:
        page = parse_page(html)
    except Exception:
        return False
    sales = page.pl.get("Sales") or page.pl.get("Revenue") or []
    return sum(v is not None for v in sales[-4:]) >= 2


# --------------------------------------------------------------------------- parse


_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def to_num(text: Any) -> Optional[float]:
    if text is None:
        return None
    s = str(text).replace(",", "").replace("\xa0", " ").strip()
    if not s or s in {"-", "--", "NA", "N/A"}:
        return None
    m = _NUM_RE.search(s)
    return float(m.group()) if m else None


def _label(cell) -> str:
    text = cell.get_text(" ", strip=True).replace("\xa0", " ")
    text = re.sub(r"\s*\+\s*$", "", text)
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class Page:
    ratios: dict[str, Any]
    pl_cols: list[str]
    pl: dict[str, list[Optional[float]]]
    bs_cols: list[str]
    bs: dict[str, list[Optional[float]]]
    cf_cols: list[str]
    cf: dict[str, list[Optional[float]]]
    ratio_cols: list[str]
    ratio_rows: dict[str, list[Optional[float]]]
    ranges: dict[str, dict[str, Optional[float]]]
    shp_cols: list[str]
    shp: dict[str, list[Optional[float]]]
    sector: str


def _section_table(soup, section_id: str, *, table_selector: str = "table.data-table"):
    section = soup.find(id=section_id)
    if section is None:
        return [], {}
    table = section.select_one(table_selector)
    if table is None:
        return [], {}
    head = table.find("thead")
    cols = [th.get_text(" ", strip=True) for th in (head.find_all("th") if head else [])][1:]
    rows: dict[str, list[Optional[float]]] = {}
    body = table.find("tbody") or table
    for tr in body.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 2:
            continue
        label = _label(cells[0])
        if label and label not in rows:
            rows[label] = [to_num(c.get_text()) for c in cells[1:]]
    return cols, rows


def parse_page(html: str) -> Page:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")

    ratios: dict[str, Any] = {}
    for li in soup.select("#top-ratios li"):
        name_el = li.select_one(".name")
        if name_el is None:
            continue
        name = name_el.get_text(" ", strip=True)
        nums = [to_num(n.get_text()) for n in li.select(".number")]
        nums = [n for n in nums if n is not None]
        if name == "High / Low":
            ratios["High"] = nums[0] if nums else None
            ratios["Low"] = nums[1] if len(nums) > 1 else None
        else:
            ratios[name] = nums[0] if nums else None

    pl_cols, pl = _section_table(soup, "profit-loss")
    bs_cols, bs = _section_table(soup, "balance-sheet")
    cf_cols, cf = _section_table(soup, "cash-flow")
    ratio_cols, ratio_rows = _section_table(soup, "ratios")

    ranges: dict[str, dict[str, Optional[float]]] = {}
    for tbl in soup.select("table.ranges-table"):
        th = tbl.find("th")
        if th is None:
            continue
        title = th.get_text(" ", strip=True)
        vals: dict[str, Optional[float]] = {}
        for tr in tbl.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) == 2:
                vals[tds[0].get_text(strip=True).rstrip(":")] = to_num(tds[1].get_text())
        ranges[title] = vals

    shp_cols, shp = _section_table(soup, "quarterly-shp")
    if not shp:
        shp_cols, shp = _section_table(soup, "shareholding")

    sector = ""
    for a in soup.select("a[title]"):
        if a.get("title") in {"Sector", "Industry", "Broad Sector"}:
            sector = a.get_text(" ", strip=True)
            if a.get("title") == "Industry":
                break

    return Page(ratios, pl_cols, pl, bs_cols, bs, cf_cols, cf, ratio_cols, ratio_rows,
                ranges, shp_cols, shp, sector)


# --------------------------------------------------------------------------- metrics


def _row(rows: dict[str, list], *names: str) -> list[Optional[float]]:
    for n in names:
        if n in rows:
            return rows[n]
    # tolerate minor label drift ("Net Profit" vs "Net profit", "ROCE %" vs "ROCE")
    lowered = {k.lower().rstrip(" %"): v for k, v in rows.items()}
    for n in names:
        v = lowered.get(n.lower().rstrip(" %"))
        if v is not None:
            return v
    return []


def _annual(cols: list[str], values: list[Optional[float]]) -> list[Optional[float]]:
    """Drop the TTM column; keep only fiscal-year columns."""
    if not values:
        return []
    keep = [i for i, c in enumerate(cols) if c.upper() != "TTM" and i < len(values)]
    if not keep:
        return list(values)
    return [values[i] for i in keep]


def _ttm(cols: list[str], values: list[Optional[float]]) -> Optional[float]:
    for i, c in enumerate(cols):
        if c.upper() == "TTM" and i < len(values):
            return values[i]
    return None


def cagr(series: list[Optional[float]], years: int) -> Optional[float]:
    """CAGR in % over the last `years` intervals. None if the base is not positive."""
    if len(series) < years + 1:
        return None
    start, end = series[-(years + 1)], series[-1]
    if start is None or end is None or start <= 0:
        return None
    if end <= 0:
        return -100.0
    return ((end / start) ** (1.0 / years) - 1.0) * 100.0


def _last(series: list[Optional[float]], n: int) -> list[float]:
    return [v for v in series[-n:] if v is not None]


def _mean(xs: list[float]) -> Optional[float]:
    return sum(xs) / len(xs) if xs else None


def compute_metrics(page: Page) -> dict[str, Any]:
    r = page.ratios
    is_fin = ("Revenue" in page.pl and "Sales" not in page.pl) or "Financing Profit" in page.pl

    sales_all = _row(page.pl, "Sales", "Revenue")
    sales = _annual(page.pl_cols, sales_all)
    np_all = _row(page.pl, "Net Profit", "Profit after tax")
    np_ = _annual(page.pl_cols, np_all)
    eps = _annual(page.pl_cols, _row(page.pl, "EPS in Rs", "EPS"))
    opm = _annual(page.pl_cols, _row(page.pl, "OPM %", "Financing Margin %"))
    payout = _annual(page.pl_cols, _row(page.pl, "Dividend Payout %"))

    equity = _row(page.bs, "Equity Capital")
    reserves = _row(page.bs, "Reserves")
    borrowings = _row(page.bs, "Borrowings", "Borrowing")
    cfo = _row(page.cf, "Cash from Operating Activity")
    roce = _annual(page.ratio_cols, _row(page.ratio_rows, "ROCE %"))
    roe_series = _annual(page.ratio_cols, _row(page.ratio_rows, "ROE %"))
    ccc = _annual(page.ratio_cols, _row(page.ratio_rows, "Cash Conversion Cycle"))

    net_worth = None
    if equity and reserves and equity[-1] is not None and reserves[-1] is not None:
        net_worth = equity[-1] + reserves[-1]
    de = None
    if net_worth and net_worth > 0 and borrowings and borrowings[-1] is not None:
        de = borrowings[-1] / net_worth

    cfo5, np5 = _last(cfo, 5), _last(np_, 5)
    cfo_pat = sum(cfo5) / sum(np5) if cfo5 and np5 and sum(np5) > 0 else None

    # Equity dilution: growth of share count proxied by profit / EPS.
    dilution = None
    if len(np_) >= 6 and len(eps) >= 6 and all(
        v not in (None, 0) for v in (np_[-1], np_[-6], eps[-1], eps[-6])
    ):
        dilution = ((np_[-1] / eps[-1]) / (np_[-6] / eps[-6]) - 1.0) * 100.0

    promoters = _row(page.shp, "Promoters")
    prom_vals = [v for v in promoters if v is not None]
    prom_latest = prom_vals[-1] if prom_vals else None
    prom_chg = prom_vals[-1] - prom_vals[-13 if len(prom_vals) >= 13 else 0] if prom_vals else None
    fii = _last(_row(page.shp, "FIIs"), 1)
    dii = _last(_row(page.shp, "DIIs"), 1)

    price, book = r.get("Current Price"), r.get("Book Value")
    pe = r.get("Stock P/E")
    sales_g5 = cagr(sales, 5)
    profit_g5 = cagr(np_, 5)
    eps_g5 = cagr(eps, 5)
    growth_for_peg = eps_g5 if eps_g5 is not None else profit_g5

    sales_ttm = _ttm(page.pl_cols, sales_all)
    sales_ttm_g = None
    if sales_ttm is not None and sales and sales[-1]:
        sales_ttm_g = (sales_ttm / sales[-1] - 1.0) * 100.0

    growth_ranges = page.ranges.get("Compounded Sales Growth", {})
    profit_ranges = page.ranges.get("Compounded Profit Growth", {})
    roe_ranges = page.ranges.get("Return on Equity", {})
    price_ranges = page.ranges.get("Stock Price CAGR", {})

    roce5 = _last(roce, 5)
    np_last5 = _last(np_, 5)
    sales_last6 = _last(sales, 6)
    up_years = sum(b > a for a, b in zip(sales_last6, sales_last6[1:]))

    return {
        "is_financial": bool(is_fin),
        "sector": page.sector,
        "mcap_cr": r.get("Market Cap"),
        "price": price,
        "pe": pe,
        "pb": price / book if price and book and book > 0 else None,
        "pct_off_52w_high": (price / r["High"] - 1.0) * 100.0 if price and r.get("High") else None,
        "roce_latest": r.get("ROCE"),
        "roe_latest": r.get("ROE"),
        "roce_avg_5y": _mean(roce5),
        "roce_min_5y": min(roce5) if roce5 else None,
        "roe_avg_5y": _mean(_last(roe_series, 5)) if roe_series else roe_ranges.get("5 Years"),
        "sales_cagr_3y": cagr(sales, 3),
        "sales_cagr_5y": sales_g5,
        "sales_cagr_10y": growth_ranges.get("10 Years") if growth_ranges else cagr(sales, 10),
        "sales_ttm_vs_fy": sales_ttm_g,
        "profit_cagr_3y": cagr(np_, 3),
        "profit_cagr_5y": profit_g5,
        "profit_cagr_10y": profit_ranges.get("10 Years"),
        "eps_cagr_5y": eps_g5,
        "sales_up_years_5": up_years,
        "loss_years_5y": sum(v < 0 for v in np_last5),
        "opm_latest": opm[-1] if opm else None,
        "opm_trend": (opm[-1] - _mean(_last(opm[:-1], 4))) if len(_last(opm, 5)) >= 3 and opm[-1] is not None else None,
        "debt_equity": de,
        "cfo_to_pat_5y": cfo_pat,
        "cash_conversion_cycle": ccc[-1] if ccc else None,
        "dilution_5y_pct": dilution,
        "dividend_payout": payout[-1] if payout else None,
        "promoter_pct": prom_latest,
        "promoter_chg_3y": prom_chg,
        "fii_pct": fii[0] if fii else None,
        "dii_pct": dii[0] if dii else None,
        "peg": pe / growth_for_peg if pe and growth_for_peg and growth_for_peg > 0 else None,
        "price_cagr_5y": price_ranges.get("5 Years"),
        "price_cagr_1y": price_ranges.get("1 Year"),
        "years_of_data": len([v for v in sales if v is not None]),
    }


# --------------------------------------------------------------------------- gates & score


@dataclass
class Gates:
    mcap_min: float = 500.0
    mcap_max: float = 50_000.0
    roce_avg_min: float = 15.0
    roce_floor_min: float = 10.0
    de_max: float = 0.75
    cfo_pat_min: float = 0.6
    sales_cagr_min: float = 12.0
    profit_cagr_min: float = 12.0
    promoter_min: float = 30.0
    promoter_drop_max: float = 10.0
    dilution_max: float = 25.0
    pe_max: float = 80.0
    # lenders
    fin_roe_min: float = 14.0
    fin_pb_max: float = 5.0


def gate_failures(m: dict[str, Any], g: Gates) -> list[str]:
    fails: list[str] = []

    def need(key: str, ok, label: str):
        v = m.get(key)
        if v is None:
            fails.append(f"no {key}")
        elif not ok(v):
            fails.append(label)

    need("mcap_cr", lambda v: g.mcap_min <= v <= g.mcap_max, "mcap out of range")
    need("years_of_data", lambda v: v >= 6, "<6y history")
    need("loss_years_5y", lambda v: v == 0, "loss year in 5y")
    need("sales_cagr_5y", lambda v: v >= g.sales_cagr_min, "sales growth low")
    need("profit_cagr_5y", lambda v: v >= g.profit_cagr_min, "profit growth low")
    if m.get("pe") is not None and m["pe"] > g.pe_max:
        fails.append("P/E too high")
    if m.get("promoter_pct") is not None:
        if m["promoter_pct"] < g.promoter_min:
            fails.append("promoter stake low")
        if (m.get("promoter_chg_3y") or 0) < -g.promoter_drop_max:
            fails.append("promoter selling")
    if (m.get("dilution_5y_pct") or 0) > g.dilution_max:
        fails.append("equity dilution")

    if m.get("is_financial"):
        need("roe_avg_5y", lambda v: v >= g.fin_roe_min, "ROE low")
        need("pb", lambda v: v <= g.fin_pb_max, "P/B too high")
    else:
        need("roce_avg_5y", lambda v: v >= g.roce_avg_min, "ROCE low")
        need("roce_min_5y", lambda v: v >= g.roce_floor_min, "ROCE dipped")
        if m.get("debt_equity") is not None and m["debt_equity"] > g.de_max:
            fails.append("leverage high")
        need("cfo_to_pat_5y", lambda v: v >= g.cfo_pat_min, "weak cash conversion")
    return fails


# (metric, weight, higher_is_better, group)
WEIGHTS: list[tuple[str, float, bool, str]] = [
    ("sales_cagr_5y", 10, True, "growth"),
    ("eps_cagr_5y", 10, True, "growth"),
    ("sales_cagr_3y", 5, True, "growth"),
    ("sales_ttm_vs_fy", 5, True, "growth"),
    ("roce_avg_5y", 12, True, "quality"),
    ("roce_min_5y", 6, True, "quality"),
    ("opm_trend", 4, True, "quality"),
    ("cfo_to_pat_5y", 8, True, "quality"),
    ("debt_equity", 10, False, "balance"),
    ("peg", 12, False, "value"),
    ("pe", 8, False, "value"),
    ("mcap_cr", 5, False, "runway"),
    ("promoter_pct", 5, True, "runway"),
]
FIN_SUBSTITUTE = {"roce_avg_5y": "roe_avg_5y", "roce_min_5y": "roe_latest",
                  "cfo_to_pat_5y": None, "debt_equity": "pb"}


def _pct_rank(values: list[Optional[float]], higher_is_better: bool) -> list[Optional[float]]:
    present = sorted(v for v in values if v is not None)
    n = len(present)
    out: list[Optional[float]] = []
    for v in values:
        if v is None or n == 0:
            out.append(None)
            continue
        below = sum(1 for p in present if p < v)
        equal = sum(1 for p in present if p == v)
        pct = (below + 0.5 * equal) / n
        out.append(pct if higher_is_better else 1.0 - pct)
    return out


def score(rows: list[dict[str, Any]]) -> None:
    """Adds score + group sub-scores in place (0-100), ranking lenders and non-lenders separately."""
    for fin in (False, True):
        cohort = [r for r in rows if bool(r.get("is_financial")) == fin]
        if not cohort:
            continue
        totals = [0.0] * len(cohort)
        wsum = [0.0] * len(cohort)
        groups: list[dict[str, list[float]]] = [{} for _ in cohort]
        for key, w, hib, grp in WEIGHTS:
            k = FIN_SUBSTITUTE.get(key, key) if fin else key
            if k is None:
                continue
            ranks = _pct_rank([r.get(k) for r in cohort], hib)
            for i, rk in enumerate(ranks):
                if rk is None:
                    continue
                totals[i] += w * rk
                wsum[i] += w
                groups[i].setdefault(grp, []).append(rk)
        for i, r in enumerate(cohort):
            r["score"] = round(100.0 * totals[i] / wsum[i], 1) if wsum[i] else None
            for grp, ranks in groups[i].items():
                r[f"score_{grp}"] = round(100.0 * sum(ranks) / len(ranks), 1)


# --------------------------------------------------------------------------- report


REPORT_COLS = [
    "symbol", "company", "segment", "industry", "is_financial", "score",
    "score_growth", "score_quality", "score_balance", "score_value", "score_runway",
    "mcap_cr", "price", "pe", "pb", "peg", "roce_avg_5y", "roce_min_5y", "roe_avg_5y",
    "sales_cagr_3y", "sales_cagr_5y", "sales_cagr_10y", "sales_ttm_vs_fy",
    "profit_cagr_3y", "profit_cagr_5y", "profit_cagr_10y", "eps_cagr_5y",
    "opm_latest", "opm_trend", "debt_equity", "cfo_to_pat_5y", "cash_conversion_cycle",
    "dilution_5y_pct", "dividend_payout", "promoter_pct", "promoter_chg_3y",
    "fii_pct", "dii_pct", "pct_off_52w_high", "price_cagr_5y", "price_cagr_1y",
    "loss_years_5y", "years_of_data", "passes", "gate_failures",
]


def _fmt(v: Any, nd: int = 1) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "–"
    if isinstance(v, float):
        return f"{v:,.{nd}f}"
    return str(v)


def run_screen(gates: Gates, top: int) -> tuple[list[dict[str, Any]], Path, Path]:
    universe = load_universe()
    rows: list[dict[str, Any]] = []
    for u in universe:
        path = _cache_path(u["symbol"])
        row: dict[str, Any] = dict(u)
        if not path.exists():
            row.update({"passes": False, "gate_failures": "no data"})
            rows.append(row)
            continue
        try:
            row.update(compute_metrics(parse_page(path.read_text())))
        except Exception as exc:  # one bad page must not sink the run
            row.update({"passes": False, "gate_failures": f"parse error: {exc}"})
            rows.append(row)
            continue
        fails = gate_failures(row, gates)
        row["passes"] = not fails
        row["gate_failures"] = "; ".join(fails)
        rows.append(row)

    passed = [r for r in rows if r.get("passes")]
    score(passed)
    rows.sort(key=lambda r: (not r.get("passes"), -(r.get("score") or 0)))

    stamp = date.today().isoformat()
    csv_path = DATA_DIR / f"screen_{stamp}.csv"
    with csv_path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=REPORT_COLS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: (round(v, 2) if isinstance(v, float) else v) for k, v in r.items()})

    md_path = REPO_ROOT / "docs" / f"multibagger_screen_{stamp}.md"
    md_path.write_text(render_markdown(rows, gates, top, stamp))
    return rows, csv_path, md_path


def render_markdown(rows: list[dict[str, Any]], gates: Gates, top: int, stamp: str) -> str:
    total = len(rows)
    with_data = sum(1 for r in rows if r.get("gate_failures") not in ("no data",) and "parse error" not in str(r.get("gate_failures")))
    passed = [r for r in rows if r.get("passes")]
    fail_counts: dict[str, int] = {}
    for r in rows:
        for f in filter(None, str(r.get("gate_failures") or "").split("; ")):
            fail_counts[f] = fail_counts.get(f, 0) + 1

    lines = [
        f"# Multibagger fundamentals screen — {stamp}",
        "",
        f"Universe: Nifty 500 + Nifty Microcap 250 — {total} symbols, {with_data} with parseable fundamentals, "
        f"**{len(passed)} pass every quality gate**.",
        "",
        "## Gates",
        "",
        f"- Market cap ₹{gates.mcap_min:,.0f}–{gates.mcap_max:,.0f} cr (room to compound), ≥6 years of history, no loss year in the last 5",
        f"- 5y sales CAGR ≥ {gates.sales_cagr_min:.0f}% and 5y profit CAGR ≥ {gates.profit_cagr_min:.0f}%; P/E ≤ {gates.pe_max:.0f}",
        f"- Non-lenders: 5y avg ROCE ≥ {gates.roce_avg_min:.0f}%, worst year ≥ {gates.roce_floor_min:.0f}%, debt/equity ≤ {gates.de_max}, 5y CFO/PAT ≥ {gates.cfo_pat_min}",
        f"- Lenders: 5y avg ROE ≥ {gates.fin_roe_min:.0f}%, P/B ≤ {gates.fin_pb_max}",
        f"- Promoter stake ≥ {gates.promoter_min:.0f}% and not cut by more than {gates.promoter_drop_max:.0f} pts in 3y; 5y share-count dilution ≤ {gates.dilution_max:.0f}%",
        "",
        "Score (0–100, percentile within the passing cohort; lenders ranked separately): growth 30, "
        "quality 30 (ROCE level + floor, margin trend, cash conversion), balance sheet 10, valuation 20 (PEG, P/E), runway 10 (smaller cap, promoter stake).",
        "",
        "## Ranked shortlist",
        "",
        "| # | Symbol | Company | Segment | Score | Mcap ₹cr | P/E | PEG | ROCE 5y | Sales 5y | EPS 5y | D/E | CFO/PAT | Promoter |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(passed[:top], 1):
        roce = r.get("roe_avg_5y") if r.get("is_financial") else r.get("roce_avg_5y")
        lines.append(
            f"| {i} | {r['symbol']} | {r.get('company','')} | {r.get('segment','')} | {_fmt(r.get('score'))} | "
            f"{_fmt(r.get('mcap_cr'),0)} | {_fmt(r.get('pe'))} | {_fmt(r.get('peg'),2)} | {_fmt(roce)}{'*' if r.get('is_financial') else ''} | "
            f"{_fmt(r.get('sales_cagr_5y'))}% | {_fmt(r.get('eps_cagr_5y'))}% | {_fmt(r.get('debt_equity'),2)} | "
            f"{_fmt(r.get('cfo_to_pat_5y'),2)} | {_fmt(r.get('promoter_pct'))}% |"
        )
    lines += ["", "\\* lender: ROE shown instead of ROCE.", "", "## Why names were rejected", "",
              "| Gate | Names failing |", "|---|---|"]
    for f, c in sorted(fail_counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"| {f} | {c} |")
    lines += ["", "Full metrics for every symbol are in `data/fundamentals/screen_" + stamp + ".csv`.", ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------- cli


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["fetch", "screen", "all"])
    ap.add_argument("--pause", type=float, default=2.0, help="seconds between screener.in requests")
    ap.add_argument("--refresh", action="store_true", help="refetch pages already cached")
    ap.add_argument("--limit", type=int, default=0, help="fetch only the first N symbols (smoke test)")
    ap.add_argument("--top", type=int, default=50, help="rows in the markdown shortlist")
    ap.add_argument("--mcap-max", type=float, default=Gates.mcap_max)
    args = ap.parse_args(argv)

    if args.command in ("fetch", "all"):
        s = _session()
        universe = fetch_universe(s)
        print(f"universe: {len(universe)} symbols")
        syms = [u["symbol"] for u in universe]
        if args.limit:
            syms = syms[: args.limit]
        status = fetch_pages(s, syms, pause=args.pause, refresh=args.refresh)
        counts: dict[str, int] = {}
        for v in status.values():
            counts[v] = counts.get(v, 0) + 1
        print("pages:", json.dumps(counts))

    if args.command in ("screen", "all"):
        rows, csv_path, md_path = run_screen(Gates(mcap_max=args.mcap_max), args.top)
        print(f"{sum(bool(r.get('passes')) for r in rows)} / {len(rows)} pass; wrote {csv_path} and {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
