"""Option-contract counterpart to the catalog key-collision guard.

2026-07-20: ``option_premium_candles`` carried ~33k MARUTI option bars filed
under ``M&M`` — strikes 11,600-16,400 against a ~3,164 underlying, going back
to 2025-02-27. The write vector is ``_persist_contracts_for_expiry``: it stamps
``underlying = <the symbol we asked for>`` onto whatever the chain fetch
returned, and the upsert's ``ON CONFLICT`` clause overwrites the ``underlying``
column outright. One crossed fetch therefore relabels the other name's
contracts, and every premium candle written through those keys inherits the
wrong company's name.

The broker's own ``trading_symbol`` is an external anchor — issued with the
contract, independent of what we requested — so it can separate the two.
"""
from market_data.catalog_integrity import filter_foreign_contracts


def _row(trading_symbol: str, underlying: str = "M&M") -> dict:
    return {
        "instrument_key": f"NSE_FO|{abs(hash(trading_symbol)) % 999999}",
        "trading_symbol": trading_symbol,
        "underlying": underlying,
        "option_type": "CE",
    }


def test_drops_foreign_contract_named_by_the_broker():
    rows = [
        _row("M&M 3160 CE 28 JUL 26"),
        _row("MARUTI 13200 CE 30 JUN 26"),
        _row("MARUTI 13100 PE 30 JUN 26"),
    ]
    kept = filter_foreign_contracts("M&M", rows)
    assert [r["trading_symbol"] for r in kept] == ["M&M 3160 CE 28 JUL 26"]


def test_keeps_everything_when_the_chain_is_clean():
    rows = [_row("MARUTI 13500 CE 28 JUL 26", underlying="MARUTI")]
    assert filter_foreign_contracts("MARUTI", rows) == rows


def test_corporate_renames_are_dropped_in_the_safe_direction():
    """A rename whose old ticker still appears in the trading symbol (LTIM ->
    LTM, TATAMOTORS -> TMPV) is dropped, not kept.

    That is the deliberate failure direction: the name goes dataless, its lane
    skips it for lack of data, and an ERROR is logged — strictly better than one
    company's premiums trading under another's name. Live chain fetches return
    the current ticker, and as of 2026-07-20 all 68 such rows in
    ``fo_contract_catalog`` are already-expired contracts, so nothing live is
    affected."""
    assert filter_foreign_contracts("LTM", [_row("LTIM 5100 CE 26 JUN 25", "LTM")]) == []
    assert filter_foreign_contracts(
        "TATAMOTORS", [_row("TMPV 340 CE 28 JUL 26", "TATAMOTORS")]) == []


def test_keeps_rows_without_a_trading_symbol():
    """Fail open on missing metadata — the guard removes only the unambiguous."""
    rows = [_row(""), {"instrument_key": "NSE_FO|1", "underlying": "M&M"}]
    assert len(filter_foreign_contracts("M&M", rows)) == 2


def test_empty_and_missing_symbol_are_passthrough():
    assert filter_foreign_contracts("M&M", []) == []
    rows = [_row("MARUTI 13200 CE 30 JUN 26")]
    assert filter_foreign_contracts("", rows) == rows


def test_case_and_whitespace_insensitive():
    rows = [_row("  maruti 13200 ce 30 jun 26  ")]
    assert filter_foreign_contracts("m&m", rows) == []
