"""The Lambda-facing re-drive endpoint: auth, idempotency, unknown keys."""

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.models import DataItem
from app.models.enums import ItemStatus
from tests.conftest import RecordingDelay

S3_EVENTS = "/api/v1/internal/s3-events"
TOKEN_HEADER = {"X-Internal-Token": settings.internal_api_token}


async def _upload_one(
    client: AsyncClient, auth: dict[str, Any], dataset: dict[str, Any]
) -> dict[str, Any]:
    resp = await client.post(
        f"/api/v1/datasets/{dataset['id']}/items",
        files=[("files", ("sign.png", b"not-really-a-png", "image/png"))],
        headers=auth["headers"],
    )
    assert resp.status_code == 201, resp.text
    return resp.json()[0]


async def _set_status(
    session_factory: async_sessionmaker[AsyncSession],
    item_id: int,
    status: ItemStatus,
) -> None:
    async with session_factory() as session:
        item = await session.get(DataItem, item_id)
        assert item is not None
        item.status = status
        await session.commit()


async def test_rejects_missing_token(client: AsyncClient):
    resp = await client.post(S3_EVENTS, json=[{"bucket": "b", "key": "raw/1/x.png"}])
    assert resp.status_code == 401


async def test_rejects_wrong_token(client: AsyncClient):
    resp = await client.post(
        S3_EVENTS,
        json=[{"bucket": "b", "key": "raw/1/x.png"}],
        headers={"X-Internal-Token": "not-the-token"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid internal token"


async def test_empty_configured_token_authenticates_nobody(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    """A blank INTERNAL_API_TOKEN must fail closed, not open.

    The header parameter defaults to "" when absent, and hmac.compare_digest
    of "" against "" is True — so without an explicit emptiness check, a
    deployment that forgot to set the token would accept every anonymous
    request to this router.
    """
    monkeypatch.setattr(settings, "internal_api_token", "")

    for headers in ({}, {"X-Internal-Token": ""}):
        resp = await client.post(
            S3_EVENTS,
            json=[{"bucket": "b", "key": "raw/1/x.png"}],
            headers=headers,
        )
        assert resp.status_code == 401, headers


async def test_unknown_key_is_reported_not_404(client: AsyncClient):
    """A 404 would make the Lambda retry the same event forever."""
    resp = await client.post(
        S3_EVENTS,
        json=[{"bucket": "b", "key": "raw/1/never-uploaded.png"}],
        headers=TOKEN_HEADER,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == [
        {
            "key": "raw/1/never-uploaded.png",
            "action": "unknown_key",
            "data_item_id": None,
        }
    ]


async def test_pending_item_is_requeued(
    client: AsyncClient,
    auth: dict[str, Any],
    dataset: dict[str, Any],
    celery_delay: RecordingDelay,
):
    item = await _upload_one(client, auth, dataset)
    enqueued_on_upload = list(celery_delay.calls)

    resp = await client.post(
        S3_EVENTS,
        json=[{"bucket": "b", "key": item["raw_s3_key"]}],
        headers=TOKEN_HEADER,
    )
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body == [
        {"key": item["raw_s3_key"], "action": "queued", "data_item_id": item["id"]}
    ]
    assert celery_delay.calls == enqueued_on_upload + [item["id"]]


async def test_failed_item_is_requeued_and_error_cleared(
    client: AsyncClient,
    auth: dict[str, Any],
    dataset: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
):
    item = await _upload_one(client, auth, dataset)

    async with session_factory() as session:
        row = await session.get(DataItem, item["id"])
        row.status = ItemStatus.FAILED
        row.error = "cv2 blew up"
        await session.commit()

    resp = await client.post(
        S3_EVENTS,
        json=[{"bucket": "b", "key": item["raw_s3_key"]}],
        headers=TOKEN_HEADER,
    )
    assert resp.json()[0]["action"] == "queued"

    async with session_factory() as session:
        row = await session.get(DataItem, item["id"])
        assert row.status == ItemStatus.PENDING
        assert row.error is None


async def test_processed_item_is_not_requeued(
    client: AsyncClient,
    auth: dict[str, Any],
    dataset: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
    celery_delay: RecordingDelay,
):
    item = await _upload_one(client, auth, dataset)
    await _set_status(session_factory, item["id"], ItemStatus.PROCESSED)
    before = list(celery_delay.calls)

    resp = await client.post(
        S3_EVENTS,
        json=[{"bucket": "b", "key": item["raw_s3_key"]}],
        headers=TOKEN_HEADER,
    )
    assert resp.json()[0]["action"] == "already_processed"
    assert celery_delay.calls == before


async def test_reviewed_item_is_never_clobbered(
    client: AsyncClient,
    auth: dict[str, Any],
    dataset: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
    celery_delay: RecordingDelay,
):
    """A duplicate S3 delivery must not undo a human's review."""
    item = await _upload_one(client, auth, dataset)
    await _set_status(session_factory, item["id"], ItemStatus.REVIEWED)
    before = list(celery_delay.calls)

    resp = await client.post(
        S3_EVENTS,
        json=[{"bucket": "b", "key": item["raw_s3_key"]}],
        headers=TOKEN_HEADER,
    )
    assert resp.json()[0]["action"] == "already_processed"
    assert celery_delay.calls == before

    async with session_factory() as session:
        row = await session.get(DataItem, item["id"])
        assert row.status == ItemStatus.REVIEWED


async def test_in_flight_item_is_left_alone(
    client: AsyncClient,
    auth: dict[str, Any],
    dataset: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
    celery_delay: RecordingDelay,
):
    item = await _upload_one(client, auth, dataset)
    await _set_status(session_factory, item["id"], ItemStatus.PROCESSING)
    before = list(celery_delay.calls)

    resp = await client.post(
        S3_EVENTS,
        json=[{"bucket": "b", "key": item["raw_s3_key"]}],
        headers=TOKEN_HEADER,
    )
    assert resp.json()[0]["action"] == "already_running"
    assert celery_delay.calls == before


async def test_batch_of_mixed_events(
    client: AsyncClient,
    auth: dict[str, Any],
    dataset: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
):
    """One Lambda invocation can carry several records; each gets a verdict."""
    pending = await _upload_one(client, auth, dataset)
    done = await _upload_one(client, auth, dataset)
    await _set_status(session_factory, done["id"], ItemStatus.PROCESSED)

    resp = await client.post(
        S3_EVENTS,
        json=[
            {"bucket": "b", "key": pending["raw_s3_key"]},
            {"bucket": "b", "key": done["raw_s3_key"]},
            {"bucket": "b", "key": "raw/1/ghost.png"},
        ],
        headers=TOKEN_HEADER,
    )
    assert resp.status_code == 200, resp.text

    assert [r["action"] for r in resp.json()] == [
        "queued",
        "already_processed",
        "unknown_key",
    ]
