"""End-to-end smoke + DATA-COVERAGE audit of the option read layer on the
June-July 2026 smoke extract. Prints the honesty numbers the study report
cites: dup rate (D4), iv/oi presence (D5), modelled-exit rate by moneyness
band (D3), and per-expiry tape coverage. Read-only; no PG access.
"""
from __future__ import annotations

import glob
import os
from datetime import date

import pandas as pd

from option_read_layer import OptionReadLayer, load_opt_extracts, load_spot_csvs

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "opt")


def main() -> None:
    opt = load_opt_extracts(sorted(glob.glob(os.path.join(DATA, "opt_*.csv"))))
    spot = load_spot_csvs(sorted(glob.glob(os.path.join(DATA, "spot_*.csv"))))
    n_raw = len(opt)
    layer = OptionReadLayer(opt, spot)
    n_dedup = len(layer.opt)
    print(f"raw option rows        : {n_raw:,}")
    print(f"deduped contract-bars  : {n_dedup:,}  "
          f"(cross-broker dup rate {1 - n_dedup / n_raw:.1%})")
    o = layer.opt
    print(f"contracts              : {o['contract_id'].nunique():,}")
    print(f"underlyings            : {o['underlying'].nunique()}")
    print(f"iv present             : {o['iv'].notna().mean():.1%}")
    print(f"oi present             : {(pd.to_numeric(o['oi'], errors='coerce').fillna(0) > 0).mean():.1%}")
    print(f"spot rows deduped      : {len(layer.spot):,} "
          f"({layer.spot['underlying'].nunique()} names)")
    exp = o.groupby("expiry").agg(bars=("close", "size"),
                                  unds=("underlying", "nunique"),
                                  t0=("time", "min"), t1=("time", "max"))
    print("\nper-expiry coverage (deduped):")
    print(exp[exp["bars"] > 5000].to_string())

    # ---- exercise the API on a real name/session --------------------------
    und, session = "RELIANCE", date(2026, 7, 15)
    for side in ("CE", "PE"):
        cs = layer.contracts_for(und, session, side)
        print(f"\ncontracts_for({und}, {session}, {side}):")
        if cs.empty:
            print("  EMPTY")
            continue
        print(cs[["contract_id", "band", "dte", "mny", "close",
                  "iv_present", "oi_present"]].to_string(index=False))
        cid = cs["contract_id"].iloc[0]
        for ts in ("2026-07-15 09:45:00+00:00", "2026-07-17 09:45:00+00:00"):
            m = layer.mark(cid, pd.Timestamp(ts))
            print(f"  mark {ts}: {m}")

    # ---- modelled-exit rate: contracts whose tape ends >45m before the
    # 15:15 bar of their last traded session in-window (the ATM-walkaway
    # signature), by band of final moneyness ------------------------------
    last = o.groupby("contract_id").agg(t_last=("time", "max"),
                                        und=("underlying", "first"))
    t_end = o["time"].max()
    active_cut = t_end - pd.Timedelta(days=2)
    ended = last[last["t_last"] < active_cut]
    print(f"\ncontracts whose tape ends >2 sessions before window end: "
          f"{len(ended):,} / {len(last):,} ({len(ended) / len(last):.1%}) "
          f"(expiry roll-off + ATM walk-away mixed; the study separates them "
          f"by expiry date at analysis time)")


if __name__ == "__main__":
    main()
