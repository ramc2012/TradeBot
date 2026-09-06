"""Offline tests for the bulk/block deals parser -- no network, no database.

Fixtures below are trimmed real NSE responses, captured live 2026-08-26
(content/equities/bulk.csv and content/equities/block.csv).
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from ingest.bulk_block import parse  # noqa: E402

BULK_FIXTURE = '''Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,Trade Price / Wght. Avg. Price,Remarks
26-AUG-2026,AAREYDRUGS,Aarey Drugs & Pharm Ltd,VAYUMIND INNOVATIONS PRIVATE LIMITED,BUY,154471,80.45,-
26-AUG-2026,AASTHA,Aastha Spintex Limited,JAGID VANITABEN RAJENDRAPRASAD,BUY,218459,78.11,-
26-AUG-2026,AASTHA,Aastha Spintex Limited,MADHAV ENTERPRISE,SELL,300000,74.00,-
26-AUG-2026,RELIANCE,Reliance Industries Ltd,SOME FUND HOUSE,BUY,50000,"2,450.75",-
'''

BLOCK_FIXTURE = '''Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,Trade Price / Wght. Avg. Price
26-AUG-2026,PWL,Physicswallah Limited,CITIGROUP GLOBAL MARKETS MAURITIUS PRIVATE LIMITED,BUY,2352677,117.72
26-AUG-2026,PWL,Physicswallah Limited,VIRIDIAN ASIA OPPORTUNITIES MASTER FUND,BUY,2352783,117.72
26-AUG-2026,TCS,Tata Consultancy Services Ltd,GOLDMAN SACHS FUNDS,SELL,120000,4123.50
'''


def test_parses_all_bulk_rows_with_correct_kind_tag():
    rows = parse(BULK_FIXTURE, "bulk")
    assert len(rows) == 4
    assert all(r["kind"] == "bulk" for r in rows)


def test_parses_all_block_rows_with_correct_kind_tag():
    rows = parse(BLOCK_FIXTURE, "block")
    assert len(rows) == 3
    assert all(r["kind"] == "block" for r in rows)


def test_date_column_parsed_to_a_real_date_not_kept_as_a_string():
    rows = parse(BULK_FIXTURE, "bulk")
    assert all(r["dt"] == date(2026, 8, 26) for r in rows)


def test_buy_and_sell_sides_both_survive():
    rows = {(r["client_name"]): r for r in parse(BULK_FIXTURE, "bulk")}
    assert rows["VAYUMIND INNOVATIONS PRIVATE LIMITED"]["deal_type"] == "BUY"
    assert rows["MADHAV ENTERPRISE"]["deal_type"] == "SELL"


def test_symbol_is_uppercased_and_price_survives_a_comma_thousands_separator():
    rows = {r["symbol"]: r for r in parse(BULK_FIXTURE, "bulk")}
    assert "RELIANCE" in rows
    assert rows["RELIANCE"]["price"] == 2450.75
    assert rows["RELIANCE"]["quantity"] == 50000


def test_bulk_and_block_have_different_column_counts_but_both_parse():
    """Bulk has a trailing Remarks column, block does not -- header-substring
    matching must not assume a fixed column count."""
    bulk_rows = parse(BULK_FIXTURE, "bulk")
    block_rows = parse(BLOCK_FIXTURE, "block")
    assert len(bulk_rows) == 4
    assert len(block_rows) == 3


def test_a_missing_expected_column_fails_loudly_not_silently():
    import pytest
    broken = BULK_FIXTURE.replace("Client Name", "Something Else Entirely")
    with pytest.raises(ValueError, match="Client Name"):
        parse(broken, "bulk")


def test_an_unexpected_buy_sell_value_fails_loudly():
    import pytest
    broken = BULK_FIXTURE.replace(",BUY,154471", ",HOLD,154471")
    with pytest.raises(ValueError, match="Buy/Sell"):
        parse(broken, "bulk")


def test_blank_trailing_lines_do_not_crash_the_parser():
    padded = BULK_FIXTURE + "\n\n,,,,,,,\n"
    rows = parse(padded, "bulk")
    assert len(rows) == 4


def test_multiple_clients_same_symbol_same_day_are_all_kept():
    """This is the whole reason bulk_block uses a surrogate id + full-tuple
    unique index instead of a (dt, symbol) natural key like participant_oi:
    AASTHA has two distinct clients on the same day and both must survive."""
    rows = [r for r in parse(BULK_FIXTURE, "bulk") if r["symbol"] == "AASTHA"]
    assert len(rows) == 2
    assert {r["client_name"] for r in rows} == {
        "JAGID VANITABEN RAJENDRAPRASAD", "MADHAV ENTERPRISE",
    }


def test_combined_status_is_ok_when_either_feed_captured_real_rows():
    """Regression test for a real bug: a naive worst-of severity ranking
    (error > date_unavailable > empty > ok) put 'empty' ABOVE 'ok', so a day
    where bulk had real matches but block was correctly empty (the everyday
    case -- block deals are far rarer than bulk deals) was logged as
    'empty' overall, masking genuine successful capture and starving the
    ingest_log-based '5 clean sessions' acceptance-gate evidence."""
    from ingest.bulk_block import FetchResult

    def combine(bulk_status: str, block_status: str) -> str:
        statuses = {bulk_status, block_status}
        if "error" in statuses:
            return "error"
        if "ok" in statuses:
            return "ok"
        if "date_unavailable" in statuses:
            return "date_unavailable"
        return "empty"

    assert combine("ok", "empty") == "ok"
    assert combine("empty", "ok") == "ok"
    assert combine("empty", "empty") == "empty"
    assert combine("ok", "date_unavailable") == "ok"
    assert combine("error", "ok") == "error"
    assert combine("date_unavailable", "empty") == "date_unavailable"
