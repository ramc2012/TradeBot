from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from api.routers.auth import (  # noqa: E402
    ensure_fyers_session,
    ensure_upstox_session,
    get_active_adapter,
    get_broker_token,
    load_persistent_credentials,
)
from data.index_futures_backfill import (  # noqa: E402
    DEFAULT_START_DATE,
    backfill_index_futures,
    normalize_underlyings,
)


def _parse_date(value: str | None, *, default: date) -> date:
    if not value:
        return default
    if value == "today":
        return date.today()
    return date.fromisoformat(value)


def _parse_fyers_symbols(values: list[str]) -> dict[str, str]:
    symbols: dict[str, str] = {}
    for value in values:
        left, sep, right = str(value or "").partition("=")
        if not sep:
            raise ValueError(f"Invalid --fyers-symbol value {value!r}; expected UNDERLYING=SYMBOL")
        underlying = left.strip().upper()
        symbol = right.strip()
        if not underlying or not symbol:
            raise ValueError(f"Invalid --fyers-symbol value {value!r}; expected UNDERLYING=SYMBOL")
        symbols[underlying] = symbol
    return symbols


async def _main(args: argparse.Namespace) -> dict[str, Any]:
    # This intentionally uses the app's credential loader. On EC2, the backend
    # container has the encrypted credentials file/database state and SECRET_KEY;
    # no broker secrets need to be copied to the local development machine.
    await asyncio.to_thread(load_persistent_credentials)

    fyers_adapter = None
    upstox_token = None
    if args.source in {"auto", "fyers"}:
        if await ensure_fyers_session(force_validate=args.force_validate):
            fyers_adapter = get_active_adapter("fyers")

    if args.source in {"auto", "upstox"}:
        if await ensure_upstox_session(force_validate=args.force_validate):
            upstox_token = get_broker_token("upstox")
        else:
            upstox_token = get_broker_token("upstox")

    try:
        summary = await backfill_index_futures(
            source=args.source,
            underlyings=normalize_underlyings(args.underlying),
            from_date=_parse_date(args.from_date, default=DEFAULT_START_DATE),
            to_date=_parse_date(args.to_date, default=date.today()),
            interval=args.interval,
            fyers_adapter=fyers_adapter,
            upstox_access_token=upstox_token,
            fyers_symbols=_parse_fyers_symbols(args.fyers_symbol),
            chunk_days=args.chunk_days,
            upstox_gap_seconds=args.upstox_gap_seconds,
            export=not args.no_export,
            output_root=Path(args.output_root).resolve() if args.output_root else None,
        )
        return summary.to_dict()
    finally:
        # ensure_fyers_session wires the live data router, which may start a
        # websocket thread. A one-shot backfill must tear that down or the
        # process can stay alive after the JSON summary is written.
        try:
            from market_data import data_router as market_data_router

            await market_data_router.stop_required_feed_watchdog()
            await market_data_router.stop_mock_feed()
            await market_data_router.unsubscribe()
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill 1-minute OHLCV for NIFTY, BANKNIFTY, and SENSEX futures. "
            "Run inside the cloud backend container to use cloud-stored broker credentials."
        )
    )
    parser.add_argument("--source", choices=["auto", "fyers", "upstox"], default="auto")
    parser.add_argument("--underlying", action="append", default=[], help="Repeatable; default all three.")
    parser.add_argument("--from-date", default=DEFAULT_START_DATE.isoformat())
    parser.add_argument("--to-date", default="today")
    parser.add_argument("--chunk-days", type=int, default=60)
    # The script name says 1minute for historical reasons; the pipeline is
    # interval-agnostic. 30minute is what the Market-Profile research consumes
    # (13 bars per NSE session), and for a 5-year expired-contract sweep it is
    # ~25x fewer rows and requests than 1minute.
    parser.add_argument(
        "--interval",
        choices=["1minute", "30minute", "day"],
        default="1minute",
        help="Candle interval to fetch and store (default 1minute).",
    )
    parser.add_argument("--upstox-gap-seconds", type=float, default=0.4)
    parser.add_argument(
        "--fyers-symbol",
        action="append",
        default=[],
        help="Override continuous symbol, e.g. NIFTY=NSE:NIFTY26JUNFUT.",
    )
    parser.add_argument("--output-root", default="")
    parser.add_argument("--no-export", action="store_true")
    parser.add_argument("--force-validate", action="store_true")
    args = parser.parse_args()

    result = asyncio.run(_main(args))
    print(json.dumps(result, indent=2, sort_keys=True))
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
