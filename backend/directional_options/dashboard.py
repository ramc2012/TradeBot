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
        from dash import Dash, html
        from starlette.middleware.wsgi import WSGIMiddleware
    except ModuleNotFoundError:
        return get_dashboard_mount_state()

    _DASHBOARD_STATE = DashboardMountState(
        mounted=True,
        url="/directional-options/dashboard/",
        reason="Dash workspace mounted successfully.",
    )
    if hasattr(service, "_summary_cache"):
        service._summary_cache = {"payload": None, "expires_at": 0.0}
    service.workspace.cache_clear()

    dash_app = Dash(__name__, requests_pathname_prefix="/directional-options/dashboard/")

    def serve_layout():
        summary = service.summary()
        automation = dict(summary.get("automation") or {})
        coverage = list(summary.get("coverage") or [])
        underlyings = list(summary.get("underlyings") or [])
        return html.Div(
            style={
                "fontFamily": "system-ui, sans-serif",
                "padding": "24px",
                "backgroundColor": "#0b1220",
                "color": "#ecf2ff",
                "minHeight": "100vh",
            },
            children=[
                html.H1("Directional Long Options Desk"),
                html.P(
                    "Embedded dashboard is mounted from cached module metadata. "
                    "Use the main React workspace for full diagnostics and heavy backtests."
                ),
                html.Div(
                    style={
                        "display": "grid",
                        "gridTemplateColumns": "repeat(4, minmax(0, 1fr))",
                        "gap": "12px",
                        "marginBottom": "20px",
                    },
                    children=[
                        _metric_card("Universe", str(len(underlyings))),
                        _metric_card("Auto Runner", "Active" if summary.get("auto_started") else "Idle"),
                        _metric_card("Loop Active", "Yes" if automation.get("loop_active") else "No"),
                        _metric_card("Last Run", str(automation.get("last_run_at") or "--")),
                    ],
                ),
                html.Div(
                    style={"display": "grid", "gridTemplateColumns": "1fr 1fr", "gap": "18px"},
                    children=[
                        html.Div(
                            style={"backgroundColor": "#101827", "borderRadius": "16px", "padding": "16px"},
                            children=[
                                html.H3("Automation"),
                                html.P(f"Runner key: {automation.get('key', 'directional_options')}"),
                                html.P(f"Enabled: {automation.get('enabled', False)}"),
                                html.P(f"Interval seconds: {automation.get('interval_seconds', '--')}"),
                                html.P(f"Next run: {automation.get('next_run_at') or '--'}"),
                                html.A(
                                    "Open full directional workspace",
                                    href="/directional-options",
                                    style={"color": "#60a5fa"},
                                ),
                            ],
                        ),
                        html.Div(
                            style={"backgroundColor": "#101827", "borderRadius": "16px", "padding": "16px"},
                            children=[
                                html.H3("Coverage"),
                                html.Div(
                                    style={
                                        "display": "grid",
                                        "gridTemplateColumns": "1.2fr 1fr 1fr",
                                        "gap": "8px",
                                        "fontSize": "13px",
                                    },
                                    children=[
                                        html.Strong("Underlying"),
                                        html.Strong("Spot Rows"),
                                        html.Strong("Option Contracts"),
                                        *[
                                            item
                                            for row in coverage
                                            for item in (
                                                html.Div(str(row.get("underlying") or "--")),
                                                html.Div(str(row.get("spot_rows") or 0)),
                                                html.Div(str(row.get("option_contracts") or 0)),
                                            )
                                        ],
                                    ],
                                ),
                            ],
                        ),
                    ],
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
