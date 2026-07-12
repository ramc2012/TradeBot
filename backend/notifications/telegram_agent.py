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
from datetime import date, datetime, timezone
from typing import Any, Optional

import httpx
from loguru import logger

from core.config import settings
from core.metrics import record_telegram_send


_SEVERITY_ORDER = {"info": 0, "trade": 1, "success": 1, "warning": 2, "error": 3}
_SEVERITY_EMOJI = {
    "info": "ℹ️",
    "trade": "📈",
    "success": "✅",
    "warning": "⚠️",
    "error": "🛑",
}
_SEND_TIMEOUT_SECONDS = 5.0


# Priority sends (trade entry/exit alerts) get a larger rate allowance than the
# operator-tunable heartbeat cap — an uncapped paper scan can close many
# positions in one cycle and those alerts must not be silently shed.
_PRIORITY_RATE_MULTIPLIER = 4


class TelegramAgent:
    def __init__(self) -> None:
        self._send_history: deque[float] = deque(maxlen=512)
        self._recent_keys: dict[str, float] = {}
        self._dedup_window_seconds = 30.0
        self._lock = asyncio.Lock()
        # Health state — a dead bot token must be a visible fact, not a silent
        # zero-notification day.
        self._sent_ok = 0
        self._failed_http = 0
        self._failed_auth = 0
        self._failed_transport = 0
        self._suppressed_rate_limit = 0
        self._suppressed_dedup = 0
        self._suppressed_no_creds = 0
        self._last_success_at: Optional[datetime] = None
        self._last_failure_at: Optional[datetime] = None
        self._last_error_status: Optional[int] = None
        self._consecutive_failures = 0
        self._auth_alert_date: Optional[date] = None

    # ── Health ────────────────────────────────────────────────────────────

    def get_health(self) -> dict[str, Any]:
        return {
            "sent_ok": self._sent_ok,
            "failed_http": self._failed_http,
            "failed_auth": self._failed_auth,
            "failed_transport": self._failed_transport,
            "suppressed_rate_limit": self._suppressed_rate_limit,
            "suppressed_dedup": self._suppressed_dedup,
            "suppressed_no_creds": self._suppressed_no_creds,
            "last_success_at": self._last_success_at.isoformat() if self._last_success_at else None,
            "last_failure_at": self._last_failure_at.isoformat() if self._last_failure_at else None,
            "last_error_status": self._last_error_status,
            "consecutive_failures": self._consecutive_failures,
            "auth_failed": self._last_error_status in (401, 403) and self._consecutive_failures > 0,
        }

    def _note_success(self) -> None:
        self._sent_ok += 1
        self._last_success_at = datetime.now(timezone.utc)
        self._consecutive_failures = 0
        self._last_error_status = None
        record_telegram_send("ok")

    def _note_failure(self, status: Optional[int], kind: str) -> None:
        if kind == "auth":
            self._failed_auth += 1
        elif kind == "transport":
            self._failed_transport += 1
        else:
            self._failed_http += 1
        self._last_failure_at = datetime.now(timezone.utc)
        self._last_error_status = status
        self._consecutive_failures += 1
        record_telegram_send("auth_error" if kind == "auth" else f"{kind}_error")

    async def _maybe_emit_auth_alert(self, status: int) -> None:
        """One loud audit event per day on a dead bot token.

        Set the day stamp BEFORE emitting: the audit bridge fans events back
        into Telegram, so the re-entrant send must find the stamp already set.
        """
        today = datetime.now(timezone.utc).date()
        if self._auth_alert_date == today:
            return
        self._auth_alert_date = today
        try:
            from agentic_rag.audit_agent import record_audit_event

            await record_audit_event(
                market="GLOBAL",
                event_type="telegram_auth_failed",
                severity="error",
                message=(
                    f"Telegram bot token rejected (HTTP {status}). "
                    "All Telegram alerts are dead until the token is fixed."
                ),
                payload={"http_status": status},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[Telegram] failed to record auth-failure audit event: {exc}")

    # ── Configuration helpers ─────────────────────────────────────────────

    def _credentials_ready(self) -> tuple[Optional[str], Optional[str]]:
        token = (settings.TELEGRAM_BOT_TOKEN or "").strip() or None
        chat = (settings.TELEGRAM_CHAT_ID or "").strip() or None
        return token, chat

    def _min_severity_threshold(self) -> int:
        raw = str(settings.TELEGRAM_EVENT_MIN_SEVERITY or "warning").strip().lower()
        return _SEVERITY_ORDER.get(raw, 2)

    # ── Rate / dedup ──────────────────────────────────────────────────────

    def _under_rate_limit(self, now: float, *, priority: bool = False) -> bool:
        limit = max(int(settings.TELEGRAM_RATE_LIMIT_PER_MINUTE), 1)
        if priority:
            limit *= _PRIORITY_RATE_MULTIPLIER
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
        parse_mode: Optional[str] = "HTML",
        priority: bool = False,
    ) -> bool:
        """Send a free-form message. Returns True on delivery, False on skip.

        `priority=True` marks operator-critical sends (trade entries/exits):
        they get a larger rate allowance and their drops log at warning.
        `parse_mode=None` sends plain text (no Telegram markup parsing).
        """
        token, chat = self._credentials_ready()
        if not token or not chat:
            self._suppressed_no_creds += 1
            record_telegram_send("suppressed_no_creds")
            return False
        if not text or not text.strip():
            return False
        async with self._lock:
            now = time.monotonic()
            if not self._dedup_admit(dedup_key, now):
                self._suppressed_dedup += 1
                record_telegram_send("suppressed_dedup")
                logger.debug(f"[Telegram] dropped duplicate within dedup window: {dedup_key}")
                return False
            if not self._under_rate_limit(now, priority=priority):
                self._suppressed_rate_limit += 1
                record_telegram_send("suppressed_rate_limit")
                log = logger.warning if priority else logger.info
                log(
                    f"[Telegram] rate limit reached "
                    f"({settings.TELEGRAM_RATE_LIMIT_PER_MINUTE}/min"
                    f"{f' ×{_PRIORITY_RATE_MULTIPLIER} priority' if priority else ''}); "
                    f"dropping message."
                )
                return False
            self._send_history.append(now)
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload: dict[str, Any] = {
            "chat_id": chat,
            "text": text[:4000],
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            async with httpx.AsyncClient(timeout=_SEND_TIMEOUT_SECONDS) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code in (401, 403):
                    self._note_failure(resp.status_code, "auth")
                    logger.error(
                        f"[Telegram] BOT TOKEN REJECTED (HTTP {resp.status_code}) — "
                        f"alerts are dead until fixed: {resp.text[:200]}"
                    )
                    await self._maybe_emit_auth_alert(resp.status_code)
                    return False
                if resp.status_code >= 400:
                    self._note_failure(resp.status_code, "http")
                    logger.warning(
                        f"[Telegram] send returned HTTP {resp.status_code}: "
                        f"{resp.text[:200]}"
                    )
                    return False
            self._note_success()
            return True
        except Exception as exc:  # noqa: BLE001
            self._note_failure(None, "transport")
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
