"""Prepare wider option-chain history for directional gamma-wall research.

The existing Upstox research sync is intentionally ATM-focused. This tool is
separate: it selects a wider contract universe, writes an auditable fetch plan,
and can download 30-minute candles for those contracts into a standalone
research dataset.

Typical dry run:
    python backend/scripts/prepare_directional_wall_history.py \
        --symbols NIFTY,BANKNIFTY,RELIANCE,HDFCBANK,ICICIBANK,SBIN,TCS,INFY,AXISBANK,ITC \
        --from-date 2026-01-01 --to-date 2026-03-24 --mode full

Download after reviewing the plan:
    python backend/scripts/prepare_directional_wall_history.py ... --download
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import httpx
import pandas as pd


BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from analysis.backtest import CANDLE_INTERVAL, MACDBacktester, UPSTOX_BASE, UpstoxAuthError  # noqa: E402
from data.run_upstox_research_sync import _load_upstox_token  # noqa: E402
from data.upstox_research_sync import (  # noqa: E402
    SECONDS_PER_YEAR,
    _implied_volatility,
    _option_greeks,
)


DEFAULT_SYMBOLS = [
    "NIFTY",
    "BANKNIFTY",
    "RELIANCE",
    "HDFCBANK",
    "ICICIBANK",
    "SBIN",
    "TCS",
    "INFY",
    "AXISBANK",
    "ITC",
]
DEFAULT_OUT_DIR = REPO_ROOT / "data" / "directional_wall_history"
DEFAULT_CONTRACTS_PATH = REPO_ROOT / "data" / "catalogs" / "contracts.parquet"
DEFAULT_SPOT_DIR = REPO_ROOT / "data" / "spot_candles"
DEFAULT_OPTION_CACHE_DIR = REPO_ROOT / "data" / "option_candles"
INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"}
UTC = timezone.utc


@dataclass(frozen=True)
class Contract:
    instrument_key: str
    trading_symbol: str
    underlying: str
    expiry: date
    strike: float
    option_type: str
    lot_size: int | None = None
    tick_size: float | None = None
    source: str = "catalog"
    existing_candle_count: int = 0


@dataclass
class PlanRow:
    underlying: str
    kind: str
    expiry: str
    selected_contracts: int
    selected_strikes: int
    selected_ce: int
    selected_pe: int
    existing_contracts_with_candles: int
    existing_candle_rows: int
    selection_mode: str
    center_price: float | None
    min_strike: float | None
    max_strike: float | None
    source: str


def _parse_symbols(raw: str | None) -> list[str]:
    if not raw:
        return DEFAULT_SYMBOLS.copy()
    return [item.strip().upper() for item in raw.split(",") if item.strip()]


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    return date.fromisoformat(raw)


def _parse_expiries(raw: str | None) -> set[date] | None:
    if not raw:
        return None
    return {date.fromisoformat(item.strip()) for item in raw.split(",") if item.strip()}


def _kind_for(symbol: str) -> str:
    return "INDEX" if symbol.upper() in INDEX_SYMBOLS else "STOCK"


def _as_date(value: Any) -> date | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return pd.to_datetime(value).date()
    except Exception:
        return None


def _as_float(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    return numeric


def _as_int(value: Any) -> int | None:
    try:
        numeric = int(float(value))
    except (TypeError, ValueError):
        return None
    return numeric


def _load_spot_cache(spot_dir: Path, symbols: Iterable[str]) -> dict[str, pd.DataFrame]:
    wanted = set(symbols)
    out: dict[str, pd.DataFrame] = {}
    for path in sorted(spot_dir.glob("*.parquet")):
        df = pd.read_parquet(path)
        if not {"time", "underlying", "close"}.issubset(df.columns):
            continue
        df = df[df["underlying"].astype(str).str.upper().isin(wanted)].copy()
        if df.empty:
            continue
        df["underlying"] = df["underlying"].astype(str).str.upper()
        df["time"] = pd.to_datetime(df["time"], errors="coerce", utc=True)
        df = df[df["time"].notna()].sort_values("time")
        for symbol, group in df.groupby("underlying"):
            out[symbol] = group[["time", "close"]].copy()
    return out


def _spot_reference(spot_cache: dict[str, pd.DataFrame], symbol: str, target: date) -> float | None:
    df = spot_cache.get(symbol)
    if df is None or df.empty:
        return None
    target_ts = pd.Timestamp(datetime.combine(target, datetime.min.time(), tzinfo=UTC))
    after = df[df["time"] >= target_ts]
    if not after.empty:
        return _as_float(after.iloc[0]["close"])
    before = df[df["time"] <= target_ts]
    if not before.empty:
        return _as_float(before.iloc[-1]["close"])
    return None


def _spot_by_time(spot_cache: dict[str, pd.DataFrame], symbol: str) -> pd.Series | None:
    df = spot_cache.get(symbol)
    if df is None or df.empty:
        return None
    series = df.drop_duplicates("time").set_index("time")["close"].sort_index()
    return pd.to_numeric(series, errors="coerce")


def _normalize_contract_row(row: Any, source: str) -> Contract | None:
    instrument_key = str(getattr(row, "instrument_key", "") or "")
    if not instrument_key:
        return None
    expiry = _as_date(getattr(row, "expiry", None))
    strike = _as_float(getattr(row, "strike", None))
    option_type = str(getattr(row, "option_type", "") or "").upper()
    underlying = str(getattr(row, "underlying", "") or "").upper()
    if expiry is None or strike is None or strike <= 0 or option_type not in {"CE", "PE"} or not underlying:
        return None
    return Contract(
        instrument_key=instrument_key,
        trading_symbol=str(getattr(row, "trading_symbol", "") or ""),
        underlying=underlying,
        expiry=expiry,
        strike=strike,
        option_type=option_type,
        lot_size=_as_int(getattr(row, "lot_size", None)),
        tick_size=_as_float(getattr(row, "tick_size", None)),
        source=source,
        existing_candle_count=_as_int(getattr(row, "candle_count", 0)) or 0,
    )


def _contracts_from_catalog(
    path: Path,
    symbols: list[str],
    start: date | None,
    end: date | None,
    explicit_expiries: set[date] | None,
) -> list[Contract]:
    if not path.exists():
        return []
    df = pd.read_parquet(path)
    required = {"instrument_key", "underlying", "expiry", "strike", "option_type"}
    if not required.issubset(df.columns):
        return []
    df = df[df["underlying"].astype(str).str.upper().isin(set(symbols))].copy()
    df["underlying"] = df["underlying"].astype(str).str.upper()
    df["expiry"] = pd.to_datetime(df["expiry"], errors="coerce").dt.date
    if explicit_expiries is not None:
        df = df[df["expiry"].isin(explicit_expiries)]
    else:
        if start is not None:
            df = df[df["expiry"] >= start]
        if end is not None:
            df = df[df["expiry"] <= end]
    contracts = [
        contract
        for contract in (_normalize_contract_row(row, "catalog") for row in df.itertuples(index=False))
        if contract is not None
    ]
    return contracts


def _center_from_contracts(contracts: list[Contract]) -> float | None:
    strikes = sorted({contract.strike for contract in contracts})
    if not strikes:
        return None
    mid = len(strikes) // 2
    if len(strikes) % 2:
        return strikes[mid]
    return (strikes[mid - 1] + strikes[mid]) / 2.0


def _select_contracts(
    contracts: list[Contract],
    *,
    mode: str,
    center: float | None,
    strike_band_pct: float,
    atm_window: int,
) -> list[Contract]:
    if mode == "full":
        return sorted(contracts, key=lambda c: (c.underlying, c.expiry, c.strike, c.option_type))

    if center is None or center <= 0:
        center = _center_from_contracts(contracts)
    if center is None or center <= 0:
        return []

    if mode == "spot-band":
        pct = max(0.0, strike_band_pct)
        lo = center * (1.0 - pct)
        hi = center * (1.0 + pct)
        return [
            contract
            for contract in contracts
            if lo <= contract.strike <= hi
        ]

    if mode == "atm-window":
        by_strike = sorted({contract.strike for contract in contracts})
        if not by_strike:
            return []
        ranked = sorted(by_strike, key=lambda strike: (abs(strike - center), strike))
        keep = set(ranked[: max(1, atm_window)])
        return [contract for contract in contracts if contract.strike in keep]

    raise ValueError(f"Unsupported selection mode: {mode}")


async def _active_chain_contracts(
    client: MACDBacktester,
    symbol: str,
    expiry: date,
) -> list[Contract]:
    underlying_key = await client._get_underlying_key(symbol)
    if not underlying_key:
        return []
    import urllib.parse

    encoded = urllib.parse.quote(underlying_key, safe="")
    url = f"{UPSTOX_BASE}/option/chain?instrument_key={encoded}&expiry_date={expiry.isoformat()}"
    await client._throttle()
    async with httpx.AsyncClient(timeout=30.0) as session:
        client._record_api_call("option_chain")
        resp = await session.get(url, headers=client.headers)
    if resp.status_code != 200:
        print(f"option chain failed for {symbol} {expiry}: HTTP {resp.status_code} {resp.text[:160]}")
        return []

    contracts: list[Contract] = []
    for item in resp.json().get("data", []) or []:
        strike = _as_float(item.get("strike_price") or item.get("strike"))
        if strike is None:
            continue
        for option_type, key in (("CE", "call_options"), ("PE", "put_options")):
            opt = item.get(key) or {}
            instrument_key = str(opt.get("instrument_key") or "")
            if not instrument_key:
                continue
            market_data = opt.get("market_data") or {}
            contracts.append(
                Contract(
                    instrument_key=instrument_key,
                    trading_symbol=str(opt.get("trading_symbol") or opt.get("tradingsymbol") or ""),
                    underlying=symbol,
                    expiry=expiry,
                    strike=strike,
                    option_type=option_type,
                    lot_size=_as_int(opt.get("lot_size") or market_data.get("lot_size")),
                    tick_size=_as_float(opt.get("tick_size")),
                    source="active_chain",
                    existing_candle_count=0,
                )
            )
    return contracts


async def _discover_from_upstox(
    client: MACDBacktester,
    symbols: list[str],
    start: date,
    end: date,
    explicit_expiries: set[date] | None,
    max_expiries_per_symbol: int,
) -> list[Contract]:
    today = date.today()
    all_contracts: list[Contract] = []
    for symbol in symbols:
        if explicit_expiries is not None:
            expiries = sorted(explicit_expiries)
        else:
            expiry_dates = await client._fetch_expiry_dates(symbol)
            monthly, _prev = client._select_monthly_expiries(expiry_dates, start, end)
            expiries = monthly
        if max_expiries_per_symbol > 0:
            expiries = expiries[-max_expiries_per_symbol:]
        for expiry in expiries:
            try:
                if expiry <= today:
                    raw = await client._fetch_expired_contracts(symbol, expiry)
                    contracts = []
                    for item in raw:
                        strike = _as_float(item.get("strike_price") or item.get("strike"))
                        option_type = str(item.get("instrument_type") or item.get("option_type") or "").upper()
                        instrument_key = str(item.get("instrument_key") or "")
                        if not instrument_key or strike is None or option_type not in {"CE", "PE"}:
                            continue
                        contracts.append(
                            Contract(
                                instrument_key=instrument_key,
                                trading_symbol=str(item.get("trading_symbol") or item.get("tradingsymbol") or ""),
                                underlying=symbol,
                                expiry=expiry,
                                strike=strike,
                                option_type=option_type,
                                lot_size=_as_int(item.get("lot_size")),
                                tick_size=_as_float(item.get("tick_size")),
                                source="expired_contracts",
                            )
                        )
                else:
                    contracts = await _active_chain_contracts(client, symbol, expiry)
            except UpstoxAuthError:
                raise
            except Exception as exc:
                print(f"contract discovery failed for {symbol} {expiry}: {exc}")
                contracts = []
            all_contracts.extend(contracts)
    return all_contracts


def _plan_rows(
    grouped_contracts: dict[tuple[str, date], list[Contract]],
    selected: dict[tuple[str, date], list[Contract]],
    spot_cache: dict[str, pd.DataFrame],
    mode: str,
) -> list[PlanRow]:
    rows: list[PlanRow] = []
    for key in sorted(grouped_contracts, key=lambda item: (item[0], item[1])):
        symbol, expiry = key
        contracts = grouped_contracts[key]
        chosen = selected.get(key, [])
        center = _spot_reference(spot_cache, symbol, expiry) or _center_from_contracts(contracts)
        rows.append(
            PlanRow(
                underlying=symbol,
                kind=_kind_for(symbol),
                expiry=expiry.isoformat(),
                selected_contracts=len(chosen),
                selected_strikes=len({contract.strike for contract in chosen}),
                selected_ce=sum(1 for contract in chosen if contract.option_type == "CE"),
                selected_pe=sum(1 for contract in chosen if contract.option_type == "PE"),
                existing_contracts_with_candles=sum(1 for contract in chosen if contract.existing_candle_count > 0),
                existing_candle_rows=sum(contract.existing_candle_count for contract in chosen),
                selection_mode=mode,
                center_price=center,
                min_strike=min((contract.strike for contract in chosen), default=None),
                max_strike=max((contract.strike for contract in chosen), default=None),
                source=",".join(sorted({contract.source for contract in chosen})) if chosen else "",
            )
        )
    return rows


def _merge_spot(candles: list[dict], spot_series: pd.Series | None) -> list[dict]:
    if not candles:
        return []
    if spot_series is None or spot_series.empty:
        return candles
    times = pd.to_datetime([item["time"] for item in candles], errors="coerce", utc=True)
    lookup = pd.DataFrame({"time": times})
    spot_df = spot_series.rename("underlying_price").reset_index()
    merged = pd.merge_asof(
        lookup.sort_values("time"),
        spot_df.sort_values("time"),
        on="time",
        direction="nearest",
        tolerance=pd.Timedelta("45min"),
    )
    prices = merged["underlying_price"].tolist()
    for candle, price in zip(candles, prices):
        if price is not None and not pd.isna(price):
            candle["underlying_price"] = float(price)
    return candles


def _load_existing_cache_rows(
    *,
    contracts: list[Contract],
    option_cache_dir: Path,
    start: date,
    end: date,
    spot_cache: dict[str, pd.DataFrame],
    risk_free_rate: float,
) -> pd.DataFrame:
    if not contracts or not option_cache_dir.exists():
        return pd.DataFrame()

    selected = pd.DataFrame([asdict(contract) for contract in contracts])
    if selected.empty:
        return pd.DataFrame()
    selected["expiry"] = pd.to_datetime(selected["expiry"], errors="coerce").dt.date
    selected["strike"] = pd.to_numeric(selected["strike"], errors="coerce")
    selected["option_type"] = selected["option_type"].astype(str).str.upper()
    selected["underlying"] = selected["underlying"].astype(str).str.upper()

    frames: list[pd.DataFrame] = []
    wanted_symbols = set(selected["underlying"].dropna().astype(str))
    wanted_expiries = set(selected["expiry"].dropna())
    for path in sorted(option_cache_dir.glob("*.parquet")):
        cached = pd.read_parquet(path)
        needed = {"time", "underlying", "expiry", "strike", "option_type", "open", "high", "low", "close", "volume", "oi"}
        if not needed.issubset(cached.columns):
            continue
        cached = cached[cached["underlying"].astype(str).str.upper().isin(wanted_symbols)].copy()
        if cached.empty:
            continue
        cached["underlying"] = cached["underlying"].astype(str).str.upper()
        cached["expiry"] = pd.to_datetime(cached["expiry"], errors="coerce").dt.date
        cached = cached[cached["expiry"].isin(wanted_expiries)]
        if cached.empty:
            continue
        cached["time"] = pd.to_datetime(cached["time"], errors="coerce", utc=True)
        cached = cached[cached["time"].notna()]
        cached = cached[(cached["time"].dt.date >= start) & (cached["time"].dt.date <= end)]
        if cached.empty:
            continue
        cached["strike"] = pd.to_numeric(cached["strike"], errors="coerce")
        cached["option_type"] = cached["option_type"].astype(str).str.upper()
        merged = cached.merge(
            selected[
                [
                    "underlying",
                    "expiry",
                    "strike",
                    "option_type",
                    "instrument_key",
                    "trading_symbol",
                    "lot_size",
                    "tick_size",
                    "source",
                ]
            ],
            on=["underlying", "expiry", "strike", "option_type"],
            how="inner",
        )
        if merged.empty:
            continue
        frames.append(merged)

    if not frames:
        return pd.DataFrame()

    out = pd.concat(frames, ignore_index=True)
    out["interval"] = CANDLE_INTERVAL
    out["source"] = out["source"].fillna("local_cache").astype(str) + "+local_cache"
    missing_spot = "underlying_price" not in out.columns or out["underlying_price"].isna().all()
    if missing_spot:
        out["underlying_price"] = pd.NA
    hydrated: list[pd.DataFrame] = []
    for symbol, group in out.groupby("underlying", sort=False):
        group = group.sort_values("time").copy()
        spot_series = _spot_by_time(spot_cache, symbol)
        if spot_series is not None and not spot_series.empty:
            spot_df = spot_series.rename("cache_spot").reset_index()
            group = pd.merge_asof(
                group.sort_values("time"),
                spot_df.sort_values("time"),
                on="time",
                direction="nearest",
                tolerance=pd.Timedelta("45min"),
            )
            group["underlying_price"] = group["underlying_price"].fillna(group["cache_spot"])
            group = group.drop(columns=["cache_spot"])
        hydrated.append(group)
    out = pd.concat(hydrated, ignore_index=True)
    records = []
    for row in out.to_dict(orient="records"):
        row["expiry"] = _as_date(row.get("expiry"))
        records.append(_add_greeks(row, risk_free_rate))
    out = pd.DataFrame(records)
    out = out.drop_duplicates(["instrument_key", "interval", "time"], keep="last")
    return out.sort_values(["underlying", "expiry", "strike", "option_type", "time"])


def _add_greeks(row: dict, risk_free_rate: float) -> dict:
    spot = _as_float(row.get("underlying_price"))
    strike = _as_float(row.get("strike"))
    premium = _as_float(row.get("close"))
    if spot is None or strike is None or premium is None or spot <= 0 or strike <= 0:
        return row
    try:
        ts = pd.Timestamp(row["time"]).to_pydatetime()
    except Exception:
        return row
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    expiry_dt = datetime.combine(row["expiry"], datetime.max.time(), tzinfo=UTC)
    tte = max((expiry_dt - ts).total_seconds() / SECONDS_PER_YEAR, 1.0 / (365.25 * 24 * 60))
    iv = _implied_volatility(row["option_type"], premium, spot, strike, tte, risk_free_rate)
    if iv is None:
        return row
    try:
        delta, gamma, theta, vega = _option_greeks(row["option_type"], spot, strike, tte, risk_free_rate, iv)
    except Exception:
        return row
    row.update(
        {
            "iv": iv,
            "delta": delta,
            "gamma": gamma,
            "theta": theta,
            "vega": vega,
            "time_to_expiry_years": tte,
        }
    )
    return row


async def _download_contracts(
    client: MACDBacktester,
    contracts: list[Contract],
    *,
    start: date,
    end: date,
    min_candles: int,
    spot_cache: dict[str, pd.DataFrame],
    risk_free_rate: float,
    skip_existing: bool,
) -> tuple[pd.DataFrame, list[dict]]:
    rows: list[dict] = []
    statuses: list[dict] = []
    total = len(contracts)
    for idx, contract in enumerate(contracts, start=1):
        if skip_existing and contract.existing_candle_count >= min_candles:
            statuses.append({**asdict(contract), "status": "skipped_existing", "candles": contract.existing_candle_count})
            continue
        window_end = min(end, contract.expiry)
        if start > window_end:
            statuses.append({**asdict(contract), "status": "skipped_window", "candles": 0})
            continue
        try:
            candles = await client._fetch_candles_from_upstox(contract.instrument_key, start, window_end)
        except UpstoxAuthError:
            raise
        except Exception as exc:
            statuses.append({**asdict(contract), "status": f"error: {exc}", "candles": 0})
            continue
        candles = _merge_spot(candles, _spot_by_time(spot_cache, contract.underlying))
        for candle in candles:
            row = {
                **candle,
                "underlying": contract.underlying,
                "expiry": contract.expiry,
                "strike": contract.strike,
                "option_type": contract.option_type,
                "instrument_key": contract.instrument_key,
                "trading_symbol": contract.trading_symbol,
                "lot_size": contract.lot_size,
                "tick_size": contract.tick_size,
                "interval": CANDLE_INTERVAL,
                "source": contract.source,
            }
            rows.append(_add_greeks(row, risk_free_rate))
        status = "ok" if len(candles) >= min_candles else "partial" if candles else "empty"
        statuses.append({**asdict(contract), "status": status, "candles": len(candles)})
        if idx % 25 == 0 or idx == total:
            print(f"downloaded {idx}/{total} contracts, rows={len(rows)}", flush=True)
    if not rows:
        return pd.DataFrame(), statuses
    df = pd.DataFrame(rows)
    df["time"] = pd.to_datetime(df["time"], errors="coerce", utc=True)
    df = df[df["time"].notna()].sort_values(["underlying", "expiry", "strike", "option_type", "time"])
    return df, statuses


async def _amain(args: argparse.Namespace) -> int:
    symbols = _parse_symbols(args.symbols)
    start = _parse_date(args.from_date)
    end = _parse_date(args.to_date)
    if start is None or end is None:
        raise SystemExit("--from-date and --to-date are required")
    if start > end:
        raise SystemExit("--from-date must be before --to-date")
    explicit_expiries = _parse_expiries(args.expiries)

    spot_cache = _load_spot_cache(args.spot_dir, symbols)
    token = _load_upstox_token() if (args.download or args.source == "upstox") else ""
    client: MACDBacktester | None = None
    if args.download or args.source == "upstox":
        if not token:
            raise SystemExit("No Upstox token available. Connect Upstox first or run without --download.")
        client = MACDBacktester(token)
        client.rate_limit_delay = args.gap_seconds

    if args.source == "catalog":
        contracts = _contracts_from_catalog(args.contracts_path, symbols, start, end, explicit_expiries)
    else:
        assert client is not None
        contracts = await _discover_from_upstox(
            client,
            symbols,
            start,
            end,
            explicit_expiries,
            args.max_expiries_per_symbol,
        )

    grouped: dict[tuple[str, date], list[Contract]] = {}
    for contract in contracts:
        grouped.setdefault((contract.underlying, contract.expiry), []).append(contract)

    selected: dict[tuple[str, date], list[Contract]] = {}
    for key, items in grouped.items():
        symbol, expiry = key
        center = _spot_reference(spot_cache, symbol, expiry) or _center_from_contracts(items)
        chosen = _select_contracts(
            items,
            mode=args.mode,
            center=center,
            strike_band_pct=args.strike_band_pct,
            atm_window=args.atm_window,
        )
        selected[key] = chosen

    selected_contracts = [
        contract
        for key in sorted(selected, key=lambda item: (item[0], item[1]))
        for contract in selected[key]
    ]
    if args.max_contracts > 0:
        selected_contracts = selected_contracts[: args.max_contracts]

    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    plan_df = pd.DataFrame([asdict(row) for row in _plan_rows(grouped, selected, spot_cache, args.mode)])
    contracts_df = pd.DataFrame([asdict(contract) for contract in selected_contracts])
    plan_path = out_dir / "fetch_plan.csv"
    contracts_path = out_dir / "selected_contracts.csv"
    plan_df.to_csv(plan_path, index=False)
    contracts_df.to_csv(contracts_path, index=False)

    manifest: dict[str, Any] = {
        "symbols": symbols,
        "from_date": start.isoformat(),
        "to_date": end.isoformat(),
        "expiries": sorted(expiry.isoformat() for expiry in explicit_expiries) if explicit_expiries else None,
        "source": args.source,
        "mode": args.mode,
        "strike_band_pct": args.strike_band_pct,
        "atm_window": args.atm_window,
        "selected_contracts": len(selected_contracts),
        "plan_csv": str(plan_path),
        "selected_contracts_csv": str(contracts_path),
        "download": bool(args.download),
    }

    if args.download:
        assert client is not None
        cache_df = (
            _load_existing_cache_rows(
                contracts=selected_contracts,
                option_cache_dir=args.option_cache_dir,
                start=start,
                end=end,
                spot_cache=spot_cache,
                risk_free_rate=args.risk_free_rate,
            )
            if args.include_existing_cache
            else pd.DataFrame()
        )
        data_df, statuses = await _download_contracts(
            client,
            selected_contracts,
            start=start,
            end=end,
            min_candles=args.min_candles,
            spot_cache=spot_cache,
            risk_free_rate=args.risk_free_rate,
            skip_existing=args.skip_existing,
        )
        status_df = pd.DataFrame(statuses)
        status_path = out_dir / "download_status.csv"
        status_df.to_csv(status_path, index=False)
        manifest["download_status_csv"] = str(status_path)
        manifest["downloaded_rows"] = int(len(data_df))
        manifest["included_cache_rows"] = int(len(cache_df))
        manifest["api_calls"] = dict(client.api_call_counts)
        if not cache_df.empty and not data_df.empty:
            data_df = pd.concat([cache_df, data_df], ignore_index=True)
            data_df["time"] = pd.to_datetime(data_df["time"], errors="coerce", utc=True)
            data_df = data_df[data_df["time"].notna()]
            data_df = data_df.drop_duplicates(["instrument_key", "interval", "time"], keep="last")
            data_df = data_df.sort_values(["underlying", "expiry", "strike", "option_type", "time"])
        elif not cache_df.empty:
            data_df = cache_df
        manifest["total_output_rows"] = int(len(data_df))
        if not data_df.empty:
            data_path = out_dir / "option_candles.parquet"
            csv_path = out_dir / "option_candles.csv"
            data_df.to_parquet(data_path, index=False)
            data_df.to_csv(csv_path, index=False)
            manifest["option_candles_parquet"] = str(data_path)
            manifest["option_candles_csv"] = str(csv_path)

    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str))
    print(json.dumps(manifest, indent=2, default=str))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", help="Comma-separated underlyings. Defaults to the 10-instrument pilot universe.")
    parser.add_argument("--from-date", required=True, help="Inclusive candle start date, YYYY-MM-DD.")
    parser.add_argument("--to-date", required=True, help="Inclusive candle end date, YYYY-MM-DD.")
    parser.add_argument("--expiries", help="Optional comma-separated expiry dates. If omitted, uses expiries in the selected source/date range.")
    parser.add_argument("--source", choices=["catalog", "upstox"], default="catalog")
    parser.add_argument("--mode", choices=["full", "spot-band", "atm-window"], default="full")
    parser.add_argument("--strike-band-pct", type=float, default=0.15, help="Used only with --mode spot-band.")
    parser.add_argument("--atm-window", type=int, default=21, help="Number of strikes to keep in --mode atm-window.")
    parser.add_argument("--max-expiries-per-symbol", type=int, default=0, help="Used only with --source upstox; 0 means no cap.")
    parser.add_argument("--max-contracts", type=int, default=0, help="Cap selected contracts for smoke tests; 0 means no cap.")
    parser.add_argument("--min-candles", type=int, default=20)
    parser.add_argument("--risk-free-rate", type=float, default=0.06)
    parser.add_argument("--gap-seconds", type=float, default=1.2)
    parser.add_argument("--download", action="store_true", help="Actually fetch candles. Without this, only writes the fetch plan.")
    parser.add_argument("--skip-existing", action="store_true", help="Skip catalog contracts with at least --min-candles rows already recorded.")
    parser.add_argument("--include-existing-cache", action="store_true", help="Merge matching local option_candles parquet rows into the output dataset.")
    parser.add_argument("--contracts-path", type=Path, default=DEFAULT_CONTRACTS_PATH)
    parser.add_argument("--spot-dir", type=Path, default=DEFAULT_SPOT_DIR)
    parser.add_argument("--option-cache-dir", type=Path, default=DEFAULT_OPTION_CACHE_DIR)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
