"""Vanguard's scheduler — the thing that makes the lane accumulate live data.

WHY THIS EXISTS: every other lane in this repo runs as an in-process agent
booted in the backend's lifespan (paper_strategy_agent, commodity_strategy_agent,
rl_auto_trainer). Vanguard had none of that, so it only advanced when a human
typed `make daily-cycle` — and its tables sat 17 hours to a month stale while
the upstream feeds it reads were perfectly current. This daemon closes that gap
WITHOUT putting research code inside the live API process.

WHAT IT DELIBERATELY DOES NOT DO: it does not backfill. Each pass appends the
current bar using short lookbacks (see the Makefile's live-cycle comment for
why the deep-history `features` target is the wrong thing to run per bar).

CADENCE — ONE RULE, applied by data provenance (owner directive 2026-08-28):

    A step is EOD only if the EXCHANGE publishes its input once a day, or it
    is a rollup that by construction needs the finished session. EVERYTHING
    ELSE IS A LIVE MARKET READING and runs on the bar grid.

Anything DERIVED from live prices is still a live reading — solving IV from
premiums, aggregating a vol surface, scoring options flow. The derivation does
not make it daily. Applying the rule moved implied-vol, IV surface, sentiment,
M2 flow and M4 sector RS off the EOD pass, where they had been parked on two
mistaken premises: that solving IV "needs settled end-of-session prints" (it
inverts whatever prints exist, mid-session included), and that M2 needs a
settled OI level (it needs the CURRENT bar's OI against the PREVIOUS session's,
true at every bar).

  live pass  on each 30-minute bar boundary + LIVE_PASS_DELAY_S, market hours
             only: implied vol -> IV surface -> sentiment -> M2 flow, then
             M3 regime, M4 sector RS and M5 timing, then M6 selects on that bar
             and M9 journals what it filled or closed. ~3.6 min measured.
  eod pass   once, after the close: OI positioning (MWPL is a genuine
             once-daily exchange publication), M10 attribution and the
             cross-sectional IC study (both rollups of the finished session).

M2 IS SCHEDULED. Leaving it out is what broke the lane: features_flow froze at
2026-07-28 and M6's flow_fresh leg, which allows 3 sessions, was handed 23 — so
every candidate died at that leg and no ticket was emitted after 2026-07-23.

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
import threading
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
VANGUARD_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = os.environ.get("VANGUARD_PYTHON", sys.executable)

# ── THE BAR GRID, and why it is :15/:45 rather than :00/:30 ────────────────
# NSE's equity session opens at 09:15, so its 30-minute bars are labelled
# 09:15, 09:45, 10:15 ... 15:15 (start-of-bar; m5_timing.py verifies this
# against live data). A bar labelled 09:15 is COMPLETE at 09:45.
#
# This daemon used to wake on the wall-clock :00/:30 grid, which is offset
# from the exchange's by 15 minutes. Every live pass therefore evaluated a bar
# that had closed roughly 17 minutes earlier -- on a 30-minute IGNITION
# trigger, more than half the signal's life was spent waiting for the
# scheduler. Confirmed in this daemon's own log for 2026-08-27: passes at
# 10:02, 10:32, 11:02, 11:32 against bars that closed at 09:45, 10:15, 10:45
# and 11:15.
#
# Waking on the exchange grid instead means each pass reads a bar that closed
# LIVE_PASS_DELAY_S ago, and nothing else about the cadence changes.
SESSION_OPEN = (9, 15)
SESSION_CLOSE = (15, 30)
BAR_MINUTES = 30
# Bar-close times run from the first completed bar (09:45) to the last
# (15:30, the close itself). The live window is bounded by those, not by the
# session open -- there is no completed bar to evaluate before 09:45.
FIRST_BAR_CLOSE = (9, 45)
LAST_BAR_CLOSE = (15, 30)
# Wait this long past a bar boundary before reading it — the upstream candle
# writers need a moment to land the bar we are about to derive features from.
#
# WHAT THIS DELAY CAN AND CANNOT ABSORB (2026-08-27). The F&O stock spot grid is
# now written in-session by the backend's `stock_spot_intraday` runner, which
# polls Upstox's intraday candle endpoint on its own 30-minute cadence. That
# cadence is NOT phase-aligned to the exchange's bar closes, so the gap between
# a bar closing and its row existing is uniform over 0-30 minutes and no fixed
# delay can cover it.
#
# The consequence is bounded and self-healing rather than silent: a pass whose
# bar has not landed yet simply finds the previous bar as the newest on-grid
# row and evaluates that, and the next pass picks up the missed one. M6 prints
# how old the bar it evaluated actually is, and the freshness legs reject inputs
# that have aged out, so the lane can never mistake a late bar for a current
# one. Five minutes covers the common case where the sweep has recently run.
LIVE_PASS_DELAY_S = int(os.environ.get("VANGUARD_LIVE_PASS_DELAY_S", "300"))
# Slack past LAST_BAR_CLOSE within which the final live pass may still run, so
# the 15:15 bar (closing at 15:30) gets evaluated like every other one.
LIVE_WINDOW_SLACK_S = 600
# Run the EOD pass at the first tick at/after this time.
EOD_AT = (15, 45)

# THE CADENCE RULE (owner directive, 2026-08-28): a step is EOD only if its
# input is something the EXCHANGE publishes once a day. Everything else is a
# live market reading and belongs on the bar grid, however it was scheduled
# before. Anything DERIVED from live prices -- solved IV, a vol surface, a
# flow score -- is a live reading too: the derivation does not make it daily.
#
# Applying that rule moved implied-vol, IV surface, sentiment, M2 and M4 off
# the EOD pass. They had been parked there for two different bad reasons: the
# IV chain was assumed to need "settled end-of-session prints" (it does not --
# it solves from whatever prints exist), and M2 was assumed to need a settled
# OI level. What M2 actually needs is for the CURRENT bar's OI to be compared
# against the PREVIOUS SESSION's, which is true at every bar of the day.
#
# ORDER IS LOAD-BEARING, and it is a straight dependency chain:
#   implied vol -> IV surface -> sentiment      (each reads what the last wrote)
#   IV surface + open interest -> M2 flow
#   M2 + M3 + M4 + M5 -> M6 -> M9
# Measured cost of the whole pass: ~3.6 min against a 30-minute bar.
LIVE_STEPS: list[tuple[str, list[str]]] = [
    # Solved from live option premiums, not read from a broker column. -1 day:
    # the live pass only needs TODAY re-solved; the backfill is a separate,
    # deliberate run. 9s measured.
    ("implied vol", ["features/m_implied_vol.py", "--lookback-days", "1", "--write"]),
    # Same-expiry premium/IV structure at each observed option bar. Ratios are
    # model inputs, never hard filters; thin chains keep their wing fields NULL.
    ("option premium ratios", ["features/m_option_ratios.py", "--lookback-days", "3", "--write"]),
    # The surface keys on dt, so an intraday run UPSERTS today's row with the
    # latest prints -- which is exactly what a live reading is. The 120-day
    # window is read (for iv_percentile/iv_rank), not recomputed from scratch.
    # 17s measured.
    ("IV surface", ["features/m_iv_surface.py", "--lookback-days", "120", "--write"]),
    ("sentiment", ["features/m_sentiment.py", "--lookback-days", "120", "--write"]),
    # Futures OI ticks intraday, so it lives on the live grid (cadence rule
    # above). ~221 front contracts against the public v3 intraday endpoint at
    # the 0.4s throttle: ~90s. Baselines recompute over the recent window so
    # today's running row gets scored against the settled history each bar.
    ("futures OI live", ["ingest/futures_oi.py", "--live"]),
    ("futures OI baselines", ["features/m_futures_oi.py", "--lookback-days", "90"]),
    # M2 options flow. Writes only the as-of session's rows; the lookback exists
    # to give the rolling z-score/percentile windows their history. 50s measured.
    ("M2 flow", ["features/m2_flow.py", "--lookback-days", "130"]),
    ("M3 regime", ["features/m3_gex.py", "--lookback-days", "60"]),
    # Sector relative strength is computed from sector INDEX PRICES, which tick
    # all day. It is not an exchange publication, so it does not belong at EOD
    # -- and M6's sector_rs leg allows only 3 sessions of age, so a daily
    # cadence spent a third of its own tolerance before the first bar. 4s.
    ("M4 sector RS", ["features/m4_sector.py"]),
    # --no-spot-check matters on the live path: without it, m5_timing widens
    # its own window back to the earliest hardcoded SPOT_CHECKS date (currently
    # 2026-08-10) no matter what --lookback-days says, so a "3-day" live pass
    # was silently recomputing weeks of bars every 30 minutes. The spot checks
    # are a development aid, not a scheduled job.
    ("M5 timing", ["features/m5_timing.py", "--lookback-days", "3", "--buffer-days", "60",
                    "--write", "--no-spot-check"]),
    ("M6 select", ["fusion/m6_select.py", "--write"]),
    # Immutable pre-close 1-2 session ranking. The command is a cheap no-op
    # unless today's causal 14:15 decision bar and both shadow rankers exist.
    # It has no ticket/order imports; M7 remains sizing-only for execution lanes.
    ("pre-close swing watchlist", ["model/preclose_swing.py"]),
    # Mark yesterday's frozen list from the exact option contracts seen in the
    # current session. This is observation only: it writes no ticket/order.
    ("model watchlist marks", ["model/watchlist.py", "--track"]),
    ("M9 paper", ["paper/engine.py"]),
    # MP-edge paper book (gap_overnight / oversold_mtf). Idempotent: on the
    # first bar of a session it settles yesterday's gap trades at the 09:15
    # open that has just arrived; the rest of the day it is a no-op until the
    # EOD pass writes new features_mp flags and the next --run opens entries.
    ("MP edges paper", ["paper/mp_edges.py", "--run"]),
    # Independent, durable views for all three named strategies. Swing marks
    # use exact contracts; the UI overlays the 150 ms quote bus between bars.
    ("strategy journals", ["journal/strategy_lanes.py", "--sync", "--track-swing"]),
]
# EOD is now reserved for its actual definition: an input the EXCHANGE
# publishes once a day, or a rollup that by construction needs the finished
# session. Nothing here is merely "expensive" or "historically ran at night" --
# that reasoning is what stranded five live readings on this pass.
EOD_STEPS: list[tuple[str, list[str]]] = [
    # MWPL is a genuine once-daily exchange publication, and the price leg needs
    # a settled close. This one is EOD by its source, not by convention.
    ("OI positioning", ["features/m_oi_positioning.py", "--lookback-days", "30", "--write"]),
    # Settled daily futures candles replace the live intraday row for today
    # (the daily endpoint excludes the current session until settlement, so
    # this is EOD by its source). Baselines then rescore on settled data.
    ("futures OI EOD", ["ingest/futures_oi.py", "--eod", "--days", "7"]),
    ("futures OI baselines EOD", ["features/m_futures_oi.py", "--lookback-days", "90"]),
    # A nightly rollup of the day's own journal -- EOD by definition.
    ("M10 attribution", ["journal/attribution.py", "--write"]),
    # The cross-sectional IC study reads the day's candidate_evaluations, which
    # only exist once every live pass has run -- hence EOD, not per bar. It is
    # the only measurement in the lane that can currently fail M2, because it
    # scores the whole universe rather than the handful of names that already
    # passed M2's own filter.
    ("cross-section IC", ["research/cross_section_ic.py", "--write"]),
    # Separate from M6: predict the underlying's direction over the next one
    # and two sessions from the final completed feature snapshot. This writes
    # shadow predictions only and has no ticket/order code path.
    ("1-2 session directional shadow", ["model/score_directional_swing.py", "--write"]),
    # Freeze the final prediction snapshot before training the next version.
    # Membership is immutable; tomorrow's live passes only update marks.
    ("freeze model watchlist", ["model/watchlist.py", "--freeze-latest", "--track"]),
    # Deliberately freeze model versions during prospective observation.
    # Research training is an explicit offline action, never a nightly
    # threshold search against a repeatedly inspected test set.
    # Market Profile + order-flow structure (features_mp). EOD because the
    # session profile only exists at the close; the two flags it emits
    # (sig_strong_close, sig_oversold_mtf) are both close-of-session signals
    # by construction. ~75s measured over 225 names / 220-day warm-up.
    ("MP structure", ["features/m_market_profile.py", "--lookback-days", "220",
                      "--write-days", "3", "--write"]),
    # Runs right after the flags are written so the evening pass OPENS the
    # positions the next morning's first live pass will settle.
    ("MP edges paper", ["paper/mp_edges.py", "--run"]),
    ("strategy journals", ["journal/strategy_lanes.py", "--sync", "--track-swing"]),
]

# Exact-contract watchlist marks are inexpensive and need not wait for the
# feature pipeline. They read the existing 3-minute archive every minute; the
# browser overlays the quote bus at sub-second cadence between persisted marks.
REALTIME_MARK_SECONDS = int(os.environ.get("VANGUARD_REALTIME_MARK_SECONDS", "60"))
REALTIME_STEPS: list[tuple[str, list[str]]] = [
    ("strategy journals", ["journal/strategy_lanes.py", "--sync", "--track-swing"]),
]


def log(message: str) -> None:
    print(f"{datetime.now(IST):%Y-%m-%d %H:%M:%S} IST | {message}", flush=True)


def in_live_window(now: datetime) -> bool:
    """True when a completed 30-minute bar exists that has not yet aged out.

    Bounded by bar CLOSES (09:45 .. 15:30 + slack), not by the session open:
    running at 09:20 would evaluate yesterday's last bar all over again.
    """
    if now.weekday() >= 5:  # Sat/Sun
        return False
    first = now.replace(hour=FIRST_BAR_CLOSE[0], minute=FIRST_BAR_CLOSE[1], second=0, microsecond=0)
    last = now.replace(hour=LAST_BAR_CLOSE[0], minute=LAST_BAR_CLOSE[1], second=0, microsecond=0)
    return first <= now <= last + timedelta(seconds=LIVE_WINDOW_SLACK_S)


# Retained under its old name because the log line and one test refer to it;
# the live window is what actually gates a pass.
in_market_hours = in_live_window


def bar_closes(day: datetime) -> list[datetime]:
    """Every 30-minute bar CLOSE in `day`'s session, on the exchange's own
    09:15-anchored grid: 09:45, 10:15, ... 15:15, 15:30."""
    start = day.replace(hour=SESSION_OPEN[0], minute=SESSION_OPEN[1], second=0, microsecond=0)
    end = day.replace(hour=SESSION_CLOSE[0], minute=SESSION_CLOSE[1], second=0, microsecond=0)
    closes = []
    cursor = start + timedelta(minutes=BAR_MINUTES)
    while cursor < end:
        closes.append(cursor)
        cursor += timedelta(minutes=BAR_MINUTES)
    closes.append(end)   # the 15:15 bar closes at the 15:30 bell, not at 15:45
    return closes


def next_bar_boundary(now: datetime) -> datetime:
    """The next exchange bar close strictly after `now`, plus the read delay.

    Falls through to the first bar close of the next calendar day once today's
    session is done, so the loop always has something to sleep until.
    """
    for close in bar_closes(now):
        candidate = close + timedelta(seconds=LIVE_PASS_DELAY_S)
        if candidate > now:
            return candidate
    tomorrow = (now + timedelta(days=1)).replace(
        hour=FIRST_BAR_CLOSE[0], minute=FIRST_BAR_CLOSE[1], second=0, microsecond=0)
    return tomorrow + timedelta(seconds=LIVE_PASS_DELAY_S)


def _verdict(stdout: str) -> str:
    """The last line a step printed, which is where these steps put their verdict.

    Without it the log said `pre-close swing watchlist: ok (3s)` on a day the
    emitter had actually returned `created: False, reason: no liquid contract
    expressions` -- a step reporting success while producing nothing, which is
    how a lane goes quiet unnoticed. Only the final line, and truncated: some
    steps print a 200-row funnel above it.
    """
    lines = [line.strip() for line in (stdout or "").splitlines() if line.strip()]
    if not lines:
        return ""
    tail = lines[-1]
    return f" :: {tail[:180]}{'…' if len(tail) > 180 else ''}"


def run_steps(label: str, steps: list[tuple[str, list[str]]], *, quiet: bool = False) -> None:
    if not quiet:
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
                log(f"   {name}: ok ({took:.0f}s){_verdict(proc.stdout)}")
            else:
                tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
                log(f"   {name}: FAILED rc={proc.returncode} ({took:.0f}s) :: {' | '.join(tail)}")
        except subprocess.TimeoutExpired:
            log(f"   {name}: TIMEOUT after {time.monotonic() - started:.0f}s — skipping to next step")
        except Exception as exc:  # never let one step kill the scheduler
            log(f"   {name}: ERROR {type(exc).__name__}: {exc}")
    if not quiet:
        log(f"── {label} pass done ──")


# The option-chain sweep for a 30-minute bar finishes 45-60 minutes after it,
# so the 14:45 entry bar is still filling in when the last live pass runs: the
# EOD pass on 2026-09-04 marked 1 of the day's 20 swing items and the other 19
# would have sat on "awaiting entry" until the next morning. Marks are a cheap
# read of an archive that is still being written, so they keep running for a
# while after the last bar close. Nothing here evaluates or selects.
MARK_TAIL_MINUTES = int(os.environ.get("VANGUARD_MARK_TAIL_MINUTES", "75"))


def in_mark_window(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    return in_live_window(now) or (
        now.hour >= 9 and now <= now.replace(hour=LAST_BAR_CLOSE[0], minute=LAST_BAR_CLOSE[1],
                           second=0, microsecond=0)
        + timedelta(seconds=LIVE_WINDOW_SLACK_S)
        + timedelta(minutes=MARK_TAIL_MINUTES))


# M1 archive collectors run after publication, and announcements hourly.
# This worker is independent of feature computation and paper mark refresh.
M1_DAILY_STEPS = [
    ("participant OI", ["ingest/m1_participant_oi.py", "--backfill-days", "7"]),
    ("bhavcopy delivery", ["ingest/bhavcopy_delivery.py", "--backfill-days", "7"]),
    ("bulk block", ["ingest/bulk_block.py", "--backfill-days", "7"]),
    ("USDINR", ["ingest/m_usdinr_fx.py", "--lookback-days", "30"]),
    ("corporate disclosures", ["ingest/corporate_announcements.py"]),
]
M1_HOURLY_STEPS = [
    ("USDINR retry", ["ingest/m_usdinr_fx.py", "--lookback-days", "30"]),
    ("announcements and results", ["ingest/corporate_announcements.py", "--skip-insider"]),
]


def m1_due(now, daily_done, hourly_done):
    if now.weekday() >= 5:
        return None
    if now.hour >= 20 and daily_done != now.date():
        return "daily"
    if 8 <= now.hour < 20 and hourly_done != (now.date(), now.hour):
        return "hourly"
    return None


def m1_feed_loop():
    daily_done = hourly_done = None
    while True:
        now = datetime.now(IST)
        due = m1_due(now, daily_done, hourly_done)
        if due == "daily":
            run_steps("M1 daily archives", M1_DAILY_STEPS)
            daily_done = now.date()
        elif due == "hourly":
            run_steps("M1 hourly disclosures", M1_HOURLY_STEPS)
            hourly_done = (now.date(), now.hour)
        time.sleep(60)


def realtime_mark_loop() -> None:
    """Persist lightweight marks independently of the multi-minute M1-M6 pass."""
    while True:
        now = datetime.now(IST)
        if in_mark_window(now):
            # One informative line a minute, not three uninformative ones.
            run_steps("realtime marks", REALTIME_STEPS, quiet=True)
        time.sleep(max(15, REALTIME_MARK_SECONDS))


def main() -> int:
    log(f"Vanguard cycle daemon up. python={PYTHON} root={VANGUARD_ROOT}")
    log(f"live pass: at each NSE 30m bar close (09:45, 10:15 ... 15:30 IST) "
        f"+{LIVE_PASS_DELAY_S}s, Mon-Fri; eod pass at {EOD_AT[0]:02d}:{EOD_AT[1]:02d} IST")
    log("cadence rule: EOD is only for exchange-published inputs; every derived "
        "market reading (implied vol, IV surface, sentiment, M2, M3, M4, M5) is live")

    eod_done_for: date | None = None
    threading.Thread(target=m1_feed_loop, name="vanguard-M1", daemon=True).start()
    threading.Thread(target=realtime_mark_loop, name="vanguard-realtime-marks", daemon=True).start()

    # Run one live pass immediately if we are already mid-session, so a restart
    # during market hours does not idle until the next boundary.
    if in_live_window(datetime.now(IST)):
        run_steps("live (startup catch-up)", LIVE_STEPS)

    while True:
        now = datetime.now(IST)
        # Wake at whichever comes first: the next bar close, or today's EOD.
        # Realigning the bar grid to the exchange's 09:15 anchor moved the last
        # live wake-up to 15:32, BEFORE the 15:45 EOD -- on the old :00/:30
        # grid the 16:02 wake-up happened to cover it. Sleeping straight to
        # tomorrow's first bar would silently skip M4 and M10 every day, so the
        # EOD time is now a wake reason in its own right rather than something
        # the bar grid is assumed to step over.
        target = next_bar_boundary(now)
        eod_target = now.replace(hour=EOD_AT[0], minute=EOD_AT[1], second=0, microsecond=0)
        if eod_done_for != now.date() and now.weekday() < 5 and eod_target > now:
            target = min(target, eod_target)
        sleep_s = max(5.0, (target - now).total_seconds())
        log(f"sleeping {sleep_s / 60:.1f}m until {target:%H:%M:%S} IST")
        time.sleep(sleep_s)

        now = datetime.now(IST)
        eod_at = now.replace(hour=EOD_AT[0], minute=EOD_AT[1], second=0, microsecond=0)
        if now >= eod_at and eod_done_for != now.date() and now.weekday() < 5:
            run_steps("eod", EOD_STEPS)
            eod_done_for = now.date()
            continue
        if in_live_window(now):
            run_steps("live", LIVE_STEPS)
        else:
            log("outside the live bar window — no pass")


if __name__ == "__main__":
    sys.exit(main())
