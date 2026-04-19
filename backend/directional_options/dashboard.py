"""Optional Dash mount for the directional options workspace."""
from __future__ import annotations

from dataclasses import asdict

from directional_options.schemas import DashboardMountState


_DASHBOARD_STATE = DashboardMountState(
    mounted=False,
    url=None,
    reason="Dash dependency is not installed yet. Add backend requirements to mount the embedded dashboard.",
)


def get_dashboard_mount_state() -> dict[str, object]:
    return asdict(_DASHBOARD_STATE)


def mount_directional_options_dashboard(app, service) -> dict[str, object]:
    """Mount a lightweight Dash dashboard when the dependency is present."""
    global _DASHBOARD_STATE

    try:
        from dash import Dash, dcc, html
        from starlette.middleware.wsgi import WSGIMiddleware
    except ModuleNotFoundError:
        return get_dashboard_mount_state()

    _DASHBOARD_STATE = DashboardMountState(
        mounted=True,
        url="/directional-options/dashboard/",
        reason="Dash workspace mounted successfully.",
    )
    service.workspace.cache_clear()

    dash_app = Dash(__name__, requests_pathname_prefix="/directional-options/dashboard/")

    def serve_layout():
        workspace = service.workspace(
            service.config["default_underlying"],
            service.config["default_timeframe"],
            int(service.config["backtest"]["lookback_sessions"]),
        )
        summary = workspace["backtest"]["summary"]
        equity_curve = workspace["backtest"]["equity_curve"]
        monthly = workspace["backtest"]["monthly"]
        selected = workspace["snapshot"].get("selected_contract") or {}
        signal = workspace["snapshot"].get("signal") or {}
        regime = workspace["snapshot"].get("regime") or {}
        return html.Div(
            style={"fontFamily": "system-ui, sans-serif", "padding": "24px", "backgroundColor": "#0b1220", "color": "#ecf2ff"},
            children=[
                html.H1("Directional Long Options Desk"),
                html.P("Convex long-premium engine with regime filters, option scoring, and bounded backtests."),
                html.Div(
                    style={"display": "grid", "gridTemplateColumns": "repeat(4, minmax(0, 1fr))", "gap": "12px", "marginBottom": "20px"},
                    children=[
                        _metric_card("Trades", str(summary.get("trade_count", 0))),
                        _metric_card("Expectancy", f"₹{summary.get('expectancy', 0):,.0f}"),
                        _metric_card("Max DD", f"{summary.get('max_drawdown_pct', 0):.1f}%"),
                        _metric_card("Engine Score", f"{summary.get('engine_score', 0):.1f}"),
                    ],
                ),
                html.Div(
                    style={"display": "grid", "gridTemplateColumns": "1.4fr 1fr", "gap": "18px"},
                    children=[
                        dcc.Graph(
                            figure={
                                "data": [
                                    {
                                        "x": [point["time"] for point in equity_curve],
                                        "y": [point["equity"] for point in equity_curve],
                                        "type": "scatter",
                                        "mode": "lines",
                                        "line": {"color": "#22c55e", "width": 2},
                                        "name": "Equity",
                                    }
                                ],
                                "layout": {
                                    "paper_bgcolor": "#101827",
                                    "plot_bgcolor": "#101827",
                                    "font": {"color": "#dbeafe"},
                                    "margin": {"l": 40, "r": 20, "t": 30, "b": 40},
                                    "title": "Backtest Equity Curve",
                                },
                            }
                        ),
                        html.Div(
                            style={"backgroundColor": "#101827", "borderRadius": "16px", "padding": "16px"},
                            children=[
                                html.H3("Latest Snapshot"),
                                html.P(f"Regime: {regime.get('label', '--')}"),
                                html.P(f"Direction: {signal.get('direction', '--')}"),
                                html.P(f"Confidence: {signal.get('confidence', 0):.0%}" if signal else "Confidence: --"),
                                html.P(f"Contract: {selected.get('trading_symbol', '--')}"),
                                html.P(f"Expected PnL: ₹{selected.get('expected_pnl', 0):,.2f}" if selected else "Expected PnL: --"),
                            ],
                        ),
                    ],
                ),
                dcc.Graph(
                    figure={
                        "data": [
                            {
                                "x": [item["month"] for item in monthly],
                                "y": [item["pnl"] for item in monthly],
                                "type": "bar",
                                "marker": {"color": ["#22c55e" if item["pnl"] >= 0 else "#ef4444" for item in monthly]},
                                "name": "Monthly PnL",
                            }
                        ],
                        "layout": {
                            "paper_bgcolor": "#101827",
                            "plot_bgcolor": "#101827",
                            "font": {"color": "#dbeafe"},
                            "margin": {"l": 40, "r": 20, "t": 30, "b": 40},
                            "title": "Monthly PnL",
                        },
                    }
                ),
            ],
        )

    dash_app.layout = serve_layout
    app.mount("/directional-options/dashboard", WSGIMiddleware(dash_app.server))
    return get_dashboard_mount_state()


def _metric_card(label: str, value: str):
    from dash import html

    return html.Div(
        style={"backgroundColor": "#101827", "borderRadius": "16px", "padding": "14px"},
        children=[
            html.Div(label, style={"fontSize": "11px", "opacity": 0.68, "textTransform": "uppercase", "letterSpacing": "0.12em"}),
            html.Div(value, style={"fontSize": "24px", "fontWeight": 700, "marginTop": "8px"}),
        ],
    )
