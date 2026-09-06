"""Offline tests for the participant-OI parser -- no network, no database.

Matches the live app's own test convention (Makefile: "mocks or gracefully
degrades Postgres, Redis and every broker, so it needs no database or
network"). The fixture below is NSE's real 2026-08-26 response, captured
live during development.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from ingest.m1_participant_oi import parse  # noqa: E402

FIXTURE = '''""Participant wise Open Interest (no. of contracts) in Equity Derivatives as on Aug 26, 2026"",,,,,,,,,,,,,,
Client Type,Future Index Long,Future Index Short,Future Stock Long,Future Stock Short       ,Option Index Call Long,Option Index Put Long,Option Index Call Short,Option Index Put Short,Option Stock Call Long,Option Stock Put Long,Option Stock Call Short,Option Stock Put Short,Total Long Contracts      ,Total Short Contracts
Client,223646,63363,3309656,194625,2512058,1853031,2320987,2516035,1252183,543141,824989,847979,9693715,6767978
DII,37353,19764,272637,4396969,4201,32019,296,40,4417,38426,154883,8984,389053,4580936
FII,25651,211711,3425528,2857765,433630,867038,669515,311287,53795,122437,132919,61342,4928078,4244538
Pro,38116,29928,779206,337668,760188,700084,719280,624810,565051,690478,762655,476177,3533123,2950518
TOTAL,324766,324766,7787027,7787027,3710077,3452171,3710077,3452171,1875446,1394482,1875446,1394482,18543969,18543969
'''


def test_parses_all_four_participants_times_six_buckets():
    rows = parse(FIXTURE)
    assert len(rows) == 4 * 6
    assert {r["participant"] for r in rows} == {"Client", "DII", "FII", "Pro"}
    assert {r["bucket"] for r in rows} == {
        "fut_index", "fut_stock", "opt_index_call", "opt_index_put",
        "opt_stock_call", "opt_stock_put",
    }


def test_the_total_row_is_dropped_not_stored_as_a_fifth_participant():
    rows = parse(FIXTURE)
    assert all(r["participant"] != "TOTAL" for r in rows)


def test_values_match_the_real_csv_exactly():
    rows = {(r["participant"], r["bucket"]): r for r in parse(FIXTURE)}
    fii_fut_index = rows[("FII", "fut_index")]
    assert fii_fut_index["long_contracts"] == 25651
    assert fii_fut_index["short_contracts"] == 211711
    client_opt_stock_put = rows[("Client", "opt_stock_put")]
    assert client_opt_stock_put["long_contracts"] == 543141
    assert client_opt_stock_put["short_contracts"] == 847979


def test_sum_of_participants_reconciles_to_the_file_s_own_total_column():
    """The TOTAL row is dropped and never trusted -- summing what we parsed
    against NSE's own total column is the actual correctness check."""
    rows = parse(FIXTURE)
    fut_index_long = sum(r["long_contracts"] for r in rows if r["bucket"] == "fut_index")
    assert fut_index_long == 324766  # TOTAL row, Future Index Long column


def test_a_missing_expected_column_fails_loudly_not_silently():
    """NSE has reshuffled this file's columns before. A collector that
    silently drops a bucket it can't find is worse than one that stops."""
    import pytest
    broken = FIXTURE.replace("Future Index Long", "Something Else Entirely")
    with pytest.raises(ValueError, match="Future Index Long"):
        parse(broken)


def test_a_missing_header_row_fails_loudly():
    import pytest
    with pytest.raises(ValueError, match="Client Type"):
        parse("not,a,participant,oi,file\\n1,2,3,4,5\\n")


def test_blank_and_footer_lines_do_not_crash_the_parser():
    padded = FIXTURE + "\n\n,,,,,,,,,,,,,,\n"
    rows = parse(padded)
    assert len(rows) == 4 * 6
