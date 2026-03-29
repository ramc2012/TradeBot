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
    poll_minutes: int,
    now_utc: datetime | None = None,
) -> tuple[datetime, float, float]:
    current_time = now_utc or datetime.now(timezone.utc)
    elapsed_seconds = max(0.0, (current_time - cycle_started_at).total_seconds())
    target_cycle_seconds = max(0.0, poll_minutes * 60)
    sleep_seconds = max(0.0, target_cycle_seconds - elapsed_seconds)
    next_run_at = cycle_started_at + timedelta(seconds=target_cycle_seconds)
    if sleep_seconds <= 0:
        next_run_at = current_time
    return next_run_at, elapsed_seconds, sleep_seconds


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

            completed_at = datetime.now(timezone.utc)
            next_run_at, elapsed_seconds, sleep_seconds = _compute_cycle_schedule(
                cycle_started_at=cycle_started_at,
                poll_minutes=args.poll_minutes,
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
