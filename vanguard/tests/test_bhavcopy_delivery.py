"""Offline tests for the bhavcopy+delivery parser -- no network, no database.

Fixture rows are NSE's real 2026-08-25 sec_bhavdata_full response, captured
live during development (same convention as test_m1_participant_oi.py).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from ingest.bhavcopy_delivery import parse  # noqa: E402

HEADER = ("SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, "
          "LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, "
          "NO_OF_TRADES, DELIV_QTY, DELIV_PER")

FIXTURE = HEADER + "\n" + "\n".join([
    "360ONE, EQ, 25-Aug-2026, 1189.80, 1191.20, 1210.00, 1186.40, 1210.00, 1210.00, 1199.98, 539607, 6475.17, 29688, 305457, 56.61",
    "ABB, EQ, 25-Aug-2026, 7504.00, 7510.00, 7627.00, 7435.00, 7627.00, 7627.00, 7537.65, 178245, 13435.49, 22185, 67601, 37.93",
    # not in the universe -- must be dropped
    "ZYDUSWELL, EQ, 25-Aug-2026, 100.00, 101.00, 102.00, 99.00, 100.50, 100.50, 100.60, 1000, 1.00, 10, 500, 50.00",
    # a government-security row: non-EQ series, blank delivery fields
    "1018GS2026, GS, 25-Aug-2026, 105.00, 104.40, 110.00, 104.40, 104.45, 104.49, 104.98, 137, 0.14, 6, 137, ",
    # an EQ-series universe row with a genuinely blank delivery percentage
    # (NSE leaves this blank for some thinly-traded names, not just non-EQ series)
    "THINLYTRADED, EQ, 25-Aug-2026, 10.00, 10.00, 10.50, 9.90, 10.10, 10.10, 10.05, 100, 0.01, 2, , ",
]) + "\n"

UNIVERSE = {"360ONE", "ABB", "1018GS2026", "THINLYTRADED"}


def test_parses_only_universe_symbols():
    rows = parse(FIXTURE, UNIVERSE)
    assert {r["symbol"] for r in rows} == {"360ONE", "ABB", "THINLYTRADED"}


def test_non_eq_series_is_dropped_even_if_in_universe():
    """1018GS2026 is in UNIVERSE here (to prove the series filter, not the
    universe filter, is what drops it) but its only row is series GS."""
    rows = parse(FIXTURE, UNIVERSE)
    assert "1018GS2026" not in {r["symbol"] for r in rows}


def test_values_match_the_real_csv_exactly():
    rows = {r["symbol"]: r for r in parse(FIXTURE, UNIVERSE)}
    abb = rows["ABB"]
    assert abb["open"] == 7510.00
    assert abb["high"] == 7627.00
    assert abb["low"] == 7435.00
    assert abb["close"] == 7627.00
    assert abb["prev_close"] == 7504.00
    assert abb["volume"] == 178245
    assert abb["deliverable_qty"] == 67601
    assert abb["delivery_pct"] == 37.93
    assert abb["date_str"] == "25-Aug-2026"


def test_turnover_lacs_is_converted_to_rupees():
    rows = {r["symbol"]: r for r in parse(FIXTURE, UNIVERSE)}
    # 6475.17 lakh -> 647,517,000 rupees
    assert rows["360ONE"]["value"] == 6475.17 * 100_000


def test_a_missing_expected_column_fails_loudly_not_silently():
    import pytest
    broken = FIXTURE.replace("DELIV_PER", "SOMETHING_ELSE")
    with pytest.raises(ValueError, match="DELIV_PER"):
        parse(broken, UNIVERSE)


def test_gs_series_row_with_universe_symbol_yields_zero_rows():
    rows = parse(FIXTURE, {"1018GS2026"})
    # GS row's only series is non-EQ, so with only that symbol in the
    # universe the parser should yield zero rows, not raise.
    assert rows == []


def test_blank_delivery_fields_become_none_not_a_crash():
    rows = {r["symbol"]: r for r in parse(FIXTURE, UNIVERSE)}
    thin = rows["THINLYTRADED"]
    assert thin["deliverable_qty"] is None
    assert thin["delivery_pct"] is None


def test_blank_and_footer_lines_do_not_crash_the_parser():
    padded = FIXTURE + "\n\n"
    rows = parse(padded, UNIVERSE)
    assert {r["symbol"] for r in rows} == {"360ONE", "ABB", "THINLYTRADED"}
