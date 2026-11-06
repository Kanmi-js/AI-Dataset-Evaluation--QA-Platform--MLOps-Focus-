"""Lightweight API smoke tests.

Uses httpx's ASGITransport, which does not run startup/shutdown lifespan
events, so these tests hit the app without needing Postgres, Redis, or S3.
The liveness endpoint and root are DB-free by design.
"""

import httpx
import pytest
from httpx import ASGITransport

from app.main import app

# Every path the app is expected to expose, one per router.
#
# This is the regression guard for an outage that already happened: main.py
# wrapped each include_router in try/except, so a broken import silently
# dropped a whole route group while the app still answered 200 on /. Four
# routers went missing that way — including /health, which the ALB target
# group, the production container healthcheck and the EC2 boot gate all poll,
# so deploys boot-looped while CI looked fine.
EXPECTED_ROUTES = frozenset(
    {
        "/api/v1/health",
        "/api/v1/health/ready",
        "/api/v1/auth/register",
        "/api/v1/auth/login",
        "/api/v1/auth/me",
        "/api/v1/datasets",
        "/api/v1/datasets/{dataset_id}",
        "/api/v1/datasets/{dataset_id}/archive",
        "/api/v1/datasets/{dataset_id}/items",
        "/api/v1/items/{item_id}",
        "/api/v1/items/{item_id}/annotations",
        "/api/v1/items/{item_id}/reprocess",
        "/api/v1/internal/s3-events",
    }
)


def test_every_router_is_mounted():
    # Read the OpenAPI schema rather than walking `app.routes`. FastAPI changed
    # how `include_router` stores routes (0.141 keeps `_IncludedRouter` wrappers
    # instead of flattening `APIRoute`s onto the app), so `route.path` is not a
    # stable way to ask this question across versions. The schema is.
    mounted = set(app.openapi()["paths"])
    missing = sorted(EXPECTED_ROUTES - mounted)
    assert not missing, f"routers not mounted in app.main: {missing}"


@pytest.mark.asyncio
async def test_root():
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/")
    assert resp.status_code == 200
    body = resp.json()
    assert "name" in body and "version" in body


@pytest.mark.asyncio
async def test_health_liveness():
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_protected_route_requires_auth():
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/datasets")
    assert resp.status_code == 401
