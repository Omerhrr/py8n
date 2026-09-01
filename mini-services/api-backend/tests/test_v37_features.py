"""V37 feature tests: authentication + multi-user ownership scoping.

New machinery:
    users table + PBKDF2 password hashing (240k iters, stdlib only)
    hand-rolled HS256 JWTs (secret auto-created at data/.jwt.key)
    POST /auth/register | POST /auth/login | GET /auth/me | GET /auth/status
    owner_id columns on workflows, datasets, folders, credentials,
      env_variables, apps, dashboards (NULL = unclaimed, visible to all)
    scoping: authed callers see unclaimed + their own; other users' rows 404
    enforced mode (PY8N_REQUIRE_AUTH=true): anonymous gets 401 on build/admin
      surfaces; webhooks, chat, published runtimes and artifact content stay
      reachable (auth.is_public_path)
    first registered user becomes admin and claims every unclaimed row

Same harness as v4-v36: httpx ASGITransport in-process, per-test asyncio.run,
uuid-suffixed data, finally-cleanup + background drain. Auth tests reset the
users table and ownership stamps around themselves so they are repeatable
against the live dev database.
"""

from __future__ import annotations

import asyncio
import uuid

import httpx
from sqlalchemy import delete, select, update

from app.config import settings
from app.main import app
from app.models import (
    App,
    Credential,
    Dashboard,
    Dataset,
    EnvVariable,
    Folder,
    User,
    Workflow,
)
from app.services import executor as executor_mod

API = "http://testserver/api/v1"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


async def _drain_background() -> None:
    tasks = [t for t in executor_mod._background_tasks if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


async def _wipe_users_and_ownership() -> None:
    """Reset auth state so the tests are repeatable against the live DB."""
    from app.db import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        await session.execute(delete(User))
        for model in (Workflow, Dataset, Folder, Credential, EnvVariable, App, Dashboard):
            await session.execute(update(model).values(owner_id=None))
        await session.commit()


async def _register(client: httpx.AsyncClient, email: str, password: str = "s3cret-pw!") -> dict:
    res = await client.post("/auth/register", json={"email": email, "password": password, "name": "V37 Test"})
    assert res.status_code == 201, res.text
    return res.json()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------ test 1
def test_v37_health_pin():
    """Strict version pin lives in the latest wave only (convention)."""

    async def _go():
        async with _client() as client:
            res = await client.get("/health")
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["app"] == "Py8n" and body["version"] >= "1.37.0", body
            assert body["require_auth"] is False  # default stays open mode

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


# ------------------------------------------------------------------ test 2
def test_v37_register_login_me_roundtrip():
    """Register, login, me, plus the classic failure modes."""

    async def _go():
        tag = _suffix()
        email = f"v37-roundtrip-{tag}@py8n.test"
        async with _client() as client:
            reg = await _register(client, email)
            assert reg["token"] and reg["user"]["email"] == email
            assert reg["user"]["role"] in ("admin", "member")

            # me: the token resolves back to the same account
            me = await client.get("/auth/me", headers=_auth(reg["token"]))
            assert me.status_code == 200, me.text
            assert me.json()["id"] == reg["user"]["id"]

            # login works and issues a fresh usable token
            login = await client.post(
                "/auth/login", json={"email": email, "password": "s3cret-pw!"}
            )
            assert login.status_code == 200, login.text
            me2 = await client.get("/auth/me", headers=_auth(login.json()["token"]))
            assert me2.status_code == 200 and me2.json()["email"] == email

            # failure modes
            bad = await client.post("/auth/login", json={"email": email, "password": "wrong-password"})
            assert bad.status_code == 401
            no_token = await client.get("/auth/me")
            assert no_token.status_code == 401
            bad_token = await client.get("/auth/me", headers=_auth("not.a.jwt"))
            assert bad_token.status_code == 401
            dup = await client.post("/auth/register", json={"email": email, "password": "another-pw1"})
            assert dup.status_code == 409
            short = await client.post("/auth/register", json={"email": f"x-{tag}@py8n.test", "password": "short"})
            assert short.status_code == 422  # Field(min_length=8) rejects it pre-handler

            # status probe stays anonymous and coherent
            status = await client.get("/auth/status")
            assert status.status_code == 200
            assert status.json()["require_auth"] is False and status.json()["has_users"] is True

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())
        asyncio.run(_wipe_users_and_ownership())


# ------------------------------------------------------------------ test 3
def test_v37_scoping_between_two_users():
    """A's workflow is invisible to B (404) and filtered from B's lists."""

    async def _go():
        tag = _suffix()
        async with _client() as client:
            a = await _register(client, f"v37-alice-{tag}@py8n.test")
            b = await _register(client, f"v37-bob-{tag}@py8n.test")

            created = await client.post(
                "/workflows",
                json={"name": f"v37 scoping secret {tag}", "graph": {"nodes": [], "edges": []}},
                headers=_auth(a["token"]),
            )
            assert created.status_code == 201, created.text
            wf_id = created.json()["id"]

            try:
                # owner sees it
                mine = await client.get("/workflows", headers=_auth(a["token"]))
                assert any(w["id"] == wf_id for w in mine.json())

                # other user: filtered from the list, 404 on direct access
                theirs = await client.get("/workflows", headers=_auth(b["token"]))
                assert not any(w["id"] == wf_id for w in theirs.json())
                direct = await client.get(f"/workflows/{wf_id}", headers=_auth(b["token"]))
                assert direct.status_code == 404

                # anonymous (open mode) still sees everything - legacy behavior
                anon = await client.get("/workflows")
                assert any(w["id"] == wf_id for w in anon.json())

                # B cannot run or delete it either
                run = await client.post(f"/workflows/{wf_id}/run", headers=_auth(b["token"]))
                assert run.status_code == 404
            finally:
                await client.delete(f"/workflows/{wf_id}", headers=_auth(a["token"]))

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())
        asyncio.run(_wipe_users_and_ownership())


# ------------------------------------------------------------------ test 4
def test_v37_enforced_mode_blocks_anonymous():
    """With require_auth on: anonymous 401 on build surfaces, runtimes stay open."""

    async def _go():
        tag = _suffix()
        async with _client() as client:
            reg = await _register(client, f"v37-enforce-{tag}@py8n.test")
            token = reg["token"]
            settings.require_auth = True
            try:
                # build surfaces blocked for anonymous, open with a token
                res = await client.get("/workflows")
                assert res.status_code == 401
                res = await client.get("/datasets")
                assert res.status_code == 401
                res = await client.get("/workflows", headers=_auth(token))
                assert res.status_code == 200

                # health + auth stay public
                assert (await client.get("/health")).status_code == 200
                assert (await client.get("/auth/status")).status_code == 200

                # machine / published-runtime surfaces stay reachable
                res = await client.post("/datasets/query", json={"sql": "SELECT 1 AS one"})
                assert res.status_code == 200, res.text

                # a bad token is still rejected
                res = await client.get("/workflows", headers=_auth("forged.token.here"))
                assert res.status_code == 401
            finally:
                settings.require_auth = False

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())
        asyncio.run(_wipe_users_and_ownership())


# ------------------------------------------------------------------ test 5
def test_v37_first_register_claims_orphans():
    """The first account inherits every unclaimed resource row."""

    async def _go():
        tag = _suffix()
        await _wipe_users_and_ownership()  # deterministic "fresh install" state
        async with _client() as client:
            orphan = await client.post(
                "/workflows",
                json={"name": f"v37 orphan {tag}", "graph": {"nodes": [], "edges": []}},
            )
            assert orphan.status_code == 201
            orphan_id = orphan.json()["id"]

            reg = await _register(client, f"v37-owner-{tag}@py8n.test")
            try:
                assert reg["user"]["role"] == "admin", reg["user"]
                assert reg["claimed"].get("workflows", 0) >= 1, reg["claimed"]

                from app.db import AsyncSessionLocal

                async with AsyncSessionLocal() as session:
                    row = await session.get(Workflow, orphan_id)
                    assert row is not None and row.owner_id == reg["user"]["id"]
            finally:
                await client.delete(f"/workflows/{orphan_id}", headers=_auth(reg["token"]))

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())
        asyncio.run(_wipe_users_and_ownership())
