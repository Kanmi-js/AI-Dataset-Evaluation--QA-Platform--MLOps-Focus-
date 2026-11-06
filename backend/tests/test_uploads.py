"""Upload pipeline: media-type inference, task enqueue, limits, annotation."""

import base64
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from app.api.routes import uploads as uploads_module
from app.models import DataItem, ProcessingTask
from app.models.enums import ItemStatus
from app.workers.tasks import process_data_item
from app.workers.telemetry import SOURCE_SIMULATED
from tests.conftest import FakeStorage, RecordingDelay

# Smallest valid PNG (1x1, opaque black) — enough for content-type sniffing.
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def items_url(dataset_id: int) -> str:
    return f"/api/v1/datasets/{dataset_id}/items"


async def test_upload_infers_media_type(
    client: AsyncClient, auth: dict[str, Any], dataset: dict[str, Any]
):
    resp = await client.post(
        items_url(dataset["id"]),
        files=[
            ("files", ("sign.png", PNG_1X1, "image/png")),
            ("files", ("caption.txt", b"a stop sign", "text/plain")),
        ],
        headers=auth["headers"],
    )
    assert resp.status_code == 201, resp.text

    body = resp.json()
    assert len(body) == 2

    by_name = {item["filename"]: item for item in body}
    assert by_name["sign.png"]["media_type"] == "image"
    assert by_name["caption.txt"]["media_type"] == "text"

    # Everything starts unprocessed; the worker fills in the rest.
    for item in body:
        assert item["status"] == "pending"
        assert item["processed_s3_key"] is None
        assert item["item_metadata"] == {}
        assert item["annotations"] == {}
        assert item["raw_s3_key"].startswith(f"raw/{dataset['id']}/")

    assert by_name["sign.png"]["size_bytes"] == len(PNG_1X1)


async def test_upload_infers_image_from_extension_alone(
    client: AsyncClient, auth: dict[str, Any], dataset: dict[str, Any]
):
    """Browsers sometimes send application/octet-stream; fall back to the suffix."""
    resp = await client.post(
        items_url(dataset["id"]),
        files=[("files", ("scan.TIFF", PNG_1X1, "application/octet-stream"))],
        headers=auth["headers"],
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()[0]["media_type"] == "image"


async def test_upload_infers_video_from_content_type(
    client: AsyncClient, auth: dict[str, Any], dataset: dict[str, Any]
):
    resp = await client.post(
        items_url(dataset["id"]),
        files=[("files", ("match.mp4", b"\x00\x00\x00\x18ftypmp42", "video/mp4"))],
        headers=auth["headers"],
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()[0]["media_type"] == "video"


async def test_upload_infers_video_from_extension_alone(
    client: AsyncClient, auth: dict[str, Any], dataset: dict[str, Any]
):
    """Without this fallback a clip sent as octet-stream is processed as text."""
    resp = await client.post(
        items_url(dataset["id"]),
        files=[("files", ("clip.MOV", b"\x00\x00\x00\x18ftypqt  ", "application/octet-stream"))],
        headers=auth["headers"],
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()[0]["media_type"] == "video"


async def test_upload_writes_raw_bytes_to_storage(
    client: AsyncClient,
    auth: dict[str, Any],
    dataset: dict[str, Any],
    storage: FakeStorage,
):
    resp = await client.post(
        items_url(dataset["id"]),
        files=[("files", ("sign.png", PNG_1X1, "image/png"))],
        headers=auth["headers"],
    )
    key = resp.json()[0]["raw_s3_key"]

    assert storage.objects[key] == PNG_1X1
    assert storage.content_types[key] == "image/png"
    assert storage.buckets_ensured >= 1


async def test_upload_enqueues_one_task_per_item(
    client: AsyncClient,
    auth: dict[str, Any],
    dataset: dict[str, Any],
    celery_delay: RecordingDelay,
    session_factory: async_sessionmaker[AsyncSession],
):
    resp = await client.post(
        items_url(dataset["id"]),
        files=[
            ("files", ("a.txt", b"alpha", "text/plain")),
            ("files", ("b.txt", b"beta", "text/plain")),
        ],
        headers=auth["headers"],
    )
    item_ids = [item["id"] for item in resp.json()]

    # One Celery message per uploaded file, with the ids the API just minted.
    assert celery_delay.calls == item_ids

    # ...and a ProcessingTask row per message, so the dashboard can trace it.
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(ProcessingTask).where(ProcessingTask.data_item_id.in_(item_ids))
            )
        ).scalars().all()

    assert {row.data_item_id for row in rows} == set(item_ids)
    assert all(row.celery_task_id for row in rows)
    assert all(row.status.value == "queued" for row in rows)


async def test_items_are_committed_before_tasks_are_enqueued(
    client: AsyncClient,
    auth: dict[str, Any],
    dataset: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
):
    """Regression guard for the enqueue race.

    The Celery worker is a separate process, so it cannot see a row that is
    still inside an open transaction: if ``.delay()`` fires first, the worker's
    ``session.get(DataItem, id)`` can return ``None`` and the upload is silently
    dropped. Recording both events on one timeline proves the ordering without
    having to race an actual worker.
    """
    timeline: list[str] = []

    def on_commit(session: Session) -> None:
        timeline.append("commit")

    # AsyncSession delegates to a plain sync Session, so a class-level listener
    # catches the commit the route issues.
    event.listen(Session, "after_commit", on_commit)

    # Wrap (not replace) the autouse stub so celery_delay.calls still records.
    stubbed_delay = process_data_item.delay

    def on_delay(data_item_id: int):
        timeline.append("delay")
        return stubbed_delay(data_item_id)

    monkeypatch.setattr(process_data_item, "delay", on_delay)

    try:
        resp = await client.post(
            items_url(dataset["id"]),
            files=[("files", ("a.txt", b"alpha", "text/plain"))],
            headers=auth["headers"],
        )
    finally:
        event.remove(Session, "after_commit", on_commit)

    assert resp.status_code == 201, resp.text
    assert "commit" in timeline, "upload_items never committed"
    assert "delay" in timeline, "upload_items never enqueued"
    assert timeline.index("commit") < timeline.index("delay"), timeline


async def test_upload_rejects_oversized_file(
    client: AsyncClient,
    auth: dict[str, Any],
    dataset: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
):
    """Patch the limit rather than pushing 25 MB through the test client."""
    monkeypatch.setattr(uploads_module, "MAX_FILE_BYTES", 16)

    resp = await client.post(
        items_url(dataset["id"]),
        files=[("files", ("big.txt", b"x" * 17, "text/plain"))],
        headers=auth["headers"],
    )
    assert resp.status_code == 413
    assert "big.txt" in resp.json()["detail"]


async def test_video_is_measured_against_the_video_limit(
    client: AsyncClient,
    auth: dict[str, Any],
    dataset: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
):
    """Clips get their own, larger cap — the image limit must not apply."""
    monkeypatch.setattr(uploads_module, "MAX_FILE_BYTES", 8)
    monkeypatch.setattr(uploads_module, "MAX_VIDEO_BYTES", 64)

    resp = await client.post(
        items_url(dataset["id"]),
        files=[("files", ("match.mp4", b"v" * 32, "video/mp4"))],
        headers=auth["headers"],
    )
    assert resp.status_code == 201, resp.text


async def test_upload_rejects_oversized_video(
    client: AsyncClient,
    auth: dict[str, Any],
    dataset: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(uploads_module, "MAX_VIDEO_BYTES", 16)

    resp = await client.post(
        items_url(dataset["id"]),
        files=[("files", ("big.mp4", b"v" * 17, "video/mp4"))],
        headers=auth["headers"],
    )
    assert resp.status_code == 413
    assert "big.mp4" in resp.json()["detail"]


async def test_upload_to_other_users_dataset_is_404(
    client: AsyncClient, other_auth: dict[str, Any], dataset: dict[str, Any]
):
    resp = await client.post(
        items_url(dataset["id"]),
        files=[("files", ("a.txt", b"alpha", "text/plain"))],
        headers=other_auth["headers"],
    )
    assert resp.status_code == 404


async def test_upload_requires_auth(client: AsyncClient, dataset: dict[str, Any]):
    resp = await client.post(
        items_url(dataset["id"]),
        files=[("files", ("a.txt", b"alpha", "text/plain"))],
    )
    assert resp.status_code == 401


# --------------------------------------------------------------------------
# Item detail + annotation (the human-in-the-loop half of the pipeline)
# --------------------------------------------------------------------------


async def test_item_detail_includes_presigned_raw_url(
    client: AsyncClient, auth: dict[str, Any], dataset: dict[str, Any]
):
    upload = await client.post(
        items_url(dataset["id"]),
        files=[("files", ("sign.png", PNG_1X1, "image/png"))],
        headers=auth["headers"],
    )
    item_id = upload.json()[0]["id"]

    resp = await client.get(f"/api/v1/items/{item_id}", headers=auth["headers"])
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["raw_url"].startswith("https://storage.test/")
    # Nothing processed yet, so no derivative URL.
    assert body["processed_url"] is None


async def test_saving_annotations_marks_item_reviewed(
    client: AsyncClient, auth: dict[str, Any], dataset: dict[str, Any]
):
    upload = await client.post(
        items_url(dataset["id"]),
        files=[("files", ("sign.png", PNG_1X1, "image/png"))],
        headers=auth["headers"],
    )
    item_id = upload.json()[0]["id"]

    boxes = {
        "boxes": [
            {"label": "stop-sign", "x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4}
        ]
    }
    resp = await client.patch(
        f"/api/v1/items/{item_id}/annotations",
        json={"annotations": boxes, "mark_reviewed": True},
        headers=auth["headers"],
    )
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["status"] == "reviewed"
    assert body["annotations"] == boxes

    # And it survives a round trip through the database.
    again = await client.get(f"/api/v1/items/{item_id}", headers=auth["headers"])
    assert again.json()["annotations"] == boxes


async def test_saving_annotations_without_review_keeps_status(
    client: AsyncClient, auth: dict[str, Any], dataset: dict[str, Any]
):
    upload = await client.post(
        items_url(dataset["id"]),
        files=[("files", ("sign.png", PNG_1X1, "image/png"))],
        headers=auth["headers"],
    )
    item_id = upload.json()[0]["id"]

    resp = await client.patch(
        f"/api/v1/items/{item_id}/annotations",
        json={"annotations": {"notes": "come back to this"}, "mark_reviewed": False},
        headers=auth["headers"],
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"


async def test_annotating_other_users_item_is_404(
    client: AsyncClient,
    auth: dict[str, Any],
    other_auth: dict[str, Any],
    dataset: dict[str, Any],
):
    upload = await client.post(
        items_url(dataset["id"]),
        files=[("files", ("sign.png", PNG_1X1, "image/png"))],
        headers=auth["headers"],
    )
    item_id = upload.json()[0]["id"]

    resp = await client.patch(
        f"/api/v1/items/{item_id}/annotations",
        json={"annotations": {}},
        headers=other_auth["headers"],
    )
    assert resp.status_code == 404


async def test_reprocess_resets_status_and_requeues(
    client: AsyncClient,
    auth: dict[str, Any],
    dataset: dict[str, Any],
    celery_delay: RecordingDelay,
):
    upload = await client.post(
        items_url(dataset["id"]),
        files=[("files", ("sign.png", PNG_1X1, "image/png"))],
        headers=auth["headers"],
    )
    item_id = upload.json()[0]["id"]

    # Mark it reviewed, then re-drive it.
    await client.patch(
        f"/api/v1/items/{item_id}/annotations",
        json={"annotations": {"boxes": []}, "mark_reviewed": True},
        headers=auth["headers"],
    )

    resp = await client.post(
        f"/api/v1/items/{item_id}/reprocess", headers=auth["headers"]
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "pending"
    assert resp.json()["error"] is None

    # Enqueued twice in total: once on upload, once on reprocess.
    assert celery_delay.calls == [item_id, item_id]


# --------------------------------------------------------------------------
# Video telemetry reaches the dashboard through the normal item endpoint
# --------------------------------------------------------------------------


async def _upload_clip_with_telemetry(
    client: AsyncClient,
    auth: dict[str, Any],
    dataset: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """Upload a clip and stand in for the worker's write-back."""
    upload = await client.post(
        items_url(dataset["id"]),
        files=[("files", ("match.mp4", b"\x00\x00\x00\x18ftypmp42", "video/mp4"))],
        headers=auth["headers"],
    )
    item_id = upload.json()[0]["id"]

    async with session_factory() as session:
        item = await session.get(DataItem, item_id)
        item.status = ItemStatus.PROCESSED
        item.processed_s3_key = f"processed/{dataset['id']}/match.jpg"
        item.item_metadata = {
            "width": 1280,
            "height": 720,
            "fps": 25.0,
            "duration_sec": 4.0,
            "telemetry": {
                "source": SOURCE_SIMULATED,
                "sport": "football",
                "frames": [
                    {"frame_index": 0, "timestamp_sec": 0.0, "tracked_entities": []}
                ],
                "tactical_summary": "simulated",
            },
        }
        await session.commit()

    return item_id


async def test_video_telemetry_is_readable_through_the_item_endpoint(
    client: AsyncClient,
    auth: dict[str, Any],
    dataset: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
):
    item_id = await _upload_clip_with_telemetry(client, auth, dataset, session_factory)

    resp = await client.get(f"/api/v1/items/{item_id}", headers=auth["headers"])
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["media_type"] == "video"
    telemetry = body["item_metadata"]["telemetry"]
    # The provenance flag must survive the round trip — the dashboard badges
    # off it, so losing it would silently present simulation as measurement.
    assert telemetry["source"] == SOURCE_SIMULATED
    assert telemetry["sport"] == "football"
    # The poster frame is served through the same presigned-URL path as images.
    assert body["processed_url"].startswith("https://storage.test/")


async def test_video_telemetry_is_not_readable_by_another_user(
    client: AsyncClient,
    auth: dict[str, Any],
    other_auth: dict[str, Any],
    dataset: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
):
    item_id = await _upload_clip_with_telemetry(client, auth, dataset, session_factory)

    resp = await client.get(f"/api/v1/items/{item_id}", headers=other_auth["headers"])
    assert resp.status_code == 404
