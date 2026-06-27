"""Alpaca US market-data adapter (options + equities).

Primary US data source for the US market module. Uses the Alpaca Market Data
REST API (v1beta1 options + v2 stocks) over httpx — no SDK dependency. Returns
the SAME `OptionChain`/`OptionChainEntry` shapes the Indian brokers return so the
existing strategy engines (MACD Refined, Directional) can consume it unchanged;
US `call`/`put` are mapped to this codebase's `CE`/`PE`.

Capability (Alpaca):
  • option-chain snapshots — strike, bid/ask, last, daily volume, IV, greeks
  • historical option bars (1/5/15/30Min, 1Day) — real premium history for MACD
  • stock/ETF bars + latest quote (spot)
  • options cover US EQUITY + ETF underlyings (SPY/QQQ/IWM/DIA as index proxies);
    SPX/NDX cash-index options are NOT on this feed.

Auth: APCA-API-KEY-ID / APCA-API-SECRET-KEY from settings (ALPACA_API_KEY_ID,
ALPACA_API_SECRET_KEY). Data feed: ALPACA_DATA_FEED (default "indicative" for
options / "iex" for stocks on the free tier; "opra"/"sip" on paid).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from loguru import logger

from brokers.base import OptionChain, OptionChainEntry
from core.config import settings

DATA_BASE = "https://data.alpaca.markets"
UTC = timezone.utc

_TF_MAP = {"1": "1Min", "5": "5Min", "15": "15Min", "30": "30Min", "60": "1Hour", "D": "1Day", "1D": "1Day"}


def parse_occ(symbol: str) -> tuple[str, str, str, float]:
    """OCC option symbol → (root, expiry_iso, CE|PE, strike).
    Format: ROOT + YYMMDD + C|P + strike×1000 (8 digits). e.g. AAPL250620C00190000."""
    s = str(symbol).strip().upper()
    strike = int(s[-8:]) / 1000.0
    cp = s[-9]
    yymmdd = s[-15:-9]
    root = s[:-15]
    expiry = f"20{yymmdd[0:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}"
    return root, expiry, ("CE" if cp == "C" else "PE"), strike


class AlpacaAdapter:
    """US market-data adapter exposing the broker-adapter surface the strategy
    engines call: get_option_chain, get_historical_candles, get_ltp,
    get_option_contracts."""

    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None

    # ── infra ──────────────────────────────────────────────────────────────
    @property
    def key_id(self) -> str:
        return str(getattr(settings, "ALPACA_API_KEY_ID", "") or "").strip()

    @property
    def secret(self) -> str:
        return str(getattr(settings, "ALPACA_API_SECRET_KEY", "") or "").strip()

    @property
    def has_credentials(self) -> bool:
        return bool(self.key_id and self.secret)

    @property
    def _stock_feed(self) -> str:
        return str(getattr(settings, "ALPACA_STOCK_FEED", "") or "iex").strip()

    @property
    def _option_feed(self) -> str:
        return str(getattr(settings, "ALPACA_OPTION_FEED", "") or "indicative").strip()

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(20.0),
                headers={"APCA-API-KEY-ID": self.key_id, "APCA-API-SECRET-KEY": self.secret},
            )
        return self._client

    async def _get(self, path: str, params: Optional[dict] = None) -> dict:
        if not self.has_credentials:
            raise RuntimeError("Alpaca credentials not configured (ALPACA_API_KEY_ID/SECRET)")
        client = self._get_client()
        resp = await client.get(f"{DATA_BASE}{path}", params=params or {})
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _coerce(*vals: Any) -> float:
        for v in vals:
            try:
                if v is None:
                    continue
                f = float(v)
                if f != 0.0:
                    return f
            except (TypeError, ValueError):
                continue
        return 0.0

    # ── spot / quotes ────────────────────────────────────────────────────
    async def get_ltp(self, symbols: list[str]) -> dict[str, float]:
        if not symbols:
            return {}
        try:
            payload = await self._get(
                "/v2/stocks/snapshots",
                {"symbols": ",".join(symbols), "feed": self._stock_feed},
            )
        except Exception as exc:
            logger.debug(f"[Alpaca] get_ltp failed: {exc}")
            return {}
        out: dict[str, float] = {}
        snaps = payload if isinstance(payload, dict) else {}
        for sym, snap in (snaps.get("snapshots", snaps) or {}).items():
            if not isinstance(snap, dict):
                continue
            lt = snap.get("latestTrade") or {}
            q = snap.get("latestQuote") or {}
            out[sym] = self._coerce(lt.get("p"), (self._coerce(q.get("bp"), 0) + self._coerce(q.get("ap"), 0)) / 2 or None)
        return out

    # ── option chain ──────────────────────────────────────────────────────
    async def get_option_chain(self, symbol: str, expiry: str | None = None) -> OptionChain:
        """Full option-chain snapshot for `symbol` (US underlying), optionally
        filtered to one expiry (ISO date). Returns OptionChain with CE/PE entries."""
        underlying = str(symbol).strip().upper()
        snapshots: dict[str, Any] = {}
        page_token: Optional[str] = None
        for _ in range(20):  # bounded pagination
            params: dict[str, Any] = {"feed": self._option_feed, "limit": 1000}
            if expiry:
                params["expiration_date"] = expiry
            if page_token:
                params["page_token"] = page_token
            try:
                payload = await self._get(f"/v1beta1/options/snapshots/{underlying}", params)
            except Exception as exc:
                logger.debug(f"[Alpaca] option snapshots {underlying} failed: {exc}")
                break
            snapshots.update(payload.get("snapshots") or {})
            page_token = payload.get("next_page_token")
            if not page_token:
                break

        spot = (await self.get_ltp([underlying])).get(underlying, 0.0)
        entries: list[OptionChainEntry] = []
        expiry_iso = expiry or ""
        for occ, snap in snapshots.items():
            try:
                _root, exp, otype, strike = parse_occ(occ)
            except Exception:
                continue
            if expiry and exp != expiry:
                continue
            if not expiry_iso:
                expiry_iso = exp
            q = snap.get("latestQuote") or {}
            t = snap.get("latestTrade") or {}
            g = snap.get("greeks") or {}
            day = snap.get("dailyBar") or {}
            bid = self._coerce(q.get("bp"))
            ask = self._coerce(q.get("ap"))
            ltp = self._coerce(t.get("p"), (bid + ask) / 2 if (bid and ask) else None)
            entries.append(OptionChainEntry(
                strike=strike, option_type=otype, ltp=ltp,
                oi=int(self._coerce(snap.get("openInterest"))),
                volume=int(self._coerce(day.get("v"))),
                bid=bid, ask=ask,
                iv=self._coerce(snap.get("impliedVolatility")) or None,
                delta=g.get("delta"), gamma=g.get("gamma"), theta=g.get("theta"), vega=g.get("vega"),
                instrument_key=occ,
            ))
        entries.sort(key=lambda e: (e.strike, e.option_type))
        return OptionChain(symbol=underlying, expiry=expiry_iso, spot_price=float(spot or 0.0), entries=entries)

    async def get_option_contracts(self, symbol: str, expiry: Optional[str] = None) -> list[dict]:
        """Distinct expiries available for `symbol` (derived from the chain)."""
        chain = await self.get_option_chain(symbol, expiry)
        exps: set[str] = set()
        # re-derive from a fresh unfiltered snapshot when no expiry given
        if expiry:
            return [{"expiry": expiry}]
        # one snapshot page is enough to enumerate near expiries
        try:
            payload = await self._get(f"/v1beta1/options/snapshots/{symbol.upper()}", {"feed": self._option_feed, "limit": 1000})
            for occ in (payload.get("snapshots") or {}):
                try:
                    exps.add(parse_occ(occ)[1])
                except Exception:
                    continue
        except Exception:
            for e in chain.entries:
                if e.instrument_key:
                    try:
                        exps.add(parse_occ(e.instrument_key)[1])
                    except Exception:
                        continue
        return [{"expiry": e} for e in sorted(exps)]

    # ── historical bars (options OR stocks) ───────────────────────────────
    async def get_historical_candles(self, symbol: str, resolution: str, range_from: str, range_to: str, cont_flag: int = 1) -> list[dict]:
        tf = _TF_MAP.get(str(resolution), "30Min")
        is_option = self._looks_like_occ(symbol)
        try:
            if is_option:
                payload = await self._get("/v1beta1/options/bars", {
                    "symbols": symbol, "timeframe": tf, "start": range_from, "end": range_to, "limit": 10000,
                })
            else:
                payload = await self._get("/v2/stocks/bars", {
                    "symbols": symbol.upper(), "timeframe": tf, "start": range_from, "end": range_to,
                    "limit": 10000, "feed": self._stock_feed, "adjustment": "raw",
                })
        except Exception as exc:
            logger.debug(f"[Alpaca] bars {symbol} failed: {exc}")
            return []
        bars = (payload.get("bars") or {})
        rows_in = bars.get(symbol) if isinstance(bars, dict) else None
        if rows_in is None and isinstance(bars, dict):
            rows_in = bars.get(symbol.upper())
        out: list[dict] = []
        for b in (rows_in or []):
            out.append({
                "time": str(b.get("t")), "open": float(b.get("o", 0) or 0), "high": float(b.get("h", 0) or 0),
                "low": float(b.get("l", 0) or 0), "close": float(b.get("c", 0) or 0), "volume": int(b.get("v", 0) or 0),
            })
        return out

    @staticmethod
    def _looks_like_occ(symbol: str) -> bool:
        s = str(symbol).strip().upper()
        return len(s) >= 15 and (s[-9] in ("C", "P")) and s[-8:].isdigit()

    async def health(self) -> dict[str, Any]:
        if not self.has_credentials:
            return {"ok": False, "reason": "no_credentials"}
        try:
            snap = await self.get_ltp(["SPY"])
            return {"ok": bool(snap), "spy": snap.get("SPY"), "checked_at": datetime.now(UTC).isoformat()}
        except Exception as exc:
            return {"ok": False, "reason": str(exc)[:160]}


alpaca_adapter = AlpacaAdapter()
