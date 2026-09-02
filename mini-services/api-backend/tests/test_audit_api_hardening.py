"""Audit hardening tests (API layer security fixes).

Covers the verified findings from the security audit session:

1.  WS auth: /ws/executions/{id} requires a valid ?token= JWT in enforced
    mode (4401 close otherwise), legacy mode stays anonymous-friendly, and
    authed callers cannot watch other users' executions (4404 close).
2.  Webhook ingest: body size cap (413) + sensitive-header redaction in the
    persisted trigger envelope.
3.  Rate limiting: 429 + Retry-After after the bucket limit, reset_all()
    clears state, kill switch honours settings.rate_limit_enabled=False.
4.  Cancel/resume ownership: another user's execution looks nonexistent.
5.  App record mutation ownership (published-app runtime PATCH/DELETE).
6.  Registry fetch: http/https-only URLs, streaming byte cap (502), clean
    502 on unreachable hosts.
7.  Keys hygiene: full key only in the creation response, revoke scoped to
    the owner, read-only keys blocked from mutating surfaces.

Same harness as v4-v44: httpx ASGITransport in-process, per-test asyncio.run,
uuid-suffixed data, finally-cleanup + background drain. WS cases use
starlette's TestClient (the only client that can do the WS handshake).
Rate-limit tests monkeypatch the module knobs and MUST reset_all() around
themselves so other tests in the shared process are never throttled.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime

import httpx
import pytest
from sqlalchemy import delete, update
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api import _ratelimit, registries as registries_mod
from app.config import settings
from app.db import AsyncSessionLocal
from app.main import app
from app.models import App, Credential, Dashboard, Dataset, EnvVariable, ExecutionLog, Folder, User, Workflow
from app.services import executor as executor_mod

API = "http://testserver/api/v1"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


def _ws_client() -> TestClient:
    return TestClient(app)  # no context manager -> lifespan (scheduler) never runs


async def _drain_background() -> None:
    tasks = [t for t in executor_mod._background_tasks if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


async def _wipe_users_and_ownership() -> None:
    """Reset auth state so the tests are repeatable (mirrors v37)."""
    async with AsyncSessionLocal() as session:
        await session.execute(delete(User))
        for model in (Workflow, Dataset, Folder, Credential, EnvVariable, App, Dashboard):
            await session.execute(update(model).values(owner_id=None))
        await session.commit()


async def _register(client: httpx.AsyncClient, email: str, password: str = "s3cret-pw!") -> dict:
    res = await client.post("/auth/register", json={"email": email, "password": password, "name": "Audit Test"})
    assert res.status_code == 201, res.text
    return res.json()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _node(nid: str, ntype: str, params: dict | None = None) -> dict:
    return {"id": nid, "type": ntype, "name": nid, "position": {"x": 0, "y": 0}, "parameters": params or {}}


async def _make_workflow(client: httpx.AsyncClient, name: str, graph: dict, token: str | None = None) -> str:
    res = await client.post("/workflows", json={"name": name, "graph": graph}, headers=_auth(token) if token else {})
    assert res.status_code == 201, res.text
    return res.json()["id"]


async def _run_and_wait(client: httpx.AsyncClient, workflow_id: str, token: str | None = None) -> dict:
    res = await client.post(
        f"/workflows/{workflow_id}/run", json={"payload": {}}, headers=_auth(token) if token else {}
    )
    assert res.status_code in (200, 202), res.text
    exec_id = res.json()["execution_id"]
    for _ in range(100):
        res = await client.get(f"/executions/{exec_id}", headers=_auth(token) if token else {})
        assert res.status_code == 200, res.text
        if res.json()["status"] != "running":
            return res.json()
        await asyncio.sleep(0.05)
    raise AssertionError("execution did not finish in time")


async def _webhook_workflow(client: httpx.AsyncClient, name: str) -> str:
    graph = {
        "nodes": [
            _node("h", "webhook_trigger", {"response_mode": "immediately"}),
            _node("s", "set_variable", {"assignments": {"ok": "1"}, "keep_input": False}),
        ],
        "edges": [{"id": "e1", "source": "h", "target": "s", "sourceHandle": "main", "targetHandle": "main"}],
    }
    wf_id = await _make_workflow(client, name, graph)
    res = await client.post(f"/workflows/{wf_id}/activate")
    assert res.status_code == 200, res.text
    return wf_id


# =========================================================== 1) WS auth
def test_audit_ws_enforced_mode_token_gate():
    """Enforced mode: no/bad token -> 4401; valid token on own run -> frames."""

    async def _go():
        tag = _suffix()
        async with _client() as client:
            reg = await _register(client, f"audit-ws-{tag}@py8n.test")
            token = reg["token"]
            graph = {"nodes": [_node("m", "manual_trigger", {"payload": {"go": 1}})], "edges": []}
            wf_id = await _make_workflow(client, f"audit ws run {tag}", graph)
            try:
                finished = await _run_and_wait(client, wf_id)
                assert finished["status"] == "success", finished
            finally:
                await _drain_background()

            settings.require_auth = True
            try:
                ws = _ws_client()
                try:
                    # no token -> rejected before accept
                    with pytest.raises(WebSocketDisconnect) as exc_info:
                        with ws.websocket_connect(f"/ws/executions/{finished['id']}"):
                            pass
                    assert exc_info.value.code == 4401
                    # garbage token -> rejected
                    with pytest.raises(WebSocketDisconnect) as exc_info:
                        with ws.websocket_connect(f"/ws/executions/{finished['id']}?token=not.a.jwt"):
                            pass
                    assert exc_info.value.code == 4401
                    # valid token -> history + terminal frames
                    with ws.websocket_connect(f"/ws/executions/{finished['id']}?token={token}") as sock:
                        first = sock.receive_json()
                        assert first["event"] == "history", first
                        last = sock.receive_json()
                        assert last["event"] == "execution_finished" and last["status"] == "success", last
                finally:
                    ws.close()
            finally:
                settings.require_auth = False

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())
        asyncio.run(_wipe_users_and_ownership())


def test_audit_ws_foreign_execution_and_legacy_anonymous():
    """Authed watcher of another user's execution -> 4404; legacy anon still OK."""

    async def _go():
        tag = _suffix()
        async with _client() as client:
            a = await _register(client, f"audit-ws-a-{tag}@py8n.test")
            b = await _register(client, f"audit-ws-b-{tag}@py8n.test")
            graph = {"nodes": [_node("m", "manual_trigger", {"payload": {"go": 1}})], "edges": []}
            wf_id = await _make_workflow(client, f"audit ws foreign {tag}", graph, token=a["token"])
            try:
                finished = await _run_and_wait(client, wf_id, token=a["token"])
            finally:
                await _drain_background()

            ws = _ws_client()
            try:
                # B (authed, foreign) -> closed as nonexistent
                with pytest.raises(WebSocketDisconnect) as exc_info:
                    with ws.websocket_connect(
                        f"/ws/executions/{finished['id']}?token={b['token']}"
                    ):
                        pass
                assert exc_info.value.code == 4404
                # A sees the frames
                with ws.websocket_connect(
                    f"/ws/executions/{finished['id']}?token={a['token']}"
                ) as sock:
                    assert sock.receive_json()["event"] == "history"
                    assert sock.receive_json()["event"] == "execution_finished"
                # legacy anonymous (no token) still replays the run
                with ws.websocket_connect(f"/ws/executions/{finished['id']}") as sock:
                    assert sock.receive_json()["event"] == "history"
            finally:
                ws.close()
            await client.delete(f"/workflows/{wf_id}", headers=_auth(a["token"]))

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())
        asyncio.run(_wipe_users_and_ownership())


# ================================================ 2) webhook cap + redaction
def test_audit_webhook_body_cap_413(monkeypatch):
    """Bodies above max_webhook_body_bytes are rejected with 413."""

    async def _go():
        tag = _suffix()
        async with _client() as client:
            wf_id = await _webhook_workflow(client, f"audit hook cap {tag}")
            try:
                res = await client.post(f"/webhooks/{wf_id}", json={"big": "x" * 512})
                assert res.status_code == 413, res.text
                assert "too large" in res.json()["detail"].lower()
                # a body under the cap still goes through (202, dispatched)
                res = await client.post(f"/webhooks/{wf_id}", json={"ping": 1})
                assert res.status_code == 202, res.text
            finally:
                await _drain_background()
                await client.delete(f"/workflows/{wf_id}")

    monkeypatch.setattr(settings, "max_webhook_body_bytes", 256)
    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


def test_audit_webhook_headers_redacted(monkeypatch):
    """Credentials never reach the persisted execution envelope."""

    async def _go():
        tag = _suffix()
        async with _client() as client:
            wf_id = await _webhook_workflow(client, f"audit hook redact {tag}")
            try:
                res = await client.post(
                    f"/webhooks/{wf_id}",
                    json={"ping": 1},
                    headers={
                        "Authorization": "Bearer topsecret",
                        "X-API-Key": "py8n_super_secret",
                        "X-Custom-Token": "leak-me",
                        "X-Benign": "keep-me",
                    },
                )
                assert res.status_code == 202, res.text
                await _drain_background()

                listing = await client.get("/executions", params={"workflow_id": wf_id, "limit": 5})
                assert listing.status_code == 200, listing.text
                exec_ids = [r["id"] for r in listing.json()]
                assert exec_ids, "webhook run was not recorded"
                detail = (await client.get(f"/executions/{exec_ids[0]}")).json()
                headers = detail["trigger_payload"]["headers"]
                assert headers["authorization"] == "[REDACTED]", headers
                assert headers["x-api-key"] == "[REDACTED]", headers
                assert headers["x-custom-token"] == "[REDACTED]", headers  # substring rule
                assert headers["x-benign"] == "keep-me", headers
                assert headers["content-type"] == "application/json", headers
                assert "topsecret" not in json.dumps(detail["trigger_payload"])
                assert "py8n_super_secret" not in json.dumps(detail["trigger_payload"])
                assert detail["trigger_payload"]["body"] == {"ping": 1}
            finally:
                await _drain_background()
                await client.delete(f"/workflows/{wf_id}")

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


# ======================================================== 3) rate limiting
def test_audit_rate_limiter_429_and_reset(monkeypatch):
    """429 + Retry-After past the bucket limit; reset_all clears; kill switch."""

    async def _go():
        async with _client() as client:
            # -- auth bucket: 3 hits allowed, 4th throttled
            for _ in range(3):
                res = await client.post("/auth/login", json={"email": "nobody@py8n.test", "password": "wrong-pw!"})
                assert res.status_code == 401, res.text
            res = await client.post("/auth/login", json={"email": "nobody@py8n.test", "password": "wrong-pw!"})
            assert res.status_code == 429, res.text
            assert res.headers.get("retry-after", "").isdigit(), dict(res.headers)
            # window reset clears it
            _ratelimit.reset_all()
            res = await client.post("/auth/login", json={"email": "nobody@py8n.test", "password": "wrong-pw!"})
            assert res.status_code == 401, res.text

            # -- webhook bucket counts too (limiter runs before 404 validation)
            for _ in range(2):
                assert (await client.post("/webhooks/does-not-exist", json={})).status_code == 404
            res = await client.post("/webhooks/does-not-exist", json={})
            assert res.status_code == 429, res.text

            # -- kill switch: settings.rate_limit_enabled=False is a no-op
            settings.rate_limit_enabled = False
            try:
                for _ in range(5):
                    res = await client.post("/auth/login", json={"email": "nobody@py8n.test", "password": "wrong-pw!"})
                    assert res.status_code == 401, res.text
            finally:
                settings.rate_limit_enabled = True

    monkeypatch.setattr(_ratelimit, "OVERRIDES", {"auth": (3, 60), "webhook": (2, 60)})
    _ratelimit.reset_all()
    try:
        asyncio.run(_go())
    finally:
        _ratelimit.reset_all()


# ============================================ 4) cancel/resume ownership
def test_audit_cancel_resume_cross_owner():
    """Another user's execution looks nonexistent on cancel AND resume."""

    async def _go():
        tag = _suffix()
        async with _client() as client:
            a = await _register(client, f"audit-exec-a-{tag}@py8n.test")
            b = await _register(client, f"audit-exec-b-{tag}@py8n.test")
            graph = {"nodes": [_node("m", "manual_trigger", {"payload": {"go": 1}})], "edges": []}
            wf_id = await _make_workflow(client, f"audit exec owner {tag}", graph, token=a["token"])
            try:
                finished = await _run_and_wait(client, wf_id, token=a["token"])
                await _drain_background()

                # foreign cancel -> 404 even though the execution exists
                res = await client.post(f"/executions/{finished['id']}/cancel", headers=_auth(b["token"]))
                assert res.status_code == 404, res.text
                # owner gets past the gate (409 = finished, nothing to cancel)
                res = await client.post(f"/executions/{finished['id']}/cancel", headers=_auth(a["token"]))
                assert res.status_code == 409, res.text

                # synthetic waiting execution owned by A, resumable with tok-xyz
                exec_id = uuid.uuid4().hex
                async with AsyncSessionLocal() as session:
                    session.add(
                        ExecutionLog(
                            id=exec_id,
                            workflow_id=wf_id,
                            status="waiting",
                            trigger_type="manual",
                            trigger_payload={},
                            started_at=datetime.utcnow(),
                            node_runs=[],
                            context_snapshot={"py8n_resume": {"token": "tok-xyz", "node_id": "m"}},
                        )
                    )
                    await session.commit()

                # B holds the CORRECT resume token but is not the owner -> 404
                res = await client.post(
                    f"/executions/{exec_id}/resume",
                    json={"token": "tok-xyz", "payload": {"answer": 1}},
                    headers=_auth(b["token"]),
                )
                assert res.status_code == 404, res.text
                # owner resumes -> past the gate (202, continuation runs)
                res = await client.post(
                    f"/executions/{exec_id}/resume",
                    json={"token": "tok-xyz", "payload": {"answer": 1}},
                    headers=_auth(a["token"]),
                )
                assert res.status_code == 202, res.text
                await _drain_background()

                # enforced mode: anonymous cancel/resume -> 401 at the router gate
                settings.require_auth = True
                try:
                    res = await client.post("/executions/nonexistent/cancel")
                    assert res.status_code == 401, res.text
                    res = await client.post("/executions/nonexistent/resume", json={"token": "x"})
                    assert res.status_code == 401, res.text
                finally:
                    settings.require_auth = False
            finally:
                await client.delete(f"/workflows/{wf_id}", headers=_auth(a["token"]))

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())
        asyncio.run(_wipe_users_and_ownership())


# ============================================ 5) app record mutation gate
def test_audit_app_record_mutation_ownership():
    """Published-app record PATCH/DELETE are owner-gated; list stays public."""

    async def _go():
        tag = _suffix()
        async with _client() as client:
            a = await _register(client, f"audit-app-a-{tag}@py8n.test")
            b = await _register(client, f"audit-app-b-{tag}@py8n.test")

            ds = await client.post("/datasets", json={"name": f"audit app ds {tag}", "rows": [{"n": 1}]})
            assert ds.status_code == 201, ds.text
            ds_id = ds.json()["id"]
            app_res = await client.post(
                "/apps",
                json={"name": f"audit app {tag}", "dataset_id": ds_id},
                headers=_auth(a["token"]),
            )
            assert app_res.status_code == 201, app_res.text
            app_id = app_res.json()["id"]
            slug = app_res.json()["slug"]
            try:
                pub = await client.post(f"/apps/{app_id}/publish", headers=_auth(a["token"]))
                assert pub.status_code == 200, pub.text
                rec = await client.post(f"/apps/{slug}/records", json={"record": {"n": 7}})
                assert rec.status_code == 201, rec.text

                # anonymous edit/delete in ENFORCED mode -> 401
                settings.require_auth = True
                try:
                    res = await client.patch(f"/apps/{slug}/records/0", json={"record": {"n": 9}})
                    assert res.status_code == 401, res.text
                    res = await client.delete(f"/apps/{slug}/records/0")
                    assert res.status_code == 401, res.text
                finally:
                    settings.require_auth = False

                # authed NON-owner -> 404 (looks nonexistent)
                res = await client.patch(
                    f"/apps/{slug}/records/0", json={"record": {"n": 9}}, headers=_auth(b["token"])
                )
                assert res.status_code == 404, res.text
                res = await client.delete(f"/apps/{slug}/records/0", headers=_auth(b["token"]))
                assert res.status_code == 404, res.text

                # owner mutates fine; anonymous listing (embedded runtime) stays open
                res = await client.patch(
                    f"/apps/{slug}/records/0", json={"record": {"n": 9}}, headers=_auth(a["token"])
                )
                assert res.status_code == 200, res.text
                assert (await client.get(f"/apps/{slug}/records")).status_code == 200
            finally:
                await client.delete(f"/apps/{app_id}", headers=_auth(a["token"]))
                await client.delete(f"/datasets/{ds_id}", headers=_auth(a["token"]))

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())
        asyncio.run(_wipe_users_and_ownership())


# ================================================ 6) registry fetch guard
def test_audit_registry_url_scheme_rejected():
    """file://, ftp:// etc. are refused at creation time."""

    async def _go():
        tag = _suffix()
        async with _client() as client:
            for url in ("file:///etc/passwd", "ftp://host/pack.json", "gopher://host/x", "javascript:alert(1)"):
                res = await client.post("/registries", json={"name": f"audit reg {tag}", "url": url})
                assert res.status_code == 400, (url, res.text)

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


def test_audit_registry_streaming_cap(monkeypatch):
    """Fetch is STREAMED and hard-capped (502 over max_registry_fetch_bytes)."""

    async def _go():
        tag = _suffix()
        big_pack = json.dumps(
            {"format": "py8n-pack", "pack_version": 1, "workflows": [], "datasets": [], "pad": "z" * 500}
        ).encode()

        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=big_pack, headers={"content-type": "application/json"})

        def _mock_client() -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(_handler), timeout=5.0)

        async with _client() as client:
            res = await client.post("/registries", json={"name": f"audit reg cap {tag}", "url": "https://mock.test/pack"})
            assert res.status_code == 201, res.text
            reg_id = res.json()["id"]
            try:
                # oversized (decoded) body aborted mid-stream -> clean 502
                monkeypatch.setattr(settings, "max_registry_fetch_bytes", 16)
                monkeypatch.setattr(registries_mod, "_make_client", _mock_client)
                res = await client.post(f"/registries/{reg_id}/check")
                assert res.status_code == 502, res.text
                assert "cap" in res.json()["detail"].lower()

                # same pack, sane cap -> fetch + parse succeed (200)
                monkeypatch.setattr(settings, "max_registry_fetch_bytes", 64_000_000)
                res = await client.post(f"/registries/{reg_id}/check")
                assert res.status_code == 200, res.text
                body = res.json()
                assert body["format"] == "py8n-pack" and body["workflow_count"] == 0, body
            finally:
                await client.delete(f"/registries/{reg_id}")

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


def test_audit_registry_unreachable_host_502():
    """Transport failures surface as a clean 502 (no stack-trace leak)."""

    async def _go():
        tag = _suffix()
        async with _client() as client:
            res = await client.post(
                "/registries", json={"name": f"audit reg dead {tag}", "url": "http://127.0.0.1:9/pack"}
            )
            assert res.status_code == 201, res.text
            dead_id = res.json()["id"]
            try:
                res = await client.post(f"/registries/{dead_id}/check")
                assert res.status_code == 502, res.text
                assert "reach" in res.json()["detail"].lower()
            finally:
                await client.delete(f"/registries/{dead_id}")

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


# ==================================================== 7) keys hygiene
def test_audit_keys_masking_scopes_and_revoke():
    """Full key only at creation; revoke owner-scoped; read-only key gated."""

    async def _go():
        tag = _suffix()
        async with _client() as client:
            a = await _register(client, f"audit-key-a-{tag}@py8n.test")
            b = await _register(client, f"audit-key-b-{tag}@py8n.test")

            created = await client.post(
                "/keys", json={"name": "ci read-only", "scopes": ["read"]}, headers=_auth(a["token"])
            )
            assert created.status_code == 201, created.text
            body = created.json()
            full_key = body["key"]
            assert full_key.startswith("py8n_")

            # list output is masked: no full key, no hash
            listing = (await client.get("/keys", headers=_auth(a["token"]))).json()
            assert len(listing) == 1 and listing[0]["id"] == body["id"]
            assert "key" not in listing[0] and "key_hash" not in listing[0]
            assert full_key not in json.dumps(listing)
            assert listing[0]["read_only"] is True

            key_hdr = {"X-API-Key": full_key}
            # read-only key reads fine...
            res = await client.get("/workflows", headers=key_hdr)
            assert res.status_code == 200, res.text
            # ...but cannot mutate admin surfaces (403 scope gate)
            res = await client.post("/packs/export", json={"workflow_ids": []}, headers=key_hdr)
            assert res.status_code == 403, res.text

            # a WRITE key passes the scope gate (400 = validation, not 403)
            write_key = (await client.post("/keys", json={"name": "ci write"}, headers=_auth(a["token"]))).json()
            res = await client.post(
                "/packs/export", json={"workflow_ids": []}, headers={"X-API-Key": write_key["key"]}
            )
            assert res.status_code == 400, res.text

            # revoke is owner-scoped: B cannot revoke A's key (404), A can
            res = await client.delete(f"/keys/{body['id']}", headers=_auth(b["token"]))
            assert res.status_code == 404, res.text
            res = await client.delete(f"/keys/{body['id']}", headers=_auth(a["token"]))
            assert res.status_code == 204, res.text
            listing = (await client.get("/keys", headers=_auth(a["token"]))).json()
            mine = next(k for k in listing if k["id"] == body["id"])
            assert mine["revoked"] is True
            # revoked key no longer authenticates as anyone
            res = await client.get("/auth/me", headers=key_hdr)
            assert res.status_code == 401, res.text

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())
        asyncio.run(_wipe_users_and_ownership())
