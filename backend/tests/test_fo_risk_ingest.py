from __future__ import annotations

import io
import zipfile

from market_data.fo_risk_ingest import _extract_csv_from_zip, parse_ban_csv, parse_mwpl_csv


def test_parse_ban_csv_handles_headerless_nil_bulletin() -> None:
    rows = parse_ban_csv("Securities in Ban For Trade Date 09-JUL-2026: NIL\n")

    assert rows == []


def test_parse_ban_csv_handles_headerless_symbol_bulletin() -> None:
    rows = parse_ban_csv("Securities in Ban For Trade Date 27-APR-2026: 1,SAIL,2,IDEA.\n")

    assert [row.symbol for row in rows] == ["SAIL", "IDEA"]


def test_parse_mwpl_csv_handles_combineoi_header() -> None:
    rows = parse_mwpl_csv(
        "\n".join(
            [
                "Date, ISIN, Scrip Name, NSE Symbol, MWPL, Open Interest, Future Equivalent Open Interest, Limit for Next Day",
                "08-JUL-2026,INE123,Example Co,EXAMPLE,100000,25000,20000,75000",
            ]
        )
    )

    assert len(rows) == 1
    assert rows[0].symbol == "EXAMPLE"
    assert rows[0].market_wide_position_limit == 100000
    assert rows[0].open_interest == 25000


def test_extract_csv_from_zip_returns_first_csv_payload() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w") as archive:
        archive.writestr("sample.csv", "symbol,value\nABC,1\n")
        archive.writestr("sample.xml", "<root />")

    assert _extract_csv_from_zip(buffer.getvalue()) == "symbol,value\nABC,1\n"
