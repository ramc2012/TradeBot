"""Repository functions for paper_* tables. Raw SQL via asyncpg, no ORM."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg


# ─── ticks ──────────────────────────────────────────────────────────
async def insert_tick(pool: asyncpg.Pool, row: dict) -> None:
    await pool.execute(
        """
        INSERT INTO paper_ticks
            (ts, symbol, instrument, ltp, last_qty,
             bid_px_1, ask_px_1, bid_qty_1, ask_qty_1, oi, raw)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        """,
        row["ts"], row["symbol"], row["instrument"], row["ltp"], row.get("last_qty"),
        row.get("bid_px_1"), row.get("ask_px_1"), row.get("bid_qty_1"), row.get("ask_qty_1"),
        row.get("oi"), json.dumps(row.get("raw", {})),
    )


async def insert_ticks_batch(pool: asyncpg.Pool, rows: list[dict]) -> None:
    if not rows:
        return
    records = [
        (
            r["ts"], r["symbol"], r["instrument"], r["ltp"], r.get("last_qty"),
            r.get("bid_px_1"), r.get("ask_px_1"), r.get("bid_qty_1"), r.get("ask_qty_1"),
            r.get("oi"), json.dumps(r.get("raw", {})),
        )
        for r in rows
    ]
    await pool.copy_records_to_table(
        "paper_ticks",
        records=records,
        columns=["ts", "symbol", "instrument", "ltp", "last_qty",
                 "bid_px_1", "ask_px_1", "bid_qty_1", "ask_qty_1", "oi", "raw"],
    )


async def recent_ticks(
    pool: asyncpg.Pool, symbol: str, lookback_seconds: int
) -> list[dict]:
    rows = await pool.fetch(
        """
        SELECT ts, ltp, last_qty, bid_px_1, ask_px_1, bid_qty_1, ask_qty_1
        FROM paper_ticks
        WHERE symbol = $1 AND ts > NOW() - ($2 || ' seconds')::INTERVAL
        ORDER BY ts ASC
        """,
        symbol, str(lookback_seconds),
    )
    return [dict(r) for r in rows]


async def session_ticks(
    pool: asyncpg.Pool, symbol: str, session_open: datetime, decision_ts: datetime
) -> list[dict]:
    rows = await pool.fetch(
        """
        SELECT ts, ltp, last_qty
        FROM paper_ticks
        WHERE symbol = $1 AND ts >= $2 AND ts < $3
        ORDER BY ts ASC
        """,
        symbol, session_open, decision_ts,
    )
    return [dict(r) for r in rows]


# ─── signals ────────────────────────────────────────────────────────
async def insert_signal(pool: asyncpg.Pool, sig: dict) -> int:
    return await pool.fetchval(
        """
        INSERT INTO paper_signals
            (decision_ts, instrument, symbol, setup_name, side,
             entry_price, stop_price, target_price,
             p_win, expected_net_R, in_distribution,
             gate_decision, gate_reason, features, model_artifact, run_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
        RETURNING signal_id
        """,
        sig["decision_ts"], sig["instrument"], sig["symbol"], sig["setup_name"], sig["side"],
        sig["entry_price"], sig["stop_price"], sig["target_price"],
        sig.get("p_win"), sig.get("expected_net_R"), sig["in_distribution"],
        sig["gate_decision"], sig.get("gate_reason"),
        json.dumps(sig["features"], default=str), sig["model_artifact"], sig["run_id"],
    )


# ─── orders & positions ─────────────────────────────────────────────
async def insert_order(pool: asyncpg.Pool, order: dict) -> int:
    return await pool.fetchval(
        """
        INSERT INTO paper_orders
            (signal_id, placed_ts, instrument, symbol, side, qty,
             intended_price, fill_ts, fill_price, slippage_inr, status)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        RETURNING order_id
        """,
        order["signal_id"], order["placed_ts"], order["instrument"], order["symbol"],
        order["side"], order["qty"], order["intended_price"],
        order.get("fill_ts"), order.get("fill_price"), order.get("slippage_inr"),
        order["status"],
    )


async def insert_position(pool: asyncpg.Pool, pos: dict) -> int:
    return await pool.fetchval(
        """
        INSERT INTO paper_positions
            (signal_id, open_order_id, instrument, symbol, side, qty,
             entry_ts, entry_price, stop_price, target_price, status)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'open')
        RETURNING position_id
        """,
        pos["signal_id"], pos["open_order_id"], pos["instrument"], pos["symbol"],
        pos["side"], pos["qty"], pos["entry_ts"], pos["entry_price"],
        pos["stop_price"], pos["target_price"],
    )


async def close_position(pool: asyncpg.Pool, position_id: int, close: dict) -> None:
    await pool.execute(
        """
        UPDATE paper_positions
        SET close_order_id = $2, exit_ts = $3, exit_price = $4,
            outcome = $5, gross_pnl = $6, costs_inr = $7,
            net_pnl = $8, net_R = $9, mae = $10, mfe = $11, status = 'closed'
        WHERE position_id = $1
        """,
        position_id, close["close_order_id"], close["exit_ts"], close["exit_price"],
        close["outcome"], close["gross_pnl"], close["costs_inr"],
        close["net_pnl"], close["net_R"], close.get("mae"), close.get("mfe"),
    )


async def open_positions(pool: asyncpg.Pool) -> list[dict]:
    rows = await pool.fetch("SELECT * FROM paper_positions WHERE status = 'open'")
    return [dict(r) for r in rows]


# ─── daily P&L + runs ───────────────────────────────────────────────
async def upsert_daily_pnl(pool: asyncpg.Pool, row: dict) -> None:
    await pool.execute(
        """
        INSERT INTO paper_daily_pnl
            (date, n_signals, n_taken, n_skipped,
             gross_pnl, costs_inr, net_pnl, consec_losses, kill_switch_tripped)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        ON CONFLICT (date) DO UPDATE SET
            n_signals = EXCLUDED.n_signals,
            n_taken = EXCLUDED.n_taken,
            n_skipped = EXCLUDED.n_skipped,
            gross_pnl = EXCLUDED.gross_pnl,
            costs_inr = EXCLUDED.costs_inr,
            net_pnl = EXCLUDED.net_pnl,
            consec_losses = EXCLUDED.consec_losses,
            kill_switch_tripped = EXCLUDED.kill_switch_tripped
        """,
        row["date"], row["n_signals"], row["n_taken"], row["n_skipped"],
        row["gross_pnl"], row["costs_inr"], row["net_pnl"],
        row["consec_losses"], row["kill_switch_tripped"],
    )


async def get_daily_pnl(pool: asyncpg.Pool, date) -> dict | None:
    row = await pool.fetchrow("SELECT * FROM paper_daily_pnl WHERE date = $1", date)
    return dict(row) if row else None


async def insert_run(pool: asyncpg.Pool, run: dict) -> UUID:
    await pool.execute(
        """
        INSERT INTO paper_runs (run_id, started_ts, model_artifact, config_hash, git_sha, notes)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        run["run_id"], run["started_ts"], run["model_artifact"],
        run["config_hash"], run["git_sha"], run.get("notes"),
    )
    return run["run_id"]
