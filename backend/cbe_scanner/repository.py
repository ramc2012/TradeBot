"""Persistence helpers for CBE scan audit trails."""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from loguru import logger
from sqlalchemy import text

from db.database import AsyncSessionLocal


async def persist_scan_payload(payload: dict[str, Any]) -> str | None:
    """Persist one scan response. Fail closed to logging, not to the scanner."""
    results = list(payload.get("results") or [])
    watchlist_symbols = {str(row.get("instrument") or "") for row in payload.get("watchlist") or []}
    try:
        async with AsyncSessionLocal() as session:
            if not await _tables_ready(session):
                return None
            run_result = await session.execute(
                text(
                    """
                    INSERT INTO cbe_scan_runs
                        (source, scan_date, universe_size, scored_count, watchlist_count, config, source_status,
                         asset_winner, composite_gate, engine_version)
                    VALUES
                        (
                            :source, :scan_date, :universe_size, :scored_count,
                            :watchlist_count, CAST(:config AS JSONB), CAST(:source_status AS JSONB),
                            :asset_winner, :composite_gate, :engine_version
                        )
                    RETURNING id
                    """
                ),
                {
                    "source": str(payload.get("source") or "unknown"),
                    "scan_date": _coerce_scan_date(payload.get("scan_date")),
                    "universe_size": int(payload.get("universe_size") or payload.get("fno_universe_size") or 0),
                    "scored_count": int(payload.get("scored_count") or 0),
                    "watchlist_count": int(payload.get("watchlist_count") or 0),
                    "config": json.dumps(payload.get("config") or {}),
                    "source_status": json.dumps(payload.get("source_status") or {}),
                    "asset_winner": payload.get("asset_winner"),
                    "composite_gate": float((payload.get("config") or {}).get("composite_gate") or 0.0) or None,
                    "engine_version": payload.get("source"),
                },
            )
            run_id = run_result.scalar_one()
            if results:
                await session.execute(
                    text(
                        """
                        INSERT INTO cbe_scan_results
                            (
                                run_id, rank, instrument, composite_score, directional_bias,
                                bias_conviction, f1_vc_score, f2_omp_score, f3_csmd_score,
                                f4_cp_score, f5_mp_score, is_watchlist, details,
                                sector_code, sector_quadrant, sector_rs_pct,
                                stock_quadrant, stock_rs_pct, stock_rank_in_sector,
                                trend_score, atr_expansion, volume_score, oi_score, iv_score,
                                atm_strike, atm_oi, atm_volume,
                                composite_alpha_score, gate_passed
                            )
                        VALUES
                            (
                                :run_id, :rank, :instrument, :composite_score, :directional_bias,
                                :bias_conviction, :f1_vc_score, :f2_omp_score, :f3_csmd_score,
                                :f4_cp_score, :f5_mp_score, :is_watchlist, CAST(:details AS JSONB),
                                :sector_code, :sector_quadrant, :sector_rs_pct,
                                :stock_quadrant, :stock_rs_pct, :stock_rank_in_sector,
                                :trend_score, :atr_expansion, :volume_score, :oi_score, :iv_score,
                                :atm_strike, :atm_oi, :atm_volume,
                                :composite_alpha_score, :gate_passed
                            )
                        """
                    ),
                    [
                        {
                            "run_id": run_id,
                            "rank": index + 1,
                            "instrument": str(row.get("instrument") or ""),
                            "composite_score": float(row.get("composite_score") or 0.0),
                            "directional_bias": str(row.get("directional_bias") or "neutral"),
                            "bias_conviction": float(row.get("bias_conviction") or 0.0),
                            "f1_vc_score": float(row.get("f1_vc_score") or 0.0),
                            "f2_omp_score": float(row.get("f2_omp_score") or 0.0),
                            "f3_csmd_score": float(row.get("f3_csmd_score") or 0.0),
                            "f4_cp_score": float(row.get("f4_cp_score") or 0.0),
                            "f5_mp_score": float(row.get("f5_mp_score") or 0.0),
                            "is_watchlist": str(row.get("instrument") or "") in watchlist_symbols,
                            "details": json.dumps(row.get("details") or {}),
                            "sector_code": row.get("sector_code"),
                            "sector_quadrant": row.get("sector_quadrant"),
                            "sector_rs_pct": _opt_float(row.get("sector_rs_pct")),
                            "stock_quadrant": row.get("stock_quadrant"),
                            "stock_rs_pct": _opt_float(row.get("stock_rs_pct")),
                            "stock_rank_in_sector": _opt_int(row.get("stock_rank_in_sector")),
                            "trend_score": _opt_float(row.get("trend_score")),
                            "atr_expansion": _opt_float(row.get("atr_expansion")),
                            "volume_score": _opt_float(row.get("volume_score")),
                            "oi_score": _opt_float(row.get("oi_score")),
                            "iv_score": _opt_float(row.get("iv_score")),
                            "atm_strike": _opt_float(row.get("atm_strike")),
                            "atm_oi": _opt_float(row.get("atm_oi")),
                            "atm_volume": _opt_float(row.get("atm_volume")),
                            "composite_alpha_score": _opt_float(row.get("composite_alpha_score")),
                            "gate_passed": bool(row.get("gate_passed")) if row.get("gate_passed") is not None else None,
                        }
                        for index, row in enumerate(results)
                        if row.get("instrument")
                    ],
                )
            await session.commit()
            return str(run_id)
    except Exception as exc:
        logger.warning(f"[CBE] Scan persistence skipped: {exc}")
        return None


async def load_latest_scan_payload(source: str | None = None) -> dict[str, Any] | None:
    try:
        async with AsyncSessionLocal() as session:
            if not await _tables_ready(session):
                return None
            run_query = """
                SELECT id, source, scan_date, universe_size, scored_count, watchlist_count,
                       config, source_status, created_at
                FROM cbe_scan_runs
            """
            params: dict[str, Any] = {}
            if source:
                run_query += " WHERE source = :source"
                params["source"] = source
            run_query += " ORDER BY created_at DESC LIMIT 1"
            run_result = await session.execute(text(run_query), params)
            run_row = run_result.mappings().first()
            if not run_row:
                return None
            run_id = run_row["id"]
            result_rows = await session.execute(
                text(
                    """
                    SELECT rank, instrument, composite_score, directional_bias, bias_conviction,
                           f1_vc_score, f2_omp_score, f3_csmd_score, f4_cp_score,
                           f5_mp_score, is_watchlist, details
                    FROM cbe_scan_results
                    WHERE run_id = :run_id
                    ORDER BY rank ASC
                    """
                ),
                {"run_id": run_id},
            )
            rows = [_result_row_to_payload(dict(row)) for row in result_rows.mappings().all()]
            return {
                "id": str(run_id),
                "source": run_row["source"],
                "scan_date": run_row["scan_date"].isoformat(),
                "created_at": run_row["created_at"].isoformat(),
                "universe_size": run_row["universe_size"],
                "scored_count": run_row["scored_count"],
                "watchlist_count": run_row["watchlist_count"],
                "config": run_row["config"] or {},
                "source_status": run_row["source_status"] or {},
                "results": rows,
                "watchlist": [row for row in rows if row.get("is_watchlist")],
            }
    except Exception as exc:
        logger.warning(f"[CBE] Latest scan load skipped: {exc}")
        return None


def _opt_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _opt_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_scan_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    return value


def _result_row_to_payload(row: dict[str, Any]) -> dict[str, Any]:
    row.pop("rank", None)
    row["details"] = row.get("details") or {}
    return row


async def _tables_ready(session) -> bool:
    result = await session.execute(
        text(
            """
            SELECT to_regclass('public.cbe_scan_runs') AS runs_table,
                   to_regclass('public.cbe_scan_results') AS results_table
            """
        )
    )
    row = result.mappings().first()
    return bool(row and row.get("runs_table") and row.get("results_table"))
