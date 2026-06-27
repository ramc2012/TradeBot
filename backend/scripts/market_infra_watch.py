#!/usr/bin/env python3
"""Market-hours infrastructure & data-plumbing watchdog.

A deterministic, dependency-light probe meant to be run on a short interval
DURING NSE market hours by the `/loop` agent. It does NOT look at strategy P&L
— it watches that the *app itself* is healthy: backend liveness, the per-service
system-health surface, broker/market-data plumbing freshness, and the latency /
status of the key feature endpoints.

Output: a compact human+machine readable report on stdout.
Exit code:
    0  → healthy (or market closed — nothing to do)
    1  → degraded/critical issues found (the loop agent should investigate/correct)
    2  → backend unreachable (hard down)

Usage:
    python -m scripts.market_infra_watch            # uses http://localhost:8000
    BASE_URL=http://localhost:8000 python -m scripts.market_infra_watch --json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN = (9, 15)
MARKET_CLOSE = (15, 30)

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")

# (path, friendly name, latency budget seconds). Feature endpoints we expect to
# answer quickly and correctly while the desk is live. Keep this list cheap.
FEATURE_ENDPOINTS: list[tuple[str, str, float]] = [
    ("/health", "liveness", 2.0),
    ("/api/system/health", "system_health", 6.0),
    ("/api/system/overview", "system_overview", 8.0),
    ("/api/directional-options/summary", "directional_summary", 6.0),
    ("/api/directional-options/paper-summary", "directional_paper", 6.0),
    ("/api/macd-refined/summary", "macd_refined_summary", 6.0),
]

# Data-plumbing freshness ceiling — a service whose last update is older than
# this while the market is open is a plumbing problem (frozen feed / dead WS).
STALE_FEED_SECONDS = 180.0


def ist_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(IST)


def _has_nse_session(now: datetime) -> bool | None:
    """Whether NSE trades on `now`'s date per the trading calendar (holidays +
    weekends). Returns None if the calendar isn't importable, so the caller can
    fall back to a weekday-only gate. Without this, the watchdog false-alarms on
    holidays (broker session legitimately absent / feed stale = no market)."""
    try:
        from core.trading_calendar import trading_calendar

        return bool(trading_calendar.has_exchange_session("NSE", now.date()))
    except Exception:
        return None


def market_is_open(now: datetime | None = None) -> tuple[bool, str]:
    now = now or ist_now()
    session = _has_nse_session(now)
    if session is None:
        # Calendar unavailable — fall back to a weekday-only gate.
        if now.weekday() >= 5:  # Sat/Sun
            return False, f"weekend ({now:%A})"
    elif not session:
        # Holiday or weekend per the calendar — markets closed all day.
        return False, f"holiday/non-session ({now:%Y-%m-%d %a})"
    after_open = (now.hour, now.minute) >= MARKET_OPEN
    before_close = (now.hour, now.minute) <= MARKET_CLOSE
    if after_open and before_close:
        return True, f"open ({now:%H:%M IST})"
    return False, f"outside 09:15–15:30 IST ({now:%H:%M})"


def _get(path: str, timeout: float) -> tuple[int, float, object | None, str | None]:
    """Return (status_code, elapsed_seconds, parsed_json_or_None, error_or_None)."""
    url = f"{BASE_URL}{path}"
    started = time.monotonic()
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout + 5.0) as resp:
            body = resp.read()
            elapsed = time.monotonic() - started
            try:
                return resp.status, elapsed, json.loads(body), None
            except json.JSONDecodeError:
                return resp.status, elapsed, None, "non-json body"
    except urllib.error.HTTPError as exc:
        return exc.code, time.monotonic() - started, None, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return 0, time.monotonic() - started, None, str(exc)


def run() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON only")
    parser.add_argument("--force", action="store_true", help="probe even when market is closed")
    args = parser.parse_args()

    now = ist_now()
    is_open, why = market_is_open(now)
    report: dict[str, object] = {
        "ts_ist": now.isoformat(),
        "market": why,
        "base_url": BASE_URL,
        "endpoints": [],
        "issues": [],
        "data_plumbing": [],
    }

    if not is_open and not args.force:
        report["status"] = "market_closed"
        print(json.dumps(report) if args.json else f"[{now:%H:%M IST}] market closed — {why}; no checks run.")
        return 0

    issues: list[str] = report["issues"]  # type: ignore[assignment]
    endpoints: list[dict] = report["endpoints"]  # type: ignore[assignment]

    # 1) Liveness first — if the backend is hard-down, everything else is moot.
    status, elapsed, _body, err = _get("/health", 2.0)
    if status == 0:
        report["status"] = "backend_down"
        issues.append(f"backend unreachable at {BASE_URL}/health: {err}")
        print(json.dumps(report) if args.json else f"CRITICAL: backend unreachable — {err}")
        return 2

    # 2) Feature endpoints — status code + latency budget.
    health_payload: dict | None = None
    for path, name, budget in FEATURE_ENDPOINTS:
        code, secs, body, e = _get(path, budget)
        row = {"name": name, "path": path, "status": code, "latency_s": round(secs, 3), "budget_s": budget}
        if e:
            row["error"] = e
        endpoints.append(row)
        if name == "system_health" and isinstance(body, dict):
            health_payload = body
        if code != 200:
            issues.append(f"{name} returned {code or 'ERR'} ({e or 'non-200'})")
        elif secs > budget:
            issues.append(f"{name} slow: {secs:.2f}s > {budget:.1f}s budget")

    # 3) System-health service breakdown + data-plumbing freshness.
    if isinstance(health_payload, dict):
        summary = health_payload.get("summary") or {}
        report["system_status"] = summary.get("status")
        for svc in health_payload.get("services") or []:
            if not isinstance(svc, dict):
                continue
            state = str(svc.get("status") or "")
            label = svc.get("label") or svc.get("key")
            if state in {"degraded", "critical"}:
                issues.append(f"service '{label}' is {state}: {svc.get('detail') or ''}".strip())
            # Data-plumbing freshness on the live-data services. The system
            # 'healthy' label is NOT enough — a feed can be ws_connected yet
            # frozen (the Mac-mini sleep-gap failure mode). Assert the actual
            # tick age and WS state from the market_data meta.
            key = str(svc.get("key") or "")
            meta = svc.get("meta") if isinstance(svc.get("meta"), dict) else {}
            if key == "market_data":
                ws_ok = bool(meta.get("ws_connected", True))
                tick_age = meta.get("last_tick_age_seconds")
                subs = set(meta.get("subscribed_symbols") or [])
                missing = [s for s in (meta.get("required_symbols") or []) if s not in subs]
                report["data_plumbing"].append({  # type: ignore[attr-defined]
                    "service": label, "status": state, "ws_connected": ws_ok,
                    "last_tick_age_s": tick_age, "missing_required": missing,
                })
                if not ws_ok:
                    issues.append("market-data WS disconnected (ws_connected=false)")
                if isinstance(tick_age, (int, float)) and tick_age > STALE_FEED_SECONDS:
                    issues.append(
                        f"market-data feed STALE: last tick {tick_age:.0f}s ago "
                        f"(>{STALE_FEED_SECONDS:.0f}s) — frozen feed despite '{state}' status"
                    )
                if missing:
                    issues.append(f"market-data missing required symbols: {missing}")
            elif key == "brokers" or key.startswith("broker"):
                ready = meta.get("broker_ready")
                report["data_plumbing"].append({  # type: ignore[attr-defined]
                    "service": label, "status": state,
                    "broker_ready": ready, "connected": meta.get("connected_brokers"),
                })
                if ready is False:
                    issues.append("no broker session ready for market data")
        if str(summary.get("status")) in {"degraded", "critical"}:
            report.setdefault("system_status_note", summary.get("status"))

    report["status"] = "issues" if issues else "healthy"

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        head = f"[{now:%H:%M IST}] infra={report['status']} system={report.get('system_status','?')}"
        print(head)
        for ep in endpoints:
            flag = "" if ep["status"] == 200 and ep["latency_s"] <= ep["budget_s"] else "  <-- check"
            print(f"  {ep['name']:<22} {ep['status']:<4} {ep['latency_s']:.2f}s{flag}")
        if report["data_plumbing"]:
            print("  data-plumbing:")
            for dp in report["data_plumbing"]:  # type: ignore[attr-defined]
                if "last_tick_age_s" in dp:
                    age = dp.get("last_tick_age_s")
                    extra = f"ws={dp.get('ws_connected')} tick_age={age if age is None else round(float(age), 1)}s"
                    if dp.get("missing_required"):
                        extra += f" missing={dp['missing_required']}"
                elif "broker_ready" in dp:
                    extra = f"ready={dp.get('broker_ready')} {dp.get('connected')}"
                else:
                    extra = ""
                print(f"    {dp['service']}: {dp['status']} {extra}")
        if issues:
            print("  ISSUES:")
            for it in issues:
                print(f"    - {it}")

    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(run())
