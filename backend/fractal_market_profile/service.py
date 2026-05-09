from __future__ import annotations

import asyncio
import csv
import gzip
import json
import math
import re
from dataclasses import asdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from time import monotonic
from typing import Any, Optional
from uuid import uuid4

import pandas as pd
from fastapi.encoders import jsonable_encoder
from loguru import logger
from sqlalchemy import text

from analytics.sector import sector_tracker
from auction_intelligence.market_profile import MarketProfileEngine
from auction_intelligence.order_flow import OrderFlowEngine
from auction_intelligence.schemas import DepthLevel, DepthSnapshot, MarketBar, QuoteSnapshot, TradePrint
from brokers.base import Tick
from core.config import settings
from db.database import AsyncSessionLocal
from fractal_market_profile.config import (
    FORCE_EXIT_TIME,
    INDEX_APP_SYMBOLS,
    IST,
    LATEST_ENTRY_TIME,
    LOT_SIZES,
    OPTION_STRIKE_STEPS,
    PAPER_ROOT,
    PROFILE_CONFIG,
    REPLAY_ROOT,
    RISK_CONFIG,
    SCAN_CONFIG,
    SESSION_CLOSE,
    SESSION_OPEN,
    SUPPORTED_SYMBOLS,
    analytics_root,
)
from fractal_market_profile.paper import FMPPaperStore
from fractal_market_profile.schemas import FMPOptionSelection, FMPReplayTrade
from market_data import atm_watchlist_service, data_router as market_data_router, market_intelligence_runtime, option_chain_service


UTC = timezone.utc
_OPTION_FILE_PATTERN = re.compile(r"(?P<underlying>[A-Z]+)\s+(?P<strike>\d+(?:\.\d+)?)\s+(?P<option_type>CE|PE)")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _to_ist(value: Any) -> datetime:
    return _ensure_dt(value).astimezone(IST)


def _iso(value: Any) -> str:
    return _ensure_dt(value).astimezone(timezone.utc).isoformat()


def _session_start(session_date: date) -> datetime:
    return datetime.combine(session_date, SESSION_OPEN, tzinfo=IST)


def _row_to_bar(row: dict[str, Any], *, timestamp_key: str = "time") -> MarketBar:
    return MarketBar(
        timestamp=_to_ist(row[timestamp_key]),
        open=float(row.get("open", row.get("close", 0.0)) or 0.0),
        high=float(row.get("high", row.get("close", 0.0)) or 0.0),
        low=float(row.get("low", row.get("close", 0.0)) or 0.0),
        close=float(row.get("close", 0.0) or 0.0),
        volume=float(row.get("volume", 0.0) or 0.0),
    )


def _aggregate_rows(rows: list[dict[str, Any]], interval_minutes: int) -> list[dict[str, Any]]:
    aggregated: list[dict[str, Any]] = []
    bucket_start: Optional[datetime] = None
    bucket: Optional[dict[str, Any]] = None

    for row in rows:
        timestamp = _to_ist(row.get("time") or row.get("timestamp"))
        session_start = _session_start(timestamp.date())
        elapsed = int((timestamp - session_start).total_seconds() // 60)
        bucket_index = max(0, elapsed // interval_minutes)
        current_bucket_start = session_start + timedelta(minutes=bucket_index * interval_minutes)

        if bucket_start != current_bucket_start:
            if bucket is not None:
                aggregated.append(bucket)
            bucket_start = current_bucket_start
            bucket = {
                "time": current_bucket_start.isoformat(),
                "open": float(row.get("open", row.get("close", 0.0)) or 0.0),
                "high": float(row.get("high", row.get("close", 0.0)) or 0.0),
                "low": float(row.get("low", row.get("close", 0.0)) or 0.0),
                "close": float(row.get("close", 0.0) or 0.0),
                "volume": float(row.get("volume", 0.0) or 0.0),
            }
            continue

        if bucket is None:
            continue

        bucket["high"] = max(float(bucket["high"]), float(row.get("high", row.get("close", 0.0)) or 0.0))
        bucket["low"] = min(float(bucket["low"]), float(row.get("low", row.get("close", 0.0)) or 0.0))
        bucket["close"] = float(row.get("close", 0.0) or 0.0)
        bucket["volume"] = float(bucket["volume"]) + float(row.get("volume", 0.0) or 0.0)

    if bucket is not None:
        aggregated.append(bucket)
    return aggregated


def _group_rows_by_session(
    rows: list[dict[str, Any]],
    *,
    allow_partial_live_session: bool = False,
) -> dict[date, list[dict[str, Any]]]:
    grouped: dict[date, list[dict[str, Any]]] = {}
    for row in rows:
        timestamp = _to_ist(row.get("time") or row.get("timestamp"))
        if timestamp.time() < SESSION_OPEN or timestamp.time() > SESSION_CLOSE:
            continue
        normalized = {
            "time": timestamp.isoformat(),
            "open": float(row.get("open", row.get("close", 0.0)) or 0.0),
            "high": float(row.get("high", row.get("close", 0.0)) or 0.0),
            "low": float(row.get("low", row.get("close", 0.0)) or 0.0),
            "close": float(row.get("close", 0.0) or 0.0),
            "volume": float(row.get("volume", 0.0) or 0.0),
        }
        grouped.setdefault(timestamp.date(), []).append(normalized)
    for session_rows in grouped.values():
        session_rows.sort(key=lambda item: item["time"])
    now_ist = datetime.now(IST)
    return {
        key: rows
        for key, rows in grouped.items()
        if len(rows) >= 180
        or (
            allow_partial_live_session
            and key == now_ist.date()
            and SESSION_OPEN <= now_ist.time() < SESSION_CLOSE
            and len(rows) >= 120
        )
    }


def _hour_number(timestamp: datetime) -> int:
    elapsed = int((timestamp.astimezone(IST) - _session_start(timestamp.astimezone(IST).date())).total_seconds() // 60)
    return max(1, min(7, (elapsed // 60) + 1))


def _tpo_rows(snapshot) -> list[dict[str, Any]]:
    prices = sorted(snapshot.tpo_counts)
    return [
        {
            "price": float(price),
            "count": int(snapshot.tpo_counts[price]),
            "letters": snapshot.tpo_letters.get(price, ""),
        }
        for price in prices
    ]


def _shape_from_snapshot(snapshot) -> str:
    tpo_rows = _tpo_rows(snapshot)
    if not tpo_rows:
        return "Unknown"
    price_span = max(snapshot.high_price - snapshot.low_price, snapshot.tick_size)
    poc_percentile = (
        (snapshot.poc - snapshot.low_price) / price_span
        if price_span > 0
        else 0.5
    )
    upper_tpos = sum(row["count"] for row in tpo_rows if row["price"] > snapshot.poc)
    lower_tpos = sum(row["count"] for row in tpo_rows if row["price"] < snapshot.poc)
    skew = (upper_tpos - lower_tpos) / max(upper_tpos + lower_tpos, 1)
    narrow_range = price_span <= max(snapshot.tick_size * 5, snapshot.initial_balance_range * 0.22)
    elongated = (
        snapshot.day_range >= max(snapshot.initial_balance_range * 1.7, snapshot.tick_size * 10)
        and max(snapshot.range_extension_up, snapshot.range_extension_down) >= snapshot.initial_balance_range * 0.45
    )
    if narrow_range:
        return "Ledge"
    if elongated:
        return "Elongated"
    if 0.38 <= poc_percentile <= 0.62 and abs(skew) <= 0.18:
        return "D-shape"
    if poc_percentile >= 0.58 and len(snapshot.buying_tail) >= 2:
        return "P-shape"
    if poc_percentile <= 0.42 and len(snapshot.selling_tail) >= 2:
        return "b-shape"
    if skew >= 0.12:
        return "P-shape"
    if skew <= -0.12:
        return "b-shape"
    return "D-shape"


def _direction_from_snapshot(snapshot) -> str:
    if snapshot.close_price > snapshot.vah and snapshot.poc >= ((snapshot.vah + snapshot.val) / 2.0):
        return "bullish"
    if snapshot.close_price < snapshot.val and snapshot.poc <= ((snapshot.vah + snapshot.val) / 2.0):
        return "bearish"
    if snapshot.close_price >= snapshot.poc:
        return "bullish"
    return "bearish"


def _daily_day_type(snapshot, prior_snapshot: Optional[Any]) -> str:
    if snapshot.close_price > snapshot.initial_balance_high and snapshot.range_extension_up > snapshot.initial_balance_range * 0.3:
        return "TREND_UP"
    if snapshot.close_price < snapshot.initial_balance_low and snapshot.range_extension_down > snapshot.initial_balance_range * 0.3:
        return "TREND_DN"
    if prior_snapshot and snapshot.value_area_overlap is not None and snapshot.value_area_overlap >= 0.7:
        return "BALANCE"
    if max(snapshot.range_extension_up, snapshot.range_extension_down) > snapshot.initial_balance_range * 0.25:
        return "NORMAL_VARIATION"
    return "NORMAL"


def _profile_payload(snapshot, *, scope: str, shape: str, direction_bias: str, hour_number: int | None = None, completed: bool = True) -> dict[str, Any]:
    rows = _tpo_rows(snapshot)
    return {
        "scope": scope,
        "hour_number": hour_number,
        "completed": completed,
        "session_date": snapshot.session_date,
        "open_price": round(float(snapshot.open_price), 2),
        "high_price": round(float(snapshot.high_price), 2),
        "low_price": round(float(snapshot.low_price), 2),
        "close_price": round(float(snapshot.close_price), 2),
        "poc": round(float(snapshot.poc), 2),
        "vah": round(float(snapshot.vah), 2),
        "val": round(float(snapshot.val), 2),
        "initial_balance_high": round(float(snapshot.initial_balance_high), 2),
        "initial_balance_low": round(float(snapshot.initial_balance_low), 2),
        "initial_balance_range": round(float(snapshot.initial_balance_range), 2),
        "day_range": round(float(snapshot.day_range), 2),
        "range_extension_up": round(float(snapshot.range_extension_up), 2),
        "range_extension_down": round(float(snapshot.range_extension_down), 2),
        "tick_size": round(float(snapshot.tick_size), 2),
        "single_prints": [round(float(value), 2) for value in snapshot.single_prints],
        "poor_high": bool(snapshot.poor_high),
        "poor_low": bool(snapshot.poor_low),
        "shape": shape,
        "direction_bias": direction_bias,
        "tpo_rows": rows,
        "sample_count": int(snapshot.sample_count),
        "period_count": int(snapshot.period_count),
        "value_area_overlap": round(float(snapshot.value_area_overlap or 0.0), 4) if snapshot.value_area_overlap is not None else None,
        "value_migration": round(float(snapshot.value_migration or 0.0), 2) if snapshot.value_migration is not None else None,
        "poc_shift": round(float(snapshot.poc_shift or 0.0), 2) if snapshot.poc_shift is not None else None,
        "prior_poc_untouched": snapshot.prior_poc_untouched,
        "bracket_state": snapshot.bracket_state,
    }


def _migration_direction(prev_profile: dict[str, Any], current_profile: dict[str, Any]) -> int:
    if current_profile["vah"] > prev_profile["vah"] and current_profile["val"] >= prev_profile["val"]:
        return 1
    if current_profile["vah"] < prev_profile["vah"] and current_profile["val"] <= prev_profile["val"]:
        return -1
    return 0


def _oscillating_migration(migrations: list[int]) -> bool:
    recent = [value for value in migrations[-4:] if value != 0]
    if len(recent) < 3:
        return False
    sign_changes = sum(1 for prev, cur in zip(recent, recent[1:]) if prev != cur)
    return sign_changes >= 2


def _target_level(current_price: float, direction: str, current_profile: dict[str, Any], prior_profile: Optional[dict[str, Any]]) -> float:
    candidates: list[float] = []
    if prior_profile:
        if direction == "LONG":
            candidates.extend([float(prior_profile["vah"]), float(prior_profile["high_price"])])
            candidates.extend(value for value in prior_profile["single_prints"] if value > current_price)
        else:
            candidates.extend([float(prior_profile["val"]), float(prior_profile["low_price"])])
            candidates.extend(value for value in prior_profile["single_prints"] if value < current_price)
    extension = max(float(current_profile["initial_balance_range"]), float(current_profile["tick_size"]) * 6)
    candidates.append(current_price + extension * 1.8 if direction == "LONG" else current_price - extension * 1.8)
    if direction == "LONG":
        upside = [value for value in candidates if value > current_price]
        return round(min(upside) if upside else current_price + extension, 2)
    downside = [value for value in candidates if value < current_price]
    return round(max(downside) if downside else current_price - extension, 2)


class HistoricalOptionRepository:
    def __init__(self, root: Path):
        self.root = root
        self._index: dict[str, list[dict[str, Any]]] = {}
        self._frames: dict[Path, pd.DataFrame] = {}

    def _ensure_index(self, underlying: str) -> list[dict[str, Any]]:
        normalized = underlying.upper()
        if normalized in self._index:
            return self._index[normalized]

        contract_root = self.root / "contracts" / f"underlying={normalized}"
        entries: list[dict[str, Any]] = []
        if contract_root.exists():
            for path in contract_root.rglob("*.csv.gz"):
                match = _OPTION_FILE_PATTERN.search(path.stem)
                if not match:
                    continue
                expiry = None
                expiry_kind = None
                for part in path.parts:
                    if part.startswith("expiry="):
                        expiry = part.split("=", 1)[1]
                    elif part.startswith("expiry_kind="):
                        expiry_kind = part.split("=", 1)[1]
                if not expiry:
                    continue
                entries.append(
                    {
                        "path": path,
                        "underlying": normalized,
                        "expiry": expiry,
                        "expiry_kind": expiry_kind or "unknown",
                        "strike": float(match.group("strike")),
                        "option_type": match.group("option_type"),
                        "trading_symbol": path.stem,
                    }
                )
        entries.sort(key=lambda row: (row["expiry"], row["option_type"], row["strike"]))
        self._index[normalized] = entries
        return entries

    def _load_frame(self, path: Path) -> pd.DataFrame:
        cached = self._frames.get(path)
        if cached is not None:
            return cached
        frame = pd.read_csv(path, compression="gzip", parse_dates=["time"])
        frame["time"] = pd.to_datetime(frame["time"], utc=True)
        frame = frame.sort_values("time").reset_index(drop=True)
        for column in ("open", "high", "low", "close", "volume", "oi"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0.0)
        self._frames[path] = frame
        return frame

    def _row_at(self, path: Path, at_time: datetime) -> dict[str, Any] | None:
        frame = self._load_frame(path)
        mask = frame["time"] <= at_time.astimezone(timezone.utc)
        if not mask.any():
            return None
        row = frame.loc[mask].iloc[-1]
        previous = frame.loc[: row.name - 1].iloc[-1] if row.name > 0 else None
        return {
            "time": row["time"].to_pydatetime().astimezone(timezone.utc),
            "premium": float(row["close"]),
            "oi": float(row["oi"]),
            "volume": float(row["volume"]),
            "prev_premium": float(previous["close"]) if previous is not None else None,
            "prev_oi": float(previous["oi"]) if previous is not None else None,
        }

    def select_option(
        self,
        *,
        underlying: str,
        at_time: datetime,
        direction: str,
        horizon: str,
        confidence: float,
        spot_price: float,
    ) -> Optional[FMPOptionSelection]:
        entries = self._ensure_index(underlying)
        if not entries:
            return None
        min_dte = int(SCAN_CONFIG["min_dte_for_long_options"])
        trade_date = at_time.astimezone(IST).date()
        option_type = "CE" if direction == "LONG" else "PE"
        eligible = [
            row
            for row in entries
            if row["option_type"] == option_type
            and date.fromisoformat(str(row["expiry"])) >= trade_date + timedelta(days=min_dte)
        ]
        if not eligible:
            return None
        first_expiry = min(row["expiry"] for row in eligible)
        eligible = [row for row in eligible if row["expiry"] == first_expiry]
        all_expiry_entries = [row for row in entries if row["expiry"] == first_expiry]
        strikes = sorted({float(row["strike"]) for row in eligible})
        if not strikes:
            return None
        step = OPTION_STRIKE_STEPS.get(underlying.upper(), max(25.0, abs(strikes[1] - strikes[0]) if len(strikes) > 1 else 50.0))
        atm = min(strikes, key=lambda strike: abs(strike - spot_price))
        if horizon == "scalp":
            offsets = [0]
        elif horizon == "positional" and confidence >= 0.82:
            offsets = [-1, 0, -2, 1] if option_type == "CE" else [1, 0, 2, -1]
        else:
            offsets = [0, -1, 1] if option_type == "CE" else [0, 1, -1]

        chosen_meta = None
        target_strike = atm
        for offset in offsets:
            candidate = atm + (offset * step)
            matches = [row for row in eligible if math.isclose(float(row["strike"]), candidate, abs_tol=step / 4)]
            if matches:
                chosen_meta = matches[0]
                target_strike = float(matches[0]["strike"])
                break
        if chosen_meta is None:
            chosen_meta = min(eligible, key=lambda row: abs(float(row["strike"]) - atm))
            target_strike = float(chosen_meta["strike"])

        row = self._row_at(Path(chosen_meta["path"]), at_time)
        if row is None:
            return None

        opposite_type = "PE" if option_type == "CE" else "CE"
        opposite_meta = None
        for meta in all_expiry_entries:
            if meta["option_type"] == opposite_type and math.isclose(float(meta["strike"]), atm, abs_tol=step / 4):
                opposite_meta = meta
                break
        opposite_row = self._row_at(Path(opposite_meta["path"]), at_time) if opposite_meta else None
        pcr_oi = None
        if option_type == "CE" and opposite_row and row["oi"] > 0:
            pcr_oi = round(float(opposite_row["oi"]) / max(float(row["oi"]), 1.0), 4)
        elif option_type == "PE" and opposite_row:
            pcr_oi = round(float(row["oi"]) / max(float(opposite_row["oi"]), 1.0), 4)

        moneyness = "ATM"
        if target_strike > atm:
            moneyness = "OTM1" if option_type == "CE" else "ITM1"
        elif target_strike < atm:
            moneyness = "ITM1" if option_type == "CE" else "OTM1"

        return FMPOptionSelection(
            underlying=underlying,
            option_type=option_type,
            strike=target_strike,
            expiry=str(first_expiry),
            premium=round(float(row["premium"]), 2),
            previous_premium=round(float(row["prev_premium"]), 2) if row["prev_premium"] is not None else None,
            trading_symbol=str(chosen_meta["trading_symbol"]),
            instrument_key=str(chosen_meta["trading_symbol"]),
            lot_size=int(LOT_SIZES.get(underlying.upper(), 1)),
            oi=float(row["oi"]),
            oi_change=round(float(row["oi"]) - float(row["prev_oi"] or 0.0), 2) if row["prev_oi"] is not None else None,
            volume=float(row["volume"]),
            pcr_oi=pcr_oi,
            iv_rank=None,
            selection_reason=f"Historical {horizon} mapping on {first_expiry}",
            moneyness=moneyness,
            horizon=horizon,
            days_to_expiry=(date.fromisoformat(str(first_expiry)) - trade_date).days,
        )


class FractalMarketProfileService:
    def __init__(self) -> None:
        self.paper = FMPPaperStore(PAPER_ROOT)
        self.option_repo = HistoricalOptionRepository(analytics_root())
        self.order_flow = OrderFlowEngine(
            {
                "trade_lookback": 60,
                "quote_lookback": 160,
                "baseline_window": 120,
                "spread_normalizer_ticks": 2.0,
                "volatility_burst_threshold": 1.5,
            }
        )
        self._live_cache_ttl_seconds = 30.0
        self._summary_cache_ttl_seconds = 60.0
        self._live_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._live_locks: dict[str, asyncio.Lock] = {}
        self._summary_cache: dict[str, Any] = {"payload": None, "expires_at": 0.0}

    async def live_snapshot(self, symbol_code: str = "NIFTY") -> dict[str, Any]:
        normalized = self._normalize_symbol(symbol_code)
        cached = self._live_cache.get(normalized)
        if cached and cached[0] > monotonic():
            return jsonable_encoder(cached[1])

        lock = self._live_locks.setdefault(normalized, asyncio.Lock())
        async with lock:
            cached = self._live_cache.get(normalized)
            if cached and cached[0] > monotonic():
                return jsonable_encoder(cached[1])

            rows, history_source, history_symbol = await self._load_live_rows(normalized)
            sessions = _group_rows_by_session(rows, allow_partial_live_session=True)
            if not sessions:
                degraded_payload = await self._degraded_live_snapshot(
                    normalized,
                    history_source=history_source,
                    history_symbol=history_symbol,
                    row_count=len(rows),
                    reason="missing_spot_history",
                    detail=f"No usable spot history sessions are available for {normalized}.",
                )
                self._live_cache[normalized] = (
                    monotonic() + self._live_cache_ttl_seconds,
                    degraded_payload,
                )
                return jsonable_encoder(degraded_payload)

            session_dates = sorted(sessions)
            current_date = session_dates[-1]
            current_rows = sessions[current_date]
            prior_rows = sessions[session_dates[-2]] if len(session_dates) >= 2 else current_rows
            session_lookup = {key: sessions[key] for key in session_dates}
            analysis = await self._analyze_session(
                normalized,
                current_rows=current_rows,
                prior_rows=prior_rows,
                session_lookup=session_lookup,
                live_mode=True,
            )
            analysis["symbol_code"] = normalized
            analysis["history_source"] = history_source
            analysis["history_symbol"] = history_symbol
            analysis["supported_symbols"] = list(SUPPORTED_SYMBOLS)
            analysis["generated_at"] = _utc_now().isoformat()
            analysis["paper_positions"] = await self.paper.list_positions(symbol=normalized, status="all", limit=8)
            analysis["paper_journal"] = await self.paper.list_journal(symbol=normalized, limit=8)
            analysis["data_status"] = dict(analysis.get("data_status") or {})
            encoded_payload = jsonable_encoder(analysis)
            self._live_cache[normalized] = (
                monotonic() + self._live_cache_ttl_seconds,
                encoded_payload,
            )
            return jsonable_encoder(encoded_payload)

    async def _degraded_live_snapshot(
        self,
        symbol_code: str,
        *,
        history_source: str,
        history_symbol: str,
        row_count: int,
        reason: str,
        detail: str,
    ) -> dict[str, Any]:
        return jsonable_encoder(
            {
                "symbol_code": symbol_code,
                "history_source": history_source,
                "history_symbol": history_symbol,
                "supported_symbols": list(SUPPORTED_SYMBOLS),
                "generated_at": _utc_now().isoformat(),
                "session": {
                    "symbol": symbol_code,
                    "session_date": None,
                    "last_price": None,
                    "current_hour": None,
                    "minutes_to_close": None,
                },
                "daily_profile": None,
                "prior_daily_profile": None,
                "hourly_profiles": [],
                "current_hour_profile": None,
                "current_signal": {
                    "actionable": False,
                    "action": None,
                    "confidence": 0.0,
                    "setup_name": "data_unavailable",
                    "reason": detail,
                },
                "paper_positions": await self.paper.list_positions(symbol=symbol_code, status="all", limit=8),
                "paper_journal": await self.paper.list_journal(symbol=symbol_code, limit=8),
                "data_status": {
                    "execution_ready": False,
                    "degraded_reason": reason,
                    "detail": detail,
                    "history_source": history_source,
                    "history_symbol": history_symbol,
                    "row_count": int(row_count),
                    "session_count": 0,
                },
            }
        )

    async def live_health(self, symbol_code: str = "NIFTY") -> dict[str, Any]:
        normalized = self._normalize_symbol(symbol_code)
        rows, history_source, history_symbol = await self._load_live_rows(normalized)
        sessions = _group_rows_by_session(rows, allow_partial_live_session=True)
        if not sessions:
            raise RuntimeError(f"No spot history available for {normalized}.")

        session_dates = sorted(sessions)
        return {
            "symbol_code": normalized,
            "history_source": history_source,
            "history_symbol": history_symbol,
            "row_count": len(rows),
            "session_count": len(session_dates),
            "latest_session_date": session_dates[-1].isoformat(),
        }

    async def record_paper_snapshot(self, symbol_code: str = "NIFTY") -> dict[str, Any]:
        snapshot = await self.live_snapshot(symbol_code)
        data_status = dict(snapshot.get("data_status") or {})
        if data_status.get("execution_ready") is False and data_status.get("degraded_reason"):
            positions = await self.paper.list_positions(symbol=snapshot.get("symbol_code"), status="all", limit=50)
            snapshot["paper_summary"] = positions.get("summary") or {}
            snapshot["paper_record_skipped"] = True
            snapshot["paper_skip_reason"] = data_status.get("degraded_reason")
            return snapshot
        summary = await self.paper.record_signal(snapshot)
        snapshot["paper_summary"] = summary
        return snapshot

    async def replay_report(self, symbol_code: str = "NIFTY", *, force: bool = False) -> dict[str, Any]:
        normalized = self._normalize_symbol(symbol_code)
        cache_path = self._replay_cache_path(normalized)
        if cache_path.exists() and not force:
            try:
                return json.loads(cache_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass

        rows = self._load_local_csv_rows(normalized)
        sessions = _group_rows_by_session(rows)
        session_dates = sorted(sessions)
        if len(session_dates) < 24:
            raise RuntimeError(f"Not enough local minute history for {normalized} replay.")

        trades: list[dict[str, Any]] = []
        equity_curve: list[dict[str, Any]] = []
        cumulative_pnl = 0.0
        last_exit_date: Optional[date] = None

        for index in range(21, len(session_dates)):
            current_date = session_dates[index]
            prior_date = session_dates[index - 1]
            session_lookup = {key: sessions[key] for key in session_dates[max(0, index - 25) : index + 1]}
            current_rows = sessions[current_date]
            prior_rows = sessions[prior_date]
            open_trade = None
            inside_value_count = 0
            hour_profiles: dict[int, dict[str, Any]] = {}
            three_minute_rows = _aggregate_rows(current_rows, 3)
            minute_rows = current_rows

            for bar_index in range(2, len(three_minute_rows)):
                snapshot_rows = [
                    row
                    for row in minute_rows
                    if _to_ist(row["time"]) <= _to_ist(three_minute_rows[bar_index]["time"]) + timedelta(minutes=2, seconds=59)
                ]
                analysis = self._analyze_session_sync(
                    normalized,
                    current_rows=snapshot_rows,
                    prior_rows=prior_rows,
                    session_lookup=session_lookup,
                )
                for profile in analysis["hourly_profiles"]:
                    hour_profiles[int(profile["hour_number"])] = profile
                timestamp = _to_ist(three_minute_rows[bar_index]["time"])
                current_hour = analysis["current_hour_profile"]
                current_signal = analysis["current_signal"]

                if open_trade is None:
                    if (
                        current_signal["actionable"]
                        and timestamp.time() <= LATEST_ENTRY_TIME
                        and (last_exit_date is None or last_exit_date < current_date or timestamp.time() >= (datetime.combine(current_date, time(0, 0)) + timedelta(minutes=15)).time())
                    ):
                        option = self.option_repo.select_option(
                            underlying=normalized,
                            at_time=timestamp,
                            direction=str(current_signal["action"]),
                            horizon=str(current_signal["horizon"]),
                            confidence=float(current_signal["confidence"]),
                            spot_price=float(current_signal["latest_close"]),
                        )
                        if option is None or option.premium <= 0:
                            continue
                        signal_option = asdict(option)
                        current_signal["options"] = signal_option
                        open_trade = {
                            "id": uuid4().hex,
                            "signal": current_signal,
                            "entry_time": timestamp,
                            "entry_underlying": float(current_signal["latest_close"]),
                            "entry_premium": float(option.premium),
                            "max_favorable_pct": 0.0,
                            "max_adverse_pct": 0.0,
                        }
                        inside_value_count = 0
                    continue

                option = self.option_repo.select_option(
                    underlying=normalized,
                    at_time=timestamp,
                    direction=str(open_trade["signal"]["action"]),
                    horizon=str(open_trade["signal"]["horizon"]),
                    confidence=float(open_trade["signal"]["confidence"]),
                    spot_price=float(analysis["session"]["last_price"]),
                )
                if option is None:
                    continue

                current_underlying = float(analysis["session"]["last_price"])
                premium = float(option.premium)
                entry_premium = float(open_trade["entry_premium"])
                premium_return = ((premium - entry_premium) / max(entry_premium, 0.01)) * 100.0
                open_trade["max_favorable_pct"] = max(float(open_trade["max_favorable_pct"]), premium_return)
                open_trade["max_adverse_pct"] = min(float(open_trade["max_adverse_pct"]), premium_return)

                signal = open_trade["signal"]
                direction = str(signal["action"])
                exit_reason = None
                if direction == "LONG":
                    if current_underlying < float(signal["stop_level"]):
                        exit_reason = "profile_stop"
                    elif current_underlying >= float(signal["target_level"]):
                        exit_reason = "target_hit"
                else:
                    if current_underlying > float(signal["stop_level"]):
                        exit_reason = "profile_stop"
                    elif current_underlying <= float(signal["target_level"]):
                        exit_reason = "target_hit"

                if premium <= entry_premium * (1.0 - float(RISK_CONFIG["max_premium_loss_pct"])):
                    exit_reason = exit_reason or "premium_stop"

                if current_hour and float(current_hour["val"]) <= current_underlying <= float(current_hour["vah"]):
                    inside_value_count += 1
                else:
                    inside_value_count = 0
                if inside_value_count >= 2:
                    exit_reason = exit_reason or "value_time_stop"

                if timestamp.time() >= FORCE_EXIT_TIME:
                    exit_reason = exit_reason or "forced_exit"

                if exit_reason is None:
                    continue

                pnl = round((premium - entry_premium) * int(option.lot_size), 2)
                cumulative_pnl = round(cumulative_pnl + pnl, 2)
                trade = asdict(
                    FMPReplayTrade(
                        trade_id=str(open_trade["id"]),
                        underlying=normalized,
                        setup_name=str(signal["setup_name"]),
                        action=str(signal["action"]),
                        horizon=str(signal["horizon"]),
                        entry_time=open_trade["entry_time"].astimezone(timezone.utc).isoformat(),
                        exit_time=timestamp.astimezone(timezone.utc).isoformat(),
                        entry_underlying=float(open_trade["entry_underlying"]),
                        exit_underlying=current_underlying,
                        entry_premium=entry_premium,
                        exit_premium=premium,
                        strike=float(option.strike),
                        expiry=str(option.expiry),
                        option_type=str(option.option_type),
                        quantity=int(option.lot_size),
                        pnl=pnl,
                        return_pct=round(premium_return, 2),
                        max_adverse_pct=round(abs(float(open_trade["max_adverse_pct"])), 2),
                        max_favorable_pct=round(float(open_trade["max_favorable_pct"]), 2),
                        stop_level=float(signal["stop_level"]),
                        target_level=float(signal["target_level"]),
                        exit_reason=exit_reason,
                        confidence=float(signal["confidence"]),
                        daily_shape=str(signal["daily_shape"]),
                        hourly_shape=str(signal["hourly_shape"]),
                    )
                )
                trades.append(trade)
                equity_curve.append({"time": trade["exit_time"], "equity": cumulative_pnl})
                open_trade = None
                last_exit_date = current_date
                inside_value_count = 0

        report = self._build_report(normalized, trades, equity_curve)
        REPLAY_ROOT.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return report

    async def replay_suite(self, *, force: bool = False) -> dict[str, Any]:
        reports = []
        for symbol in SUPPORTED_SYMBOLS:
            try:
                reports.append(await self.replay_report(symbol, force=force))
            except Exception:
                continue
        return {
            "generated_at": _utc_now().isoformat(),
            "symbols": list(SUPPORTED_SYMBOLS),
            "reports": reports,
        }

    async def summary(self) -> dict[str, Any]:
        cached_payload = self._summary_cache.get("payload")
        if cached_payload is not None and float(self._summary_cache.get("expires_at") or 0.0) > monotonic():
            return jsonable_encoder(cached_payload)

        positions = await self.paper.list_positions(status="all", limit=10)
        replay_reports = [
            cached_report
            for symbol in SUPPORTED_SYMBOLS
            if (cached_report := self._load_cached_replay(symbol)) is not None
        ]
        from core.market_hours_paper_supervisor import market_hours_paper_supervisor

        automation = market_hours_paper_supervisor.get_runner_status("fractal_market_profile")
        payload = {
            "description": "Dedicated Fractal Market Profile strategy stack",
            "supported_symbols": list(SUPPORTED_SYMBOLS),
            "auto_started": bool(automation.get("enabled") and automation.get("loop_active")),
            "automation": automation,
            "paper_summary": positions["summary"],
            "replay_reports": replay_reports,
        }
        encoded_payload = jsonable_encoder(payload)
        self._summary_cache = {
            "payload": encoded_payload,
            "expires_at": monotonic() + self._summary_cache_ttl_seconds,
        }
        return jsonable_encoder(encoded_payload)

    async def paper_journal(self, symbol: str | None = None, limit: int = 50) -> dict[str, Any]:
        return await self.paper.list_journal(symbol=symbol, limit=limit)

    async def paper_positions(self, symbol: str | None = None, status: str = "all", limit: int = 50) -> dict[str, Any]:
        return await self.paper.list_positions(symbol=symbol, status=status, limit=limit)

    def _build_report(self, symbol: str, trades: list[dict[str, Any]], equity_curve: list[dict[str, Any]]) -> dict[str, Any]:
        total = len(trades)
        winners = [trade for trade in trades if float(trade["pnl"]) > 0]
        losers = [trade for trade in trades if float(trade["pnl"]) <= 0]
        gross_profit = sum(float(trade["pnl"]) for trade in winners)
        gross_loss = abs(sum(float(trade["pnl"]) for trade in losers))
        profit_factor = round(gross_profit / gross_loss, 4) if gross_loss > 0 else None
        expectancy = round(sum(float(trade["pnl"]) for trade in trades) / max(total, 1), 2)
        avg_rr = round(
            sum(float(trade["max_favorable_pct"]) / max(float(trade["max_adverse_pct"]) or 1.0, 1.0) for trade in winners)
            / max(len(winners), 1),
            2,
        ) if winners else 0.0
        max_drawdown = self._max_drawdown(equity_curve)
        setup_breakdown: dict[str, dict[str, Any]] = {}
        for trade in trades:
            bucket = setup_breakdown.setdefault(
                str(trade["setup_name"]),
                {"count": 0, "pnl": 0.0, "wins": 0},
            )
            bucket["count"] += 1
            bucket["pnl"] = round(float(bucket["pnl"]) + float(trade["pnl"]), 2)
            bucket["wins"] += 1 if float(trade["pnl"]) > 0 else 0
        weeks = 52 if trades else 1
        metrics = {
            "trade_count": total,
            "win_rate": round((len(winners) / total) * 100.0, 2) if total else 0.0,
            "expectancy": expectancy,
            "profit_factor": profit_factor,
            "max_drawdown": round(max_drawdown, 2),
            "avg_risk_reward": avg_rr,
            "trades_per_week": round(total / weeks, 2),
            "net_pnl": round(sum(float(trade["pnl"]) for trade in trades), 2),
        }
        thresholds = {
            "win_rate_min": 48.0,
            "win_rate_target": 55.0,
            "avg_risk_reward_min": 1.8,
            "profit_factor_min": 1.4,
            "max_drawdown_max": 18.0,
        }
        status = {
            "win_rate": metrics["win_rate"] >= thresholds["win_rate_min"],
            "avg_risk_reward": metrics["avg_risk_reward"] >= thresholds["avg_risk_reward_min"],
            "profit_factor": (metrics["profit_factor"] or 0.0) >= thresholds["profit_factor_min"],
            "max_drawdown": abs(metrics["max_drawdown"]) <= thresholds["max_drawdown_max"] * max(abs(metrics["net_pnl"]) or 1.0, 1.0),
        }
        return {
            "symbol": symbol,
            "generated_at": _utc_now().isoformat(),
            "metrics": metrics,
            "thresholds": thresholds,
            "gate_status": status,
            "equity_curve": equity_curve[-200:],
            "setup_breakdown": [
                {
                    "setup_name": name,
                    "count": bucket["count"],
                    "pnl": round(bucket["pnl"], 2),
                    "win_rate": round((bucket["wins"] / bucket["count"]) * 100.0, 2) if bucket["count"] else 0.0,
                }
                for name, bucket in sorted(setup_breakdown.items())
            ],
            "trades": trades[-80:],
        }

    def _max_drawdown(self, equity_curve: list[dict[str, Any]]) -> float:
        peak = 0.0
        max_dd = 0.0
        for point in equity_curve:
            equity = float(point["equity"])
            peak = max(peak, equity)
            max_dd = min(max_dd, equity - peak)
        return max_dd

    async def _analyze_session(
        self,
        symbol_code: str,
        *,
        current_rows: list[dict[str, Any]],
        prior_rows: list[dict[str, Any]],
        session_lookup: dict[date, list[dict[str, Any]]],
        live_mode: bool,
    ) -> dict[str, Any]:
        analysis = await asyncio.to_thread(
            self._analyze_session_sync,
            symbol_code,
            current_rows=current_rows,
            prior_rows=prior_rows,
            session_lookup=session_lookup,
        )
        try:
            order_flow = await asyncio.wait_for(self._build_live_order_flow(symbol_code, current_rows), timeout=3.0)
        except Exception as exc:
            logger.warning(f"[FMP] Live order-flow build timed out/degraded for {symbol_code}: {exc}")
            order_flow = self._build_bar_order_flow(current_rows)
            order_flow["source"] = "bar_proxy_timeout"
            order_flow["degraded_reason"] = "live_order_flow_timeout"
        analysis["order_flow"] = order_flow
        analysis["data_status"] = self._build_live_data_status(current_rows, order_flow)
        try:
            analysis["current_signal"] = await asyncio.wait_for(
                self._build_live_signal(symbol_code, analysis, order_flow),
                timeout=4.0,
            )
        except Exception as exc:
            logger.warning(f"[FMP] Live signal build timed out/degraded for {symbol_code}: {exc}")
            signal = dict(analysis.get("current_signal") or {})
            if not signal:
                signal = self._build_signal(
                    symbol_code,
                    current_rows=current_rows,
                    daily_profile=analysis["daily_profile"],
                    prior_daily_profile=analysis["prior_daily_profile"],
                    current_hour_profile=analysis["current_hour_profile"],
                    hourly_profiles=analysis["hourly_profiles"],
                    order_flow=order_flow,
                    historical_options=True,
            )
            signal["actionable"] = False
            filters = list(signal.get("filters") or [])
            filters.append("Live option/order-flow confirmation timed out.")
            metadata = dict(signal.get("metadata") or {})
            advisories = list(metadata.get("advisories") or [])
            advisories.append("Snapshot is using a bounded bar-proxy fallback.")
            metadata["advisories"] = advisories
            signal["filters"] = filters
            signal["metadata"] = metadata
            analysis["current_signal"] = signal
        await self._persist_hourly_profiles(symbol_code, analysis["session"]["session_date"], analysis["hourly_profiles"])
        return analysis

    def _analyze_session_sync(
        self,
        symbol_code: str,
        *,
        current_rows: list[dict[str, Any]],
        prior_rows: list[dict[str, Any]],
        session_lookup: dict[date, list[dict[str, Any]]],
    ) -> dict[str, Any]:
        normalized = self._normalize_symbol(symbol_code)
        session_date = _to_ist(current_rows[-1]["time"]).date()
        daily_references = self._daily_references(session_lookup, session_date)
        tick_size = self._adaptive_tick_size(normalized, daily_references["avg_atr"])

        daily_profile = self._build_profile(
            normalized,
            rows=_aggregate_rows(current_rows, int(PROFILE_CONFIG["daily_period_minutes"])),
            tick_size=tick_size,
            period_minutes=int(PROFILE_CONFIG["daily_period_minutes"]),
            initial_balance_periods=int(PROFILE_CONFIG["daily_initial_balance_periods"]),
            prior_snapshot=None,
            scope="daily",
        )
        prior_daily_profile = self._build_profile(
            normalized,
            rows=_aggregate_rows(prior_rows, int(PROFILE_CONFIG["daily_period_minutes"])),
            tick_size=tick_size,
            period_minutes=int(PROFILE_CONFIG["daily_period_minutes"]),
            initial_balance_periods=int(PROFILE_CONFIG["daily_initial_balance_periods"]),
            prior_snapshot=None,
            scope="daily",
        )
        if daily_profile and prior_daily_profile:
            daily_profile["value_area_overlap"] = round(
                self._overlap(
                    float(daily_profile["val"]),
                    float(daily_profile["vah"]),
                    float(prior_daily_profile["val"]),
                    float(prior_daily_profile["vah"]),
                ),
                4,
            )
            daily_profile["value_migration"] = round(
                ((float(daily_profile["vah"]) + float(daily_profile["val"])) / 2.0)
                - ((float(prior_daily_profile["vah"]) + float(prior_daily_profile["val"])) / 2.0),
                2,
            )
        daily_profile["shape"] = _shape_from_snapshot(self._raw_profile_snapshot(normalized, _aggregate_rows(current_rows, 30), tick_size, 30, 2))
        daily_profile["direction_bias"] = _direction_from_snapshot(self._raw_profile_snapshot(normalized, _aggregate_rows(current_rows, 30), tick_size, 30, 2))
        daily_profile["day_type"] = _daily_day_type(self._raw_profile_snapshot(normalized, _aggregate_rows(current_rows, 30), tick_size, 30, 2), None if not prior_daily_profile else self._raw_profile_snapshot(normalized, _aggregate_rows(prior_rows, 30), tick_size, 30, 2))
        daily_profile["avg_daily_ib"] = round(float(daily_references["avg_daily_ib"]), 2)
        daily_profile["daily_ib_ratio"] = round(
            float(daily_profile["initial_balance_range"]) / max(float(daily_references["avg_daily_ib"]) or 1.0, 1.0),
            4,
        )

        hourly_profiles = self._build_hourly_profiles(normalized, current_rows, tick_size=tick_size)
        current_hour_profile = hourly_profiles[-1] if hourly_profiles else None
        migrations: list[int] = []
        score = 0
        for index, profile in enumerate(hourly_profiles):
            if index == 0:
                profile["value_migration_step"] = 0
                profile["value_migration_score"] = 0
                continue
            step = _migration_direction(hourly_profiles[index - 1], profile)
            score += step
            profile["value_migration_step"] = step
            profile["value_migration_score"] = score
            migrations.append(step)

        signal = self._build_signal(
            normalized,
            current_rows=current_rows,
            daily_profile=daily_profile,
            prior_daily_profile=prior_daily_profile,
            current_hour_profile=current_hour_profile,
            hourly_profiles=hourly_profiles,
            order_flow=self._build_bar_order_flow(current_rows),
            historical_options=True,
        )
        intraday_3m_bars = _aggregate_rows(current_rows, int(PROFILE_CONFIG["hourly_period_minutes"]))
        intraday_30m_bars = _aggregate_rows(current_rows, int(PROFILE_CONFIG["daily_period_minutes"]))

        return {
            "session": {
                "symbol": normalized,
                "session_date": session_date.isoformat(),
                "last_price": round(float(current_rows[-1]["close"]), 2),
                "current_hour": current_hour_profile["hour_number"] if current_hour_profile else None,
                "minutes_to_close": max(
                    0,
                    int(
                        (
                            datetime.combine(session_date, SESSION_CLOSE, tzinfo=IST)
                            - _to_ist(current_rows[-1]["time"])
                        ).total_seconds()
                        // 60
                    ),
                ),
            },
            "daily_profile": daily_profile,
            "prior_daily_profile": prior_daily_profile,
            "hourly_profiles": hourly_profiles,
            "current_hour_profile": current_hour_profile,
            "intraday_bars_3m": intraday_3m_bars,
            "intraday_bars_30m": intraday_30m_bars,
            "filters": {
                "oscillating_hourly_va": _oscillating_migration(migrations),
                "wide_daily_ib": float(daily_profile["daily_ib_ratio"]) > float(SCAN_CONFIG["wide_daily_ib_factor"]),
            },
            "order_flow": self._build_bar_order_flow(current_rows),
            "current_signal": signal,
        }

    async def _build_live_signal(self, symbol_code: str, analysis: dict[str, Any], order_flow: dict[str, Any]) -> dict[str, Any]:
        signal = self._build_signal(
            symbol_code,
            current_rows=[],
            daily_profile=analysis["daily_profile"],
            prior_daily_profile=analysis["prior_daily_profile"],
            current_hour_profile=analysis["current_hour_profile"],
            hourly_profiles=analysis["hourly_profiles"],
            order_flow=order_flow,
            historical_options=False,
        )
        if signal["action"] == "FLAT":
            return signal

        filters = list(signal.get("filters") or [])
        rationale = list(signal.get("rationale") or [])
        advisories = list(signal.get("metadata", {}).get("advisories") or [])
        data_status = dict(analysis.get("data_status") or {})
        if data_status and not bool(data_status.get("execution_ready", True)):
            reason = str(data_status.get("degraded_reason") or "live_order_flow_unavailable").replace("_", " ")
            filters.append(
                "Live tick/order-flow data is not ready for paper entries; "
                f"current snapshot is degraded ({reason})."
            )
            advisories.append(
                f"Order-flow source is {data_status.get('order_flow_source') or 'unknown'}."
            )
        try:
            option_selection = await asyncio.wait_for(
                self._live_option_selection(
                    symbol_code,
                    direction=str(signal["action"]),
                    horizon=str(signal["horizon"]),
                    confidence=float(signal["confidence"]),
                ),
                timeout=3.0,
            )
        except Exception:
            option_selection = None
        if option_selection is None:
            filters.append("Live option context is unavailable for this underlying.")
        else:
            signal["options"] = option_selection
            pcr = option_selection.get("pcr_oi")
            oi_change = float(option_selection.get("oi_change") or 0.0)
            iv_rank = option_selection.get("iv_rank")
            signal["confidence"], advisories = self._apply_option_confirmation_penalties(
                action=str(signal["action"]),
                confidence=float(signal["confidence"]),
                pcr=pcr,
                oi_change=oi_change,
                iv_rank=iv_rank,
                advisories=advisories,
            )
            if not filters and not advisories:
                rationale.append("Live ATM options flow is aligned with the FMP thesis.")

        try:
            vix_payload = await asyncio.wait_for(sector_tracker._get_india_vix(), timeout=2.0)
            india_vix = float(vix_payload.get("price") or 0.0)
        except Exception:
            india_vix = 0.0
        signal["metadata"]["india_vix"] = india_vix
        if india_vix > float(SCAN_CONFIG["india_vix_defined_risk"]):
            signal["confidence"] = round(
                max(0.0, float(signal["confidence"]) - float(SCAN_CONFIG["soft_vix_penalty"])),
                4,
            )
            advisories.append("India VIX is above the naked premium-buying threshold.")

        signal["filters"] = filters
        signal["rationale"] = rationale
        signal["metadata"]["advisories"] = advisories
        signal["actionable"] = bool(
            signal["action"] != "FLAT"
            and not filters
            and float(signal["confidence"]) >= float(SCAN_CONFIG["actionable_confidence_min"])
        )
        return signal

    def _apply_option_confirmation_penalties(
        self,
        *,
        action: str,
        confidence: float,
        pcr: float | None,
        oi_change: float | None,
        iv_rank: float | None,
        advisories: list[str] | None = None,
    ) -> tuple[float, list[str]]:
        adjusted = float(confidence)
        messages = list(advisories or [])
        option_penalty = float(SCAN_CONFIG["soft_option_penalty"])
        iv_penalty = float(SCAN_CONFIG["soft_iv_penalty"])

        if action == "LONG":
            if pcr is not None and float(pcr) < float(SCAN_CONFIG["bullish_pcr_min"]):
                adjusted -= option_penalty
                messages.append("PCR is not confirming the bullish thesis.")
            if oi_change is not None and oi_change <= 0:
                adjusted -= option_penalty
                messages.append("Call-side OI is not building yet.")
        else:
            if pcr is not None and float(pcr) > float(SCAN_CONFIG["bearish_pcr_max"]):
                adjusted -= option_penalty
                messages.append("PCR is not confirming the bearish thesis.")
            if oi_change is not None and oi_change <= 0:
                adjusted -= option_penalty
                messages.append("Put-side OI is not building yet.")
        if iv_rank is not None and float(iv_rank) > float(SCAN_CONFIG["max_iv_rank_for_buying"]):
            adjusted -= iv_penalty
            messages.append("IV rank is above the premium-buying threshold.")

        return round(max(0.0, adjusted), 4), messages

    async def _live_option_selection(
        self,
        symbol_code: str,
        *,
        direction: str,
        horizon: str,
        confidence: float,
    ) -> dict[str, Any] | None:
        watchlist = await atm_watchlist_service.get_watchlist(symbols=[symbol_code])
        row = next(
            (item for item in watchlist.get("rows", []) if str(item.get("underlying") or "").upper() == symbol_code.upper()),
            None,
        )
        if not row:
            return None

        option_type = "CE" if direction == "LONG" else "PE"
        option_payload = row.get("ce") if option_type == "CE" else row.get("pe")
        if not option_payload:
            return None

        chain = await option_chain_service.get_cached(
            INDEX_APP_SYMBOLS.get(symbol_code, symbol_code),
            str(row.get("expiry")),
        )
        pcr_oi = float(chain.get("pcr_oi")) if chain and chain.get("pcr_oi") is not None else None
        iv_payload = await sector_tracker.get_iv_rank(symbol_code)
        iv_rank = float(iv_payload.get("iv_rank") or 0.0) if isinstance(iv_payload, dict) else None
        selection_reason = f"Live {horizon} mapping uses the ATM watchlist contract"
        if confidence >= 0.82:
            selection_reason += " with the strongest current ATM OI confirmation"

        return {
            "underlying": symbol_code,
            "option_type": option_type,
            "strike": float(option_payload.get("strike") or row.get("atm_strike") or 0.0),
            "expiry": str(row.get("expiry") or ""),
            "premium": round(float(option_payload.get("ltp") or 0.0), 2),
            "previous_premium": round(float(option_payload.get("prev_close") or 0.0), 2) if option_payload.get("prev_close") is not None else None,
            "trading_symbol": option_payload.get("trading_symbol"),
            "instrument_key": option_payload.get("instrument_key"),
            "lot_size": int(row.get("lot_size") or LOT_SIZES.get(symbol_code.upper(), 1)),
            "oi": float(option_payload.get("oi") or 0.0),
            "oi_change": float(option_payload.get("oi_change") or 0.0) if option_payload.get("oi_change") is not None else None,
            "volume": float(option_payload.get("volume") or 0.0),
            "pcr_oi": pcr_oi,
            "iv_rank": iv_rank,
            "selection_reason": selection_reason,
            "moneyness": "ATM",
            "horizon": horizon,
            "days_to_expiry": max((date.fromisoformat(str(row.get("expiry"))) - _utc_now().astimezone(IST).date()).days, 0)
            if row.get("expiry")
            else 0,
        }

    def _build_signal(
        self,
        symbol_code: str,
        *,
        current_rows: list[dict[str, Any]],
        daily_profile: dict[str, Any],
        prior_daily_profile: Optional[dict[str, Any]],
        current_hour_profile: Optional[dict[str, Any]],
        hourly_profiles: list[dict[str, Any]],
        order_flow: dict[str, Any],
        historical_options: bool,
    ) -> dict[str, Any]:
        if not current_hour_profile:
            return {
                "underlying": symbol_code,
                "action": "FLAT",
                "setup_name": "no_hour_profile",
                "confidence": 0.0,
                "horizon": "none",
                "actionable": False,
                "filters": ["Current hour profile is unavailable."],
                "rationale": ["Need at least one populated hourly fractal profile."],
                "order_flow_bias": order_flow,
            }

        last_price = float(current_hour_profile["close_price"])
        hour_number = int(current_hour_profile["hour_number"] or 0)
        va_score = int(current_hour_profile.get("value_migration_score") or 0)
        daily_shape = str(daily_profile.get("shape") or "Unknown")
        hourly_shape = str(current_hour_profile.get("shape") or "Unknown")
        balance_day = daily_shape == "D-shape" and hourly_shape == "D-shape"

        filters: list[str] = []
        if hour_number < 2:
            filters.append("Layer 1 starts after the opening hour (10:15 IST).")
        if hourly_shape == "Ledge":
            filters.append("Current hourly profile is a ledge; wait for the break.")
        if float(daily_profile.get("daily_ib_ratio") or 0.0) > float(SCAN_CONFIG["wide_daily_ib_factor"]):
            filters.append("Daily IB is already too wide versus the 20-session reference.")
        if _oscillating_migration([int(profile.get("value_migration_step") or 0) for profile in hourly_profiles]):
            filters.append("Hourly value migration is oscillating across recent hours.")

        daily_direction = str(daily_profile.get("direction_bias") or "neutral")
        order_flow_direction = "bullish" if float(order_flow.get("delta") or 0.0) >= 0 else "bearish"
        order_flow_alignment = float(order_flow.get("timing_confidence") or 0.0)
        pullback_tolerance = max(
            float(daily_profile["tick_size"]) * 6,
            float(current_hour_profile["initial_balance_range"]) * float(SCAN_CONFIG["trend_pullback_tolerance_factor"]),
        )
        balance_reversion_tolerance = max(
            float(daily_profile["tick_size"]) * 6,
            float(daily_profile["initial_balance_range"]) * float(SCAN_CONFIG["balance_reversion_tolerance_factor"]),
        )
        near_balance_vah = abs(last_price - float(daily_profile["vah"])) <= balance_reversion_tolerance
        near_balance_val = abs(last_price - float(daily_profile["val"])) <= balance_reversion_tolerance
        balance_breakout_up = (
            balance_day
            and hour_number >= 2
            and last_price > float(current_hour_profile["initial_balance_high"])
            and va_score >= 0
        )
        balance_breakout_down = (
            balance_day
            and hour_number >= 2
            and last_price < float(current_hour_profile["initial_balance_low"])
            and va_score <= 0
        )
        if balance_day and not (near_balance_vah or near_balance_val or balance_breakout_up or balance_breakout_down):
            filters.append("Daily and hourly profiles are both D-shape balance away from the edge.")

        setup_name = "no_trade"
        action = "FLAT"
        confidence = 0.22
        daily_context = str(daily_profile.get("day_type") or "NORMAL")
        if daily_context.startswith("TREND_UP"):
            daily_direction = "bullish"
        elif daily_context.startswith("TREND_DN"):
            daily_direction = "bearish"
        rationale = [
            f"Daily context {daily_context} with {daily_shape} structure.",
            f"Hourly H{hour_number} is {hourly_shape} with VA migration score {va_score}.",
        ]

        if daily_direction == "bullish" and va_score >= int(SCAN_CONFIG["min_value_migration_abs"]) and hourly_shape == "Elongated" and last_price > float(current_hour_profile["initial_balance_high"]):
            setup_name = "hourly_ib_breakout_call"
            action = "LONG"
            confidence = 0.58 + min(abs(va_score), 4) * 0.05
            rationale.append("Daily upside acceptance and hourly elongated breakout are aligned.")
        elif daily_direction == "bearish" and va_score <= -int(SCAN_CONFIG["min_value_migration_abs"]) and hourly_shape == "Elongated" and last_price < float(current_hour_profile["initial_balance_low"]):
            setup_name = "hourly_ib_breakout_put"
            action = "SHORT"
            confidence = 0.58 + min(abs(va_score), 4) * 0.05
            rationale.append("Daily downside acceptance and hourly elongated breakdown are aligned.")
        elif daily_shape == "D-shape" and hourly_shape == "P-shape" and abs(last_price - float(daily_profile["vah"])) <= max(float(daily_profile["tick_size"]) * 6, float(daily_profile["initial_balance_range"]) * 0.16):
            setup_name = "daily_balance_mean_reversion_put"
            action = "SHORT"
            confidence = 0.62
            rationale.append("Balanced day is distributing at VAH with a P-shape hourly profile.")
        elif daily_shape == "D-shape" and hourly_shape == "b-shape" and abs(last_price - float(daily_profile["val"])) <= max(float(daily_profile["tick_size"]) * 6, float(daily_profile["initial_balance_range"]) * 0.16):
            setup_name = "daily_balance_mean_reversion_call"
            action = "LONG"
            confidence = 0.62
            rationale.append("Balanced day is rejecting VAL with a b-shape hourly profile.")
        elif balance_day and near_balance_vah and order_flow_direction == "bearish":
            setup_name = "daily_balance_extreme_reversion_put"
            action = "SHORT"
            confidence = 0.56
            rationale.append("Balanced profiles are rejecting VAH with bearish order flow at the upper edge.")
        elif balance_day and near_balance_val and order_flow_direction == "bullish":
            setup_name = "daily_balance_extreme_reversion_call"
            action = "LONG"
            confidence = 0.56
            rationale.append("Balanced profiles are defending VAL with bullish order flow at the lower edge.")
        elif balance_breakout_up:
            setup_name = "daily_balance_breakout_call"
            action = "LONG"
            confidence = 0.55 + min(abs(va_score), 3) * 0.04
            rationale.append("Balanced profiles are expanding above hourly IB and can rotate into a fresh upside auction.")
        elif balance_breakout_down:
            setup_name = "daily_balance_breakout_put"
            action = "SHORT"
            confidence = 0.55 + min(abs(va_score), 3) * 0.04
            rationale.append("Balanced profiles are breaking below hourly IB and can rotate into a fresh downside auction.")
        elif (
            daily_direction == "bullish"
            and va_score >= int(SCAN_CONFIG["min_value_migration_abs"])
            and hourly_shape in {"Elongated", "P-shape", "D-shape"}
            and last_price >= (float(current_hour_profile["poc"]) - pullback_tolerance)
        ):
            setup_name = "trend_pullback_call"
            action = "LONG"
            confidence = 0.56 + min(abs(va_score), 4) * 0.05
            rationale.append("Bullish value migration is holding through an intraday pullback near POC.")
        elif (
            daily_direction == "bearish"
            and va_score <= -int(SCAN_CONFIG["min_value_migration_abs"])
            and hourly_shape in {"Elongated", "b-shape", "P-shape"}
            and last_price <= (float(current_hour_profile["poc"]) + pullback_tolerance)
        ):
            setup_name = "trend_pullback_put"
            action = "SHORT"
            confidence = 0.56 + min(abs(va_score), 4) * 0.05
            rationale.append("Bearish value migration is holding through an intraday pullback near POC.")

        horizon = "swing"
        if setup_name.startswith("daily_balance"):
            horizon = "scalp"
        elif confidence >= 0.80 and hour_number <= 4 and abs(va_score) >= 3:
            horizon = "positional"

        if action != "FLAT" and order_flow_direction == ("bullish" if action == "LONG" else "bearish"):
            confidence += 0.08
            rationale.append("Order flow is leaning in the same direction as the profile thesis.")
        elif action != "FLAT":
            confidence -= 0.05
            rationale.append("Order flow is not fully aligned with the profile thesis yet.")

        options = None
        advisories: list[str] = []
        signal_timestamp = _utc_now() if not current_rows else _to_ist(current_rows[-1]["time"]).astimezone(timezone.utc)
        if action != "FLAT":
            if historical_options:
                option_selection = self.option_repo.select_option(
                    underlying=symbol_code,
                    at_time=signal_timestamp.astimezone(IST),
                    direction=action,
                    horizon=horizon,
                    confidence=confidence,
                    spot_price=last_price,
                )
            else:
                option_selection = None
            if option_selection is not None:
                options = asdict(option_selection)
                pcr = float(option_selection.pcr_oi or 1.0) if option_selection.pcr_oi is not None else 1.0
                oi_change = float(option_selection.oi_change or 0.0)
                confidence, advisories = self._apply_option_confirmation_penalties(
                    action=action,
                    confidence=confidence,
                    pcr=pcr,
                    oi_change=oi_change,
                    iv_rank=getattr(option_selection, "iv_rank", None),
                    advisories=advisories,
                )
                if not advisories:
                    confidence += 0.08
                    rationale.append("The options overlay confirms the direction at the chosen strike.")
            elif historical_options:
                filters.append("Historical options context was unavailable for this timestamp.")

        confidence = max(0.0, min(confidence, 0.94))
        actionable = (
            action != "FLAT"
            and not filters
            and confidence >= float(SCAN_CONFIG["actionable_confidence_min"])
            and hour_number <= 6
        )

        stop_level = (
            max(float(current_hour_profile["poc"]), float(current_hour_profile["initial_balance_high"]))
            if action == "LONG"
            else min(float(current_hour_profile["poc"]), float(current_hour_profile["initial_balance_low"]))
        ) if action != "FLAT" else last_price
        target_level = _target_level(last_price, action if action != "FLAT" else "LONG", current_hour_profile, prior_daily_profile)
        return {
            "underlying": symbol_code,
            "signal_time": signal_timestamp.isoformat(),
            "setup_name": setup_name,
            "action": action,
            "confidence": round(confidence, 4),
            "horizon": horizon,
            "actionable": actionable,
            "latest_close": round(last_price, 2),
            "entry_trigger": round(
                float(current_hour_profile["initial_balance_high"]) if action == "LONG" else float(current_hour_profile["initial_balance_low"]),
                2,
            ) if action != "FLAT" else round(last_price, 2),
            "stop_level": round(stop_level, 2),
            "target_level": round(target_level, 2),
            "hourly_shape": hourly_shape,
            "daily_shape": daily_shape,
            "hourly_number": hour_number,
            "value_migration_score": va_score,
            "daily_context": daily_context,
            "rationale": rationale,
            "filters": filters,
            "order_flow_bias": order_flow,
            "options": options,
            "metadata": {
                "daily_direction": daily_direction,
                "order_flow_direction": order_flow_direction,
                "order_flow_alignment": round(order_flow_alignment, 4),
                "advisories": advisories,
            },
        }

    async def _build_live_order_flow(self, symbol_code: str, current_rows: list[dict[str, Any]]) -> dict[str, Any]:
        app_symbol = INDEX_APP_SYMBOLS[symbol_code]
        snapshot_end = _to_ist(current_rows[-1]["time"]).astimezone(timezone.utc)
        tick_size = OPTION_STRIKE_STEPS.get(symbol_code, 50.0) / 100.0
        quote_history = await self._recent_quote_history(app_symbol, snapshot_end=snapshot_end, tick_size=tick_size)
        if not quote_history:
            return self._build_bar_order_flow(current_rows)
        trades = self._recent_trade_prints_from_history(quote_history)
        quote = quote_history[-1] if quote_history else self._bar_quote(current_rows[-1], tick_size=tick_size)
        depth = self._depth_from_quote_history(quote_history or [quote], tick_size=tick_size)
        snapshot = self.order_flow.compute(
            quote=QuoteSnapshot(
                timestamp=_ensure_dt(quote["timestamp"]),
                bid=float(quote["bid"]),
                ask=float(quote["ask"]),
                bid_size=float(quote["bid_size"]),
                ask_size=float(quote["ask_size"]),
            ),
            trades=[
                TradePrint(
                    timestamp=_ensure_dt(item["timestamp"]),
                    price=float(item["price"]),
                    quantity=float(item["quantity"]),
                    aggressor_side=str(item["aggressor_side"]),
                )
                for item in trades
            ],
            depth=DepthSnapshot(
                timestamp=_ensure_dt(depth["timestamp"]),
                bids=[DepthLevel(price=float(level["price"]), quantity=float(level["quantity"])) for level in depth["bids"]],
                asks=[DepthLevel(price=float(level["price"]), quantity=float(level["quantity"])) for level in depth["asks"]],
            ),
            tick_size=tick_size,
            quote_history=[
                QuoteSnapshot(
                    timestamp=_ensure_dt(item["timestamp"]),
                    bid=float(item["bid"]),
                    ask=float(item["ask"]),
                    bid_size=float(item["bid_size"]),
                    ask_size=float(item["ask_size"]),
                )
                for item in quote_history
            ],
        )
        payload = jsonable_encoder(asdict(snapshot))
        payload["quote_history"] = quote_history
        payload["trade_prints"] = trades
        payload["depth_snapshot"] = depth
        payload["source"] = "market_ticks" if quote_history else "bar_fallback"
        return payload

    def _build_bar_order_flow(self, current_rows: list[dict[str, Any]]) -> dict[str, Any]:
        tick_size = 0.5
        trades = []
        quote_history = []
        for row in current_rows[-24:]:
            timestamp = _to_ist(row["time"])
            close_price = float(row["close"])
            open_price = float(row["open"])
            volume = max(float(row.get("volume", 0.0) or 0.0), 1.0)
            side = "buy" if close_price >= open_price else "sell"
            trades.append(
                TradePrint(
                    timestamp=timestamp,
                    price=close_price,
                    quantity=max(volume / 12.0, 1.0),
                    aggressor_side=side,
                )
            )
            quote_history.append(
                QuoteSnapshot(
                    timestamp=timestamp,
                    bid=close_price - tick_size,
                    ask=close_price + tick_size,
                    bid_size=max(volume / 16.0, 10.0),
                    ask_size=max(volume / 16.0, 10.0),
                )
            )
        latest = quote_history[-1]
        depth = DepthSnapshot(
            timestamp=latest.timestamp,
            bids=[DepthLevel(price=latest.bid - (tick_size * level), quantity=max(latest.bid_size * (1 - 0.18 * level), 1.0)) for level in range(3)],
            asks=[DepthLevel(price=latest.ask + (tick_size * level), quantity=max(latest.ask_size * (1 - 0.18 * level), 1.0)) for level in range(3)],
        )
        snapshot = self.order_flow.compute(
            quote=latest,
            trades=trades,
            depth=depth,
            tick_size=tick_size,
            quote_history=quote_history,
        )
        payload = jsonable_encoder(asdict(snapshot))
        payload["quote_history"] = [
            {
                "timestamp": item.timestamp.isoformat(),
                "bid": float(item.bid),
                "ask": float(item.ask),
                "bid_size": float(item.bid_size),
                "ask_size": float(item.ask_size),
                "last_price": round((float(item.bid) + float(item.ask)) / 2.0, 2),
            }
            for item in quote_history
        ]
        payload["trade_prints"] = [
            {
                "timestamp": item.timestamp.isoformat(),
                "price": float(item.price),
                "quantity": float(item.quantity),
                "aggressor_side": str(item.aggressor_side),
            }
            for item in trades
        ]
        payload["depth_snapshot"] = {
            "timestamp": depth.timestamp.isoformat(),
            "bids": [{"price": float(level.price), "quantity": float(level.quantity)} for level in depth.bids],
            "asks": [{"price": float(level.price), "quantity": float(level.quantity)} for level in depth.asks],
        }
        payload["source"] = "bar_proxy"
        return payload

    def _build_live_data_status(
        self,
        current_rows: list[dict[str, Any]],
        order_flow: dict[str, Any],
    ) -> dict[str, Any]:
        latest_row_time = _to_ist(current_rows[-1]["time"]).astimezone(timezone.utc) if current_rows else None
        minute_history_age_seconds = (
            max(0.0, (datetime.now(timezone.utc) - latest_row_time).total_seconds())
            if latest_row_time is not None
            else None
        )
        order_flow_source = str(order_flow.get("source") or "unknown")
        tick_ready = order_flow_source == "market_ticks"
        minute_history_ready = bool(current_rows) and (
            minute_history_age_seconds is None or minute_history_age_seconds <= 180.0
        )
        execution_ready = bool(minute_history_ready and tick_ready)
        degraded_reason = None
        if not minute_history_ready:
            degraded_reason = "minute_history_stale_or_missing"
        elif not tick_ready:
            degraded_reason = "tick_order_flow_unavailable"
        return {
            "minute_history_ready": bool(minute_history_ready),
            "minute_history_age_seconds": (
                round(float(minute_history_age_seconds), 3)
                if minute_history_age_seconds is not None
                else None
            ),
            "order_flow_source": order_flow_source,
            "tick_ready": bool(tick_ready),
            "execution_ready": execution_ready,
            "degraded_reason": degraded_reason,
        }

    async def _recent_quote_history(self, app_symbol: str, *, snapshot_end: datetime, tick_size: float) -> list[dict[str, Any]]:
        from_time = max(snapshot_end - timedelta(minutes=20), datetime.combine(snapshot_end.astimezone(IST).date(), SESSION_OPEN, tzinfo=IST).astimezone(timezone.utc))
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    """
                    SELECT time, ltp, bid, ask, bid_qty, ask_qty
                    FROM market_ticks
                    WHERE symbol = :symbol
                      AND time >= :from_time
                      AND time <= :snapshot_end
                    ORDER BY time ASC
                    LIMIT 400
                    """
                ),
                {"symbol": app_symbol, "from_time": from_time, "snapshot_end": snapshot_end},
            )
            rows = result.mappings().all()
        history = []
        for row in rows:
            ltp = float(row["ltp"] or 0.0)
            if ltp <= 0:
                continue
            bid = float(row["bid"] or 0.0)
            ask = float(row["ask"] or 0.0)
            if bid <= 0:
                bid = round(ltp - tick_size, 2)
            if ask <= 0:
                ask = round(ltp + tick_size, 2)
            history.append(
                {
                    "timestamp": _iso(row["time"]),
                    "bid": bid,
                    "ask": ask,
                    "bid_size": max(float(row["bid_qty"] or 0.0), 1.0),
                    "ask_size": max(float(row["ask_qty"] or 0.0), 1.0),
                    "last_price": ltp,
                }
            )
        if history:
            latest_tick = market_data_router.get_latest_tick(app_symbol)
            if latest_tick and latest_tick.timestamp and latest_tick.timestamp.astimezone(timezone.utc) > _ensure_dt(history[-1]["timestamp"]):
                history.append(
                    {
                        "timestamp": latest_tick.timestamp.astimezone(timezone.utc).isoformat(),
                        "bid": float(latest_tick.bid or latest_tick.ltp - tick_size),
                        "ask": float(latest_tick.ask or latest_tick.ltp + tick_size),
                        "bid_size": max(float(latest_tick.bid_qty or 0.0), 1.0),
                        "ask_size": max(float(latest_tick.ask_qty or 0.0), 1.0),
                        "last_price": float(latest_tick.ltp or 0.0),
                    }
                )
        return history

    def _recent_trade_prints_from_history(self, quote_history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(quote_history) < 2:
            return []
        prints = []
        prev = quote_history[0]
        for item in quote_history[1:]:
            last_price = float(item["last_price"])
            prev_price = float(prev["last_price"])
            side = "buy" if last_price >= prev_price else "sell"
            prints.append(
                {
                    "timestamp": item["timestamp"],
                    "price": last_price,
                    "quantity": 1.0,
                    "aggressor_side": side,
                }
            )
            prev = item
        return prints[-120:]

    def _bar_quote(self, row: dict[str, Any], *, tick_size: float) -> dict[str, Any]:
        close_price = float(row["close"])
        return {
            "timestamp": _iso(row["time"]),
            "bid": round(close_price - tick_size, 2),
            "ask": round(close_price + tick_size, 2),
            "bid_size": max(float(row.get("volume", 0.0) or 0.0) / 12.0, 10.0),
            "ask_size": max(float(row.get("volume", 0.0) or 0.0) / 12.0, 10.0),
            "last_price": close_price,
        }

    def _depth_from_quote_history(self, history: list[dict[str, Any]], *, tick_size: float) -> dict[str, Any]:
        latest = history[-1]
        return {
            "timestamp": latest["timestamp"],
            "bids": [
                {"price": round(float(latest["bid"]) - tick_size * idx, 2), "quantity": round(max(float(latest["bid_size"]) * (1 - idx * 0.18), 1.0), 2)}
                for idx in range(3)
            ],
            "asks": [
                {"price": round(float(latest["ask"]) + tick_size * idx, 2), "quantity": round(max(float(latest["ask_size"]) * (1 - idx * 0.18), 1.0), 2)}
                for idx in range(3)
            ],
        }

    def _build_profile(
        self,
        symbol_code: str,
        *,
        rows: list[dict[str, Any]],
        tick_size: float,
        period_minutes: int,
        initial_balance_periods: int,
        prior_snapshot: Any | None,
        scope: str,
    ) -> dict[str, Any]:
        raw = self._raw_profile_snapshot(symbol_code, rows, tick_size, period_minutes, initial_balance_periods, prior_snapshot=prior_snapshot)
        shape = _shape_from_snapshot(raw)
        direction = _direction_from_snapshot(raw)
        return _profile_payload(raw, scope=scope, shape=shape, direction_bias=direction, completed=True)

    def _raw_profile_snapshot(
        self,
        symbol_code: str,
        rows: list[dict[str, Any]],
        tick_size: float,
        period_minutes: int,
        initial_balance_periods: int,
        *,
        prior_snapshot: Any | None = None,
    ):
        engine = MarketProfileEngine(
            {
                "period_minutes": period_minutes,
                "tick_size": tick_size,
                "value_area_pct": PROFILE_CONFIG["value_area_pct"],
                "initial_balance_periods": initial_balance_periods,
                "min_tail_tpos": 2,
                "balance_overlap_min": 0.65,
            }
        )
        bars = [_row_to_bar(row) for row in rows]
        return engine.build_profile(f"{symbol_code} FMP", bars, prior_profile=prior_snapshot)

    def _build_hourly_profiles(self, symbol_code: str, rows: list[dict[str, Any]], *, tick_size: float) -> list[dict[str, Any]]:
        three_minute_rows = _aggregate_rows(rows, int(PROFILE_CONFIG["hourly_period_minutes"]))
        grouped: dict[int, list[dict[str, Any]]] = {}
        for row in three_minute_rows:
            hour_num = _hour_number(_to_ist(row["time"]))
            if hour_num > 6:
                continue
            grouped.setdefault(hour_num, []).append(row)

        profiles: list[dict[str, Any]] = []
        for hour_num in sorted(grouped):
            hour_rows = grouped[hour_num]
            if len(hour_rows) < 2:
                continue
            raw = self._raw_profile_snapshot(
                symbol_code,
                hour_rows,
                tick_size,
                int(PROFILE_CONFIG["hourly_period_minutes"]),
                int(PROFILE_CONFIG["hourly_initial_balance_periods"]),
            )
            shape = _shape_from_snapshot(raw)
            direction = _direction_from_snapshot(raw)
            payload = _profile_payload(
                raw,
                scope="hourly",
                shape=shape,
                direction_bias=direction,
                hour_number=hour_num,
                completed=len(hour_rows) >= 20,
            )
            payload["window_start"] = hour_rows[0]["time"]
            payload["window_end"] = hour_rows[-1]["time"]
            profiles.append(payload)
        return profiles

    async def _persist_hourly_profiles(self, symbol_code: str, session_date: str, profiles: list[dict[str, Any]]) -> None:
        rows = []
        for profile in profiles:
            if not profile.get("completed"):
                continue
            rows.append(
                {
                    "time": _ensure_dt(profile["window_end"]).astimezone(timezone.utc),
                    "symbol": symbol_code,
                    "session_date": date.fromisoformat(session_date),
                    "hour_num": int(profile["hour_number"]),
                    "window_start": _ensure_dt(profile["window_start"]).astimezone(timezone.utc),
                    "window_end": _ensure_dt(profile["window_end"]).astimezone(timezone.utc),
                    "ib_high": float(profile["initial_balance_high"]),
                    "ib_low": float(profile["initial_balance_low"]),
                    "vah": float(profile["vah"]),
                    "val": float(profile["val"]),
                    "poc": float(profile["poc"]),
                    "shape": str(profile["shape"]),
                    "direction_bias": str(profile["direction_bias"]),
                    "single_prints": json.dumps(profile["single_prints"]),
                    "tpo_rows": json.dumps(profile["tpo_rows"]),
                    "poor_high": bool(profile["poor_high"]),
                    "poor_low": bool(profile["poor_low"]),
                    "tick_size": float(profile["tick_size"]),
                    "value_migration_score": int(profile.get("value_migration_score") or 0),
                    "source": "fmp_live_snapshot",
                }
            )
        if not rows:
            return
        async with AsyncSessionLocal() as session:
            try:
                await session.execute(
                    text(
                        """
                        INSERT INTO hourly_profiles (
                            time, symbol, session_date, hour_num, window_start, window_end,
                            ib_high, ib_low, vah, val, poc, shape, direction_bias,
                            single_prints, tpo_rows, poor_high, poor_low, tick_size,
                            value_migration_score, source, updated_at
                        ) VALUES (
                            :time, :symbol, :session_date, :hour_num, :window_start, :window_end,
                            :ib_high, :ib_low, :vah, :val, :poc, :shape, :direction_bias,
                            CAST(:single_prints AS jsonb), CAST(:tpo_rows AS jsonb), :poor_high, :poor_low, :tick_size,
                            :value_migration_score, :source, NOW()
                        )
                        ON CONFLICT (symbol, session_date, hour_num) DO UPDATE SET
                            time = EXCLUDED.time,
                            window_start = EXCLUDED.window_start,
                            window_end = EXCLUDED.window_end,
                            ib_high = EXCLUDED.ib_high,
                            ib_low = EXCLUDED.ib_low,
                            vah = EXCLUDED.vah,
                            val = EXCLUDED.val,
                            poc = EXCLUDED.poc,
                            shape = EXCLUDED.shape,
                            direction_bias = EXCLUDED.direction_bias,
                            single_prints = EXCLUDED.single_prints,
                            tpo_rows = EXCLUDED.tpo_rows,
                            poor_high = EXCLUDED.poor_high,
                            poor_low = EXCLUDED.poor_low,
                            tick_size = EXCLUDED.tick_size,
                            value_migration_score = EXCLUDED.value_migration_score,
                            source = EXCLUDED.source,
                            updated_at = NOW()
                        """
                    ),
                    rows,
                )
                await session.commit()
            except Exception as exc:
                await session.rollback()
                logger.warning(f"Skipping hourly_profiles persistence for {symbol_code}: {exc}")

    def _daily_references(self, session_lookup: dict[date, list[dict[str, Any]]], current_date: date) -> dict[str, float]:
        prior_dates = sorted(key for key in session_lookup if key < current_date)[-20:]
        if not prior_dates:
            return {"avg_daily_ib": 1.0, "avg_atr": 1.0}
        daily_ibs = []
        atrs = []
        previous_close = None
        for session_date in prior_dates:
            rows = session_lookup[session_date]
            ib_rows = [row for row in rows if _to_ist(row["time"]) < _session_start(session_date) + timedelta(minutes=60)]
            if ib_rows:
                daily_ibs.append(max(float(row["high"]) for row in ib_rows) - min(float(row["low"]) for row in ib_rows))
            session_high = max(float(row["high"]) for row in rows)
            session_low = min(float(row["low"]) for row in rows)
            true_range = session_high - session_low
            if previous_close is not None:
                true_range = max(true_range, abs(session_high - previous_close), abs(session_low - previous_close))
            previous_close = float(rows[-1]["close"])
            atrs.append(true_range)
        return {
            "avg_daily_ib": round(sum(daily_ibs) / max(len(daily_ibs), 1), 2),
            "avg_atr": round(sum(atrs) / max(len(atrs), 1), 2),
        }

    def _adaptive_tick_size(self, symbol_code: str, avg_atr: float) -> float:
        lot_based = float(LOT_SIZES.get(symbol_code, 1)) * 0.1
        atr_based = round(max(avg_atr, 1.0) / 50.0, 2)
        return round(max(lot_based, atr_based, 0.5), 2)

    async def _load_live_rows(self, symbol_code: str) -> tuple[list[dict[str, Any]], str, str]:
        if symbol_code.upper() == "CRUDEOIL":
            try:
                from market_data.commodity_runtime_history import load_commodity_history_rows

                rows, history_symbol = await load_commodity_history_rows(
                    symbol_code,
                    interval="1minute",
                    lookback_days=10,
                )
                if rows:
                    return rows, "commodity_broker_history", history_symbol
            except Exception:
                pass

        if settings.MARKET_INTELLIGENCE_STRATEGY_LOCAL_ONLY or settings.PAPER_TRADING_ONLY:
            rows, source, history_symbol = await market_intelligence_runtime.load_local_spot_rows(
                symbol_code,
                lookback_days=10,
            )
            if rows:
                return rows, source, history_symbol

        # Reuse the broker-aware minute-history fallback already hardened for Auction IQ.
        try:
            from auction_intelligence.live import _fetch_recent_minute_rows as _fetch_shared_recent_minute_rows

            rows, source, history_symbol = await _fetch_shared_recent_minute_rows(
                symbol_code,
                lookback_days=10,
                allow_live_broker_refresh=True,
            )
            if rows:
                return rows, source, history_symbol
        except Exception:
            pass

        from_date = date.today() - timedelta(days=10)
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    """
                    SELECT time, open, high, low, close, volume
                    FROM underlying_spot_candles
                    WHERE underlying = :underlying
                      AND interval = '1minute'
                      AND time >= :from_time
                    ORDER BY time ASC
                    """
                ),
                {"underlying": symbol_code, "from_time": datetime.combine(from_date, time.min, tzinfo=IST).astimezone(timezone.utc)},
            )
            rows = result.mappings().all()
        payload = [
            {
                "time": _iso(row["time"]),
                "open": float(row["open"] or row["close"] or 0.0),
                "high": float(row["high"] or row["close"] or 0.0),
                "low": float(row["low"] or row["close"] or 0.0),
                "close": float(row["close"] or 0.0),
                "volume": float(row["volume"] or 0.0),
            }
            for row in rows
        ]
        if payload:
            return payload, "timescaledb_spot_1minute", symbol_code

        local_rows = self._load_local_csv_rows(symbol_code)
        return local_rows, "local_csv_spot", f"{symbol_code}.1minute.csv.gz"

    def _load_local_csv_rows(self, symbol_code: str) -> list[dict[str, Any]]:
        path = analytics_root() / "spot" / f"underlying={symbol_code}" / "1minute.csv.gz"
        if not path.exists():
            raise RuntimeError(f"Local CSV history missing for {symbol_code}.")
        rows: list[dict[str, Any]] = []
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                try:
                    timestamp = _ensure_dt(row["time"])
                except Exception:
                    continue
                rows.append(
                    {
                        "time": timestamp.isoformat(),
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                        "volume": float(row.get("volume") or 0.0),
                    }
                )
        return rows

    def _normalize_symbol(self, symbol_code: str) -> str:
        normalized = str(symbol_code or "").upper().strip()
        if normalized not in SUPPORTED_SYMBOLS:
            raise ValueError(f"Unsupported FMP symbol: {symbol_code}")
        return normalized

    def _replay_cache_path(self, symbol_code: str) -> Path:
        REPLAY_ROOT.mkdir(parents=True, exist_ok=True)
        return REPLAY_ROOT / f"{symbol_code.lower()}_replay.json"

    def _load_cached_replay(self, symbol_code: str) -> dict[str, Any] | None:
        cache_path = self._replay_cache_path(self._normalize_symbol(symbol_code))
        if not cache_path.exists():
            return None
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def _overlap(self, a_low: float, a_high: float, b_low: float, b_high: float) -> float:
        overlap = max(0.0, min(a_high, b_high) - max(a_low, b_low))
        union = max(a_high, b_high) - min(a_low, b_low)
        return overlap / union if union > 0 else 0.0


fmp_service = FractalMarketProfileService()
