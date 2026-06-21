"""CLI / daemon entry for the automatic historical-data backfill.

Examples
--------
One pass, then exit (good for cron or manual runs):
    python -m data.run_auto_backfill --once

Continuous daemon (used by the FastAPI lifespan when AUTO_BACKFILL_ENABLED=true):
    python -m data.run_auto_backfill --daemon --poll-minutes 60

Coverage report only (no fetching):
    python -m data.run_auto_backfill --report
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging

from core.config import settings
from data.historical_backfill import coverage_report, run_auto_backfill_once

logger = logging.getLogger(__name__)


async def _run_once(max_option_contracts: int) -> None:
    summary = await run_auto_backfill_once(max_option_contracts=max_option_contracts)
    logger.info("auto-backfill pass: %s", json.dumps(summary.get("totals", summary)))
    print(json.dumps(summary, indent=2, default=str))


async def run_daemon(poll_minutes: int, max_option_contracts: int) -> None:
    """Loop forever, sleeping poll_minutes between bounded passes."""
    logger.info("auto-backfill daemon started (poll=%sm)", poll_minutes)
    while True:
        try:
            summary = await run_auto_backfill_once(max_option_contracts=max_option_contracts)
            logger.info("auto-backfill pass totals: %s",
                        json.dumps(summary.get("totals", {}), default=str))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never let one bad pass kill the loop
            logger.exception("auto-backfill pass failed: %s", exc)
        await asyncio.sleep(max(60, poll_minutes * 60))


async def run_daemon_from_env() -> None:
    """Lifespan-friendly entry: reads cadence/limits from settings."""
    await run_daemon(settings.AUTO_BACKFILL_POLL_MINUTES,
                     settings.AUTO_BACKFILL_MAX_OPTION_CONTRACTS)


def main() -> None:
    parser = argparse.ArgumentParser(description="Automatic historical-data backfill")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="run one pass and exit")
    mode.add_argument("--daemon", action="store_true", help="run continuously")
    mode.add_argument("--report", action="store_true", help="print coverage report and exit")
    parser.add_argument("--poll-minutes", type=int,
                        default=settings.AUTO_BACKFILL_POLL_MINUTES)
    parser.add_argument("--max-option-contracts", type=int,
                        default=settings.AUTO_BACKFILL_MAX_OPTION_CONTRACTS)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if args.report:
        print(json.dumps(asyncio.run(coverage_report()), indent=2, default=str))
    elif args.once:
        asyncio.run(_run_once(args.max_option_contracts))
    else:
        asyncio.run(run_daemon(args.poll_minutes, args.max_option_contracts))


if __name__ == "__main__":
    main()
