"""V41 feature tests: API keys for machine access.

New machinery:
    api_keys table        name + display prefix + sha256 hash (the full
                          ``py8n_`` key exists only in the creation response)
    X-API-Key channel     get_optional_user now resolves machine keys AS THEIR
                          OWNER - same scoping as the owner's JWT, so scripts
                          and CI hit every surface even in enforced mode
    /keys CRUD            list (masked), create (full key shown once),
                          delete = revoke (stamps revoked_at; rows stay);
                          foreign keys 404 (v37 own_or_404), last_used_at is
                          touched with a 60s throttle

Same harness as v4-v40: httpx ASGITransport in-process, per-test asyncio.run,
uuid-suffixed data, finally-cleanup + background drain. Auth tests wipe users
and ownership stamps around themselves (v37 pattern) to stay repeatable
against the live dev DB.
"""

from __future__ import annotations

import asyncio
import uuid

import httpx

from app.main import app

API = "http://testserver/api/v1"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


async def _drain_background() -> None:
    from app.services import executor as executor_mod

    tasks = [t for t in executor_mod._background_tasks if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _cleanup_done() -> None:
    asyncio.run(_wipe_users_and_ownership())
    asyncio.run(_drain_background())


async def _wipe_users_and_ownership() -> None:
    from sqlalchemy import delete, update

    from app.db import AsyncSessionLocal
    from app.models import ApiKey, App, Credential, Dashboard, Dataset, EnvVariable, Folder, User, Workflow

    async with AsyncSessionLocal() as session:
        await session.execute(delete(ApiKey))
        await session.execute(delete(User))
        for model in (Workflow, Dataset, Folder, Credential, EnvVariable, App, Dashboard):
            await session.execute(update(model).values(owner_id=None))
        await session.commit()


async def _register(client: httpx.AsyncClient, email: str, password: str = "s3cret-pw!") -> dict:
    res = await client.post("/auth/register", json={"email": email, "password": password, "name": "V41 Test"})
    assert res.status_code == 201, res.text
    return res.json()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _key_header(full_key: str) -> dict:
    return {"X-API-Key": full_key}


# ------------------------------------------------------------------ test 1
def test_v41_health_pin():
    """Strict version pin lives in the latest wave only (convention)."""

    async def _go():
        async with _client() as client:
            res = await client.get("/health")
            assert res.status_code == 200, res.text
            return res.json()

    body = asyncio.run(_go())
    assert body["app"] == "Py8n"
    assert body["version"] >= "1.41.0", f"expected floor 1.41.0, got {body['version']}"


# ------------------------------------------------------------------ test 2
def test_v41_key_lifecycle():
    """Create -> list (masked) -> use -> revoke -> use fails."""
    tag = uuid.uuid4().hex[:8]
    try:
        async def _go():
            async with _client() as client:
                user = await _register(client, f"v41-lc-{tag}@py8n.test")
                uid = user["user"]["id"]
                headers = _auth(user["token"])

                # anonymous create is refused
                res = await client.post("/keys", json={"name": "nope"})
                assert res.status_code == 401, res.text

                # create: full key shown exactly once, prefix matches
                res = await client.post("/keys", json={"name": "CI pipeline"}, headers=headers)
                assert res.status_code == 201, res.text
                created = res.json()
                assert created["key"].startswith("py8n_") and len(created["key"]) > 20
                assert created["prefix"] == created["key"][:12]
                assert created["name"] == "CI pipeline"
                assert created["revoked"] is False
                # the stored row never echoes the full key back on list
                res = await client.get("/keys", headers=headers)
                assert res.status_code == 200, res.text
                listed = res.json()
                assert len(listed) == 1
                assert "key" not in listed[0]
                assert listed[0]["prefix"] == created["prefix"]

                # the key WORKS as the user: /auth/me resolves to the owner
                res = await client.get("/auth/me", headers=_key_header(created["key"]))
                assert res.status_code == 200, res.text
                me = res.json()
                assert me["email"] == f"v41-lc-{tag}@py8n.test"

                # key-created resources carry the owner stamp
                res = await client.post(
                    "/workflows",
                    json={"name": f"v41 keyed wf {tag}", "graph": {"nodes": [], "edges": []}},
                    headers=_key_header(created["key"]),
                )
                assert res.status_code == 201, res.text
                wf = res.json()
                assert wf.get("owner_id") == uid, wf

                # garbage and unknown keys resolve to anonymous (open mode: pass)
                res = await client.get("/auth/me", headers=_key_header("py8n_totally-fake"))
                assert res.status_code == 401, res.text

                # revoke -> the key stops working
                res = await client.delete(f"/keys/{created['id']}", headers=headers)
                assert res.status_code == 204, res.text
                res = await client.get("/auth/me", headers=_key_header(created["key"]))
                assert res.status_code == 401, res.text
                # revoke is idempotent
                res = await client.delete(f"/keys/{created['id']}", headers=headers)
                assert res.status_code == 204, res.text
                # the row stays, now flagged
                res = await client.get("/keys", headers=headers)
                assert res.json()[0]["revoked"] is True

                await client.delete(f"/workflows/{wf['id']}")
                return created

        created = asyncio.run(_go())
        assert created["prefix"].startswith("py8n_")
    finally:
        _cleanup_done()


# ------------------------------------------------------------------ test 3
def test_v41_key_scoping_between_users():
    """Alice's key acts as Alice (sees Alice's rows); Bob cannot touch her keys."""
    tag = uuid.uuid4().hex[:8]
    try:
        async def _go():
            async with _client() as client:
                alice = await _register(client, f"v41-alice-{tag}@py8n.test")
                bob = await _register(client, f"v41-bob-{tag}@py8n.test")

                res = await client.post("/keys", json={"name": "alice-script"}, headers=_auth(alice["token"]))
                alice_key = res.json()

                # alice's key sees alice's workflow
                res = await client.post(
                    "/workflows",
                    json={"name": f"v41 alice wf {tag}", "graph": {"nodes": [], "edges": []}},
                    headers=_auth(alice["token"]),
                )
                wf = res.json()
                res = await client.get("/workflows", headers=_key_header(alice_key["key"]))
                assert res.status_code == 200
                assert any(w["id"] == wf["id"] for w in res.json())

                # bob cannot revoke alice's key (looks nonexistent)
                res = await client.delete(f"/keys/{alice_key['id']}", headers=_auth(bob["token"]))
                assert res.status_code == 404, res.text
                # and bob's key list does not contain alice's
                res = await client.post("/keys", json={"name": "bob-script"}, headers=_auth(bob["token"]))
                bob_key = res.json()
                res = await client.get("/keys", headers=_auth(bob["token"]))
                ids = [k["id"] for k in res.json()]
                assert alice_key["id"] not in ids and bob_key["id"] in ids

                await client.delete(f"/workflows/{wf['id']}")
                return True

        assert asyncio.run(_go())
    finally:
        _cleanup_done()


# ------------------------------------------------------------------ test 4
def test_v41_key_works_in_enforced_mode():
    """The whole point: PY8N_REQUIRE_AUTH=true does not lock out machines
    carrying a valid X-API-Key, while anonymous callers still get 401."""
    tag = uuid.uuid4().hex[:8]
    from app.config import settings

    original = settings.require_auth
    try:
        async def _go():
            async with _client() as client:
                user = await _register(client, f"v41-enf-{tag}@py8n.test")
                res = await client.post("/keys", json={"name": "enf"}, headers=_auth(user["token"]))
                key = res.json()

                settings.require_auth = True
                try:
                    # anonymous build surface -> 401
                    res = await client.get("/workflows")
                    assert res.status_code == 401, res.text
                    # the same surface through the key -> 200
                    res = await client.get("/workflows", headers=_key_header(key["key"]))
                    assert res.status_code == 200, res.text
                    # key can still create
                    res = await client.post(
                        "/workflows",
                        json={"name": f"v41 enf wf {tag}", "graph": {"nodes": [], "edges": []}},
                        headers=_key_header(key["key"]),
                    )
                    assert res.status_code == 201, res.text
                    wf_id = res.json()["id"]
                    # public paths stay open (webhook-less probe: health)
                    res = await client.get("/health")
                    assert res.status_code == 200
                    return wf_id, user["token"]
                finally:
                    settings.require_auth = False

        wf_id, _tok = asyncio.run(_go())

        async def _cleanup_wf():
            async with _client() as client:
                await client.delete(f"/workflows/{wf_id}")

        asyncio.run(_cleanup_wf())
    finally:
        settings.require_auth = original
        _cleanup_done()
