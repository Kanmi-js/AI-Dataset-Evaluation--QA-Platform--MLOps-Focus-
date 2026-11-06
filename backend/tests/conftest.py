"""Shared pytest fixtures.

The suite runs against a real database and the real FastAPI app, but stubs the
two things that would otherwise require containers:

* **Object storage** — ``app.services.storage`` is swapped for an in-memory
  dict, so no MinIO/S3.
* **Celery** — ``process_data_item.delay`` is stubbed, so no Redis broker. The
  worker logic itself is covered directly in ``test_processing.py``.

By default the database is in-memory SQLite, which means ``pytest`` works on a
bare laptop. CI sets ``TEST_DATABASE_URL`` to a PostgreSQL service container so
that ``JSONB`` and the native enum types are exercised for real before merge.
"""

import os
import uuid
from collections.abc import AsyncGenerator, Iterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from app.database import Base, get_db
from app.main import app
from app.services import storage as storage_module
from app.workers.tasks import process_data_item

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "sqlite+aiosqlite://")


def _make_engine():
    """Build an engine suited to whichever backend the suite is pointed at."""
    if TEST_DATABASE_URL.startswith("sqlite"):
        # StaticPool keeps every connection on the same in-memory database;
        # without it each connection would get its own empty one.
        return create_async_engine(
            TEST_DATABASE_URL,
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
    return create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)


@pytest_asyncio.fixture
async def session_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    """Fresh schema per test — no cross-test state to reason about."""
    engine = _make_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    yield async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


# --------------------------------------------------------------------------
# Stubs
# --------------------------------------------------------------------------


class FakeStorage:
    """Minimal in-memory stand-in for the S3 helpers in app.services.storage."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
        self.buckets_ensured = 0

    def ensure_bucket(self, bucket: str | None = None) -> None:
        self.buckets_ensured += 1

    def upload_bytes(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        bucket: str | None = None,
    ) -> str:
        self.objects[key] = data
        self.content_types[key] = content_type
        return key

    def download_bytes(self, key: str, bucket: str | None = None) -> bytes:
        return self.objects[key]

    def generate_presigned_url(
        self, key: str, expires_in: int = 3600, bucket: str | None = None
    ) -> str:
        return f"https://storage.test/{key}?signed=1"

    def delete_object(self, key: str, bucket: str | None = None) -> None:
        self.objects.pop(key, None)


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch) -> FakeStorage:
    """Replace both the sync helpers and their async wrappers."""
    fake = FakeStorage()

    monkeypatch.setattr(storage_module, "ensure_bucket", fake.ensure_bucket)
    monkeypatch.setattr(storage_module, "upload_bytes", fake.upload_bytes)
    monkeypatch.setattr(storage_module, "download_bytes", fake.download_bytes)
    monkeypatch.setattr(storage_module, "delete_object", fake.delete_object)
    monkeypatch.setattr(
        storage_module, "generate_presigned_url", fake.generate_presigned_url
    )

    async def ensure_bucket_async(bucket: str | None = None) -> None:
        fake.ensure_bucket(bucket)

    async def upload_bytes_async(
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        bucket: str | None = None,
    ) -> str:
        return fake.upload_bytes(key, data, content_type, bucket)

    async def download_bytes_async(key: str, bucket: str | None = None) -> bytes:
        return fake.download_bytes(key, bucket)

    async def delete_object_async(key: str, bucket: str | None = None) -> None:
        fake.delete_object(key, bucket)

    monkeypatch.setattr(storage_module, "ensure_bucket_async", ensure_bucket_async)
    monkeypatch.setattr(storage_module, "upload_bytes_async", upload_bytes_async)
    monkeypatch.setattr(storage_module, "download_bytes_async", download_bytes_async)
    monkeypatch.setattr(storage_module, "delete_object_async", delete_object_async)

    return fake


class FakeAsyncResult:
    def __init__(self, task_id: str) -> None:
        self.id = task_id


class RecordingDelay:
    """Captures the item ids that would have been enqueued."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def __call__(self, data_item_id: int) -> FakeAsyncResult:
        self.calls.append(data_item_id)
        return FakeAsyncResult(f"test-task-{uuid.uuid4().hex[:8]}")


@pytest.fixture(autouse=True)
def celery_delay(monkeypatch: pytest.MonkeyPatch) -> Iterator[RecordingDelay]:
    """Stop every test from trying to reach a Redis broker."""
    recorder = RecordingDelay()
    monkeypatch.setattr(process_data_item, "delay", recorder)
    yield recorder


# --------------------------------------------------------------------------
# HTTP clients
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
    storage: FakeStorage,
) -> AsyncGenerator[AsyncClient, None]:
    """Unauthenticated client wired to the test database.

    Mirrors app.database.get_db: commit on success, roll back on error.
    """

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def register_and_login(
    client: AsyncClient, email: str, password: str = "hunter2-hunter2"
) -> dict[str, Any]:
    """Register a user, log in, and return headers plus the user payload."""
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "full_name": "Test User"},
    )
    assert resp.status_code == 201, resp.text
    user = resp.json()

    resp = await client.post(
        "/api/v1/auth/login",
        data={"username": email, "password": password},
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]

    return {"user": user, "headers": {"Authorization": f"Bearer {token}"}}


@pytest_asyncio.fixture
async def auth(client: AsyncClient) -> dict[str, Any]:
    """A registered, logged-in user."""
    return await register_and_login(client, "owner@example.com")


@pytest_asyncio.fixture
async def other_auth(client: AsyncClient) -> dict[str, Any]:
    """A second user, for tenant-isolation assertions."""
    return await register_and_login(client, "intruder@example.com")


@pytest_asyncio.fixture
async def dataset(client: AsyncClient, auth: dict[str, Any]) -> dict[str, Any]:
    resp = await client.post(
        "/api/v1/datasets",
        json={"name": "Traffic signs", "description": "batch 3"},
        headers=auth["headers"],
    )
    assert resp.status_code == 201, resp.text
    return resp.json()
