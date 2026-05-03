from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from loguru import logger

from data.upstox_research_sync import UpstoxResearchSync


BACKEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CREDS_PATH = BACKEND_DIR / "credentials.json"
RUNTIME_DIR = BACKEND_DIR / "runtime"
STATE_FILE = RUNTIME_DIR / "research_sync_status.json"
MAX_DAEMON_SLEEP_SECONDS = 15.0
DEFAULT_BACKLOG_POLL_SECONDS = 60.0
DEFAULT_ERROR_POLL_SECONDS = 180.0
RUNTIME_HISTORY_RETENTION_HOURS = 48
RUNTIME_HISTORY_MAX_ENTRIES = 256
IST = timezone(timedelta(hours=5, minutes=30))


def _load_upstox_token() -> str:
    try:
        from api.routers.auth import (
            _broker_credentials,
            _token_is_expired,
            get_broker_token,
            load_persistent_credentials,
        )

        load_persistent_credentials()
        token = get_broker_token("upstox")
        if token:
            upstox_creds = _broker_credentials.get("upstox", {})
            if not _token_is_expired(token, expires_at=upstox_creds.get("expires_at")):
                return token.strip()
            logger.warning("Saved Upstox token is expired; waiting for reconnect")
    except Exception as exc:
        logger.warning(f"Could not load Upstox token through credential store: {exc}")

    if not DEFAULT_CREDS_PATH.exists():
        return ""
    try:
        payload = json.loads(DEFAULT_CREDS_PATH.read_text())
    except Exception as exc:
        logger.warning(f"Could not read {DEFAULT_CREDS_PATH}: {exc}")
        return ""
    return str(payload.get("upstox", {}).get("access_token", "")).strip()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_runtime_state(payload: dict) -> None:
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        safe_payload = {
            **payload,
            "updated_at": _utc_now_iso(),
        }
        STATE_FILE.write_text(json.dumps(safe_payload, indent=2))
    except Exception as exc:
        logger.warning(f"Could not write research sync runtime state: {exc}")


def _load_runtime_state() -> dict:
    try:
        if not STATE_FILE.exists():
            return {}
        return json.loads(STATE_FILE.read_text())
    except Exception as exc:
        logger.warning(f"Could not read research sync runtime state: {exc}")
        return {}


def _parse_iso_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_hhmm(value: str) -> time:
    try:
        hour_text, minute_text = value.split(":", 1)
        parsed = time(hour=int(hour_text), minute=int(minute_text))
    except Exception as exc:
        raise argparse.ArgumentTypeError(
            f"Invalid HH:MM time value: {value!r}"
        ) from exc
    return parsed


def _window_session_date(local_dt: datetime, *, start_time: time, end_time: time) -> date:
    if start_time < end_time:
        return local_dt.date()
    if local_dt.timetz().replace(tzinfo=None) < end_time:
        return local_dt.date() - timedelta(days=1)
    return local_dt.date()


def _window_status(
    now_utc: datetime,
    *,
    start_time: time,
    end_time: time,
) -> tuple[bool, datetime, date]:
    local_now = now_utc.astimezone(IST)
    now_time = local_now.timetz().replace(tzinfo=None)
    overnight = start_time >= end_time

    if not overnight:
        in_window = start_time <= now_time < end_time
        next_date = local_now.date()
        if now_time >= end_time:
            next_date += timedelta(days=1)
        next_start_local = datetime.combine(next_date, start_time, tzinfo=IST)
        return in_window, next_start_local.astimezone(timezone.utc), local_now.date()

    in_window = now_time >= start_time or now_time < end_time
    if now_time >= start_time:
        session_date = local_now.date()
        next_start_date = local_now.date() + timedelta(days=1)
    elif now_time < end_time:
        session_date = local_now.date() - timedelta(days=1)
        next_start_date = local_now.date()
    else:
        session_date = local_now.date()
        next_start_date = local_now.date()
    next_start_local = datetime.combine(next_start_date, start_time, tzinfo=IST)
    return in_window, next_start_local.astimezone(timezone.utc), session_date


def _trim_history(entries: list[dict], now_utc: datetime) -> list[dict]:
    cutoff = now_utc - timedelta(hours=RUNTIME_HISTORY_RETENTION_HOURS)
    trimmed: list[dict] = []
    for entry in entries:
        completed_at_raw = entry.get("completed_at")
        if not isinstance(completed_at_raw, str):
            continue
        try:
            completed_at = datetime.fromisoformat(completed_at_raw)
        except ValueError:
            continue
        if completed_at.tzinfo is None:
            completed_at = completed_at.replace(tzinfo=timezone.utc)
        if completed_at >= cutoff:
            trimmed.append(entry)
    return trimmed[-RUNTIME_HISTORY_MAX_ENTRIES:]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Incrementally sync Upstox F&O research data into TimescaleDB."
    )
    parser.add_argument(
        "--from-date",
        default=(date.today() - timedelta(days=365)).isoformat(),
        help="Inclusive history start date in YYYY-MM-DD format.",
    )
    parser.add_argument(
        "--to-date",
        default=date.today().isoformat(),
        help="Inclusive history end date in YYYY-MM-DD format.",
    )
    parser.add_argument(
        "--poll-minutes",
        type=int,
        default=30,
        help="Recurring sync interval in minutes when --daemon is set.",
    )
    parser.add_argument(
        "--backlog-poll-seconds",
        type=float,
        default=DEFAULT_BACKLOG_POLL_SECONDS,
        help="Short retry gap used while pending contract backlog still exists.",
    )
    parser.add_argument(
        "--underlying-limit",
        type=int,
        default=25,
        help="How many underlyings to discover expiries for per run.",
    )
    parser.add_argument(
        "--expiry-limit",
        type=int,
        default=80,
        help="How many expiry buckets to discover contracts for per run.",
    )
    parser.add_argument(
        "--spot-limit",
        type=int,
        default=25,
        help="How many underlying spot histories to sync per run.",
    )
    parser.add_argument(
        "--contract-limit",
        type=int,
        default=120,
        help="How many option contracts to sync candles for per run.",
    )
    parser.add_argument(
        "--risk-free-rate",
        type=float,
        default=0.06,
        help="Annual risk-free rate used for implied volatility and greeks.",
    )
    parser.add_argument(
        "--upstox-gap-seconds",
        type=float,
        default=1.2,
        help="Delay between Upstox API calls. Keep this conservative for long syncs.",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Keep running the sync every --poll-minutes instead of exiting after one pass.",
    )
    parser.add_argument(
        "--window-start-ist",
        type=_parse_hhmm,
        default=None,
        help="Optional IST window start (HH:MM). When set with --window-end-ist, daemon runs only inside that daily window.",
    )
    parser.add_argument(
        "--window-end-ist",
        type=_parse_hhmm,
        default=None,
        help="Optional IST window end (HH:MM). Supports overnight windows such as 16:30 to 08:45.",
    )
    parser.add_argument(
        "--daily-once-per-window",
        action="store_true",
        help="When used with an IST window, run at most one sync cycle per window session.",
    )
    return parser.parse_args()


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or str(value).strip() == "":
        return default
    return int(str(value).strip())


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None or str(value).strip() == "":
        return default
    return float(str(value).strip())


def _env_time(name: str) -> time | None:
    value = os.environ.get(name)
    if value is None or str(value).strip() == "":
        return None
    return _parse_hhmm(str(value).strip())


def _build_daemon_args_from_env() -> argparse.Namespace:
    return argparse.Namespace(
        from_date=os.environ.get(
            "RESEARCH_FROM_DATE",
            (date.today() - timedelta(days=365)).isoformat(),
        ),
        to_date=os.environ.get("RESEARCH_TO_DATE", date.today().isoformat()),
        poll_minutes=_env_int("RESEARCH_POLL_MINUTES", 30),
        backlog_poll_seconds=_env_float(
            "RESEARCH_BACKLOG_POLL_SECONDS",
            DEFAULT_BACKLOG_POLL_SECONDS,
        ),
        underlying_limit=_env_int("RESEARCH_UNDERLYING_LIMIT", 25),
        expiry_limit=_env_int("RESEARCH_EXPIRY_LIMIT", 80),
        spot_limit=_env_int("RESEARCH_SPOT_LIMIT", 25),
        contract_limit=_env_int("RESEARCH_CONTRACT_LIMIT", 120),
        risk_free_rate=_env_float("RESEARCH_RISK_FREE_RATE", 0.06),
        upstox_gap_seconds=_env_float("RESEARCH_UPSTOX_GAP_SECONDS", 1.2),
        daemon=True,
        window_start_ist=_env_time("RESEARCH_WINDOW_START_IST"),
        window_end_ist=_env_time("RESEARCH_WINDOW_END_IST"),
        daily_once_per_window=_env_bool("RESEARCH_DAILY_ONCE_PER_WINDOW", False),
    )


def _configure_logging() -> None:
    logger.remove()
    logger.add(sys.stderr, level="INFO")


def _compute_cycle_schedule(
    *,
    cycle_started_at: datetime,
    sleep_seconds: float,
    now_utc: datetime | None = None,
) -> tuple[datetime, float, float]:
    current_time = now_utc or datetime.now(timezone.utc)
    elapsed_seconds = max(0.0, (current_time - cycle_started_at).total_seconds())
    target_cycle_seconds = max(0.0, sleep_seconds)
    sleep_seconds = max(0.0, target_cycle_seconds - elapsed_seconds)
    next_run_at = cycle_started_at + timedelta(seconds=target_cycle_seconds)
    if sleep_seconds <= 0:
        next_run_at = current_time
    return next_run_at, elapsed_seconds, sleep_seconds


def _planned_sleep_seconds(
    *,
    poll_minutes: int,
    backlog_poll_seconds: float,
    last_result: dict | None,
    errored: bool,
) -> float:
    if errored:
        return DEFAULT_ERROR_POLL_SECONDS

    if not last_result:
        return max(60.0, poll_minutes * 60.0)

    pending_contracts = int(
        ((last_result.get("db_summary") or {}).get("contract_status") or {}).get("pending", 0) or 0
    )
    rate_limit_hits = int(((last_result.get("rate_limit") or {}).get("hits", 0)) or 0)
    focus_mode = str(last_result.get("focus_mode") or "")
    if pending_contracts > 0 and rate_limit_hits == 0:
        return max(15.0, backlog_poll_seconds)
    if pending_contracts > 0 and focus_mode == "backlog_drain":
        return max(60.0, min(poll_minutes * 60.0, backlog_poll_seconds * 2))
    return max(60.0, poll_minutes * 60.0)


async def _sleep_until(next_run_at: datetime) -> None:
    while True:
        remaining_seconds = (next_run_at - datetime.now(timezone.utc)).total_seconds()
        if remaining_seconds <= 0:
            return
        await asyncio.sleep(min(MAX_DAEMON_SLEEP_SECONDS, remaining_seconds))


async def _run_with_args(args: argparse.Namespace) -> int:
    token = _load_upstox_token().strip()
    sync: UpstoxResearchSync | None = None

    def _build_sync(access_token: str) -> UpstoxResearchSync:
        return UpstoxResearchSync(
            access_token=access_token,
            from_date=date.fromisoformat(args.from_date),
            to_date=date.fromisoformat(args.to_date),
            risk_free_rate=args.risk_free_rate,
            upstox_gap_seconds=args.upstox_gap_seconds,
        )

    if token:
        sync = _build_sync(token)

    if args.daemon:
        persisted_state = _load_runtime_state()
        history = list(persisted_state.get("history") or [])
        while True:
            runtime_state = _load_runtime_state()
            history = list(runtime_state.get("history") or history)
            now_utc = datetime.now(timezone.utc)

            latest_token = _load_upstox_token().strip()
            if not latest_token:
                next_run_at, elapsed_seconds, sleep_seconds = _compute_cycle_schedule(
                    cycle_started_at=now_utc,
                    sleep_seconds=min(DEFAULT_ERROR_POLL_SECONDS, max(60.0, args.poll_minutes * 60.0)),
                    now_utc=now_utc,
                )
                _write_runtime_state(
                    {
                        "state": "waiting",
                        "poll_minutes": args.poll_minutes,
                        "run_started_at": runtime_state.get("run_started_at"),
                        "run_completed_at": runtime_state.get("run_completed_at"),
                        "next_run_at": next_run_at.isoformat(),
                        "sleep_seconds": round(sleep_seconds, 2),
                        "elapsed_seconds": round(elapsed_seconds, 2),
                        "error": None,
                        "detail": "Waiting for a valid saved Upstox token in the credential store",
                        "last_result": runtime_state.get("last_result"),
                        "history": history,
                    }
                )
                logger.warning("No valid saved Upstox token found in the credential store")
                await _sleep_until(next_run_at)
                continue

            if sync is None:
                sync = _build_sync(latest_token)
            elif latest_token != sync.client.access_token:
                logger.info("Reloaded Upstox access token from saved credentials for research sync")
                sync.client.set_access_token(latest_token)

            if args.window_start_ist and args.window_end_ist:
                in_window, next_window_start, session_date = _window_status(
                    now_utc,
                    start_time=args.window_start_ist,
                    end_time=args.window_end_ist,
                )
                last_completed_at = _parse_iso_datetime(runtime_state.get("run_completed_at"))
                already_ran_window = False
                if last_completed_at and args.daily_once_per_window:
                    completed_local = last_completed_at.astimezone(IST)
                    already_ran_window = (
                        _window_session_date(
                            completed_local,
                            start_time=args.window_start_ist,
                            end_time=args.window_end_ist,
                        )
                        == session_date
                    )
                if not in_window or already_ran_window:
                    detail = (
                        "Daily research sync already completed for the current allowed window."
                        if already_ran_window
                        else "Waiting for the next allowed research sync window."
                    )
                    _write_runtime_state(
                        {
                            "state": "waiting",
                            "poll_minutes": args.poll_minutes,
                            "run_started_at": runtime_state.get("run_started_at"),
                            "run_completed_at": runtime_state.get("run_completed_at"),
                            "next_run_at": next_window_start.isoformat(),
                            "sleep_seconds": round(
                                max(0.0, (next_window_start - now_utc).total_seconds()), 2
                            ),
                            "elapsed_seconds": runtime_state.get("elapsed_seconds"),
                            "error": None,
                            "detail": detail,
                            "last_result": runtime_state.get("last_result"),
                            "history": history,
                        }
                    )
                    logger.info(
                        f"Research sync waiting until allowed window at {next_window_start.isoformat()}"
                    )
                    await _sleep_until(next_window_start)
                    continue

            cycle_started_at = datetime.now(timezone.utc)
            sync.to_date = max(sync.to_date, date.today())

            _write_runtime_state(
                {
                    "state": "running",
                    "poll_minutes": args.poll_minutes,
                    "run_started_at": cycle_started_at.isoformat(),
                    "next_run_at": None,
                    "last_result": None,
                    "history": history,
                }
            )

            try:
                result = await sync.run_once(
                    underlying_limit=args.underlying_limit,
                    expiry_limit=args.expiry_limit,
                    spot_limit=args.spot_limit,
                    contract_limit=args.contract_limit,
                )
                state = "waiting"
                error_message = None
            except Exception as exc:
                result = None
                state = "error"
                error_message = str(exc)
                logger.exception(f"Recurring research sync failed: {exc}")

            planned_sleep_seconds = _planned_sleep_seconds(
                poll_minutes=args.poll_minutes,
                backlog_poll_seconds=args.backlog_poll_seconds,
                last_result=result,
                errored=state == "error",
            )
            completed_at = datetime.now(timezone.utc)
            next_run_at, elapsed_seconds, sleep_seconds = _compute_cycle_schedule(
                cycle_started_at=cycle_started_at,
                sleep_seconds=planned_sleep_seconds,
                now_utc=completed_at,
            )

            _write_runtime_state(
                {
                    "state": state,
                    "poll_minutes": args.poll_minutes,
                    "run_started_at": cycle_started_at.isoformat(),
                    "run_completed_at": completed_at.isoformat(),
                    "next_run_at": next_run_at.isoformat(),
                    "sleep_seconds": round(sleep_seconds, 2),
                    "elapsed_seconds": round(elapsed_seconds, 2),
                    "error": error_message if state == "error" else None,
                    "last_result": result,
                    "history": history,
                }
            )
            if result is not None:
                api_calls = result.get("api_calls") or {}
                history.append(
                    {
                        "started_at": cycle_started_at.isoformat(),
                        "completed_at": completed_at.isoformat(),
                        "elapsed_seconds": round(elapsed_seconds, 2),
                        "sleep_seconds": round(sleep_seconds, 2),
                        "api_calls": {
                            "total": int(api_calls.get("total") or 0),
                            "by_endpoint": dict(api_calls.get("by_endpoint") or {}),
                        },
                        "rate_limit": dict(result.get("rate_limit") or {}),
                        "focus_mode": str(result.get("focus_mode") or ""),
                    }
                )
                history = _trim_history(history, completed_at)
                _write_runtime_state(
                    {
                        "state": state,
                        "poll_minutes": args.poll_minutes,
                        "run_started_at": cycle_started_at.isoformat(),
                        "run_completed_at": completed_at.isoformat(),
                        "next_run_at": next_run_at.isoformat(),
                        "sleep_seconds": round(sleep_seconds, 2),
                        "elapsed_seconds": round(elapsed_seconds, 2),
                        "error": error_message if state == "error" else None,
                        "last_result": result,
                        "history": history,
                    }
                )
            logger.info(
                f"Research sync daemon sleeping {sleep_seconds:.1f}s until {next_run_at.isoformat()}"
            )
            if sleep_seconds > 0:
                await _sleep_until(next_run_at)
        return 0

    if sync is None:
        print("No valid saved Upstox token found in the credential store")
        return 1

    result = await sync.run_once(
        underlying_limit=args.underlying_limit,
        expiry_limit=args.expiry_limit,
        spot_limit=args.spot_limit,
        contract_limit=args.contract_limit,
    )
    print(json.dumps(result, indent=2))
    return 0


async def run_daemon_from_env() -> None:
    await _run_with_args(_build_daemon_args_from_env())


async def _run() -> int:
    _configure_logging()
    return await _run_with_args(_parse_args())


def main() -> None:
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
