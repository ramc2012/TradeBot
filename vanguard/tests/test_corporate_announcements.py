"""Offline tests for the corporate-announcements / pledge / insider / results
parsers -- no network, no database. Fixtures below are NSE's real live
responses, captured during development on 2026-08-26 (trimmed to the fields
exercised).
"""
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from ingest.corporate_announcements import (  # noqa: E402
    _dedupe_last_wins,
    classify_category,
    parse_announcements,
    parse_board_meeting_results,
    parse_insider_trades,
    parse_pledges,
)

ANNOUNCEMENTS_FIXTURE = [
    {
        "an_dt": "26-Aug-2026 22:17:50",
        "attFileSize": "233.30 KB",
        "attchmntFile": "https://nsearchives.nseindia.com/corporate/AHFL2021_26082026221726_Intimation_of_ESOP_allotment.pdf",
        "attchmntText": "Aadhar Housing Finance Limited has informed the Exchange regarding allotment of 469259 Equity Shares.",
        "desc": "ESOP/ESOS/ESPS",
        "symbol": "AADHARHFC",
    },
    {
        "an_dt": "21-Aug-2026 16:42:17",
        "attFileSize": "1.18 MB",
        "attchmntFile": "https://nsearchives.nseindia.com/corporate/kavinavora_21082026164208_SE_21082026.pdf",
        "attchmntText": "Reliance Industries Limited has informed the Exchange regarding the Amendment to AOA/MOA of the company.",
        "desc": "Amendment to AOA/MOA",
        "symbol": "RELIANCE",
    },
    {
        # No symbol -- must be dropped, not crash.
        "an_dt": "21-Aug-2026 16:42:17",
        "attchmntText": "orphan row",
        "desc": "General Updates",
        "symbol": "",
    },
    {
        # Unparseable timestamp -- must be dropped, not crash.
        "an_dt": "not-a-date",
        "attchmntText": "bad timestamp row",
        "desc": "General Updates",
        "symbol": "TCS",
    },
]

PLEDGE_FIXTURE = [
    {
        "attachment": "https://nsearchives.nseindia.com/corporate/team_sandeshc_18062026120718_Utility.pdf",
        "broadcastDate": "13-Jul-2026 20:25:38",
        "companyName": "Gmr Power And Urban Infra Limited",
        "promoterName": "GMR ENTERPRISES PRIVATE LIMITED",
        "shareMoreThan20Per": "Yes",
        "shareMoreThan50Per": "Yes",
        "symbol": "GMRP&UI",
    },
    {
        "attachment": "",
        "broadcastDate": "-",
        "companyName": "Omaxe Limited",
        "promoterName": "1. M/s Dream Home Developers Pvt. Ltd.",
        "shareMoreThan20Per": None,
        "shareMoreThan50Per": None,
        "symbol": "OMAXE",
    },
    {
        # Missing promoter name -- must be dropped, not crash.
        "companyName": "No Promoter Co",
        "promoterName": "",
        "symbol": "NOPROMO",
    },
]

INSIDER_FIXTURE = [
    {
        "acqMode": "Off Market",
        "acqName": "BALANADU NARAYAN",
        "acqfromDt": "13-Feb-2026",
        "acqtoDt": "13-Feb-2026",
        "afterAcqSharesNo": "1600",
        "afterAcqSharesPer": "0",
        "befAcqSharesNo": "3920",
        "befAcqSharesPer": "0",
        "company": "Reliance Industries Limited",
        "did": "563850",
        "intimDt": "16-Feb-2026",
        "personCategory": "Other",
        "secAcq": "2320",
        "secType": "Equity Shares",
        "secVal": "3294168",
        "symbol": "RELIANCE",
        "tdpTransactionType": "Sell",
    },
    {
        # No `did` -- must be dropped, not crash (did is the dedup key).
        "acqName": "NO DID PERSON",
        "symbol": "RELIANCE",
        "did": "",
    },
]

BOARD_MEETINGS_FIXTURE = [
    {"bm_symbol": "TECHNOCRAF", "bm_date": "02-Sep-2026", "bm_purpose": "Financial Results"},
    {"bm_symbol": "SUPREMEENG", "bm_date": "29-Aug-2026", "bm_purpose": "Financial Results/Fund Raising"},
    {"bm_symbol": "SUMEETINDS", "bm_date": "09-Sep-2026", "bm_purpose": "Other business matters"},
    {"bm_symbol": "", "bm_date": "02-Sep-2026", "bm_purpose": "Financial Results"},  # no symbol
    {"bm_symbol": "OUTOFUNIV", "bm_date": "02-Sep-2026", "bm_purpose": "Financial Results"},  # filtered by universe
]


# ---------------------------------------------------------------------------
# classify_category
# ---------------------------------------------------------------------------

def test_classify_category_matches_results():
    assert classify_category("GICL Clarification - Financial Results") == "results"


def test_classify_category_matches_board_meeting():
    assert classify_category("Outcome of Board Meeting") == "board_meeting"


def test_classify_category_falls_back_to_general():
    assert classify_category("Copy of Newspaper Publication") == "general"


# ---------------------------------------------------------------------------
# announcements
# ---------------------------------------------------------------------------

def test_parse_announcements_maps_nse_fields_to_schema():
    rows = parse_announcements(ANNOUNCEMENTS_FIXTURE)
    assert len(rows) == 2  # the no-symbol and bad-timestamp rows are dropped
    row = next(r for r in rows if r["symbol"] == "AADHARHFC")
    assert row["dt"] == datetime(2026, 8, 26, 22, 17, 50)
    assert row["subject"] == "ESOP/ESOS/ESPS"
    assert row["description"].startswith("Aadhar Housing Finance")
    assert row["attachment_url"].endswith(".pdf")
    assert row["category"] == "general"


def test_parse_announcements_scopes_to_universe():
    rows = parse_announcements(ANNOUNCEMENTS_FIXTURE, universe={"RELIANCE"})
    assert len(rows) == 1
    assert rows[0]["symbol"] == "RELIANCE"


def test_parse_announcements_rows_with_no_symbol_or_bad_timestamp_do_not_crash():
    rows = parse_announcements(ANNOUNCEMENTS_FIXTURE)
    assert all(r["symbol"] for r in rows)


# ---------------------------------------------------------------------------
# pledge_disclosures
# ---------------------------------------------------------------------------

def test_parse_pledges_converts_yes_no_to_bool():
    rows = parse_pledges(PLEDGE_FIXTURE)
    assert len(rows) == 2  # the no-promoter row is dropped
    gmr = next(r for r in rows if r["symbol"] == "GMRP&UI")
    assert gmr["encumbered_gt_20pct"] is True
    assert gmr["encumbered_gt_50pct"] is True
    assert gmr["broadcast_dt"] == datetime(2026, 7, 13, 20, 25, 38)


def test_parse_pledges_handles_placeholder_dash_broadcast_date():
    rows = parse_pledges(PLEDGE_FIXTURE)
    omaxe = next(r for r in rows if r["symbol"] == "OMAXE")
    assert omaxe["broadcast_dt"] is None
    assert omaxe["encumbered_gt_20pct"] is None


def test_parse_pledges_scopes_to_universe():
    rows = parse_pledges(PLEDGE_FIXTURE, universe={"OMAXE"})
    assert len(rows) == 1
    assert rows[0]["symbol"] == "OMAXE"


# ---------------------------------------------------------------------------
# insider_trades
# ---------------------------------------------------------------------------

def test_parse_insider_trades_maps_and_converts_numeric_fields():
    rows = parse_insider_trades(INSIDER_FIXTURE)
    assert len(rows) == 1  # the no-did row is dropped
    row = rows[0]
    assert row["nse_disclosure_id"] == "563850"
    assert row["transaction_type"] == "Sell"
    assert row["securities_acquired"] == 2320
    assert row["value_acquired"] == 3294168.0
    assert row["acq_from_dt"] == date(2026, 2, 13)
    assert row["intimation_dt"] == date(2026, 2, 16)


def test_parse_insider_trades_rows_without_did_are_dropped_not_crashed_on():
    rows = parse_insider_trades(INSIDER_FIXTURE)
    assert all(r["nse_disclosure_id"] for r in rows)


# ---------------------------------------------------------------------------
# results_calendar (from board meetings)
# ---------------------------------------------------------------------------

def test_parse_board_meeting_results_only_keeps_financial_results_purpose():
    rows = parse_board_meeting_results(BOARD_MEETINGS_FIXTURE)
    symbols = {r["symbol"] for r in rows}
    assert "SUMEETINDS" not in symbols  # "Other business matters" excluded
    assert "TECHNOCRAF" in symbols
    assert "SUPREMEENG" in symbols  # combined purpose still matches


def test_parse_board_meeting_results_scopes_to_universe():
    rows = parse_board_meeting_results(BOARD_MEETINGS_FIXTURE, universe={"TECHNOCRAF"})
    assert len(rows) == 1
    assert rows[0]["symbol"] == "TECHNOCRAF"


def test_parse_board_meeting_results_parses_the_date():
    rows = parse_board_meeting_results(BOARD_MEETINGS_FIXTURE, universe={"TECHNOCRAF"})
    assert rows[0]["results_date"] == date(2026, 9, 2)


def test_parse_board_meeting_results_drops_rows_with_no_symbol():
    rows = parse_board_meeting_results(BOARD_MEETINGS_FIXTURE)
    assert all(r["symbol"] for r in rows)


# ---------------------------------------------------------------------------
# _dedupe_last_wins -- guards against psycopg2's CardinalityViolation when
# the same natural key appears twice in one execute_values batch (a real
# failure hit live against NSE's own multi-day announcements response).
# ---------------------------------------------------------------------------

def test_dedupe_last_wins_collapses_duplicate_keys_keeping_the_last_row():
    rows = [
        {"symbol": "TCS", "dt": "d1", "subject": "s1", "v": "first"},
        {"symbol": "TCS", "dt": "d1", "subject": "s1", "v": "second"},
    ]
    result = _dedupe_last_wins(rows, ("symbol", "dt", "subject"))
    assert len(result) == 1
    assert result[0]["v"] == "second"


def test_dedupe_last_wins_leaves_distinct_keys_untouched():
    rows = [
        {"symbol": "TCS", "dt": "d1", "subject": "s1"},
        {"symbol": "TCS", "dt": "d2", "subject": "s1"},
    ]
    result = _dedupe_last_wins(rows, ("symbol", "dt", "subject"))
    assert len(result) == 2
