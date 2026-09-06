"""Use the same configured NSE calendar as the paper backend."""
import importlib.util
from pathlib import Path
from datetime import timedelta

path = Path(__file__).resolve().parents[2] / "backend/core/trading_calendar.py"
if not path.exists():
    path = Path("/app/core/trading_calendar.py")
spec = importlib.util.spec_from_file_location("vanguard_trading_calendar", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
calendar = module.trading_calendar


def is_session(day):
    return calendar.has_exchange_session("NSE", day)


def previous_session(day):
    day -= timedelta(days=1)
    while not is_session(day):
        day -= timedelta(days=1)
    return day
