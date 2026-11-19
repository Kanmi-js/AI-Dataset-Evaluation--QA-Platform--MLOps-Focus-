"""The breakdown endpoint and the breakdown URL on item detail."""

from typing import Any

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import DataItem
from tests.conftest import RecordingDelay


async def _upload_clip(client: AsyncClient, auth: dict[str, Any], dataset: dict[str, Any]) -> int:
    resp = await client.post(
        f"/api/v1/datasets/{dataset['id']}/items",
        files=[("files", ("match.mp4", b"\x00\x00\x00\x18ftypmp42", "video/mp4"))],
        headers=auth["headers"],
    )
    assert resp.status_code == 201, resp.text
    return resp.json()[0]["id"]


async def _set_metadata(
    session_factory: async_sessionmaker[AsyncSession], item_id: int, metadata: dict[str, Any]
) -> None:
    async with session_factory() as session:
        item = await session.get(DataItem, item_id)
        assert item is not None
        item.item_metadata = metadata
        await session.commit()


async def test_breakdown_needs_tracking_data(
    client: AsyncClient,
    auth: dict[str, Any],
    dataset: dict[str, Any],
    breakdown_delay: RecordingDelay,
):
    item_id = await _upload_clip(client, auth, dataset)
    resp = await client.post(f"/api/v1/items/{item_id}/breakdown", headers=auth["headers"])
    assert resp.status_code == 409
    assert breakdown_delay.calls == []


async def test_breakdown_queues_and_reports_state(
    client: AsyncClient,
    auth: dict[str, Any],
    dataset: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
    breakdown_delay: RecordingDelay,
):
    item_id = await _upload_clip(client, auth, dataset)
    await _set_metadata(session_factory, item_id, {"tracks_key": "processed/1/x.tracks.json"})

    resp = await client.post(f"/api/v1/items/{item_id}/breakdown", headers=auth["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["item_metadata"]["breakdown"] == {"status": "queued"}
    assert breakdown_delay.calls == [item_id]

    # A second request while one is in flight is refused, not double-queued.
    again = await client.post(f"/api/v1/items/{item_id}/breakdown", headers=auth["headers"])
    assert again.status_code == 409
    assert breakdown_delay.calls == [item_id]


async def test_breakdown_url_only_when_ready(
    client: AsyncClient,
    auth: dict[str, Any],
    dataset: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
):
    item_id = await _upload_clip(client, auth, dataset)
    key = "processed/1/x.breakdown.mp4"

    running = {"breakdown": {"status": "running", "video_key": key}}
    await _set_metadata(session_factory, item_id, running)
    detail = (await client.get(f"/api/v1/items/{item_id}", headers=auth["headers"])).json()
    assert detail["breakdown_url"] is None

    ready = {"breakdown": {"status": "ready", "video_key": key}}
    await _set_metadata(session_factory, item_id, ready)
    detail = (await client.get(f"/api/v1/items/{item_id}", headers=auth["headers"])).json()
    assert detail["breakdown_url"] and key in detail["breakdown_url"]


async def test_breakdown_is_owner_scoped(
    client: AsyncClient,
    auth: dict[str, Any],
    other_auth: dict[str, Any],
    dataset: dict[str, Any],
):
    item_id = await _upload_clip(client, auth, dataset)
    resp = await client.post(f"/api/v1/items/{item_id}/breakdown", headers=other_auth["headers"])
    assert resp.status_code == 404
