"""Offline tests for the USDINR collector's pure-logic pieces -- no network,
no database. Matches the live app's own test convention (see
tests/test_m1_participant_oi.py).
"""
import io
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from ingest.m_usdinr_fx import fetch, pick_front_month_symbol  # noqa: E402


class _FakeResponse:
    def __init__(self, status_code=200, text="", json_payload=None):
        self.status_code = status_code
        self.text = text
        self._json = json_payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("bad status", request=None, response=self)

    def json(self):
        return self._json


class _FakeClient:
    """Stand-in for httpx.Client -- returns a canned response per call."""

    def __init__(self, response):
        self._response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._response


# A trimmed real fragment of NSE_CD.csv (captured live 2026-08-26): three
# USDINR *FUT rows at different expiries, plus one EURINR row that must be
# ignored (different root), and one USDINR row that already expired.
MASTER_FRAGMENT = """\
10122608271028,EURINR 27 Aug 26 FUT,16,1,0.0025,,0900-1700|1815-1915:,2026-08-25,1787814000,NSE:EURINR26AUGFUT,10,12,1028,EURINR,25,-1.0,XX,101200000025,None,0,0.0
99900001,USDINR 01 Jan 20 FUT,16,1,0.0025,,0900-1700|1815-1915:,2020-01-01,1000000,NSE:USDINR20JANFUT,10,12,99,USDINR,1,-1.0,XX,10120000001,None,0,0.0
10122608271103,USDINR 27 Aug 26 FUT,16,1,0.0025,,0900-1700|1815-1915:,2026-08-25,1787814000,NSE:USDINR26AUGFUT,10,12,1103,USDINR,1,-1.0,XX,10120000001,None,0,0.0
10122609042674,USDINR 04 Sep 26 FUT,16,1,0.0025,,0900-1700|1815-1915:,2026-08-25,1788505200,NSE:USDINR26904FUT,10,12,2674,USDINR,1,-1.0,XX,10120000001,None,0,0.0
"""


def test_picks_the_nearest_not_yet_expired_usdinr_future_not_eurinr_or_expired():
    # as_of pinned between the expired 2020 row and the two live rows.
    as_of = 1_700_000_000.0
    client = _FakeClient(_FakeResponse(status_code=200, text=MASTER_FRAGMENT))
    symbol = pick_front_month_symbol(client, as_of=as_of)
    assert symbol == "NSE:USDINR26AUGFUT"  # earlier of the two live expiries


def test_fetch_parses_candles_into_dated_ohlcv_rows():
    payload = {
        "s": "ok",
        "candles": [
            [1787184000, 95.51, 95.68, 95.51, 95.65, 365465],
            [1787270400, 95.62, 95.72, 95.61, 95.6825, 812447],
        ],
    }
    client = _FakeClient(_FakeResponse(status_code=200, json_payload=payload))
    result = fetch(client, "id:token", "NSE:USDINR26AUGFUT",
                    start=__import__("datetime").date(2026, 8, 1),
                    end=__import__("datetime").date(2026, 8, 26))
    assert result.status == "ok"
    assert len(result.rows) == 2
    assert result.rows[0]["close"] == 95.65
    assert result.rows[1]["volume"] == 812447


def test_fetch_reports_error_status_without_raising_on_fyers_error_payload():
    payload = {"s": "error", "message": "invalid symbol"}
    client = _FakeClient(_FakeResponse(status_code=200, json_payload=payload))
    result = fetch(client, "id:token", "NSE:BOGUSFUT",
                    start=__import__("datetime").date(2026, 8, 1),
                    end=__import__("datetime").date(2026, 8, 26))
    assert result.status == "error"
    assert "invalid symbol" in result.detail


def test_fetch_reports_empty_status_for_zero_candles():
    client = _FakeClient(_FakeResponse(status_code=200, json_payload={"s": "ok", "candles": []}))
    result = fetch(client, "id:token", "NSE:USDINR26AUGFUT",
                    start=__import__("datetime").date(2026, 8, 1),
                    end=__import__("datetime").date(2026, 8, 26))
    assert result.status == "empty"
