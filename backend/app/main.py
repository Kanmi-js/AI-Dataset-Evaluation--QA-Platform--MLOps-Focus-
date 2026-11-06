"""FastAPI application wiring: middleware, startup, and route registration.

TerrainAI runs sports tactical analysis on top of a generic dataset QA
pipeline. An upload is streamed to S3, a Celery worker pre-processes it, and the
browser dashboard reviews the result — so this module stays thin: it builds the
app, decides what runs at startup, and mounts every router exactly once.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.api.routes import auth, datasets, health, internal, items, uploads
from app.config import settings
from app.database import Base, engine
from app.services import storage

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

APP_VERSION = "0.1.0"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Prepare development dependencies; production arrives already migrated."""
    if settings.is_development:
        # Create tables straight from the models so a fresh clone has no
        # migration step to remember. Production runs `alembic upgrade head` in
        # a one-shot container before this process binds — see
        # infrastructure/ec2/user-data.sh.tftpl.
        #
        # `engine` is an AsyncEngine (app/database.py), and
        # `Base.metadata.create_all` is synchronous, so it has to be handed to
        # `run_sync` on a real connection. Calling it directly raises — and
        # catching that error would leave the API serving a database with no
        # tables, which is worse than failing to start.
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database schema ensured (development)")

        # A missing bucket, unlike a missing schema, is recoverable: the upload
        # route calls ensure_bucket_async() on every request
        # (app/api/routes/uploads.py). So storage that is not up yet should not
        # stop the API from booting.
        try:
            await storage.ensure_bucket_async()
            logger.info("Storage bucket %r ensured (development)", settings.s3_bucket)
        except Exception:
            logger.warning(
                "Could not ensure the storage bucket at startup; the upload "
                "route will retry on each request.",
                exc_info=True,
            )

    yield

    logger.info("Application shutdown")


app = FastAPI(
    title=settings.app_name,
    version=APP_VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)


class TunnelHeaderMiddleware(BaseHTTPMiddleware):
    """Stop devtunnel-style proxies injecting their interstitial page.

    Deliberately limited to that one header. An earlier version also forced
    `Access-Control-Allow-Origin: *` here, which silently overrode the origin
    allowlist below and — combined with `allow_credentials=True` — produced a
    header combination browsers reject outright. Origin policy belongs to
    CORSMiddleware alone.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        response.headers["X-Tunnel-Skip-Anti-Phishing-Page"] = "1"
        return response


if settings.enable_tunnel_headers:
    app.add_middleware(TunnelHeaderMiddleware)

# The dashboard is served same-origin in both dev (Vite proxies /api — see
# frontend/vite.config.ts) and production (nginx), so this allowlist exists for
# direct API clients rather than the app itself. Credentialed requests require
# explicit origins; a wildcard is invalid alongside allow_credentials.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", tags=["meta"])
async def root() -> dict:
    """Unauthenticated service banner. DB-free, so it answers before readiness."""
    return {"name": settings.app_name, "version": APP_VERSION, "docs": "/docs"}


# Every router, mounted unconditionally.
#
# These were previously wrapped in try/except ImportError, which turned a
# broken import into a silently missing route group: the API kept answering,
# but every path it owned returned 404 — including /health, which the ALB
# health check, the container healthcheck and the boot gate all depend on. An
# import error here must stop the process.
app.include_router(health.router, prefix=settings.api_v1_prefix)
app.include_router(auth.router, prefix=settings.api_v1_prefix)
app.include_router(datasets.router, prefix=settings.api_v1_prefix)
app.include_router(uploads.router, prefix=settings.api_v1_prefix)
app.include_router(items.router, prefix=settings.api_v1_prefix)
app.include_router(internal.router, prefix=settings.api_v1_prefix)
