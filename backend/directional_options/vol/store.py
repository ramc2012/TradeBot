"""Tables and writers for the index volatility substrate.

DDL lives here as idempotent `CREATE TABLE IF NOT EXISTS` rather than in an
alembic revision, matching how `core.runtime_state` and the other late-added
tables in this stack bootstrap themselves.  `ensure_tables()` is safe to call
on every pass.

Four stores, and the quarantine is as important as the other three:

  index_vol_surface_slices  one row per (bar, underlying, expiry) — the fit
  index_vol_cm_points       the fixed surface coordinates
  index_vol_tenor_metrics   skew/VRP per tenor, the shape the lane reads
  index_vol_quarantine      every quote the solver or the fit refused, with why

A day where the quarantine suddenly fills is a data incident, and having it in
a table means it is visible rather than inferred from a missing feature.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Sequence

from loguru import logger
from sqlalchemy import text

from db.database import AsyncSessionLocal
from directional_options.vol.realized import VariancePremium
from directional_options.vol.series import CMTenor
from directional_options.vol.surface import SurfaceSnapshot

_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS index_vol_surface_slices (
        ts                  timestamptz NOT NULL,
        underlying          text        NOT NULL,
        expiry              date        NOT NULL,
        tenor_years         double precision,
        days_to_expiry      double precision,
        forward             double precision,
        spot                double precision,
        atm_strike          double precision,
        atm_iv              double precision,
        atm_total_variance  double precision,
        fit_status          text        NOT NULL,
        n_quotes            integer,
        n_used              integer,
        n_rejects           integer,
        rmse_vol            double precision,
        max_abs_resid_vol   double precision,
        butterfly_ok        boolean,
        min_g               double precision,
        k_min               double precision,
        k_max               double precision,
        svi_a               double precision,
        svi_b               double precision,
        svi_rho             double precision,
        svi_m               double precision,
        svi_s               double precision,
        stale_seconds       double precision,
        calendar_ok         boolean,
        reason              text,
        computed_at         timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (ts, underlying, expiry)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ivss_underlying_ts ON index_vol_surface_slices (underlying, ts DESC)",
    """
    CREATE TABLE IF NOT EXISTS index_vol_cm_points (
        ts              timestamptz NOT NULL,
        underlying      text        NOT NULL,
        tenor_days      integer     NOT NULL,
        tenor_kind      text        NOT NULL,
        tag             text        NOT NULL,
        implied_vol     double precision,
        total_variance  double precision,
        log_moneyness   double precision,
        strike          double precision,
        forward         double precision,
        interpolated    boolean,
        status          text        NOT NULL,
        reason          text,
        computed_at     timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (ts, underlying, tenor_days, tenor_kind, tag)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ivcm_lookup ON index_vol_cm_points (underlying, tag, tenor_days, ts DESC)",
    """
    CREATE TABLE IF NOT EXISTS index_vol_tenor_metrics (
        ts              timestamptz NOT NULL,
        underlying      text        NOT NULL,
        tenor_days      integer     NOT NULL,
        tenor_kind      text        NOT NULL,
        forward         double precision,
        atm_iv          double precision,
        rr_25           double precision,
        bf_25           double precision,
        skew_slope      double precision,
        rv_window_days  integer,
        rv_estimator    text,
        realized_vol    double precision,
        vrp_spread      double precision,
        vrp_ratio       double precision,
        rv_percentile   double precision,
        status          text        NOT NULL,
        reason          text,
        computed_at     timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (ts, underlying, tenor_days, tenor_kind)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ivtm_underlying_ts ON index_vol_tenor_metrics (underlying, ts DESC)",
    """
    CREATE TABLE IF NOT EXISTS index_vol_quarantine (
        id            bigserial    PRIMARY KEY,
        ts            timestamptz  NOT NULL,
        underlying    text         NOT NULL,
        expiry        date,
        strike        double precision,
        option_type   text,
        price         double precision,
        log_moneyness double precision,
        stage         text         NOT NULL,
        status        text         NOT NULL,
        reason        text,
        computed_at   timestamptz  NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ivq_underlying_ts ON index_vol_quarantine (underlying, ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_ivq_stage ON index_vol_quarantine (stage, ts DESC)",
)


async def ensure_tables() -> None:
    async with AsyncSessionLocal() as session:
        for statement in _DDL:
            await session.execute(text(statement))
        await session.commit()


_UPSERT_SLICE = text(
    """
    INSERT INTO index_vol_surface_slices (
        ts, underlying, expiry, tenor_years, days_to_expiry, forward, spot,
        atm_strike, atm_iv, atm_total_variance, fit_status, n_quotes, n_used,
        n_rejects, rmse_vol, max_abs_resid_vol, butterfly_ok, min_g, k_min,
        k_max, svi_a, svi_b, svi_rho, svi_m, svi_s, stale_seconds, calendar_ok,
        reason, computed_at
    ) VALUES (
        :ts, :underlying, :expiry, :tenor_years, :days_to_expiry, :forward, :spot,
        :atm_strike, :atm_iv, :atm_total_variance, :fit_status, :n_quotes, :n_used,
        :n_rejects, :rmse_vol, :max_abs_resid_vol, :butterfly_ok, :min_g, :k_min,
        :k_max, :svi_a, :svi_b, :svi_rho, :svi_m, :svi_s, :stale_seconds, :calendar_ok,
        :reason, now()
    )
    ON CONFLICT (ts, underlying, expiry) DO UPDATE SET
        tenor_years = EXCLUDED.tenor_years,
        days_to_expiry = EXCLUDED.days_to_expiry,
        forward = EXCLUDED.forward,
        spot = EXCLUDED.spot,
        atm_strike = EXCLUDED.atm_strike,
        atm_iv = EXCLUDED.atm_iv,
        atm_total_variance = EXCLUDED.atm_total_variance,
        fit_status = EXCLUDED.fit_status,
        n_quotes = EXCLUDED.n_quotes,
        n_used = EXCLUDED.n_used,
        n_rejects = EXCLUDED.n_rejects,
        rmse_vol = EXCLUDED.rmse_vol,
        max_abs_resid_vol = EXCLUDED.max_abs_resid_vol,
        butterfly_ok = EXCLUDED.butterfly_ok,
        min_g = EXCLUDED.min_g,
        k_min = EXCLUDED.k_min,
        k_max = EXCLUDED.k_max,
        svi_a = EXCLUDED.svi_a, svi_b = EXCLUDED.svi_b, svi_rho = EXCLUDED.svi_rho,
        svi_m = EXCLUDED.svi_m, svi_s = EXCLUDED.svi_s,
        stale_seconds = EXCLUDED.stale_seconds,
        calendar_ok = EXCLUDED.calendar_ok,
        reason = EXCLUDED.reason,
        computed_at = now()
    """
)

_UPSERT_CM_POINT = text(
    """
    INSERT INTO index_vol_cm_points (
        ts, underlying, tenor_days, tenor_kind, tag, implied_vol, total_variance,
        log_moneyness, strike, forward, interpolated, status, reason, computed_at
    ) VALUES (
        :ts, :underlying, :tenor_days, :tenor_kind, :tag, :implied_vol, :total_variance,
        :log_moneyness, :strike, :forward, :interpolated, :status, :reason, now()
    )
    ON CONFLICT (ts, underlying, tenor_days, tenor_kind, tag) DO UPDATE SET
        implied_vol = EXCLUDED.implied_vol,
        total_variance = EXCLUDED.total_variance,
        log_moneyness = EXCLUDED.log_moneyness,
        strike = EXCLUDED.strike,
        forward = EXCLUDED.forward,
        interpolated = EXCLUDED.interpolated,
        status = EXCLUDED.status,
        reason = EXCLUDED.reason,
        computed_at = now()
    """
)

_UPSERT_TENOR_METRICS = text(
    """
    INSERT INTO index_vol_tenor_metrics (
        ts, underlying, tenor_days, tenor_kind, forward, atm_iv, rr_25, bf_25,
        skew_slope, rv_window_days, rv_estimator, realized_vol, vrp_spread,
        vrp_ratio, rv_percentile, status, reason, computed_at
    ) VALUES (
        :ts, :underlying, :tenor_days, :tenor_kind, :forward, :atm_iv, :rr_25, :bf_25,
        :skew_slope, :rv_window_days, :rv_estimator, :realized_vol, :vrp_spread,
        :vrp_ratio, :rv_percentile, :status, :reason, now()
    )
    ON CONFLICT (ts, underlying, tenor_days, tenor_kind) DO UPDATE SET
        forward = EXCLUDED.forward,
        atm_iv = EXCLUDED.atm_iv,
        rr_25 = EXCLUDED.rr_25,
        bf_25 = EXCLUDED.bf_25,
        skew_slope = EXCLUDED.skew_slope,
        rv_window_days = EXCLUDED.rv_window_days,
        rv_estimator = EXCLUDED.rv_estimator,
        realized_vol = EXCLUDED.realized_vol,
        vrp_spread = EXCLUDED.vrp_spread,
        vrp_ratio = EXCLUDED.vrp_ratio,
        rv_percentile = EXCLUDED.rv_percentile,
        status = EXCLUDED.status,
        reason = EXCLUDED.reason,
        computed_at = now()
    """
)

_DELETE_QUARANTINE = text(
    "DELETE FROM index_vol_quarantine WHERE ts = :ts AND underlying = :underlying"
)

_INSERT_QUARANTINE = text(
    """
    INSERT INTO index_vol_quarantine (
        ts, underlying, expiry, strike, option_type, price, log_moneyness,
        stage, status, reason, computed_at
    ) VALUES (
        :ts, :underlying, :expiry, :strike, :option_type, :price, :log_moneyness,
        :stage, :status, :reason, now()
    )
    """
)


def _slice_params(snapshot: SurfaceSnapshot) -> list[dict[str, Any]]:
    calendar_ok = snapshot.calendar.get("ok") if snapshot.calendar else None
    out: list[dict[str, Any]] = []
    for sl in snapshot.slices:
        fit = sl.fit
        params = fit.params if fit and fit.params else None
        out.append(
            {
                "ts": sl.ts,
                "underlying": sl.underlying,
                "expiry": sl.expiry,
                "tenor_years": sl.tenor_years,
                "days_to_expiry": sl.days_to_expiry,
                "forward": sl.forward,
                "spot": sl.spot,
                "atm_strike": sl.atm_strike,
                "atm_iv": sl.atm_iv(),
                "atm_total_variance": sl.atm_total_variance(),
                "fit_status": (fit.status if fit else "none"),
                "n_quotes": len(sl.quotes),
                "n_used": (fit.n_used if fit else 0),
                "n_rejects": len(sl.rejects),
                "rmse_vol": (fit.rmse_vol if fit else None),
                "max_abs_resid_vol": (fit.max_abs_resid_vol if fit else None),
                "butterfly_ok": (fit.butterfly_ok if fit else None),
                "min_g": (fit.min_g if fit else None),
                "k_min": (fit.k_min if fit else None),
                "k_max": (fit.k_max if fit else None),
                "svi_a": (params.a if params else None),
                "svi_b": (params.b if params else None),
                "svi_rho": (params.rho if params else None),
                "svi_m": (params.m if params else None),
                "svi_s": (params.s if params else None),
                "stale_seconds": sl.stale_seconds,
                "calendar_ok": calendar_ok,
                "reason": (sl.reason or None),
            }
        )
    return out


def _quarantine_params(snapshot: SurfaceSnapshot) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for sl in snapshot.slices:
        for rej in sl.rejects:
            rows.append(
                {
                    "ts": sl.ts,
                    "underlying": sl.underlying,
                    "expiry": sl.expiry,
                    "strike": rej.get("strike"),
                    "option_type": rej.get("option_type"),
                    "price": rej.get("price"),
                    "log_moneyness": rej.get("log_moneyness"),
                    "stage": "iv_solve",
                    "status": rej.get("status") or "unknown",
                    "reason": rej.get("reason"),
                }
            )
        if sl.fit and sl.fit.quarantined:
            for q in sl.fit.quarantined:
                rows.append(
                    {
                        "ts": sl.ts,
                        "underlying": sl.underlying,
                        "expiry": sl.expiry,
                        "strike": q.get("strike"),
                        "option_type": None,
                        "price": None,
                        "log_moneyness": q.get("log_moneyness"),
                        "stage": "svi_fit",
                        "status": "outlier",
                        "reason": (
                            f"{q.get('reason')}: observed {q.get('observed_iv'):.4f} "
                            f"vs fitted {q.get('fitted_iv'):.4f}"
                        ),
                    }
                )
    return rows


async def persist_snapshot(
    snapshot: SurfaceSnapshot,
    tenors: Sequence[CMTenor] = (),
    premiums: dict[tuple[int, str], VariancePremium] | None = None,
    *,
    front_tenor_days: int | None = None,
) -> dict[str, int]:
    """Write one bar's surface, coordinates, metrics and quarantine.

    The quarantine for this (ts, underlying) is deleted and rewritten rather
    than appended to, so a re-run of the same bar does not accumulate
    duplicate rejects.
    """
    premiums = premiums or {}
    slice_rows = _slice_params(snapshot)
    quarantine_rows = _quarantine_params(snapshot)

    point_rows: list[dict[str, Any]] = []
    metric_rows: list[dict[str, Any]] = []
    for tenor in tenors:
        kind = "front" if front_tenor_days is not None and tenor.tenor_days == front_tenor_days else "grid"
        for tag, point in tenor.points.items():
            point_rows.append(
                {
                    "ts": tenor.ts,
                    "underlying": tenor.underlying,
                    "tenor_days": tenor.tenor_days,
                    "tenor_kind": kind,
                    "tag": tag,
                    "implied_vol": point.implied_vol,
                    "total_variance": point.total_variance,
                    "log_moneyness": point.log_moneyness,
                    "strike": point.strike,
                    "forward": point.forward,
                    "interpolated": point.interpolated,
                    "status": point.status,
                    "reason": (point.reason or None),
                }
            )
        vp = premiums.get((tenor.tenor_days, kind))
        metric_rows.append(
            {
                "ts": tenor.ts,
                "underlying": tenor.underlying,
                "tenor_days": tenor.tenor_days,
                "tenor_kind": kind,
                "forward": tenor.forward,
                "atm_iv": tenor.atm_iv(),
                "rr_25": tenor.rr_25,
                "bf_25": tenor.bf_25,
                "skew_slope": tenor.skew_slope,
                "rv_window_days": (
                    max(int(round(tenor.tenor_days * 252.0 / 365.0)), 5) if vp else None
                ),
                "rv_estimator": (vp.estimator if vp else None),
                "realized_vol": (vp.realized_vol if vp else None),
                "vrp_spread": (vp.spread if vp else None),
                "vrp_ratio": (vp.ratio if vp else None),
                "rv_percentile": (vp.realized_percentile if vp else None),
                "status": tenor.status,
                "reason": (tenor.reason or None),
            }
        )

    async with AsyncSessionLocal() as session:
        if slice_rows:
            await session.execute(_UPSERT_SLICE, slice_rows)
        if point_rows:
            await session.execute(_UPSERT_CM_POINT, point_rows)
        if metric_rows:
            await session.execute(_UPSERT_TENOR_METRICS, metric_rows)
        await session.execute(
            _DELETE_QUARANTINE, {"ts": snapshot.ts, "underlying": snapshot.underlying}
        )
        if quarantine_rows:
            await session.execute(_INSERT_QUARANTINE, quarantine_rows)
        await session.commit()

    written = {
        "slices": len(slice_rows),
        "cm_points": len(point_rows),
        "tenor_metrics": len(metric_rows),
        "quarantined": len(quarantine_rows),
    }
    logger.debug(f"[vol.store] {snapshot.underlying} {snapshot.ts.isoformat()} wrote {written}")
    return written


_LATEST_METRICS = text(
    """
    SELECT * FROM index_vol_tenor_metrics
    WHERE underlying = :underlying
      AND tenor_kind = :tenor_kind
      AND ts >= :lower
      AND ts <= :upper
    ORDER BY ts DESC
    LIMIT 1
    """
)


async def latest_tenor_metrics(
    underlying: str,
    *,
    lower: datetime,
    upper: datetime,
    tenor_kind: str = "front",
) -> dict[str, Any] | None:
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                _LATEST_METRICS,
                {
                    "underlying": underlying,
                    "tenor_kind": tenor_kind,
                    "lower": lower,
                    "upper": upper,
                },
            )
        ).mappings().first()
    return dict(row) if row else None
