"""Strategy 1 entry scanning and position-open helpers."""
from __future__ import annotations

import uuid
from datetime import date
from typing import Any, Optional, TYPE_CHECKING

from loguru import logger
from sqlalchemy import text

from analysis.macd_engine import check_iv_filter, compute_ema, compute_macd, compute_spot_ma_context
from agent.macd_quadrant import compute_quadrant
from agent.strategy_config import (
    COMMENTARY_MAX,
    EXCLUDED_UNDERLYINGS,
    HARD_MAX_IV_PCT,
    KELLY_CAUTIOUS_FRACTION,
    KELLY_FRACTION,
    KELLY_PREMIUM_FRACTION,
    MACD_FAST,
    MACD_MIN_BARS,
    MACD_SIGNAL,
    MACD_SLOW,
    MAX_ENTRY_IV_PCT,
    MAX_PREMIUM,
    MAX_SIMULTANEOUS_POSITIONS,
    MIN_PREMIUM,
    MIN_TTE_DAYS,
    OPTION_ENTRY_MA_FAST,
    OPTION_ENTRY_MA_SLOW,
    OPTION_ENTRY_REQUIRE_ABOVE_MA20,
    REGIME_BEARISH,
    REGIME_BULLISH,
    REGIME_DEAD,
    SETUP_BREAKOUT,
    SETUP_PREMIUM,
)
from agent.window_calculator import days_remaining_in_window
from analytics.technicals import latest_macd_rsi
from market_data import market_profile_builder, option_history_service
from db.database import AsyncSessionLocal
from paper_engine.base_strategy_agent import _latest_session_rows, _now_ist, _round_or_none
from paper_engine.portfolio import PaperPortfolio
from paper_engine.strategy_agent_state import StrategyEvent, StrategyPosition, StrategyRuntime

if TYPE_CHECKING:
    from paper_engine.strategy_agent import PaperStrategyAgent
    from paper_engine.strategy_agent import detect_macd_zero_cross


class StrategyEntryMixin:
    max_positions = MAX_SIMULTANEOUS_POSITIONS

    async def _build_strategy1_market_profile_gate(
        self: "PaperStrategyAgent",
        underlying: str,
        expected_direction: str,
    ) -> dict[str, Any]:
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
            "1minute" if spot_source in {"upstox", "fyers"} else "30minute",
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
        capacity = self.max_positions - len(runtime.positions)
        if capacity <= 0:
            self._append_commentary(runtime.label, "Position cap reached. Managing exits only.", tone="warning")
            return

        candidates: list[dict[str, Any]] = []

        for row in rows:
            underlying = row.get("underlying", "")
            expiry_str = row.get("expiry", "")

            if underlying in EXCLUDED_UNDERLYINGS:
                continue

            window = window_map.get(underlying)
            if not window:
                continue

            tte = days_remaining_in_window(window, as_of=_now_ist().date())
            if tte < MIN_TTE_DAYS:
                continue

            if expiry_str:
                try:
                    opt_expiry = date.fromisoformat(expiry_str)
                    if (opt_expiry - _now_ist().date()).days < 3:
                        continue
                except (ValueError, TypeError):
                    pass

            if self._has_underlying_position(runtime, underlying):
                continue

            ce_side = row.get("ce")
            pe_side = row.get("pe")
            if not ce_side or not pe_side:
                continue

            ce_candles = await self._load_candles(row, ce_side)
            pe_candles = await self._load_candles(row, pe_side)

            ce_closes = [float(c["close"]) for c in ce_candles if c.get("close")] if ce_candles else []
            pe_closes = [float(c["close"]) for c in pe_candles if c.get("close")] if pe_candles else []

            quadrant = compute_quadrant(
                ce_closes,
                pe_closes,
                underlying=underlying,
                expiry=expiry_str,
            )
            self._regime_cache[underlying] = quadrant
            if quadrant.regime == REGIME_DEAD:
                continue

            expected_mp_direction: Optional[str] = None
            if quadrant.regime == REGIME_BULLISH and quadrant.ce_has_zero_cross:
                side = ce_side
                candles = ce_candles
                closes = ce_closes
                opt_type = "CE"
                expected_mp_direction = "CE"
            elif quadrant.regime == REGIME_BEARISH and quadrant.pe_has_zero_cross:
                side = pe_side
                candles = pe_candles
                closes = pe_closes
                opt_type = "PE"
                expected_mp_direction = "PE"
            else:
                continue

            if len(closes) < MACD_MIN_BARS:
                continue

            from paper_engine.strategy_agent import detect_macd_zero_cross

            should_enter, strength, reason = detect_macd_zero_cross(closes, opt_type)
            if not should_enter or not reason:
                continue

            live_ltp = float(side.get("ltp") or 0.0)
            if live_ltp > 0:
                candle_close = closes[-1]
                if abs(candle_close - live_ltp) / max(live_ltp, 1.0) > 0.15:
                    logger.warning(
                        f"[Strategy] Stale candle detected for {underlying} {opt_type}: "
                        f"candle_close={candle_close:.2f} vs live_ltp={live_ltp:.2f}. "
                        "Using live LTP as entry basis."
                    )
                latest_close = live_ltp
            elif closes:
                latest_close = closes[-1]
            else:
                continue

            if latest_close < MIN_PREMIUM or latest_close > MAX_PREMIUM:
                continue

            option_ma20 = compute_ema(closes, OPTION_ENTRY_MA_FAST)[-1] if len(closes) >= OPTION_ENTRY_MA_FAST else None
            option_ma50 = compute_ema(closes, OPTION_ENTRY_MA_SLOW)[-1] if len(closes) >= OPTION_ENTRY_MA_SLOW else None
            above_option_ma20 = bool(option_ma20 is not None and latest_close >= option_ma20)
            above_option_ma50 = bool(option_ma50 is not None and latest_close >= option_ma50)
            if OPTION_ENTRY_REQUIRE_ABOVE_MA20 and not above_option_ma20:
                continue

            latest_candle = candles[-1] if candles else {}
            iv_pct = None
            iv_raw = side.get("iv") or latest_candle.get("iv")
            if iv_raw is not None:
                iv_val = float(iv_raw)
                iv_pct = iv_val * 100.0 if iv_val < 1.0 else iv_val
            iv_status = check_iv_filter(iv_pct, MAX_ENTRY_IV_PCT, HARD_MAX_IV_PCT)
            if iv_status == "reject":
                continue

            latest_bar_time = str(candles[-1]["time"]) if candles else ""
            signal_key = f"{underlying}:{opt_type}"
            if runtime.processed_signals.get(signal_key) == latest_bar_time:
                continue

            spot_context = await self._compute_spot_context(underlying, window)
            setup = spot_context.get("setup", "unknown")
            mp_gate = await self._build_strategy1_market_profile_gate(underlying, expected_mp_direction)
            if not mp_gate.get("confirmed"):
                continue

            macd_line, _, _ = compute_macd(closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
            indicators = latest_macd_rsi(closes)
            candidates.append(
                {
                    "row": row,
                    "side": side,
                    "candles": candles,
                    "closes": closes,
                    "latest_close": latest_close,
                    "latest_bar_time": latest_bar_time,
                    "signal_key": signal_key,
                    "strength": strength or 0.0,
                    "reason": reason,
                    "rsi": indicators.get("rsi"),
                    "opt_type": opt_type,
                    "iv_pct": iv_pct,
                    "iv_status": iv_status,
                    "spot_setup": setup,
                    "option_ma20": _round_or_none(option_ma20, 2),
                    "option_ma50": _round_or_none(option_ma50, 2),
                    "above_option_ma20": above_option_ma20,
                    "above_option_ma50": above_option_ma50,
                    "quadrant": quadrant,
                    "window": window,
                    "tte_days": tte,
                    "macd_line": macd_line,
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

        setup_rank = {SETUP_PREMIUM: 0, SETUP_BREAKOUT: 1, "trend": 2, "reversal": 3, "unknown": 4}
        candidates.sort(
            key=lambda c: (
                setup_rank.get(c["spot_setup"], 4),
                c.get("iv_pct") or 999,
                -(c["strength"] or 0),
            )
        )

        if self._kill_switch_active:
            if candidates:
                self._append_commentary(
                    runtime.label,
                    f"NSE kill switch active. {len(candidates)} candidate signals observed, but new entries are blocked.",
                    tone="warning",
                )
            return

        opened = 0
        for candidate in candidates[:capacity]:
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

        indicators = latest_macd_rsi(candidate["closes"])
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
