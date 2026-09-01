"""V43 feature tests: vault hardening, key scopes, pack registries.

New machinery:
    POST /credentials/{id}/rotate   swap ONLY the provided secret fields, keep
                                    the rest of the config, stamp rotated_at
    GET  /credentials/{id}/events   per-credential audit trail (field names
                                    only, never values); nodes resolving a
                                    credential write "used" rows too
    api_keys.scopes                 ["read","write"] (default) or ["read"];
                                    read-only keys get 403 on unsafe methods
    /registries CRUD + check/sync   point Py8n at a URL serving a py8n-pack;
                                    check = dry-run, sync = import + stamp

Same harness as v4-v42: httpx ASGITransport in-process, per-test asyncio.run,
uuid-suffixed data, finally-cleanup + background drain. Registry fetches use
an injected httpx.MockTransport so tests stay fully offline.
"""

from __future__ import annotations

import asyncio
import json
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


def _node(nid: str, ntype: str, params: dict | None = None, name: str | None = None) -> dict:
    return {"id": nid, "type": ntype, "name": name or nid, "position": {"x": 0, "y": 0}, "parameters": params or {}}


async def _register(client: httpx.AsyncClient, email: str, password: str = "s3cret-pw!") -> dict:
    res = await client.post("/auth/register", json={"email": email, "password": password, "name": "V43 Test"})
    assert res.status_code == 201, res.text
    return res.json()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


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


# ------------------------------------------------------------------ test 1
def test_v43_health_pin():
    """Strict version pin lives in the latest wave only (convention)."""

    async def _go():
        async with _client() as client:
            return (await client.get("/health")).json()

    body = asyncio.run(_go())
    assert body["app"] == "Py8n"
    assert body["version"] >= "1.43.0", f"expected at least 1.43.0, got {body['version']}"


# ------------------------------------------------------------------ test 2
def test_v43_credential_rotation_and_audit():
    """Rotate replaces only the provided fields, stamps rotated_at and every
    lifecycle action lands in the audit trail with field names, never values."""
    tag = uuid.uuid4().hex[:8]
    cred_ids: list[str] = []

    async def _go():
        async with _client() as client:
            # header_auth: header_name is non-secret config, value is the secret
            res = await client.post(
                "/credentials",
                json={"name": f"v43 vault {tag}", "type": "header_auth",
                      "data": {"header_name": "X-Api-Key", "value": "super-secret-abc"}},
            )
            assert res.status_code == 201, res.text
            cred = res.json()
            cred_ids.append(cred["id"])
            assert cred["rotated_at"] is None

            # rotate ONLY the value; header_name must carry over
            rot = await client.post(f"/credentials/{cred['id']}/rotate", json={"secrets": {"value": "rotated-xyz-123"}})
            assert rot.status_code == 200, rot.text
            body = rot.json()
            assert body["rotated_at"] is not None, "rotation must stamp rotated_at"
            assert body["masked_hint"] != cred["masked_hint"], "hint reflects the new secret"

            detail = (await client.get(f"/credentials/{cred['id']}")).json()
            assert detail["data"]["header_name"] == "X-Api-Key", "non-rotated field carries over"
            assert detail["data"]["value"] == "", "secret fields stay blanked"

            # rename via PATCH -> renamed event; payload PATCH -> updated event
            ren = await client.patch(f"/credentials/{cred['id']}", json={"name": f"v43 vault renamed {tag}"})
            assert ren.status_code == 200, ren.text
            upd = await client.patch(
                f"/credentials/{cred['id']}",
                json={"data": {"header_name": "X-Auth", "value": "__keep__"}},
            )
            assert upd.status_code == 200, upd.text

            events = (await client.get(f"/credentials/{cred['id']}/events")).json()
            actions = [e["action"] for e in events]
            assert actions == ["updated", "renamed", "rotated", "created"], f"newest first, got {actions}"
            rotated = next(e for e in events if e["action"] == "rotated")
            assert rotated["detail"]["fields"] == ["value"]
            assert rotated["detail"]["changed"] == ["value"]
            created = next(e for e in events if e["action"] == "created")
            assert created["detail"]["fields"] == ["header_name", "value"]
            renamed = next(e for e in events if e["action"] == "renamed")
            assert renamed["detail"]["to"] == f"v43 vault renamed {tag}"

            # empty rotation is a 400; foreign trails are protected by v37 (own_or_404
            # with anonymous user is a no-op in open mode, so just check the schema)
            empty = await client.post(f"/credentials/{cred['id']}/rotate", json={"secrets": {}})
            assert empty.status_code == 400, empty.text

            # the audit trail must never carry a secret value
            blob = json.dumps(events)
            assert "super-secret-abc" not in blob and "rotated-xyz-123" not in blob

            return body

    try:
        asyncio.run(_go())
    finally:
        async def _cleanup():
            async with _client() as client:
                for cid in cred_ids:
                    await client.delete(f"/credentials/{cid}?force=true")
        asyncio.run(_cleanup())


# ------------------------------------------------------------------ test 3
def test_v43_credential_used_audit():
    """A node resolving a credential writes a "used" row with workflow refs."""
    from types import SimpleNamespace

    from app.services.crypto import decrypt_credential

    tag = uuid.uuid4().hex[:8]
    cred_ids: list[str] = []

    async def _go():
        async with _client() as client:
            res = await client.post(
                "/credentials",
                json={"name": f"v43 used {tag}", "type": "generic", "data": {"token": "tok-usage-1"}},
            )
            assert res.status_code == 201, res.text
            cred_ids.append(res.json()["id"])
            cid = cred_ids[0]

            ctx = SimpleNamespace(workflow_id="wf-123", workflow_name="Usage WF")
            resolved = await decrypt_credential(ctx, cid)
            assert resolved["token"] == "tok-usage-1"

            events = (await client.get(f"/credentials/{cid}/events")).json()
            used = [e for e in events if e["action"] == "used"]
            assert len(used) == 1, f"expected exactly one used event, got {used}"
            assert used[0]["detail"]["workflow_id"] == "wf-123"
            assert used[0]["detail"]["workflow_name"] == "Usage WF"
            assert "tok-usage-1" not in json.dumps(events)

            # unknown id still raises LookupError (and writes nothing)
            try:
                await decrypt_credential(ctx, "no-such-cred-id")
                raise AssertionError("expected LookupError")
            except LookupError:
                pass

    try:
        asyncio.run(_go())
    finally:
        async def _cleanup():
            async with _client() as client:
                for cid in cred_ids:
                    await client.delete(f"/credentials/{cid}?force=true")
        asyncio.run(_cleanup())


# ------------------------------------------------------------------ test 4
def test_v43_key_scopes():
    """Read-only keys: safe methods pass, everything mutating 403s; legacy
    NULL-scope keys stay unrestricted; JWT sessions are never gated."""
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []
    key_ids: list[str] = []

    async def _go():
        async with _client() as client:
            user = await _register(client, f"v43-scopes-{tag}@py8n.test")
            hdrs = _auth(user["token"])

            # read-only key
            res = await client.post("/keys", json={"name": f"ro {tag}", "scopes": ["read"]}, headers=hdrs)
            assert res.status_code == 201, res.text
            ro = res.json()
            key_ids.append(ro["id"])
            assert ro["scopes"] == ["read"] and ro["read_only"] is True
            ro_h = {"X-API-Key": ro["key"]}

            # full-access key (default scopes)
            res = await client.post("/keys", json={"name": f"full {tag}"}, headers=hdrs)
            assert res.status_code == 201, res.text
            full = res.json()
            key_ids.append(full["id"])
            assert full["scopes"] == ["read", "write"] and full["read_only"] is False
            full_h = {"X-API-Key": full["key"]}

            # unknown scope -> 400
            bad = await client.post("/keys", json={"name": "bad", "scopes": ["root"]}, headers=hdrs)
            assert bad.status_code == 400, bad.text
            empty = await client.post("/keys", json={"name": "empty", "scopes": []}, headers=hdrs)
            assert empty.status_code == 400, empty.text

            # read-only key: GET passes everywhere on the enforced surface
            assert (await client.get("/workflows", headers=ro_h)).status_code == 200
            assert (await client.get("/datasets", headers=ro_h)).status_code == 200
            assert (await client.get("/credentials", headers=ro_h)).status_code == 200
            assert (await client.get("/keys", headers=ro_h)).status_code == 200

            # read-only key: mutations 403 with a clear message
            for method, url, jsonbody in (
                ("post", "/workflows", {"name": f"nope {tag}", "graph": {"nodes": [], "edges": []}}),
                ("post", "/credentials", {"name": f"nope {tag}", "type": "generic", "data": {"token": "x"}}),
                ("post", "/registries", {"name": f"nope {tag}", "url": "http://localhost:8000/x"}),
            ):
                r = await client.request(method, url, json=jsonbody, headers=ro_h)
                assert r.status_code == 403, f"{method} {url} -> {r.status_code}"
                assert "read-only" in r.json()["detail"]

            # even revoking ITSELF is a write -> 403 (use the JWT for cleanup)
            r = await client.delete(f"/keys/{ro['id']}", headers=ro_h)
            assert r.status_code == 403, r.text

            # full key: writes land with the owner's scoping (v41 semantics)
            r = await client.post("/workflows", json={"name": f"v43 keyed wf {tag}", "graph": {"nodes": [], "edges": []}}, headers=full_h)
            assert r.status_code == 201, r.text
            wf_ids.append(r.json()["id"])
            assert r.json()["owner_id"] == user["user"]["id"]

            # JWT on the same account is never scope-gated
            r = await client.post("/workflows", json={"name": f"v43 jwt wf {tag}", "graph": {"nodes": [], "edges": []}}, headers=hdrs)
            assert r.status_code == 201, r.text
            wf_ids.append(r.json()["id"])

            # legacy key with NULL scopes stays unrestricted (pre-v43 rows)
            from sqlalchemy import update

            from app.db import AsyncSessionLocal
            from app.models import ApiKey

            async with AsyncSessionLocal() as session:
                await session.execute(update(ApiKey).where(ApiKey.id == full["id"]).values(scopes=None))
                await session.commit()
            r = await client.post("/workflows", json={"name": f"v43 legacy wf {tag}", "graph": {"nodes": [], "edges": []}}, headers=full_h)
            assert r.status_code == 201, r.text
            wf_ids.append(r.json()["id"])

    try:
        asyncio.run(_go())
    finally:
        async def _cleanup():
            async with _client() as client:
                for wid in wf_ids:
                    await client.delete(f"/workflows/{wid}")
                for kid in key_ids:
                    await client.delete(f"/keys/{kid}")
            await _wipe_users_and_ownership()
        asyncio.run(_cleanup())
        asyncio.run(_drain_background())


# ------------------------------------------------------------------ helpers for registry tests
def _mock_transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _pack_doc(wf_name: str, ds_name: str) -> dict:
    return {
        "format": "py8n-pack",
        "pack_version": 1,
        "generated_at": "2026-09-01T00:00:00Z",
        "py8n_version": "1.43.0",
        "manifest": {},
        "workflows": [
            {"name": wf_name, "description": "from the mock registry",
             "graph": {"nodes": [_node("s", "schedule_trigger")], "edges": []}}
        ],
        "datasets": [
            {"name": ds_name, "description": "", "schema": [{"name": "city", "type": "string"}],
             "rows": [{"city": "lima"}, {"city": "oslo"}]}
        ],
    }


# ------------------------------------------------------------------ test 5
def test_v43_registry_check_and_sync():
    """check dry-runs a fetched pack without writing; sync imports through the
    ordinary pack pipeline and stamps the outcome; a second sync renames."""
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []
    reg_ids: list[str] = []
    pack = _pack_doc(f"v43 registry wf {tag}", f"v43 registry cities {tag}")

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pack)

    async def _go():
        import app.api.registries as reg_mod

        reg_mod._make_client = lambda: httpx.AsyncClient(
            transport=_mock_transport(_handler), follow_redirects=True
        )

        async with _client() as client:
            user = await _register(client, f"v43-reg-{tag}@py8n.test")
            hdrs = _auth(user["token"])

            res = await client.post("/registries", json={"name": f"mock reg {tag}", "url": "http://registry.test/pack"}, headers=hdrs)
            assert res.status_code == 201, res.text
            reg = res.json()
            reg_ids.append(reg["id"])
            assert reg["last_status"] is None

            listing = (await client.get("/registries", headers=hdrs)).json()
            assert len(listing) == 1 and listing[0]["url"] == "http://registry.test/pack"

            # check: honest preview, nothing created
            chk = await client.post(f"/registries/{reg['id']}/check", headers=hdrs)
            assert chk.status_code == 200, chk.text
            preview = chk.json()
            assert preview["workflow_count"] == 1 and preview["dataset_count"] == 1
            assert preview["workflows"][0]["valid"] is True
            assert preview["workflows"][0]["exists"] is False
            assert preview["py8n_version"] == "1.43.0"
            after_check = (await client.get("/workflows", headers=hdrs)).json()
            assert not any(w["name"] == f"v43 registry wf {tag}" for w in after_check), "check must not import"

            # sync #1: import lands, stamp is ok
            sync = await client.post(f"/registries/{reg['id']}/sync", headers=hdrs)
            assert sync.status_code == 200, sync.text
            body = sync.json()
            assert len(body["import"]["workflows"]) == 1 and len(body["import"]["datasets"]) == 1
            wf_ids.append(body["import"]["workflows"][0]["id"])
            assert body["registry"]["last_status"] == "ok"
            assert body["registry"]["last_summary"]["workflows_created"] == 1

            imported = await client.get(f"/workflows/{body['import']['workflows'][0]['id']}", headers=hdrs)
            assert imported.json()["is_active"] is False, "registry imports stay inactive like every pack"

            rows = await client.get(f"/datasets/{body['import']['datasets'][0]['id']}/rows", headers=hdrs)
            assert rows.status_code == 200 and len(rows.json()["rows"]) == 2

            # sync #2: name collision -> numbered rename, still ok
            sync2 = await client.post(f"/registries/{reg['id']}/sync", headers=hdrs)
            assert sync2.status_code == 200, sync2.text
            wf_ids.append(sync2.json()["import"]["workflows"][0]["id"])
            ds2 = sync2.json()["import"]["datasets"][0]
            assert ds2["name"] == f"v43 registry cities {tag} (2)", ds2

            return None

    try:
        asyncio.run(_go())
    finally:
        async def _cleanup():
            async with _client() as client:
                for wid in dict.fromkeys(wf_ids):
                    await client.delete(f"/workflows/{wid}")
                for rid in reg_ids:
                    await client.delete(f"/registries/{rid}")
            await _wipe_users_and_ownership()
        asyncio.run(_cleanup())
        asyncio.run(_drain_background())


# ------------------------------------------------------------------ test 6
def test_v43_registry_errors_and_scoping():
    """Non-pack URLs 502 with the error stamped on the registry; foreign
    registries 404; scheme validation rejects non-http URLs at creation."""
    tag = uuid.uuid4().hex[:8]
    reg_ids: list[str] = []

    def _html_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not a pack</html>")

    def _wrong_json_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"format": "some-other-export"})

    def _404_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "nope"})

    async def _go():
        import app.api.registries as reg_mod

        async with _client() as client:
            alice = await _register(client, f"v43-reg-a-{tag}@py8n.test")
            bob = await _register(client, f"v43-reg-b-{tag}@py8n.test")
            a = _auth(alice["token"])
            b = _auth(bob["token"])

            # scheme validation
            for bad_url in ("ftp://example.com/pack", "file:///etc/passwd", "not-a-url"):
                r = await client.post("/registries", json={"name": "bad", "url": bad_url}, headers=a)
                assert r.status_code == 400, f"{bad_url} -> {r.status_code}: {r.text}"

            # HTML at the URL -> sync stamps the error and 502s (invalid JSON shape)
            reg_mod._make_client = lambda: httpx.AsyncClient(transport=_mock_transport(_html_handler))
            res = await client.post("/registries", json={"name": f"html reg {tag}", "url": "http://registry.test/pack"}, headers=a)
            reg = res.json()
            reg_ids.append(reg["id"])
            sync = await client.post(f"/registries/{reg['id']}/sync", headers=a)
            assert sync.status_code == 502, sync.text
            assert "valid JSON" in sync.json()["detail"]
            listing = (await client.get("/registries", headers=a)).json()
            assert listing[0]["last_status"] == "error"
            assert "error" in listing[0]["last_summary"]

            # valid JSON but the wrong format marker -> the pack-specific message
            reg_mod._make_client = lambda: httpx.AsyncClient(transport=_mock_transport(_wrong_json_handler))
            sync = await client.post(f"/registries/{reg['id']}/sync", headers=a)
            assert sync.status_code == 502, sync.text
            assert "Py8n pack" in sync.json()["detail"]

            # HTTP 404 from the remote -> 502 too
            reg_mod._make_client = lambda: httpx.AsyncClient(transport=_mock_transport(_404_handler))
            sync = await client.post(f"/registries/{reg['id']}/sync", headers=a)
            assert sync.status_code == 502 and "HTTP 404" in sync.json()["detail"]

            # check on a 404 URL is a transient 502 (nothing stamped twice)
            chk = await client.post(f"/registries/{reg['id']}/check", headers=a)
            assert chk.status_code == 502, chk.text

            # bob cannot see, sync or delete alice's registry
            assert (await client.get("/registries", headers=b)).json() == []
            assert (await client.post(f"/registries/{reg['id']}/sync", headers=b)).status_code == 404
            assert (await client.post(f"/registries/{reg['id']}/check", headers=b)).status_code == 404
            assert (await client.delete(f"/registries/{reg['id']}", headers=b)).status_code == 404

            # unknown registry ids 404
            assert (await client.post("/registries/nope/sync", headers=a)).status_code == 404
            assert (await client.delete("/registries/nope", headers=a)).status_code == 404

            # a read-only key cannot sync a registry (403 before ownership even matters)
            ro = (await client.post("/keys", json={"name": "ro", "scopes": ["read"]}, headers=a)).json()
            ro_h = {"X-API-Key": ro["key"]}
            reg_mod._make_client = lambda: httpx.AsyncClient(transport=_mock_transport(_html_handler))
            r = await client.post(f"/registries/{reg['id']}/sync", headers=ro_h)
            assert r.status_code == 403, r.text
            await client.delete(f"/keys/{ro['id']}", headers=a)

    try:
        asyncio.run(_go())
    finally:
        async def _cleanup():
            async with _client() as client:
                for rid in reg_ids:
                    await client.delete(f"/registries/{rid}")
            await _wipe_users_and_ownership()
        asyncio.run(_cleanup())
