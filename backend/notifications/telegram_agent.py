"""Unified Telegram notification agent.

Why this exists: previously only the NSE PaperStrategyAgent could push
Telegram messages. Commodity desk, FMP, Directional Options, Auction
Intelligence, kill-switch trips, paper resets, and audit-event severities
were silent. ₹-507K of commodity P&L bled without a single Telegram ping.

This module is the single owner for:
  * Event alerts — every audit event with severity ≥ TELEGRAM_EVENT_MIN_SEVERITY
    is pushed automatically (the audit_agent calls notify_event after the row
    is persisted).
  * Cross-desk heartbeats — call notify_summary(...) to push a consolidated
    view (commodity + NSE + AI + FMP + DO + DataQuality) on a cadence.

Design rules:
  * Fail-open. Telegram outage never blocks trading. Every send is wrapped
    in try/except + a hard timeout.
  * Rate-limited. Capped at TELEGRAM_RATE_LIMIT_PER_MINUTE (default 12).
    Excess events are dropped with a log line, never queued forever.
  * Deduped. Identical messages within 30 s are suppressed.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any, Optional

import httpx
from loguru import logger

from core.config import settings


_SEVERITY_ORDER = {"info": 0, "trade": 1, "success": 1, "warning": 2, "error": 3}
_SEVERITY_EMOJI = {
    "info": "ℹ️",
    "trade": "📈",
    "success": "✅",
    "warning": "⚠️",
    "error": "🛑",
}
_SEND_TIMEOUT_SECONDS = 5.0


class TelegramAgent:
    def __init__(self) -> None:
        self._send_history: deque[float] = deque(maxlen=128)
        self._recent_keys: dict[str, float] = {}
        self._dedup_window_seconds = 30.0
        self._lock = asyncio.Lock()

    # ── Configuration helpers ─────────────────────────────────────────────

    def _credentials_ready(self) -> tuple[Optional[str], Optional[str]]:
        token = (settings.TELEGRAM_BOT_TOKEN or "").strip() or None
        chat = (settings.TELEGRAM_CHAT_ID or "").strip() or None
        return token, chat

    def _min_severity_threshold(self) -> int:
        raw = str(settings.TELEGRAM_EVENT_MIN_SEVERITY or "warning").strip().lower()
        return _SEVERITY_ORDER.get(raw, 2)

    # ── Rate / dedup ──────────────────────────────────────────────────────

    def _under_rate_limit(self, now: float) -> bool:
        limit = max(int(settings.TELEGRAM_RATE_LIMIT_PER_MINUTE), 1)
        window_start = now - 60.0
        while self._send_history and self._send_history[0] < window_start:
            self._send_history.popleft()
        return len(self._send_history) < limit

    def _dedup_admit(self, key: Optional[str], now: float) -> bool:
        if not key:
            return True
        prune_before = now - self._dedup_window_seconds
        stale = [k for k, ts in self._recent_keys.items() if ts < prune_before]
        for k in stale:
            self._recent_keys.pop(k, None)
        last = self._recent_keys.get(key)
        if last is not None and now - last < self._dedup_window_seconds:
            return False
        self._recent_keys[key] = now
        return True

    # ── Public API ────────────────────────────────────────────────────────

    async def send(
        self,
        text: str,
        *,
        dedup_key: Optional[str] = None,
        parse_mode: str = "HTML",
    ) -> bool:
        """Send a free-form message. Returns True on delivery, False on skip."""
        token, chat = self._credentials_ready()
        if not token or not chat:
            return False
        if not text or not text.strip():
            return False
        async with self._lock:
            now = time.monotonic()
            if not self._dedup_admit(dedup_key, now):
                logger.debug(f"[Telegram] dropped duplicate within dedup window: {dedup_key}")
                return False
            if not self._under_rate_limit(now):
                logger.warning(
                    f"[Telegram] rate limit reached "
                    f"({settings.TELEGRAM_RATE_LIMIT_PER_MINUTE}/min); dropping message."
                )
                return False
            self._send_history.append(now)
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat,
            "text": text[:4000],
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        try:
            async with httpx.AsyncClient(timeout=_SEND_TIMEOUT_SECONDS) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code >= 400:
                    logger.warning(
                        f"[Telegram] send returned HTTP {resp.status_code}: "
                        f"{resp.text[:200]}"
                    )
                    return False
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[Telegram] send failed: {exc}")
            return False

    async def notify_event(
        self,
        *,
        market: str,
        event_type: str,
        severity: str = "info",
        message: Optional[str] = None,
        symbol: Optional[str] = None,
        underlying: Optional[str] = None,
        previous_state: Optional[str] = None,
        new_state: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
        actor: str = "system",
    ) -> bool:
        """Route an audit event to Telegram if severity gate clears."""
        if not settings.TELEGRAM_EVENT_ALERTS_ENABLED:
            return False
        sev_num = _SEVERITY_ORDER.get((severity or "info").lower(), 0)
        if sev_num < self._min_severity_threshold():
            return False
        emoji = _SEVERITY_EMOJI.get((severity or "info").lower(), "•")
        head = f"{emoji} <b>{event_type.replace('_', ' ').title()}</b>"
        bits: list[str] = [head, f"<i>{market.upper()}</i>"]
        if symbol:
            bits.append(f"sym <code>{symbol}</code>")
        elif underlying:
            bits.append(f"on <code>{underlying}</code>")
        if previous_state or new_state:
            bits.append(
                f"{previous_state or '?'} → <b>{new_state or '?'}</b>"
            )
        bits.append(f"actor <code>{actor}</code>")
        text = " · ".join(bits)
        if message:
            text = f"{text}\n{self._escape_html(message)[:600]}"
        if payload:
            # Show only the small/numeric/string scalar fields to keep messages tight.
            scalar = {
                k: v
                for k, v in payload.items()
                if isinstance(v, (str, int, float, bool))
            }
            if scalar:
                tail = ", ".join(f"<code>{k}={v}</code>" for k, v in list(scalar.items())[:8])
                text = f"{text}\n{tail}"
        dedup = f"{market}:{event_type}:{symbol or underlying or ''}:{new_state or ''}"
        return await self.send(text, dedup_key=dedup)

    async def notify_summary(
        self,
        *,
        title: str,
        sections: list[tuple[str, list[str]]],
        dedup_key: Optional[str] = None,
        respect_reports_enabled: bool = True,
    ) -> bool:
        """Push a cross-desk heartbeat. `sections` = [(heading, [lines]), ...]

        `respect_reports_enabled` controls whether
        TELEGRAM_REPORTS_ENABLED gates this send. Periodic reports set it
        True (so the operator can mute the cadence); manual heartbeats
        from the dashboard set it False because the operator explicitly
        asked for delivery in that moment.
        """
        if respect_reports_enabled and not settings.TELEGRAM_REPORTS_ENABLED:
            return False
        lines: list[str] = [f"<b>{self._escape_html(title)}</b>"]
        for heading, items in sections:
            if not items:
                continue
            lines.append(f"<b>{self._escape_html(heading)}</b>")
            for item in items[:8]:
                lines.append(self._escape_html(item))
        text = "\n".join(lines)
        return await self.send(text, dedup_key=dedup_key)

    @staticmethod
    def _escape_html(text: str) -> str:
        return (
            (text or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )


telegram_agent = TelegramAgent()


__all__ = ["telegram_agent", "TelegramAgent"]
