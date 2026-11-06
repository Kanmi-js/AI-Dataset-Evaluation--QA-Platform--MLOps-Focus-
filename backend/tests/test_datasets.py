"""Dataset CRUD, item counts, and cross-user isolation."""

from typing import Any

from httpx import AsyncClient

DATASETS = "/api/v1/datasets"


async def test_create_dataset(client: AsyncClient, auth: dict[str, Any]):
    resp = await client.post(
        DATASETS,
        json={"name": "Street signs", "description": "Batch 1"},
        headers=auth["headers"],
    )
    assert resp.status_code == 201, resp.text

    body = resp.json()
    assert body["name"] == "Street signs"
    assert body["description"] == "Batch 1"
    assert body["status"] == "active"
    assert body["owner_id"] == auth["user"]["id"]
    assert body["item_count"] == 0


async def test_create_dataset_rejects_blank_name(client: AsyncClient, auth: dict[str, Any]):
    resp = await client.post(DATASETS, json={"name": ""}, headers=auth["headers"])
    assert resp.status_code == 422


async def test_create_dataset_requires_auth(client: AsyncClient):
    resp = await client.post(DATASETS, json={"name": "Anonymous"})
    assert resp.status_code == 401


async def test_list_datasets_only_returns_own(
    client: AsyncClient, auth: dict[str, Any], other_auth: dict[str, Any]
):
    await client.post(DATASETS, json={"name": "Mine"}, headers=auth["headers"])
    await client.post(DATASETS, json={"name": "Theirs"}, headers=other_auth["headers"])

    resp = await client.get(DATASETS, headers=auth["headers"])
    assert resp.status_code == 200

    names = [d["name"] for d in resp.json()]
    assert names == ["Mine"]


async def test_list_datasets_reports_item_count(
    client: AsyncClient, auth: dict[str, Any], dataset: dict[str, Any]
):
    """item_count comes from a grouped COUNT, so verify it against real rows."""
    upload = await client.post(
        f"{DATASETS}/{dataset['id']}/items",
        files=[
            ("files", ("a.txt", b"alpha", "text/plain")),
            ("files", ("b.txt", b"beta", "text/plain")),
        ],
        headers=auth["headers"],
    )
    assert upload.status_code == 201, upload.text

    resp = await client.get(DATASETS, headers=auth["headers"])
    assert resp.status_code == 200

    row = next(d for d in resp.json() if d["id"] == dataset["id"])
    assert row["item_count"] == 2


async def test_get_dataset_includes_items(
    client: AsyncClient, auth: dict[str, Any], dataset: dict[str, Any]
):
    await client.post(
        f"{DATASETS}/{dataset['id']}/items",
        files=[("files", ("notes.txt", b"hello", "text/plain"))],
        headers=auth["headers"],
    )

    resp = await client.get(f"{DATASETS}/{dataset['id']}", headers=auth["headers"])
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["item_count"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["filename"] == "notes.txt"


async def test_get_other_users_dataset_is_404_not_403(
    client: AsyncClient, other_auth: dict[str, Any], dataset: dict[str, Any]
):
    """404 rather than 403 — a stranger should not learn the id exists."""
    resp = await client.get(
        f"{DATASETS}/{dataset['id']}", headers=other_auth["headers"]
    )
    assert resp.status_code == 404


async def test_get_missing_dataset_is_404(client: AsyncClient, auth: dict[str, Any]):
    resp = await client.get(f"{DATASETS}/999999", headers=auth["headers"])
    assert resp.status_code == 404


async def test_archive_dataset(
    client: AsyncClient, auth: dict[str, Any], dataset: dict[str, Any]
):
    resp = await client.post(
        f"{DATASETS}/{dataset['id']}/archive", headers=auth["headers"]
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "archived"


async def test_delete_dataset_cascades_to_items(
    client: AsyncClient, auth: dict[str, Any], dataset: dict[str, Any]
):
    upload = await client.post(
        f"{DATASETS}/{dataset['id']}/items",
        files=[("files", ("notes.txt", b"hello", "text/plain"))],
        headers=auth["headers"],
    )
    item_id = upload.json()[0]["id"]

    resp = await client.delete(f"{DATASETS}/{dataset['id']}", headers=auth["headers"])
    assert resp.status_code == 204

    assert (
        await client.get(f"{DATASETS}/{dataset['id']}", headers=auth["headers"])
    ).status_code == 404
    assert (
        await client.get(f"/api/v1/items/{item_id}", headers=auth["headers"])
    ).status_code == 404


async def test_delete_other_users_dataset_is_404(
    client: AsyncClient, other_auth: dict[str, Any], dataset: dict[str, Any]
):
    resp = await client.delete(
        f"{DATASETS}/{dataset['id']}", headers=other_auth["headers"]
    )
    assert resp.status_code == 404
