"""Capture prospective inputs, run isolated books, or read their report."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import signal

from edge_research.archive import Archive
from edge_research.engine import ShadowEngine
from edge_research.report import read_report


async def capture(args, config):
    from edge_research.sources import LiveSource

    args.archive.parent.mkdir(parents=True, exist_ok=True)
    with args.archive.with_suffix(".writer.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        archive = Archive(args.archive, config, datetime.now(timezone.utc).isoformat())
        engine, source = ShadowEngine(archive, config), LiveSource()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        try:
            while not stop.is_set():
                errors = 0
                for symbol, market in config["symbols"].items():
                    held = [
                        json.loads(r[0])["contract"]
                        for r in archive.db.execute("SELECT payload FROM states")
                        if json.loads(r[0])["symbol"] == symbol
                    ]
                    try:
                        frame, quotes = await source.capture(symbol, market, held)
                        engine.observe(frame, quotes)
                    except Exception as exc:
                        errors += 1
                        # Never persist broker response bodies, auth headers, or
                        # database connection strings as error text.
                        with archive.db:
                            archive.event(
                                "capture_error",
                                datetime.now(timezone.utc).isoformat(),
                                {
                                    "symbol": symbol,
                                    "error_type": type(exc).__name__,
                                    "sqlstate": getattr(
                                        getattr(exc, "orig", None), "sqlstate", None
                                    ),
                                    "database_diagnostic": (
                                        str(exc.orig).splitlines()[0][:300]
                                        if type(exc).__name__ == "ProgrammingError"
                                        else None
                                    ),
                                },
                            )
                with archive.db:
                    archive.event(
                        "heartbeat",
                        datetime.now(timezone.utc).isoformat(),
                        {"symbols": len(config["symbols"]), "capture_errors": errors},
                    )
                if not args.watch:
                    break
                try:
                    await asyncio.wait_for(stop.wait(), timeout=args.interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            await source.close()
            archive.close()
    print(json.dumps(read_report(args.archive), indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["capture", "report"])
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument(
        "--config", type=Path, default=Path(__file__).with_name("frozen_v1.json")
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Keep collecting prospectively until stopped",
    )
    parser.add_argument(
        "--unseal-holdout",
        action="store_true",
        help="Reveal final holdout only after its 20 observed sessions finish",
    )
    parser.add_argument("--interval", type=float, default=15)
    args = parser.parse_args()
    if args.interval < 15:
        parser.error("Minimum capture interval is 15 seconds")
    if args.command == "report":
        print(
            json.dumps(
                read_report(args.archive, unseal_holdout=args.unseal_holdout), indent=2
            )
        )
    else:
        config = json.loads(args.config.read_text())
        asyncio.run(capture(args, config))


if __name__ == "__main__":
    main()
