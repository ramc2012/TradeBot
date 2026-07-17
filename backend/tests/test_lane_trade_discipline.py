from datetime import datetime, timedelta, timezone

from auction_intelligence.paper.book import PaperPositionBook
from directional_options.config import clone_default_config
from directional_options.schemas import RegimeSnapshot
from directional_options.service import _fresh_quote_time
from directional_options.signals import DirectionalSignalEngine


def _bar() -> dict[str, float]:
    return {
        "ema_spread_pct": 0.0032,
        "breakout_up": 0.4,
        "breakout_down": 0.0,
        "plus_di": 31.0,
        "minus_di": 16.0,
        "momentum_3": 0.004,
        "momentum_8": 0.009,
        "atr": 72.0,
        "close": 24850.0,
        "range_expansion": 1.3,
        "rv_percentile": 0.42,
    }


def _regime(label: str, *, trade_allowed: bool) -> RegimeSnapshot:
    return RegimeSnapshot(
        label=label,
        trade_allowed=trade_allowed,
        confidence=0.78,
        reasons=[],
        preferred_expiry_kind="weekly",
        delta_target_min=0.35,
        delta_target_max=0.55,
        exit_profile="balanced",
    )


def test_all_regimes_reach_the_policy_uncapped(monkeypatch) -> None:
    """REVERSED 2026-07-17 (owner: "uncap signals, no hard gate"): regimes are
    FEATURES the RL policy sees as a one-hot, never barriers. Chop and
    exploration bars must still produce a signal — the policy learns act/skip
    from realised R-multiples; cadence discipline lives in the execution
    layer (paper.py cooldowns + flip confirmation), not in signal vetoes."""
    from core.config import settings

    monkeypatch.setattr(settings, "DIRECTIONAL_POSITIONAL_OPTIONS_ENABLED", False)
    engine = DirectionalSignalEngine(clone_default_config()["signal_engine"])

    assert engine.predict(_bar(), _regime("chop", trade_allowed=False), "3minute", underlying="RELIANCE") is not None
    assert engine.predict(_bar(), _regime("exploration", trade_allowed=True), "3minute", underlying="RELIANCE") is not None
    assert engine.predict(_bar(), _regime("trend", trade_allowed=True), "3minute", underlying="RELIANCE") is not None


def test_quote_freshness_requires_current_parseable_timestamp() -> None:
    now = datetime.now(timezone.utc)
    assert _fresh_quote_time(now.isoformat(), max_age_seconds=60)
    assert not _fresh_quote_time((now - timedelta(minutes=30)).isoformat(), max_age_seconds=60)
    assert not _fresh_quote_time(None, max_age_seconds=60)


def test_auction_flip_requires_hold_and_two_confirmations(tmp_path) -> None:
    book = PaperPositionBook(
        tmp_path,
        limits={"min_hold_seconds": 900, "exit_confirmation_cycles": 2},
    )
    now = datetime.now(timezone.utc)
    position = {"opened_at": (now - timedelta(minutes=5)).isoformat()}
    assert not book._exit_signal_confirmed(position, "SHORT", now=now.isoformat())

    position["opened_at"] = (now - timedelta(minutes=20)).isoformat()
    assert not book._exit_signal_confirmed(position, "SHORT", now=now.isoformat())
    assert book._exit_signal_confirmed(position, "SHORT", now=now.isoformat())
