"""Offline tests for tools/multibagger_screen.py against a screener.in-shaped fixture."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import multibagger_screen as ms  # noqa: E402

YEARS = ["Mar 2019", "Mar 2020", "Mar 2021", "Mar 2022", "Mar 2023", "Mar 2024", "Mar 2025"]


def _table(section_id: str, cols: list[str], rows: dict[str, list]) -> str:
    head = "".join(f"<th>{c}</th>" for c in cols)
    body = ""
    for label, vals in rows.items():
        cells = "".join(f"<td>{'' if v is None else v}</td>" for v in vals)
        body += (f'<tr><td class="text"><button class="button-plain">{label}&nbsp;'
                 f'<span class="blue-icon">+</span></button></td>{cells}</tr>')
    return (f'<section id="{section_id}"><table class="data-table"><thead><tr><th class="text"></th>'
            f"{head}</tr></thead><tbody>{body}</tbody></table></section>")


def _ranges(title: str, vals: dict[str, str]) -> str:
    trs = "".join(f"<tr><td>{k}:</td><td>{v}</td></tr>" for k, v in vals.items())
    return f'<table class="ranges-table"><tr><th colspan="2">{title}</th></tr>{trs}</table>'


def make_page(*, lender: bool = False, mcap: str = "8,450", promoter_series=None) -> str:
    sales = [100, 120, 140, 170, 210, 250, 300]  # 5y CAGR from 120 -> 300 = 20.1%
    net = [10, 12, 15, 19, 24, 30, 36]
    eps = [5, 6, 7.5, 9.5, 12, 15, 18]
    pl_cols = YEARS + ["TTM"]
    if lender:
        pl_rows = {"Revenue": sales + [330], "Financing Profit": [20] * 8,
                   "Financing Margin %": ["20%"] * 8, "Net Profit": net + [40],
                   "EPS in Rs": eps + [20], "Dividend Payout %": ["10%"] * 8}
    else:
        pl_rows = {"Sales": [f"{v:,}" for v in sales] + ["330"],
                   "Operating Profit": [20] * 8,
                   "OPM %": ["18%", "18%", "19%", "19%", "20%", "20%", "22%", "22%"],
                   "Net Profit": net + [40], "EPS in Rs": eps + [20],
                   "Dividend Payout %": ["15%"] * 8}
    promoter_series = promoter_series or [60.0] * 13
    shp_cols = [f"Q{i}" for i in range(len(promoter_series))]
    html = f"""<html><body>
    <a href="/market/IN07/" title="Sector">Capital Goods</a>
    <a href="/market/IN07/IN0701/" title="Industry">Electrical Equipment</a>
    <ul id="top-ratios">
      <li><span class="name">Market Cap</span><span class="value">₹ <span class="number">{mcap}</span> Cr.</span></li>
      <li><span class="name">Current Price</span><span class="value">₹ <span class="number">720</span></span></li>
      <li><span class="name">High / Low</span><span class="value">₹ <span class="number">900</span> / <span class="number">500</span></span></li>
      <li><span class="name">Stock P/E</span><span class="value"><span class="number">36.0</span></span></li>
      <li><span class="name">Book Value</span><span class="value">₹ <span class="number">120</span></span></li>
      <li><span class="name">ROCE</span><span class="value"><span class="number">26.1</span> %</span></li>
      <li><span class="name">ROE</span><span class="value"><span class="number">21.0</span> %</span></li>
    </ul>
    {_table("profit-loss", pl_cols, pl_rows)}
    {_ranges("Compounded Sales Growth", {"10 Years": "18%", "5 Years": "20%", "3 Years": "21%", "TTM": "15%"})}
    {_ranges("Return on Equity", {"10 Years": "19%", "5 Years": "20%", "3 Years": "21%", "Last Year": "21%"})}
    {_ranges("Stock Price CAGR", {"10 Years": "30%", "5 Years": "40%", "1 Year": "-5%"})}
    {_table("balance-sheet", YEARS, {"Equity Capital": [20] * 7, "Reserves": [80, 90, 100, 120, 140, 160, 180],
                                       "Borrowings": [30, 30, 25, 20, 20, 20, 40]})}
    {_table("cash-flow", YEARS, {"Cash from Operating Activity": [8, 11, 13, 17, 22, 27, 30]})}
    {_table("ratios", YEARS, {"ROCE %": ["20%", "21%", "22%", "24%", "25%", "26%", "27%"],
                              "Cash Conversion Cycle": [60, 58, 55, 50, 48, 45, 40]})}
    <div id="quarterly-shp">{_table("shp-inner", shp_cols, {"Promoters": [f"{v}%" for v in promoter_series],
                                                           "FIIs": ["8%"] * len(promoter_series),
                                                           "DIIs": ["12%"] * len(promoter_series)})}</div>
    </body></html>"""
    return html


def test_to_num_handles_indian_grouping_and_symbols():
    assert ms.to_num("1,23,456") == 123456
    assert ms.to_num("-1,234.5") == -1234.5
    assert ms.to_num("22%") == 22
    assert ms.to_num("") is None
    assert ms.to_num("--") is None


def test_cagr():
    assert ms.cagr([100, 200], 1) == pytest.approx(100.0)
    assert ms.cagr([0, 10, 20], 2) is None  # non-positive base
    assert ms.cagr([10, -5], 1) == -100.0
    assert ms.cagr([10], 1) is None


def test_parse_and_metrics_non_lender():
    m = ms.compute_metrics(ms.parse_page(make_page()))
    assert m["is_financial"] is False
    assert m["sector"] == "Electrical Equipment"
    assert m["mcap_cr"] == 8450
    assert m["pe"] == 36.0
    assert m["pb"] == pytest.approx(6.0)
    assert m["pct_off_52w_high"] == pytest.approx(-20.0)
    assert m["sales_cagr_5y"] == pytest.approx(((300 / 120) ** 0.2 - 1) * 100)
    assert m["eps_cagr_5y"] == pytest.approx(((18 / 6) ** 0.2 - 1) * 100)
    assert m["sales_ttm_vs_fy"] == pytest.approx(10.0)  # TTM is excluded from annual series
    assert m["roce_avg_5y"] == pytest.approx(24.8)
    assert m["roce_min_5y"] == 22
    assert m["debt_equity"] == pytest.approx(40 / 200)
    assert m["cfo_to_pat_5y"] == pytest.approx((13 + 17 + 22 + 27 + 30) / (15 + 19 + 24 + 30 + 36))
    assert m["dilution_5y_pct"] == pytest.approx(0.0)
    assert m["promoter_pct"] == 60.0 and m["promoter_chg_3y"] == 0.0
    assert m["sales_cagr_10y"] == 18  # from the ranges table
    assert m["price_cagr_1y"] == -5
    assert m["peg"] == pytest.approx(36.0 / m["eps_cagr_5y"])
    assert m["loss_years_5y"] == 0 and m["sales_up_years_5"] == 5
    assert ms.gate_failures(m, ms.Gates()) == []


def test_lender_detection_and_gates():
    m = ms.compute_metrics(ms.parse_page(make_page(lender=True)))
    assert m["is_financial"] is True
    assert m["roe_avg_5y"] == 20  # falls back to the ranges table
    assert ms.gate_failures(m, ms.Gates()) == ["P/B too high"]  # 6.0 > 5.0


def test_gates_reject_large_caps_and_promoter_selling():
    m = ms.compute_metrics(ms.parse_page(make_page(
        mcap="1,20,000", promoter_series=[60.0] + [55.0] * 11 + [45.0])))
    fails = ms.gate_failures(m, ms.Gates())
    assert "mcap out of range" in fails
    assert "promoter selling" in fails


def test_score_ranks_better_business_higher():
    good = ms.compute_metrics(ms.parse_page(make_page()))
    worse = dict(good, sales_cagr_5y=13.0, eps_cagr_5y=12.0, roce_avg_5y=16.0, peg=4.0, pe=60.0)
    rows = [worse, good]
    ms.score(rows)
    assert good["score"] > worse["score"]
    assert 0 <= worse["score"] <= 100
