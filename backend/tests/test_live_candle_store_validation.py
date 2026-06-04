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


def test_reference_not_poisoned_by_rejected_prints():
    store = LiveCandleStore()
    sym = _index_symbol()
    _warmup(store, sym)
    assert store._validate_tick(_mk(sym, 60000.0)) is False  # rejected
    # A legit price right after is still accepted — the rolling median held.
    assert store._validate_tick(_mk(sym, 23100.0)) is True


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
