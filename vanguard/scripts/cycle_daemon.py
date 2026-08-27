"""Vanguard's scheduler — the thing that makes the lane accumulate live data.

WHY THIS EXISTS: every other lane in this repo runs as an in-process agent
booted in the backend's lifespan (paper_strategy_agent, commodity_strategy_agent,
rl_auto_trainer). Vanguard had none of that, so it only advanced when a human
typed `make daily-cycle` — and its tables sat 17 hours to a month stale while
the upstream feeds it reads were perfectly current. This daemon closes that gap
WITHOUT putting research code inside the live API process.

WHAT IT DELIBERATELY DOES NOT DO: it does not backfill. Each pass appends the
current bar using short lookbacks (see the Makefile's live-cycle comment for
why the deep-history `features` target is the wrong thing to run per bar). It
also never runs M2 — that module's IV source died on 2026-07-28 and running it
today would write saturated single-ingredient flow scores instead of leaving an
honest gap. Wire M2 into the EOD pass the day a live IV feed lands.

CADENCE, matched to what each feature actually is:
  live pass  on each 30-minute bar boundary + LIVE_PASS_DELAY_S, during market
             hours only. M3 regime and M5 timing are bar-cadence; M6 then
             selects on that bar and M9 journals what it filled or closed.
  eod pass   once, after the close. M4 sector RS is session-cadence and M10
             attribution is a nightly rollup by definition.

FAILURE POLICY: a failing step is logged and the loop continues. A scheduler
that dies on one bad bar is worse than one that skips it — the next bar is
usually fine, and every attempt is visible in the log either way (doctrine #5).
Steps are individually idempotent, so a skipped pass self-heals on the next one.

HOLIDAYS: no exchange calendar is consulted, deliberately. On a holiday the
upstream candle tables simply have no new bar, so M3/M5 write nothing new and
M6 finds no fresh timing row — the pass is a cheap no-op rather than a wrong
answer. Encoding a holiday list here would be a second source of truth that
silently rots.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
VANGUARD_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = os.environ.get("VANGUARD_PYTHON", sys.executable)

# NSE equity/F&O session. The live pass runs only inside this window.
MARKET_OPEN = (9, 15)
MARKET_CLOSE = (15, 30)
# Wait this long past a bar boundary before reading it — the upstream candle
# writers need a moment to land the bar we are about to derive features from.
LIVE_PASS_DELAY_S = int(os.environ.get("VANGUARD_LIVE_PASS_DELAY_S", "120"))
# Run the EOD pass at the first tick at/after this time.
EOD_AT = (15, 45)

LIVE_STEPS: list[tuple[str, list[str]]] = [
    ("M3 regime", ["features/m3_gex.py", "--lookback-days", "60"]),
    ("M5 timing", ["features/m5_timing.py", "--lookback-days", "3", "--buffer-days", "60", "--write"]),
    ("M6 select", ["fusion/m6_select.py", "--write"]),
    ("M9 paper", ["paper/engine.py"]),
]
EOD_STEPS: list[tuple[str, list[str]]] = [
    ("M4 sector RS", ["features/m4_sector.py"]),
    ("M10 attribution", ["journal/attribution.py", "--write"]),
]


def log(message: str) -> None:
    print(f"{datetime.now(IST):%Y-%m-%d %H:%M:%S} IST | {message}", flush=True)


def in_market_hours(now: datetime) -> bool:
    if now.weekday() >= 5:  # Sat/Sun
        return False
    open_at = now.replace(hour=MARKET_OPEN[0], minute=MARKET_OPEN[1], second=0, microsecond=0)
    close_at = now.replace(hour=MARKET_CLOSE[0], minute=MARKET_CLOSE[1], second=0, microsecond=0)
    return open_at <= now <= close_at


def next_bar_boundary(now: datetime) -> datetime:
    """The next :00/:30 boundary strictly after `now`, plus the read delay."""
    base = now.replace(second=0, microsecond=0)
    minute = 0 if base.minute < 30 else 30
    boundary = base.replace(minute=minute)
    while boundary <= now:
        boundary += timedelta(minutes=30)
    return boundary + timedelta(seconds=LIVE_PASS_DELAY_S)


def run_steps(label: str, steps: list[tuple[str, list[str]]]) -> None:
    log(f"── {label} pass starting ──")
    for name, argv in steps:
        started = time.monotonic()
        try:
            proc = subprocess.run(
                [PYTHON, *argv],
                cwd=VANGUARD_ROOT,
                capture_output=True,
                text=True,
                timeout=int(os.environ.get("VANGUARD_STEP_TIMEOUT_S", "1500")),
            )
            took = time.monotonic() - started
            if proc.returncode == 0:
                log(f"   {name}: ok ({took:.0f}s)")
            else:
                tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
                log(f"   {name}: FAILED rc={proc.returncode} ({took:.0f}s) :: {' | '.join(tail)}")
        except subprocess.TimeoutExpired:
            log(f"   {name}: TIMEOUT after {time.monotonic() - started:.0f}s — skipping to next step")
        except Exception as exc:  # never let one step kill the scheduler
            log(f"   {name}: ERROR {type(exc).__name__}: {exc}")
    log(f"── {label} pass done ──")


def main() -> int:
    log(f"Vanguard cycle daemon up. python={PYTHON} root={VANGUARD_ROOT}")
    log(f"live pass: every 30m +{LIVE_PASS_DELAY_S}s within "
        f"{MARKET_OPEN[0]:02d}:{MARKET_OPEN[1]:02d}-{MARKET_CLOSE[0]:02d}:{MARKET_CLOSE[1]:02d} IST, "
        f"Mon-Fri; eod pass at {EOD_AT[0]:02d}:{EOD_AT[1]:02d} IST")
    log("M2 is intentionally NOT scheduled — its IV source died 2026-07-28 (see README)")

    eod_done_for: date | None = None

    # Run one live pass immediately if we are already mid-session, so a restart
    # during market hours does not idle until the next boundary.
    if in_market_hours(datetime.now(IST)):
        run_steps("live (startup catch-up)", LIVE_STEPS)

    while True:
        now = datetime.now(IST)
        target = next_bar_boundary(now)
        sleep_s = max(5.0, (target - now).total_seconds())
        log(f"sleeping {sleep_s / 60:.1f}m until {target:%H:%M:%S} IST")
        time.sleep(sleep_s)

        now = datetime.now(IST)
        eod_at = now.replace(hour=EOD_AT[0], minute=EOD_AT[1], second=0, microsecond=0)
        if now >= eod_at and eod_done_for != now.date() and now.weekday() < 5:
            run_steps("eod", EOD_STEPS)
            eod_done_for = now.date()
            continue
        if in_market_hours(now):
            run_steps("live", LIVE_STEPS)
        else:
            log("outside market hours — no pass")


if __name__ == "__main__":
    sys.exit(main())
