"""Strategy 1 entry scanning and position-open helpers."""
from __future__ import annotations

import uuid
from datetime import date
from types import SimpleNamespace
from typing import Any, Optional, TYPE_CHECKING

from loguru import logger
from sqlalchemy import text

from analysis.macd_engine import check_iv_filter, compute_spot_ma_context
from agent.iv_size_policy import iv_size_scaler
from agent.strategy_config import (
    COMMENTARY_MAX,
    EXCLUDED_UNDERLYINGS,
    HARD_MAX_IV_PCT,
    KELLY_CAUTIOUS_FRACTION,
    KELLY_FRACTION,
    KELLY_PREMIUM_FRACTION,
    MAX_ENTRY_IV_PCT,
    MAX_PREMIUM,
    MAX_SIMULTANEOUS_POSITIONS,
    MIN_PREMIUM,
    MIN_TTE_DAYS,
    REGIME_BEARISH,
    REGIME_BULLISH,
    SETUP_BREAKOUT,
    SETUP_PREMIUM,
)
from agent.window_calculator import days_remaining_in_window, trading_days_remaining
from analytics.technicals import latest_macd_rsi
from core.config import settings
from market_data import market_profile_builder, option_history_service
from db.database import AsyncSessionLocal
from paper_engine.base_strategy_agent import _latest_session_rows, _now_ist, _parse_iso_timestamp, _round_or_none
from paper_engine.portfolio import PaperPortfolio
from paper_engine.strategy_learning import strategy_learning_service
from paper_engine.strategy_agent_state import StrategyEvent, StrategyPosition, StrategyRuntime

if TYPE_CHECKING:
    from paper_engine.strategy_agent import PaperStrategyAgent
    from paper_engine.strategy_agent import detect_macd_zero_cross


def _data_quality_observation_block_reason(
    *,
    symbol: str,
    source: str,
    observed_at: str,
) -> Optional[str]:
    if not settings.DATA_QUALITY_SCAN_GATE_ENABLED:
        return None
    symbol = str(symbol or "").strip()
    observed = _parse_iso_timestamp(observed_at)
    try:
        from market_data.data_quality_agent import data_quality_agent

        verdict = data_quality_agent.assess_observation(
            symbol=symbol,
            source=source,
            observed_at=observed,
            now=_now_ist(),
        )
    except Exception as exc:
        return f"Data quality gate could not verify {symbol or 'option snapshot'}: {exc}"
    if verdict.stale:
        return verdict.reason or f"Data quality gate blocked stale {source} for {symbol}."
    return None


class StrategyEntryMixin:
    max_positions = MAX_SIMULTANEOUS_POSITIONS

    async def _load_strategy1_recent_snapshot_state(
        self: "PaperStrategyAgent",
        rows: list[dict[str, Any]],
    ) -> dict[str, dict[int, dict[str, Any]]]:
        instrument_keys = sorted(
            {
                str((side or {}).get("instrument_key") or "").strip()
                for row in rows
                for side in (row.get("ce"), row.get("pe"))
                if str((side or {}).get("instrument_key") or "").strip()
            }
        )
        if not instrument_keys:
            return {}

        async with AsyncSessionLocal() as session:
            trading_day = await session.scalar(
                text(
                    """
                    SELECT MAX(timezone('Asia/Kolkata', time)::date)
                    FROM atm_option_watchlist_snapshots
                    WHERE instrument_key = ANY(:instrument_keys)
                    """
                ),
                {"instrument_keys": instrument_keys},
            )
            if trading_day is None:
                return {}

            result = await session.execute(
                text(
                    """
                    WITH ranked AS (
                        SELECT *,
                               ROW_NUMBER() OVER (
                                   PARTITION BY instrument_key
                                   ORDER BY macd_bucket DESC
                               ) AS rn
                        FROM (
                            SELECT instrument_key,
                                   option_type,
                                   time,
                                   (
                                       date_trunc('hour', timezone('Asia/Kolkata', time))
                                       + (
                                           floor(date_part('minute', timezone('Asia/Kolkata', time)) / 30)::int
                                           * interval '30 minutes'
                                       )
                                   ) AS macd_bucket,
                                   macd,
                                   macd_signal,
                                   macd_histogram,
                                   rsi,
                                   ltp,
                                   ROW_NUMBER() OVER (
                                       PARTITION BY instrument_key,
                                                    (
                                                        date_trunc('hour', timezone('Asia/Kolkata', time))
                                                        + (
                                                            floor(date_part('minute', timezone('Asia/Kolkata', time)) / 30)::int
                                                            * interval '30 minutes'
                                                        )
                                                    )
                                       ORDER BY time DESC
                                   ) AS bucket_rn
                            FROM atm_option_watchlist_snapshots
                            WHERE timezone('Asia/Kolkata', time)::date = :trading_day
                              AND instrument_key = ANY(:instrument_keys)
                              AND macd IS NOT NULL
                        ) bucketed
                        WHERE bucket_rn = 1
                    )
                    SELECT instrument_key,
                           option_type,
                           time,
                           macd_bucket,
                           macd,
                           macd_signal,
                           macd_histogram,
                           rsi,
                           ltp,
                           rn
                    FROM ranked
                    WHERE rn <= 6
                    ORDER BY instrument_key ASC, rn ASC
                    """
                ),
                {
                    "trading_day": trading_day,
                    "instrument_keys": instrument_keys,
                },
            )
            rows = result.mappings().all()

        state: dict[str, dict[int, dict[str, Any]]] = {}
        for row in rows:
            instrument_key = str(row.get("instrument_key") or "").strip()
            if not instrument_key:
                continue
            try:
                rank = int(row.get("rn") or 0)
            except (TypeError, ValueError):
                continue
            if rank <= 0:
                continue
            state.setdefault(instrument_key, {})[rank] = dict(row)
        return state

    @staticmethod
    def _strategy1_side_snapshot(
        side: dict[str, Any],
        snapshot_state: dict[str, dict[int, dict[str, Any]]],
    ) -> dict[str, Any]:
        instrument_key = str(side.get("instrument_key") or "").strip()
        ranked = snapshot_state.get(instrument_key, {})
        latest = ranked.get(1) or {}
        previous = {}
        latest_bucket = latest.get("macd_bucket")
        for rank in sorted(key for key in ranked if key > 1):
            candidate = ranked.get(rank) or {}
            if candidate.get("macd") is None:
                continue
            if latest_bucket is not None and candidate.get("macd_bucket") == latest_bucket:
                continue
            previous = candidate
            break

        current_macd = side.get("macd")
        if current_macd is None:
            current_macd = latest.get("macd")
        current_signal = side.get("macd_signal")
        if current_signal is None:
            current_signal = latest.get("macd_signal")
        current_histogram = side.get("macd_histogram")
        if current_histogram is None:
            current_histogram = latest.get("macd_histogram")
        current_rsi = side.get("rsi")
        if current_rsi is None:
            current_rsi = latest.get("rsi")
        latest_ltp = side.get("ltp")
        if latest_ltp in (None, 0, 0.0):
            latest_ltp = latest.get("ltp")

        latest_time = latest.get("time")
        latest_time_iso = latest_time.isoformat() if hasattr(latest_time, "isoformat") else str(latest_time or "")

        return {
            "instrument_key": instrument_key or None,
            "current_macd": float(current_macd) if current_macd is not None else None,
            "previous_macd": float(previous.get("macd")) if previous.get("macd") is not None else None,
            "current_signal": float(current_signal) if current_signal is not None else None,
            "current_histogram": float(current_histogram) if current_histogram is not None else None,
            "current_rsi": float(current_rsi) if current_rsi is not None else None,
            "latest_ltp": float(latest_ltp) if latest_ltp is not None else 0.0,
            "latest_bar_time": latest_time_iso,
            "latest_macd_bucket": str(latest_bucket or ""),
            "previous_macd_bucket": str(previous.get("macd_bucket") or ""),
        }

    async def _build_strategy1_market_profile_gate(
        self: "PaperStrategyAgent",
        underlying: str,
        expected_direction: str,
    ) -> dict[str, Any]:
        if settings.NSE_STRATEGY_BYPASS_MARKET_PROFILE_GATE:
            return {
                "confirmed": True,
                "direction": expected_direction,
                "day_type": "bypassed",
                "reason": "market_profile_gate_bypassed",
                "source": "bypass",
                "session_date": _now_ist().date().isoformat(),
                "poc": None,
                "vah": None,
                "val": None,
            }

        started_at = _now_ist()
        spot_rows: list[dict[str, Any]] = []
        spot_source = "spot_store"
        async with AsyncSessionLocal() as session:
            db_rows = (
                await session.execute(
                    text(
                        """
                        SELECT time, open, high, low, close, volume
                        FROM underlying_spot_candles
                        WHERE underlying = :underlying
                          AND interval = '30minute'
                          AND time::date BETWEEN (:from_day)::date AND (:to_day)::date
                        ORDER BY time ASC
                        """
                    ),
                    {
                        "underlying": underlying,
                        "from_day": started_at.date() - date.resolution,
                        "to_day": started_at.date(),
                    },
                )
            ).mappings().all()
        if db_rows:
            spot_rows = [dict(row) for row in db_rows]
        else:
            spot_rows, spot_source = await self._load_strategy2_spot_rows(underlying, started_at)
        session_rows, session_date = _latest_session_rows(spot_rows)
        if len(session_rows) < 4:
            return {
                "confirmed": False,
                "direction": None,
                "day_type": "pending",
                "reason": "mp_insufficient_rows",
                "source": spot_source,
                "session_date": session_date.isoformat() if session_date else None,
            }

        profile = market_profile_builder.build_profile_from_rows(
            underlying,
            session_rows,
            "day",
            "30minute" if spot_source == "spot_store" else "1minute",
        )
        if not profile:
            return {
                "confirmed": False,
                "direction": None,
                "day_type": "pending",
                "reason": "mp_profile_unavailable",
                "source": spot_source,
                "session_date": session_date.isoformat() if session_date else None,
            }

        current_spot = float(session_rows[-1].get("close") or 0.0)
        direction, day_type, gate_reason = self._classify_strategy2_market_profile(
            profile=profile,
            current_spot=current_spot,
            today_rows=session_rows,
        )
        return {
            "confirmed": direction == expected_direction,
            "direction": direction,
            "day_type": day_type,
            "reason": gate_reason,
            "source": spot_source,
            "session_date": session_date.isoformat() if session_date else None,
            "poc": _round_or_none(getattr(profile, "poc", None), 2),
            "vah": _round_or_none(getattr(profile, "vah", None), 2),
            "val": _round_or_none(getattr(profile, "val", None), 2),
        }

    async def _scan_entries(
        self: "PaperStrategyAgent",
        runtime: StrategyRuntime,
        rows: list[dict[str, Any]],
        window_map: dict[str, dict],
    ) -> None:
        capacity = max(self.max_positions - len(runtime.positions), 0)
        cap_reached = capacity <= 0

        candidates: list[dict[str, Any]] = []
        snapshot_state = await self._load_strategy1_recent_snapshot_state(rows)
        persist_observation = getattr(self, "_persist_agent_signal_observation", None)

        for row in rows:
            underlying = row.get("underlying", "")
            expiry_str = row.get("expiry", "")

            if underlying in EXCLUDED_UNDERLYINGS:
                continue

            window = window_map.get(underlying)
            if not window:
                continue

            tte_calendar = days_remaining_in_window(window, as_of=_now_ist().date())
            tte_trading = trading_days_remaining(window, as_of=_now_ist().date())
            # Kind-aware TTE threshold, measured in *trading* days
            # (Mon–Fri) so a Friday-with-Monday-expiry doesn't masquerade
            # as 3 days remaining.
            #   INDEX → ≥5 trading days on the active expiry
            #   STOCK → ≤3 trading days triggers a rollover to next
            #           expiry. Active-expiry candidate is skipped and
            #           a rollover_candidate marker is recorded; the
            #           next pipeline tick picks up the rolled contract
            #           once window_calculator promotes it.
            kind = str(row.get("kind") or "").upper()
            from agent.strategy_config import MIN_TTE_DAYS_INDEX, MIN_TTE_DAYS_STOCK
            tte_threshold = MIN_TTE_DAYS_INDEX if kind == "INDEX" else MIN_TTE_DAYS_STOCK
            if tte_trading <= tte_threshold:
                next_expiry_meta = row.get("next_expiry") or {}
                next_expiry_iso = (
                    next_expiry_meta.get("expiry")
                    if isinstance(next_expiry_meta, dict)
                    else None
                )
                if kind == "STOCK":
                    await persist_raw_signal(
                        "rollover_candidate",
                        (
                            f"tte_{tte_trading}td_at_or_below_{tte_threshold}td"
                            + (f"_roll_to_{next_expiry_iso}" if next_expiry_iso else "_await_next_expiry")
                        ),
                        ltp=None,
                    )
                continue
            tte = tte_calendar  # retained downstream for legacy uses

            if expiry_str:
                try:
                    opt_expiry = date.fromisoformat(expiry_str)
                    if (opt_expiry - _now_ist().date()).days < 3:
                        continue
                except (ValueError, TypeError):
                    pass

            ce_side = row.get("ce")
            pe_side = row.get("pe")
            if not ce_side or not pe_side:
                continue

            ce_snapshot = self._strategy1_side_snapshot(ce_side, snapshot_state)
            pe_snapshot = self._strategy1_side_snapshot(pe_side, snapshot_state)
            ce_macd = ce_snapshot.get("current_macd")
            pe_macd = pe_snapshot.get("current_macd")
            if ce_macd is None or pe_macd is None:
                continue

            ce_cross = ce_snapshot.get("previous_macd") is not None and ce_snapshot["previous_macd"] <= 0 < ce_macd
            pe_cross = pe_snapshot.get("previous_macd") is not None and pe_snapshot["previous_macd"] >= 0 > pe_macd
            if ce_cross:
                side = ce_side
                side_snapshot = ce_snapshot
                opt_type = "CE"
                regime_name = REGIME_BULLISH
                expected_mp_direction = "CE"
            elif pe_cross:
                side = pe_side
                side_snapshot = pe_snapshot
                opt_type = "PE"
                regime_name = REGIME_BEARISH
                expected_mp_direction = "PE"
            else:
                continue
            quadrant = SimpleNamespace(regime=regime_name)
            self._regime_cache[underlying] = quadrant

            latest_bar_time = str(side_snapshot.get("latest_bar_time") or "")
            if not latest_bar_time:
                continue

            strength = abs(float(side_snapshot.get("current_macd") or 0.0))
            reason = "macd_zero_cross"
            signal_key = f"{underlying}:{opt_type}"

            async def persist_raw_signal(status_value: str, block_reason: Optional[str] = None, **extra: Any) -> None:
                if not callable(persist_observation):
                    return
                side_for_signal = side or {}
                payload = {
                    "strategy": "Strategy 1",
                    "source": "live_scan_raw_macd",
                    "signal_key": f"{runtime.key}:raw_macd_cross:{signal_key}:{latest_bar_time}",
                    "underlying": underlying,
                    "signal_date": latest_bar_time[:10],
                    "as_of": latest_bar_time,
                    "direction": opt_type,
                    "reason": block_reason or reason,
                    "strength": strength,
                    "status": status_value,
                    "freshness": "live",
                    "instruction": (
                        f"{underlying}: {opt_type} raw MACD zero-cross observed; "
                        "filter outcome is stored in metadata."
                    ),
                    "expiry": row.get("expiry"),
                    "atm_strike": side_for_signal.get("strike"),
                    "ltp": extra.get("ltp", side_snapshot.get("latest_ltp")),
                    "iv_pct": extra.get("iv_pct"),
                    "tte_days": tte,
                    "spot_setup": extra.get("spot_setup"),
                    "regime": regime_name,
                    "option_last_bar_time": latest_bar_time,
                    "previous_macd": side_snapshot.get("previous_macd"),
                    "current_macd": side_snapshot.get("current_macd"),
                    "ce_macd": ce_macd,
                    "pe_macd": pe_macd,
                    "ce_cross": ce_cross,
                    "pe_cross": pe_cross,
                    "latest_macd_bucket": side_snapshot.get("latest_macd_bucket"),
                    "previous_macd_bucket": side_snapshot.get("previous_macd_bucket"),
                    **extra,
                }
                await persist_observation(
                    runtime,
                    payload,
                    status=status_value,
                    row=row,
                )

            await persist_raw_signal("observed", raw_stage="pre_filter")

            data_quality_block = _data_quality_observation_block_reason(
                symbol=str(side.get("instrument_key") or side.get("trading_symbol") or signal_key),
                source="option_history_30m",
                observed_at=latest_bar_time,
            )
            if data_quality_block:
                await persist_raw_signal(
                    "blocked",
                    "data_stale",
                    freshness="stale",
                    data_quality_reason=data_quality_block,
                )
                continue

            if self._has_underlying_position(runtime, underlying):
                await persist_raw_signal("blocked", "existing_underlying_position")
                continue

            if cap_reached:
                await persist_raw_signal("blocked", "position_cap_reached")
                continue

            live_ltp = float(side_snapshot.get("latest_ltp") or side.get("ltp") or 0.0)
            if live_ltp > 0:
                latest_close = live_ltp
            else:
                await persist_raw_signal("blocked", "missing_ltp")
                continue

            # No premium price filter. We trade ATM only — the ATM
            # contract on a live F&O underlying is liquid by
            # construction, so absolute / relative premium bands don't
            # add signal here.

            option_ma20 = None
            option_ma50 = None
            above_option_ma20 = True
            above_option_ma50 = True

            iv_pct = None
            iv_raw = side.get("iv")
            if iv_raw is not None:
                iv_val = float(iv_raw)
                iv_pct = iv_val * 100.0 if iv_val < 1.0 else iv_val
            # New IV policy: relative-to-market spread drives a size
            # scaler in (0.25 .. 1.0]. We only reject when the IV is
            # implausibly high (>90% — broker data sanity check).
            market_iv_pct = float(
                snapshot_state.get("market_iv_pct")
                or snapshot_state.get("market_iv")
                or 0.0
            ) or None
            iv_scaler, iv_note = iv_size_scaler(iv_pct, market_iv_pct)
            if iv_scaler <= 0:
                await persist_raw_signal("blocked", f"iv_{iv_note}", ltp=latest_close, iv_pct=iv_pct)
                continue
            # Map scaler back into the legacy iv_status tag so the
            # downstream sizing function (which still keys off the
            # "preferred"/"acceptable"/"reject" buckets) keeps working.
            iv_status = (
                "preferred" if iv_scaler >= 0.95
                else "acceptable" if iv_scaler >= 0.65
                else "cautious"
            )

            if runtime.processed_signals.get(signal_key) == latest_bar_time:
                await persist_raw_signal("blocked", "already_processed", ltp=latest_close, iv_pct=iv_pct)
                continue

            spot_context = await self._compute_spot_context(underlying, window)
            setup = spot_context.get("setup", "unknown")
            mp_gate = await self._build_strategy1_market_profile_gate(underlying, expected_mp_direction)
            if not mp_gate.get("confirmed"):
                await persist_raw_signal(
                    "blocked",
                    f"mp_gate_{mp_gate.get('reason') or 'not_confirmed'}",
                    ltp=latest_close,
                    iv_pct=iv_pct,
                    spot_setup=setup,
                    mp_day_type=mp_gate.get("day_type"),
                    mp_direction=mp_gate.get("direction"),
                    mp_reason=mp_gate.get("reason"),
                )
                continue

            candidates.append(
                {
                    "row": row,
                    "side": side,
                    "candles": [],
                    "closes": [],
                    "latest_close": latest_close,
                    "latest_bar_time": latest_bar_time,
                    "signal_key": signal_key,
                    "strength": strength or 0.0,
                    "reason": reason,
                    "rsi": side_snapshot.get("current_rsi"),
                    "opt_type": opt_type,
                    "iv_pct": iv_pct,
                    "iv_status": iv_status,
                    # New relative-IV policy: scaler in (0, 1] multiplies
                    # the Kelly-sized lot count downstream. iv_size_note
                    # records the bucket (normal/caution/heavy/extreme)
                    # so the audit trail explains the size reduction.
                    "iv_size_scaler": iv_scaler,
                    "iv_size_note": iv_note,
                    "market_iv_pct": market_iv_pct,
                    "spot_setup": setup,
                    "option_ma20": _round_or_none(option_ma20, 2),
                    "option_ma50": _round_or_none(option_ma50, 2),
                    "above_option_ma20": above_option_ma20,
                    "above_option_ma50": above_option_ma50,
                    "quadrant": quadrant,
                    "window": window,
                    "tte_days": tte,
                    "macd_line": [value for value in (side_snapshot.get("previous_macd"), side_snapshot.get("current_macd")) if value is not None],
                    "macd_signal": side_snapshot.get("current_signal"),
                    "macd_histogram": side_snapshot.get("current_histogram"),
                    "mp_day_type": mp_gate.get("day_type"),
                    "mp_reason": mp_gate.get("reason"),
                    "mp_direction": mp_gate.get("direction"),
                    "mp_source": mp_gate.get("source"),
                    "mp_session_date": mp_gate.get("session_date"),
                    "mp_poc": mp_gate.get("poc"),
                    "mp_vah": mp_gate.get("vah"),
                    "mp_val": mp_gate.get("val"),
                }
            )

        learning_scores = await strategy_learning_service.load_scores(runtime.key)
        for candidate in candidates:
            row = candidate["row"]
            score = strategy_learning_service.pick_score(
                learning_scores,
                strategy_key=runtime.key,
                underlying=str(row.get("underlying") or ""),
                option_type=str(candidate.get("opt_type") or ""),
                signal_reason=str(candidate.get("reason") or ""),
            )
            strategy_learning_service.annotate_payload(candidate, score)

        setup_rank = {SETUP_PREMIUM: 0, SETUP_BREAKOUT: 1, "trend": 2, "reversal": 3, "unknown": 4}
        candidates.sort(
            key=lambda c: (
                setup_rank.get(c["spot_setup"], 4),
                -(float(c.get("learning_score") or 0.0)),
                c.get("iv_pct") or 999,
                -(c["strength"] or 0),
            )
        )
        if callable(persist_observation):
            for candidate in candidates:
                row = candidate["row"]
                side = candidate["side"]
                await persist_observation(
                    runtime,
                    {
                        "strategy": "Strategy 1",
                        "source": "live_scan",
                        "signal_key": f"{runtime.key}:{candidate['signal_key']}:{candidate['latest_bar_time']}",
                        "underlying": row.get("underlying"),
                        "signal_date": str(row.get("time") or candidate["latest_bar_time"])[:10],
                        "as_of": str(candidate["latest_bar_time"]),
                        "direction": candidate.get("opt_type"),
                        "reason": candidate.get("reason"),
                        "strength": candidate.get("strength"),
                        "status": "entry-ready",
                        "freshness": "live",
                        "instruction": (
                            f"{row.get('underlying')}: {candidate.get('opt_type')} zero-cross passed "
                            "Strategy 1 filters; paper entry depends on capacity and risk state."
                        ),
                        "expiry": row.get("expiry"),
                        "atm_strike": side.get("strike"),
                        "ltp": candidate.get("latest_close"),
                        "iv_pct": candidate.get("iv_pct"),
                        "tte_days": candidate.get("tte_days"),
                        "spot_setup": candidate.get("spot_setup"),
                        "regime": getattr(candidate.get("quadrant"), "regime", None),
                        "mp_day_type": candidate.get("mp_day_type"),
                        "option_last_bar_time": candidate.get("latest_bar_time"),
                        "learning_score": candidate.get("learning_score"),
                        "learning_confidence": candidate.get("learning_confidence"),
                        "learning_size_multiplier": candidate.get("learning_size_multiplier"),
                        "learning_risk_multiplier": candidate.get("learning_risk_multiplier"),
                        "learning_blocked": candidate.get("learning_blocked"),
                    },
                    status="candidate",
                    row=row,
                )

        if self._kill_switch_active:
            if candidates:
                self._append_commentary(
                    runtime.label,
                    f"NSE kill switch active. {len(candidates)} candidate signals observed, but new entries are blocked.",
                    tone="warning",
                )
            return

        tradable_candidates = [candidate for candidate in candidates if not candidate.get("learning_blocked")]
        if candidates and not tradable_candidates:
            self._append_commentary(
                runtime.label,
                "Learning risk gate blocked all current Strategy 1 candidates from opening new entries.",
                tone="warning",
            )

        opened = 0
        for candidate in tradable_candidates[:capacity]:
            await self._open_position(runtime, candidate)
            opened += 1

        if candidates:
            top = candidates[0]
            self._append_commentary(
                runtime.label,
                f"Found {len(candidates)} signals. Best: {top['row']['underlying']} "
                f"{top['opt_type']} (setup={top['spot_setup']}, IV={top.get('iv_pct') or 0:.0f}%, "
                f"regime={top['quadrant'].regime}, mp={top.get('mp_day_type')}). Opened {opened}.",
                tone="info",
            )

    async def _open_position(
        self: "PaperStrategyAgent",
        runtime: StrategyRuntime,
        candidate: dict[str, Any],
    ) -> None:
        row = candidate["row"]
        side = candidate["side"]
        latest_close = float(candidate["latest_close"])
        opt_type = candidate["opt_type"]
        window = candidate.get("window") or {}

        if latest_close <= 0:
            return

        expiry = row["expiry"]
        strike = float(side["strike"])
        symbol = self._contract_symbol(row["underlying"], expiry, strike, opt_type)
        signal_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{runtime.key}:{symbol}:{candidate['latest_bar_time']}:{candidate['reason']}",
            )
        )

        lot_size: Optional[int] = None
        if row.get("lot_size"):
            try:
                lot_size = int(row["lot_size"])
            except (TypeError, ValueError):
                pass

        if not lot_size:
            lot_size = await option_history_service.resolve_lot_size(
                underlying=row["underlying"],
                expiry=date.fromisoformat(expiry),
                strike=strike,
                option_type=opt_type,
                instrument_key=side.get("instrument_key"),
            )

        if not lot_size:
            logger.warning(
                f"[Strategy] lot_size unknown for {row['underlying']} — using DEFAULT_LOT_SIZE. "
                "Check Upstox contract sync or broker connectivity."
            )
            lot_size = PaperPortfolio.DEFAULT_LOT_SIZE

        fraction_override = candidate.get("fraction_override")
        if fraction_override is not None:
            fraction = float(fraction_override)
        else:
            sizing_mode = self._get_sizing_mode(candidate)
            if sizing_mode == "premium":
                fraction = KELLY_PREMIUM_FRACTION
            elif sizing_mode == "cautious":
                fraction = KELLY_CAUTIOUS_FRACTION
            else:
                fraction = KELLY_FRACTION
        learning_size_multiplier = float(candidate.get("learning_size_multiplier") or 1.0)
        fraction *= max(0.5, min(1.2, learning_size_multiplier))
        # Apply the relative-IV size scaler from iv_size_policy. When
        # the instrument IV is far above market IV we still take the
        # trade — just at 0.75× / 0.50× / 0.25× of base size. This
        # replaces the old hard IV reject which threw away setups that
        # were merely paying for genuine vol.
        iv_size_scaler_value = float(candidate.get("iv_size_scaler") or 1.0)
        fraction *= max(0.25, min(1.0, iv_size_scaler_value))

        allocation = max(runtime.portfolio.total_equity * fraction, latest_close * lot_size)
        lots = max(1, int(allocation // max(latest_close * lot_size, 1.0)))
        lots = min(lots, 5)
        qty = lot_size * lots

        order = runtime.order_book.place_order(
            symbol=symbol,
            action="BUY",
            order_type="MARKET",
            qty=qty,
            instrument_type=opt_type,
            expiry=expiry,
            strike=strike,
            option_type=opt_type,
            ltp=latest_close,
            signal_id=signal_id,
            setup_type=candidate.get("spot_setup"),
            entry_iv_pct=_round_or_none(candidate.get("iv_pct"), 1),
            regime=getattr(candidate.get("quadrant"), "regime", None) or candidate.get("mp_day_type"),
        )

        fill_price = float(order.fill_price or latest_close)
        regime_label = getattr(candidate.get("quadrant"), "regime", None) or candidate.get("mp_day_type") or "n/a"
        runtime.positions[symbol] = StrategyPosition(
            signal_id=signal_id,
            symbol=symbol,
            underlying=row["underlying"],
            expiry=expiry,
            strike=strike,
            option_type=opt_type,
            instrument_key=side.get("instrument_key"),
            trading_symbol=side.get("trading_symbol"),
            qty=qty,
            initial_qty=qty,
            entry_price=fill_price,
            current_price=fill_price,
            peak_price=fill_price,
            entry_bar_time=str(candidate["latest_bar_time"]),
            entered_at=_now_ist().isoformat(),
            signal_reason=str(candidate["reason"]),
            signal_strength=_round_or_none(float(candidate["strength"]), 2),
            latest_rsi=_round_or_none(candidate.get("rsi"), 2),
            phase=self.PHASE_1,
            entry_iv_pct=_round_or_none(candidate.get("iv_pct"), 1),
            spot_setup=candidate.get("spot_setup"),
            regime=regime_label,
            option_ma20=candidate.get("option_ma20"),
            option_ma50=candidate.get("option_ma50"),
            above_option_ma20=bool(candidate.get("above_option_ma20")),
            above_option_ma50=bool(candidate.get("above_option_ma50")),
            window_end=str(window.get("window_end") or expiry),
            macd_line=candidate.get("macd_line"),
            lot_size=lot_size,
        )
        runtime.entries += 1
        runtime.processed_signals[candidate["signal_key"]] = str(candidate["latest_bar_time"])

        self._append_event(
            runtime,
            StrategyEvent(
                time=_now_ist().isoformat(),
                event="entry",
                symbol=symbol,
                underlying=row["underlying"],
                option_type=opt_type,
                strike=strike,
                price=fill_price,
                qty=qty,
                reason=str(candidate["reason"]),
                signal_strength=_round_or_none(float(candidate["strength"]), 2),
                phase=self.PHASE_1,
            ),
        )

        self._append_commentary(
            runtime.label,
            f"ENTRY {row['underlying']} {opt_type} {int(strike)} @{fill_price:.2f} | "
            f"Qty={qty} | Setup={candidate.get('spot_setup')} | "
            f"IV={candidate.get('iv_pct') or 0:.0f}% | TTE={candidate['tte_days']}d | "
            f"Regime={regime_label} | MP={candidate.get('mp_day_type')}",
            tone="trade",
        )
        await self._send_telegram_text(
            f"ENTRY | {row['underlying']} {opt_type} {int(strike)} @{fill_price:.2f}\n"
            f"Qty: {qty} | Setup: {candidate.get('spot_setup')} | "
            f"IV: {candidate.get('iv_pct') or 0:.0f}% | Regime: {regime_label} | "
            f"MP: {candidate.get('mp_day_type')}"
        )

        if candidate.get("closes"):
            indicators = latest_macd_rsi(candidate["closes"])
        else:
            indicators = {
                "macd_signal": candidate.get("macd_signal"),
                "macd_histogram": candidate.get("macd_histogram"),
                "rsi": candidate.get("rsi"),
            }
        await self._persist_macd_signal(
            underlying=row["underlying"],
            expiry=expiry,
            strike=strike,
            option_type=opt_type,
            macd_value=float(candidate["strength"] or 0),
            signal_value=indicators.get("macd_signal"),
            histogram=indicators.get("macd_histogram"),
            signal_type="zero_cross_entry",
            premium_at_signal=fill_price,
        )
        await self._persist_order(
            runtime,
            symbol,
            "BUY",
            qty,
            fill_price,
            expiry,
            strike,
            opt_type,
            str(candidate["reason"]),
        )
        await self._persist_position(runtime, runtime.positions[symbol])
        await self._persist_agent_signal(
            runtime,
            runtime.positions[symbol],
            status="open",
            metadata={
                "signal_key": candidate["signal_key"],
                "tte_days": candidate["tte_days"],
                "entry_bar_time": candidate["latest_bar_time"],
                "setup_type": candidate.get("spot_setup"),
                "entry_iv_pct": _round_or_none(candidate.get("iv_pct"), 1),
                "regime": regime_label,
                "mp_day_type": candidate.get("mp_day_type"),
                "mp_reason": candidate.get("mp_reason"),
                "mp_source": candidate.get("mp_source"),
                "mp_session_date": candidate.get("mp_session_date"),
                "learning_score": candidate.get("learning_score"),
                "learning_confidence": candidate.get("learning_confidence"),
                "learning_size_multiplier": candidate.get("learning_size_multiplier"),
                "learning_risk_multiplier": candidate.get("learning_risk_multiplier"),
            },
        )

    def _get_sizing_mode(self, candidate: dict[str, Any]) -> str:
        setup = candidate.get("spot_setup")
        iv_status = candidate.get("iv_status", "unknown")

        if setup in (SETUP_PREMIUM, SETUP_BREAKOUT) and iv_status == "preferred":
            return "premium"
        if iv_status == "acceptable" or setup == "reversal":
            return "cautious"
        return "standard"
