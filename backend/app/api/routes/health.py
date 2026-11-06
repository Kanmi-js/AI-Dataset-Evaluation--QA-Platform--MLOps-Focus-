"""Liveness / readiness endpoints for Docker healthchecks and load balancers."""

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    """Liveness: process is up."""
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness(db: AsyncSession = Depends(get_db)) -> dict:
    """Readiness: dependencies (DB) are reachable."""
    await db.execute(text("SELECT 1"))
    return {"status": "ready", "database": "ok"}
