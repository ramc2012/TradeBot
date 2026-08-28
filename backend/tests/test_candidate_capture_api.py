"""Contract for the read-only candidate-capture API.

The load-bearing assertion is that this surface stays READ-ONLY. It sits over a
research dataset that models will be trained on, and an endpoint that could
mutate it — or trigger a run — would put a write path behind an HTTP verb that
looks like a query.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from api.routers.candidate_capture import router


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


class TestSurfaceShape:
    def test_every_route_is_a_get(self):
        """No POST/PUT/PATCH/DELETE: this surface cannot change anything."""
        for route in router.routes:
            assert set(getattr(route, "methods", set())) <= {"GET", "HEAD"}, (
                f"{route.path} exposes a mutating verb; the capture surface is read-only"
            )

    def test_expected_routes_are_registered(self):
        paths = {route.path for route in router.routes}
        assert paths == {
            "/api/candidate-capture/readiness",
            "/api/candidate-capture/coverage",
            "/api/candidate-capture/decidability",
            "/api/candidate-capture/sessions",
            "/api/candidate-capture/filters",
            "/api/candidate-capture/method",
            "/api/candidate-capture/direction",
            "/api/candidate-capture/models",
            "/api/candidate-capture/training-runs",
            "/api/candidate-capture/snapshots",
            "/api/candidate-capture/outcomes",
        }

    def test_router_is_registered_on_the_app(self):
        """A router nobody mounts is an endpoint that does not exist."""
        import main

        assert any(
            getattr(r, "path", "").startswith("/api/candidate-capture")
            for r in main.app.routes
        ), "candidate_capture router is not included in main.app"


class TestInputValidation:
    """Bad input is refused with a reason, never answered with an empty list."""

    @pytest.mark.asyncio
    async def test_malformed_session_date_is_a_400_with_the_reason(self):
        async with AsyncClient(
            transport=ASGITransport(app=_app()), base_url="http://t"
        ) as client:
            resp = await client.get(
                "/api/candidate-capture/snapshots", params={"session_date": "garbage"}
            )
        assert resp.status_code == 400
        assert "ISO date" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_missing_required_session_date_is_a_422(self):
        async with AsyncClient(
            transport=ASGITransport(app=_app()), base_url="http://t"
        ) as client:
            resp = await client.get("/api/candidate-capture/snapshots")
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_limit_is_bounded(self):
        async with AsyncClient(
            transport=ASGITransport(app=_app()), base_url="http://t"
        ) as client:
            resp = await client.get(
                "/api/candidate-capture/snapshots",
                params={"session_date": "2026-08-25", "limit": 100_000},
            )
        # Refused by the Query bound rather than silently returning everything.
        assert resp.status_code == 422
