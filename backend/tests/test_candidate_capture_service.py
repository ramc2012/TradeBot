"""Capture cycle behaviour + the read-only safety boundary.

The safety test is the load-bearing one. This codebase has NO central
`allow_live_orders` switch — whether code can reach a broker is decided purely
by what it imports — so the observer's guarantee is only as good as an
assertion that its import list stays clean.
"""
from __future__ import annotations

import ast
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from candidate_capture.service import (
    ELIGIBLE,
    NO_TRADE,
    REJECT_CROSSED,
    REJECT_NO_QUOTE,
    REJECT_STALE,
    REJECT_WIDE_SPREAD,
    assess_quote,
    build_no_trade_row,
    build_rows,
    chain_features,
    within_envelope,
)

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

CAPTURE_PACKAGE = Path(__file__).resolve().parent.parent / "candidate_capture"

LISTED = [date(2026, 8, 25), date(2026, 9, 29), date(2026, 10, 27)]


def _payload(entries, *, spot=24000.0, stamp="2026-08-25T09:00:00+00:00"):
    return {
        "symbol": "NIFTY",
        "expiry": "2026-08-25",
        "spot_price": spot,
        "timestamp": stamp,
        "source": "upstox",
        "entries": entries,
        "pcr_oi": 0.92,
        "max_pain": 24000.0,
        "atm_strike": 24000.0,
        "atm_iv": 13.4,
        "data_quality": {"execution_ready": True},
    }


def _entry(strike, option_type="CE", **overrides):
    base = {
        "strike": strike,
        "option_type": option_type,
        "ltp": 120.0,
        "bid": 119.5,
        "ask": 120.5,
        "oi": 100000,
        "volume": 50000,
        "iv": 13.5,
        "delta": 0.5,
        "gamma": 0.001,
        "theta": -8.0,
        "vega": 12.0,
        "oi_change": 1500.0,
    }
    base.update(overrides)
    return base


class TestAssessQuote:
    def test_healthy_two_sided_quote_is_eligible(self):
        verdict = assess_quote(ltp=120.0, bid=119.5, ask=120.5, quote_age_seconds=5.0)
        assert verdict.status == ELIGIBLE
        assert verdict.spread == pytest.approx(1.0)
        assert verdict.spread_pct == pytest.approx(1.0 / 120.0, abs=1e-6)
        assert verdict.is_stale is False

    def test_missing_side_is_recorded_not_dropped(self):
        verdict = assess_quote(ltp=120.0, bid=None, ask=120.5, quote_age_seconds=1.0)
        assert verdict.status == REJECT_NO_QUOTE
        assert verdict.reason == "no_two_sided_quote"

    def test_crossed_book_wins_over_staleness(self):
        verdict = assess_quote(ltp=120.0, bid=125.0, ask=120.0, quote_age_seconds=9999.0)
        assert verdict.status == REJECT_CROSSED
        # Age is still reported — the two facts are independent.
        assert verdict.is_stale is True

    def test_wide_spread_rejected_with_the_measured_fraction(self):
        verdict = assess_quote(ltp=10.0, bid=5.0, ask=15.0, quote_age_seconds=1.0)
        assert verdict.status == REJECT_WIDE_SPREAD
        assert verdict.spread_pct == pytest.approx(1.0)

    def test_stale_quote_keeps_its_spread(self):
        verdict = assess_quote(ltp=120.0, bid=119.5, ask=120.5, quote_age_seconds=500.0)
        assert verdict.status == REJECT_STALE
        assert verdict.is_stale is True
        assert verdict.spread == pytest.approx(1.0)

    def test_unknown_age_is_not_treated_as_stale(self):
        assert assess_quote(
            ltp=1.0, bid=0.9, ask=1.1, quote_age_seconds=None
        ).is_stale is False


class TestEnvelope:
    def test_bounds_by_steps(self):
        assert within_envelope(3.0, max_steps=8.0) is True
        assert within_envelope(-8.0, max_steps=8.0) is True
        assert within_envelope(9.0, max_steps=8.0) is False

    def test_uncomputable_moneyness_is_kept(self):
        # Otherwise a cycle with no spot would look like a thin chain rather
        # than a degraded capture.
        assert within_envelope(None, max_steps=8.0) is True


class TestBuildRows:
    def _build(self, entries, **kwargs):
        return build_rows(
            payload=_payload(entries),
            underlying="NIFTY",
            expiry=date(2026, 8, 25),
            listed_expiries=LISTED,
            decision_id="11111111-1111-1111-1111-111111111111",
            captured_at=datetime(2026, 8, 25, 9, 0, 30, tzinfo=UTC),
            vix=13.2,
            vix_missing_reason=None,
            **kwargs,
        )

    def test_one_row_per_contract_with_taxonomy_and_market_state(self):
        # A realistic ladder: the step is read off the chain's own strikes, so a
        # chain needs more than one rung before moneyness can be computed.
        rows = self._build([_entry(s) for s in (23900, 23950, 24000, 24050, 24100)])
        assert len(rows) == 5
        row = next(r for r in rows if r["strike"] == 24000.0)
        assert row["underlying"] == "NIFTY"
        assert row["underlying_class"] == "INDEX"
        assert row["expiry_class"] == "MONTHLY"
        assert row["moneyness"] == "ATM"
        assert row["eligibility_status"] == ELIGIBLE
        assert row["spot"] == pytest.approx(24000.0)
        assert row["chain_quote_age_seconds"] == pytest.approx(30.0)
        assert row["features"]["india_vix"] == pytest.approx(13.2)
        assert row["features"]["max_pain"] == pytest.approx(24000.0)
        assert row["is_selected"] is False

    def test_rejected_contracts_are_stored_with_their_reason(self):
        rows = self._build(
            [_entry(23950), _entry(24000), _entry(24050, bid=None, ask=None)]
        )
        statuses = {r["strike"]: r["eligibility_status"] for r in rows}
        assert statuses[24000.0] == ELIGIBLE
        assert statuses[24050.0] == REJECT_NO_QUOTE
        # Stored, not dropped — the whole envelope is present.
        assert len(rows) == 3

    def test_contracts_outside_the_envelope_are_not_captured(self):
        # 25000 is 20 rungs from a 24000 spot on a 50-wide ladder.
        rows = self._build(
            [_entry(s) for s in (23950, 24000, 24050, 25000)], max_moneyness_steps=8.0
        )
        assert [r["strike"] for r in rows] == [23950.0, 24000.0, 24050.0]

    def test_missing_fields_are_named_never_zero_filled(self):
        rows = self._build([_entry(24000, iv=None, delta=None, oi=None)])
        missing = rows[0]["missing_fields"]
        assert "iv" in missing and "delta" in missing and "oi" in missing
        assert rows[0]["iv"] is None
        assert rows[0]["oi"] is None
        # v1 does not capture underlying momentum; that is stated in the data.
        assert "underlying_momentum_not_captured_in_v1" in missing

    def test_absent_vix_is_recorded_with_its_reason(self):
        rows = build_rows(
            payload=_payload([_entry(24000)]),
            underlying="NIFTY",
            expiry=date(2026, 8, 25),
            listed_expiries=LISTED,
            decision_id="11111111-1111-1111-1111-111111111111",
            captured_at=datetime(2026, 8, 25, 9, 0, 30, tzinfo=UTC),
            vix=None,
            vix_missing_reason="vix_source_returned_none",
        )
        assert rows[0]["features"]["india_vix"] is None
        assert rows[0]["features"]["india_vix_unavailable"] == "vix_source_returned_none"
        assert "india_vix" in rows[0]["missing_fields"]

    def test_liquidity_is_ranked_within_the_chain(self):
        rows = self._build(
            [
                _entry(23950, oi=10, volume=10),
                _entry(24000, oi=500000, volume=400000),
                _entry(24050, oi=1000, volume=1000),
            ]
        )
        by_strike = {r["strike"]: r["liquidity_bucket"] for r in rows}
        assert by_strike[24000.0] == "TOP"
        assert by_strike[23950.0] == "LOW"

    def test_empty_chain_produces_no_contract_rows(self):
        assert self._build([]) == []


class TestNoTradeRow:
    def test_shape_is_a_real_candidate_with_a_null_contract(self):
        row = build_no_trade_row(
            underlying="NIFTY",
            decision_id="22222222-2222-2222-2222-222222222222",
            captured_at=datetime(2026, 8, 25, 9, 0, tzinfo=UTC),
            features={"india_vix": 13.2},
        )
        assert row["option_type"] == NO_TRADE
        assert row["strike"] is None
        assert row["underlying_class"] == "INDEX"
        assert row["eligibility_status"] == ELIGIBLE
        assert row["features"]["india_vix"] == pytest.approx(13.2)

    def test_records_why_a_cycle_found_nothing(self):
        row = build_no_trade_row(
            underlying="NIFTY",
            decision_id="22222222-2222-2222-2222-222222222222",
            captured_at=datetime(2026, 8, 25, 9, 0, tzinfo=UTC),
            missing_fields=["option_chain"],
            reason="no chain payload was cached for any in-window expiry",
        )
        assert "option_chain" in row["missing_fields"]
        assert row["eligibility_reason"]


class TestChainFeatures:
    def test_reuses_the_service_analytics_and_omits_absent_keys(self):
        features = chain_features({"pcr_oi": 0.9, "max_pain": 24000.0})
        assert features == {"pcr_oi": 0.9, "max_pain": 24000.0}
        assert "atm_iv" not in features


class TestReadOnlySafetyBoundary:
    """The observer must never be able to reach an order path.

    Enforced on the import list because nothing else enforces it: there is no
    central paper/live gate in this codebase.
    """

    FORBIDDEN_PREFIXES = (
        "live_engine",
        "paper_engine",
        "brokers",
        "api.routers.trading",
    )

    def _imported_modules(self, path: Path) -> set[str]:
        tree = ast.parse(path.read_text())
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                modules.add(node.module)
        return modules

    def test_no_module_imports_an_order_path(self):
        offenders: dict[str, set[str]] = {}
        for path in sorted(CAPTURE_PACKAGE.glob("*.py")):
            bad = {
                module
                for module in self._imported_modules(path)
                if module.split(".")[0] in {p.split(".")[0] for p in self.FORBIDDEN_PREFIXES}
                and any(module == p or module.startswith(p + ".") for p in self.FORBIDDEN_PREFIXES)
            }
            if bad:
                offenders[path.name] = bad
        assert not offenders, (
            f"candidate_capture must stay read-only but imports order paths: {offenders}. "
            "There is no central allow_live_orders switch — this import list IS the guarantee."
        )

    def test_no_order_submission_call_appears_anywhere(self):
        for path in sorted(CAPTURE_PACKAGE.glob("*.py")):
            source = path.read_text()
            for forbidden in ("place_order", "exit_positions", "modify_order", "cancel_order"):
                assert forbidden not in source, f"{path.name} references {forbidden}"
