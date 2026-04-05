from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date, datetime, timedelta, timezone
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


def _load_upstox_token() -> str:
    if not DEFAULT_CREDS_PATH.exists():
        return ""
    payload = json.loads(DEFAULT_CREDS_PATH.read_text())
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
    return parser.parse_args()


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


async def _run() -> int:
    _configure_logging()
    args = _parse_args()
    token = _load_upstox_token()
    if not token:
        print("No saved Upstox token found in backend/credentials.json")
        return 1

    sync = UpstoxResearchSync(
        access_token=token,
        from_date=date.fromisoformat(args.from_date),
        to_date=date.fromisoformat(args.to_date),
        risk_free_rate=args.risk_free_rate,
        upstox_gap_seconds=args.upstox_gap_seconds,
    )

    if args.daemon:
        persisted_state = _load_runtime_state()
        history = list(persisted_state.get("history") or [])
        while True:
            cycle_started_at = datetime.now(timezone.utc)
            latest_token = _load_upstox_token().strip()
            if not latest_token:
                logger.warning("No saved Upstox token found in backend/credentials.json")
            elif latest_token != sync.client.access_token:
                logger.info("Reloaded Upstox access token from saved credentials for research sync")
                sync.client.set_access_token(latest_token)
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

    result = await sync.run_once(
        underlying_limit=args.underlying_limit,
        expiry_limit=args.expiry_limit,
        spot_limit=args.spot_limit,
        contract_limit=args.contract_limit,
    )
    print(json.dumps(result, indent=2))
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
