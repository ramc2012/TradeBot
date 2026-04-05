from __future__ import annotations

from api.routers.auth import format_broker_status_summary


def test_format_broker_status_summary_humanizes_upstox_status() -> None:
    summary = format_broker_status_summary(
        {
            "connected_brokers": ["fyers"],
            "upstox_ready": False,
            "upstox_token_health": {"status": "expired_reconnect_required"},
        }
    )

    assert summary == (
        "Broker Status: FYERS connected | UPSTOX expired reconnect required | "
        "BREEZE disconnected | 5PAISA disconnected"
    )


def test_format_broker_status_summary_marks_upstox_connected_when_ready() -> None:
    summary = format_broker_status_summary(
        {
            "connected_brokers": ["fyers", "upstox"],
            "upstox_ready": True,
            "upstox_token_health": {"status": "valid_no_refresh"},
        }
    )

    assert "UPSTOX connected" in summary
