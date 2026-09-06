"""Causal auction chart data, derived solely from the lane's existing inputs."""
from datetime import datetime
from auction_intelligence.market_profile.engine import MarketProfileEngine
from auction_intelligence.schemas import MarketBar
from mp_core.cache import fingerprint, stats
from mp_core.intelligence import unified_signals


def build_insights(request, bundle, config):
    bars = [MarketBar(**{**b, "timestamp": datetime.fromisoformat(str(b["timestamp"]))}) for b in request["bars"]]
    engine = MarketProfileEngine(config["market_profile"])
    path = []
    cumulative = 0.0
    for index, bar in enumerate(bars):
        profile = engine.build_profile(bundle.market_profile.symbol, bars[:index + 1], bundle.prior_market_profile)
        signed = bar.volume * (bar.close - bar.open) / max(bar.high - bar.low, profile.tick_size)
        cumulative += signed
        path.append({"time": bar.timestamp.isoformat(), "close": bar.close, "high": bar.high, "low": bar.low,
                     "poc": profile.poc, "vah": profile.vah, "val": profile.val,
                     "volume": bar.volume, "flow_proxy": signed, "cumulative_flow_proxy": cumulative,
                     "location": "above" if bar.close > profile.vah else "below" if bar.close < profile.val else "inside"})
    profile = bundle.market_profile
    location = path[-1]["location"] if path else "unavailable"
    age = request.get("metadata", {}).get("data_status", {}).get("minute_history_age_seconds")
    return {"snapshot_id": fingerprint([request["bars"], request["quote"], config["market_profile"]])[:16],
            "execution_mode": "paper", "allow_live_orders": False,
            "risk_limits_enforced": True, "cost_model": "Estimated slippage + fixed order fees; statutory charges excluded",
            "as_of": request["metadata"]["snapshot_time"], "session_date": profile.session_date,
            "path": path, "location": location, "intelligence": unified_signals(profile),
            "flow_label": "Candle pressure proxy · not aggressor volume",
            "flow_available": any(b.volume > 0 for b in bars),
            "source": request["metadata"]["history_source"],
            "bar_interval_minutes": request["metadata"].get("bar_interval_minutes", 1),
            "bar_age_seconds": age, "cache": stats(),
            "readout": f"Price is {location} developing value. " +
                       ("POC is migrating higher." if (profile.poc_shift or 0) > 0 else "POC is migrating lower." if (profile.poc_shift or 0) < 0 else "No directional POC migration."),
            "entry_gate": "historical_replay" if request["metadata"]["snapshot_mode"] != "live_session" else
                          "proposal_available" if bundle.execution_plan else "risk_blocked" if not bundle.risk.allowed else "waiting_for_setup_or_contract"}
