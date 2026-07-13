from analytics.technicals import MACD_MIN_BARS, latest_macd_rsi


def test_latest_macd_requires_full_slow_and_signal_warmup() -> None:
    cold = latest_macd_rsi([100.0 + index for index in range(MACD_MIN_BARS - 1)])
    warm = latest_macd_rsi([100.0 + index for index in range(MACD_MIN_BARS)])

    assert cold["macd"] is None
    assert cold["macd_signal"] is None
    assert warm["macd"] is not None
    assert warm["macd_signal"] is not None
