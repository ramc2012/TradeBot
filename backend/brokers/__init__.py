from brokers.base import BrokerAdapter
from brokers.fyers import FyersAdapter
from brokers.upstox import UpstoxAdapter
from brokers.fivepaisa import FivePaisaAdapter
from brokers.icici_breeze import ICICIBreezeAdapter

BROKER_MAP = {
    "fyers": FyersAdapter,
    "upstox": UpstoxAdapter,
    "fivepaisa": FivePaisaAdapter,
    "icici_breeze": ICICIBreezeAdapter,
}

def get_broker(name: str) -> BrokerAdapter:
    cls = BROKER_MAP.get(name)
    if not cls:
        raise ValueError(f"Unknown broker: {name}")
    return cls()
