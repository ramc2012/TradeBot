"""WS-0.1a — ingest validation gate (LiveCandleStore._validate_tick).

Guards against the documented cross-symbol contamination (index spot prints at
2-3x true spot) without false-rejecting legit index moves or option-premium
multiples. Pure/sync — no DB or event loop required.
"""
from brokers.base import Tick
from market_data.live_candle_store import LiveCandleStore
from market_data.symbols import DISPLAY_NAMES


def _mk(symbol: str, ltp: float, volume: int = 100) -> Tick:
    return Tick(symbol=symbol, ltp=ltp, volume=volume)


def _index_symbol() -> str:
    return next(iter(DISPLAY_NAMES))


def _warmup(store: LiveCandleStore, symbol: str, price: float = 23000.0, n: int = 6) -> None:
    for _ in range(n):
        store._validate_tick(_mk(symbol, price))


def test_rejects_cross_symbol_contamination_for_index_spot():
    store = LiveCandleStore()
    sym = _index_symbol()
    _warmup(store, sym)
    # Documented contamination magnitudes vs ~23k spot (2.3x and 3.25x).
    assert store._validate_tick(_mk(sym, 53362.0)) is False
    assert store._validate_tick(_mk(sym, 75831.0)) is False


def test_reference_not_poisoned_by_run_of_bad_prints():
    store = LiveCandleStore()
    sym = _index_symbol()
    _warmup(store, sym)
    # A sustained BURST of contaminating prints (the recorded signature: NIFTY
    # ~57.8k for many consecutive ticks). The old self-referential rolling median
    # would flip once such a burst won a majority of its window and INVERT the
    # guard. The absolute band has no window to poison — every one is rejected...
    for _ in range(50):
        assert store._validate_tick(_mk(sym, 57800.0)) is False
    # ...and a legit price right after is still accepted.
    assert store._validate_tick(_mk(sym, 23100.0)) is True
    assert store._validate_tick(_mk(sym, 24075.0)) is True


def test_accepts_legit_index_intraday_moves():
    store = LiveCandleStore()
    sym = _index_symbol()
    _warmup(store, sym)
    assert store._validate_tick(_mk(sym, 22400.0)) is True   # ~ -3%
    assert store._validate_tick(_mk(sym, 23600.0)) is True   # ~ +2.6%


def test_rejects_structural_garbage_for_any_symbol():
    store = LiveCandleStore()
    sym = _index_symbol()
    assert store._validate_tick(_mk(sym, 0.0)) is False       # non-positive
    assert store._validate_tick(_mk(sym, -5.0)) is False      # negative
    assert store._validate_tick(_mk(sym, 23000.0, volume=-1)) is False  # negative volume


def test_option_premiums_exempt_from_magnitude_gate():
    store = LiveCandleStore()
    opt = "NSE:NIFTY2520023000CE"  # not an index spot symbol
    assert store._validate_tick(_mk(opt, 5.0)) is True
    assert store._validate_tick(_mk(opt, 50.0)) is True       # 10x premium move is legit
    assert store._validate_tick(_mk(opt, 0.0)) is False       # structural still applies


def test_rejects_crosswired_contract_tick_against_top_of_book():
    store = LiveCandleStore()
    tick = Tick(
        symbol="MCX:CRUDEOIL26JULFUT",
        ltp=24217.95,
        bid=6967.0,
        ask=6969.0,
        volume=100,
    )

    assert store._validate_tick(tick) is False


# ─── F1 (2026-07-20): NSE equity spot magnitude gate ──────────────────────


def test_equity_ticks_are_rejected_against_their_external_anchor():
    from market_data import stock_band_guard

    stock_band_guard.clear_reference_closes()
    try:
        stock_band_guard.set_reference_close("BHEL", 443.85)
        store = LiveCandleStore()
        # The 2026-07-17 tape under NSE:BHEL-EQ: three complete, internally
        # coherent frames belonging to OTHER instruments.
        assert store._validate_tick(_mk("NSE:BHEL-EQ", 13847.0)) is False
        assert store._validate_tick(_mk("NSE:BHEL-EQ", 949.30)) is False
        assert store._validate_tick(_mk("NSE:BHEL-EQ", 254.50)) is False
        # Real BHEL prints are untouched.
        assert store._validate_tick(_mk("NSE:BHEL-EQ", 443.85)) is True
        assert store._validate_tick(_mk("NSE:BHEL-EQ", 446.15)) is True
        assert store._validate_tick(_mk("NSE:BHEL-EQ", 420.80)) is True
    finally:
        stock_band_guard.clear_reference_closes()


def test_equity_tick_without_an_anchor_is_not_blocked():
    """Fail-open on an unjudgeable name — a guard must not blind a new lane."""
    from market_data import stock_band_guard

    stock_band_guard.clear_reference_closes()
    try:
        store = LiveCandleStore()
        assert store._validate_tick(_mk("NSE:NEWLISTING-EQ", 1234.5)) is True
        # ...but the name is queued so the next refresh anchors it.
        assert "NEWLISTING" in stock_band_guard._pending
    finally:
        stock_band_guard.clear_reference_closes()


def test_equity_gate_does_not_disturb_the_commodity_gate():
    from market_data import stock_band_guard

    stock_band_guard.clear_reference_closes()
    try:
        store = LiveCandleStore()
        assert store._validate_tick(_mk("MCX:GOLD25AUGFUT", 141240.0)) is True
        # 222k SILVER frame under GOLD's key — still the commodity anchor's job.
        assert store._validate_tick(_mk("MCX:GOLD25AUGFUT", 222400.0)) is False
    finally:
        stock_band_guard.clear_reference_closes()
