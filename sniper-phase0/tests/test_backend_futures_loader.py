from __future__ import annotations

from pathlib import Path

import pandas as pd

from nomad_sniper.data.bars import load_minute_bars
from nomad_sniper.utils.timeutil import IST


def test_load_backend_futures_cache_csv(tmp_path: Path):
    root = tmp_path / "futures" / "underlying=NIFTY"
    root.mkdir(parents=True)
    path = root / "1minute.csv.gz"
    pd.DataFrame(
        [
            {
                "time": "2026-05-29T03:45:00+00:00",
                "underlying": "NIFTY",
                "expiry": "2026-06-25",
                "instrument_key": "NSE:NIFTY26JUNFUT",
                "trading_symbol": "NIFTY26JUNFUT",
                "open": 25000.0,
                "high": 25010.0,
                "low": 24990.0,
                "close": 25005.0,
                "volume": 1000,
            }
        ]
    ).to_csv(path, index=False)

    bars = load_minute_bars("nifty", futures_dir=tmp_path / "futures")

    assert len(bars) == 1
    assert bars.index[0].tzinfo is not None
    assert bars.index[0].astimezone(IST).hour == 9
    assert bars.index[0].astimezone(IST).minute == 15
    assert bars.iloc[0]["contract_expiry"].isoformat() == "2026-06-25"
    assert bars.iloc[0]["instrument_key"] == "NSE:NIFTY26JUNFUT"
