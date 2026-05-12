"""Notification endpoints — manual fire of cross-desk telegram summary.

`GET /api/notifications/telegram/heartbeat` collects the cross-desk state
(commodity P&L + bucket counts, NSE strategies, data quality) and pushes
the same summary the periodic NSE report sends — useful pre-market open
to verify the wiring before the scan loop runs.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

from fastapi import APIRouter

from core.config import settings
from notifications.telegram_agent import telegram_agent


router = APIRouter(prefix="/api/notifications", tags=["notifications"])


IST = timezone(timedelta(hours=5, minutes=30))


@router.post("/telegram/heartbeat")
async def fire_telegram_heartbeat() -> dict[str, Any]:
    """Push a cross-desk summary right now. Skips delivery if Telegram is
    disabled but still returns the sections so the caller can preview them.
    """
    now = datetime.now(IST)
    sections: list[tuple[str, list[str]]] = []

    # Commodity
    try:
        from paper_engine.commodity_strategy_agent import commodity_strategy_agent

        cstatus = commodity_strategy_agent.get_status(refresh=False)
        csum = cstatus.get("summary") or {}
        kill_label = "KILL" if cstatus.get("kill_switch_active") else "live"
        commodity_lines = [
            f"Commodity [{kill_label}] — equity ₹{float(csum.get('total_equity') or 0):,.0f}; "
            f"realized ₹{float(csum.get('realized_pnl') or 0):,.0f}; "
            f"day ₹{float(csum.get('day_pnl') or 0):,.0f}; "
            f"open {int(csum.get('open_positions') or 0)}; "
            f"win {float(csum.get('win_rate') or 0):.0%}",
        ]
        buckets = {"favourable": 0, "drifting": 0, "ready": 0, "active": 0, "neutral": 0}
        for row in (cstatus.get("watchlist") or []):
            b = str(row.get("bucket") or "")
            if b in buckets:
                buckets[b] += 1
        commodity_lines.append(
            f"Buckets — ready {buckets['ready']}; active {buckets['active']}; "
            f"favourable {buckets['favourable']}; drifting {buckets['drifting']}"
        )
        sections.append(("Commodity Desk", commodity_lines))
    except Exception as exc:  # noqa: BLE001
        sections.append(("Commodity Desk", [f"unavailable: {exc}"]))

    # NSE
    try:
        from paper_engine.strategy_agent import paper_strategy_agent

        nse_status = paper_strategy_agent.get_status(refresh=False)
        nse_lines: list[str] = []
        for s in (nse_status.get("strategies") or []):
            label = s.get("label") or s.get("key")
            summary = s.get("portfolio_summary") or {}
            nse_lines.append(
                f"{label} — equity ₹{float(summary.get('total_equity') or 0):,.0f}; "
                f"realized ₹{float(summary.get('realized_pnl') or 0):,.0f}; "
                f"open {int(s.get('open_positions') or 0)}; "
                f"entries {int(s.get('entries') or 0)}; "
                f"exits {int(s.get('exits') or 0)}"
            )
        sections.append(("NSE Desk", nse_lines))
    except Exception as exc:  # noqa: BLE001
        sections.append(("NSE Desk", [f"unavailable: {exc}"]))

    # Data quality
    try:
        from market_data.data_quality_agent import data_quality_agent

        dq = data_quality_agent.snapshot()
        sections.append(
            (
                "Data Quality",
                [
                    f"overall {dq.get('overall')}; market {dq.get('market_state')}; "
                    f"{dq.get('symbol_count')} symbols; stale {dq.get('stale_count')}; "
                    f"flagged {dq.get('flagged_count')}"
                ],
            )
        )
    except Exception:  # noqa: BLE001
        pass

    title = f"Nomad Curie · Heartbeat · {now.strftime('%d %b %Y %I:%M %p IST')}"
    sent = False
    if settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID:
        sent = await telegram_agent.notify_summary(
            title=title,
            sections=sections,
            dedup_key=f"manual_heartbeat:{now.strftime('%Y%m%d%H%M%S')}",
        )
    return {
        "sent": sent,
        "title": title,
        "sections": [
            {"heading": heading, "lines": lines} for heading, lines in sections
        ],
    }
