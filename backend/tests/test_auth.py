"""Authentication flow: register, duplicate detection, login, /me."""

from httpx import AsyncClient

from tests.conftest import register_and_login

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"
ME = "/api/v1/auth/me"


async def test_register_returns_user_without_password(client: AsyncClient):
    resp = await client.post(
        REGISTER,
        json={
            "email": "ada@example.com",
            "password": "correct-horse",
            "full_name": "Ada Lovelace",
        },
    )
    assert resp.status_code == 201, resp.text

    body = resp.json()
    assert body["email"] == "ada@example.com"
    assert body["full_name"] == "Ada Lovelace"
    assert body["is_active"] is True
    assert body["is_superuser"] is False
    # The hash must never leave the server.
    assert "password" not in body
    assert "hashed_password" not in body


async def test_register_rejects_duplicate_email(client: AsyncClient):
    payload = {"email": "dup@example.com", "password": "correct-horse"}
    first = await client.post(REGISTER, json=payload)
    assert first.status_code == 201

    second = await client.post(REGISTER, json=payload)
    assert second.status_code == 409
    assert "already exists" in second.json()["detail"]


async def test_register_rejects_short_password(client: AsyncClient):
    resp = await client.post(
        REGISTER, json={"email": "short@example.com", "password": "abc"}
    )
    assert resp.status_code == 422


async def test_login_returns_bearer_token(client: AsyncClient):
    await client.post(
        REGISTER, json={"email": "grace@example.com", "password": "correct-horse"}
    )

    resp = await client.post(
        LOGIN, data={"username": "grace@example.com", "password": "correct-horse"}
    )
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]


async def test_login_rejects_wrong_password(client: AsyncClient):
    await client.post(
        REGISTER, json={"email": "grace@example.com", "password": "correct-horse"}
    )

    resp = await client.post(
        LOGIN, data={"username": "grace@example.com", "password": "wrong-password"}
    )
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"


async def test_login_rejects_unknown_email(client: AsyncClient):
    resp = await client.post(
        LOGIN, data={"username": "nobody@example.com", "password": "correct-horse"}
    )
    # Same 401 as a bad password — no account enumeration.
    assert resp.status_code == 401


async def test_me_returns_the_token_holder(client: AsyncClient):
    session = await register_and_login(client, "me@example.com")

    resp = await client.get(ME, headers=session["headers"])
    assert resp.status_code == 200
    assert resp.json()["email"] == "me@example.com"


async def test_me_rejects_garbage_token(client: AsyncClient):
    resp = await client.get(ME, headers={"Authorization": "Bearer not-a-jwt"})
    assert resp.status_code == 401


async def test_me_requires_a_token(client: AsyncClient):
    resp = await client.get(ME)
    assert resp.status_code == 401
