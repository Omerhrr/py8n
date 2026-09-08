"""v104 tests - host routing, the scheduled pings, the system's own keys.

The identity pillar's third stride: the deployed system gets a REAL
address (host routing), a health dot fed by evidence on a rhythm
(scheduled pings), and a machine voice of its own (system-scoped API
keys).

* THE DERIVED ROUTE SHEET: GET /systems/deployment/routes.caddy writes
  the Caddy site blocks FROM the live deployments - one block per domain
  mapping the hostname onto the branded front door (rewrite * /go/
  {domain}); paused/offline deployments appear ONLY as comments (a dark
  door must not receive traffic); the upstream is a query parameter.
  The Caddyfile on disk carries the same contract (the tenant switches:
  the glob import + the PY8N_TENANT_HOSTS block) - the test reads it so
  the edge and the estate cannot drift apart silently.
* THE SCHEDULED PINGS: ping_due_deployments re-probes every LIVE
  deployment whose latest probe is older than the interval (injectable
  clock), stamps the SAME evidence columns the manual door stamps, and
  is loud only on TRANSITIONS - deployment_ping_lost /
  deployment_ping_recovered in the operations log (steady-state probes
  update the evidence silently - an honest log is not a flooded log).
  The health dot listens: a live domain whose newest probe failed moves
  the row to attention (unreachable=true), recovery moves it back.
  The tick door carries the sweep's records under ``deploy_ping``.
* THE SYSTEM API KEYS: py8n_sys_... machine credentials that speak AS
  the system - the role derived from scopes (write -> editor, else
  viewer), owner-only minting (the full key shown once), and the
  BOUNDARY: a key on a foreign system is a 404 even where an anonymous
  caller would be an owner (auth-off) - a machine credential must never
  inherit the anonymous fall-through. Revoked keys resolve to nothing;
  v41 user keys keep working unchanged.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402

API = "http://testserver/api/v1"


@pytest.fixture(autouse=True)
def _clean_event_subscribers():
    from app.services import system_events as events_svc

    events_svc._subscribers.clear()
    yield
    events_svc._subscribers.clear()


@pytest.fixture(autouse=True)
def _fresh_rate_limit():
    from app.api import _ratelimit

    _ratelimit.reset_all()
    yield
    _ratelimit.reset_all()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


async def _drain_background() -> None:
    from app.services import executor as executor_mod
    from app.services import system_events as events_svc

    for _ in range(5):
        tasks = [t for t in events_svc._DISPATCH_TASKS if not t.done()]
        if not tasks:
            break
        await asyncio.gather(*tasks, return_exceptions=True)
    tasks = [t for t in executor_mod._background_tasks if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _sync(coro):
    return asyncio.run(coro)


async def _wrap(coro):
    try:
        return await coro
    finally:
        await _drain_background()


async def _mk_user(client: httpx.AsyncClient, tag: str) -> dict:
    email = f"v104-{tag}-{uuid.uuid4().hex[:6]}@py8n.test"
    res = await client.post("/auth/register", json={
        "email": email,
        "password": "correct-horse-battery",
        "name": f"v104 {tag}",
    })
    assert res.status_code == 201, res.text
    return {"token": res.json()["token"], "id": res.json()["user"]["id"],
            "email": email}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _syskey(key: str) -> dict:
    return {"X-API-Key": key}


async def _mk_system(client: httpx.AsyncClient, headers: dict | None, name: str,
                     **extra) -> dict:
    res = await client.post("/systems", headers=headers or {},
                            json={"name": name, **extra})
    assert res.status_code == 201, res.text
    return res.json()


async def _mk_live_deployment(client: httpx.AsyncClient, system_id: str,
                              domain: str) -> dict:
    """PUT the domain, then deploy - the loud path to a LIVE deployment."""
    r = await client.put(f"/systems/{system_id}/deployment",
                         json={"domain": domain, "environment": "production"})
    assert r.status_code == 200, r.text
    r = await client.post(f"/systems/{system_id}/deployment/deploy")
    assert r.status_code == 200, r.text
    return r.json()["deployment"]


# ---------------------------------------------------------------------------
# 1. the derived route sheet - the estate's domains as Caddy eats them
# ---------------------------------------------------------------------------

def test_v104_host_routes_sheet():
    async def _run():
        async with _client() as client:
            live = await _mk_system(client, None, "Routed v104")
            await _mk_live_deployment(client, live["id"], "routed.v104-test.com")

            dark = await _mk_system(client, None, "Dark v104")
            r = await client.put(f"/systems/{dark['id']}/deployment",
                                 json={"domain": "dark.v104-test.com"})
            assert r.status_code == 200, r.text
            # never deployed -> the deployment is OFFLINE - dark either way

            bare = await _mk_system(client, None, "Bare v104")  # no domain at all

            r = await client.get("/systems/deployment/routes.caddy")
            assert r.status_code == 200, r.text
            assert r.headers["content-type"].startswith("text/plain"), r.headers
            sheet = r.text

            # the live domain is a REAL site block mapping onto the front door
            assert "routed.v104-test.com {" in sheet, sheet
            assert "rewrite * /go/routed.v104-test.com" in sheet, sheet
            assert "reverse_proxy localhost:3000" in sheet, sheet

            # the dark deployment is a COMMENT - never routed
            dark_line = [l for l in sheet.splitlines()
                         if "dark.v104-test.com" in l]
            assert dark_line and all(l.lstrip().startswith("#") for l in dark_line), sheet
            live_block = sheet.split("# dark deployments")[0]
            assert "dark.v104-test.com {" not in live_block, sheet

            # a domainless system leaves no trace
            assert "Bare v104" not in sheet.split("# dark deployments")[0], sheet
            assert live["name"] in sheet, sheet
            assert dark["name"] in sheet, sheet

            # the live-route count rides the headers
            live_count = int(r.headers["X-Py8n-Live-Routes"])
            assert live_count >= 1, r.headers

            # the upstream is a query parameter (where the door lives)
            r2 = await client.get("/systems/deployment/routes.caddy?upstream=frontend:3000")
            assert "reverse_proxy frontend:3000" in r2.text, r2.text
            assert "reverse_proxy localhost:3000" not in r2.text, r2.text

            # the sheet is a projection: pause the live system -> block disappears
            r = await client.post(f"/systems/{live['id']}/deployment/pause")
            assert r.status_code == 200, r.text
            r = await client.get("/systems/deployment/routes.caddy")
            sheet = r.text
            assert "rewrite * /go/routed.v104-test.com" not in sheet, sheet
            assert "routed.v104-test.com" in sheet, sheet  # still named, as a comment

    _sync(_wrap(_run()))


def test_v104_caddyfile_carries_the_tenant_contract():
    """The edge config on disk must keep speaking the tenant contract:
    the derived-sheet import and the static env block mapping hosts onto
    /go/{host}. If someone rewrites the Caddyfile without them, the
    estate's domains stop reaching their front doors."""
    caddyfile = Path(__file__).resolve().parents[3] / "Caddyfile"
    assert caddyfile.exists(), caddyfile
    text = caddyfile.read_text()
    assert "import /etc/caddy/tenants/*.caddy" in text, text
    assert "PY8N_TENANT_HOSTS" in text, text
    assert "rewrite * /go/{host}" in text, text


# ---------------------------------------------------------------------------
# 2. the scheduled pings - the health dot fed by evidence on a rhythm
# ---------------------------------------------------------------------------

def test_v104_scheduled_pings(monkeypatch):
    from app.services import system_deployment as deploy_svc

    state = {"ok": True}

    async def _probe(url: str) -> dict:
        if state["ok"]:
            return {"ok": True, "ms": 21, "code": 200, "detail": "HTTP 200"}
        return {"ok": False, "ms": 900, "code": None,
                "detail": "ConnectError: connection refused"}

    monkeypatch.setattr(deploy_svc, "probe_url", _probe)
    monkeypatch.setattr(settings, "deploy_ping_interval_seconds", 600)

    async def _run():
        async with _client() as client:
            system = await _mk_system(client, None, "Pinged v104")
            dep = await _mk_live_deployment(client, system["id"], "ping.v104-test.com")
            assert dep["status"] == "live", dep
            now = datetime.now(timezone.utc)

            # the tick door runs the sweep - response carries deploy_ping
            r = await client.post("/scheduler/escalations/tick")
            assert r.status_code == 200, r.text
            body = r.json()
            assert "deploy_ping" in body, list(body)
            sweep = body["deploy_ping"]
            assert sweep["probed"] >= 1, sweep
            mine = [p for p in sweep["lost"] + sweep["recovered"] if p["domain"] == "ping.v104-test.com"]
            assert not mine, sweep  # green steady state is silent

            r = await client.get(f"/systems/{system['id']}/deployment")
            last = r.json()["deployment"]["last_ping"]
            assert last and last["ok"] is True and last["ms"] == 21, r.text

            # CADENCE: an immediate re-sweep does not re-probe (the rhythm
            # holds) - the evidence timestamp proves nothing fresher fired
            r = await client.post("/scheduler/escalations/tick")
            sweep = r.json()["deploy_ping"]
            assert all(p["domain"] != "ping.v104-test.com"
                       for p in sweep["lost"] + sweep["recovered"]), sweep

            # the health dot is green
            r = await client.get("/systems/health/overview")
            row = next(row for row in r.json()["systems"] if row["id"] == system["id"])
            assert row["status"] == "running", row
            assert row["deployment"]["unreachable"] is False, row

            # THE DOMAIN GOES DARK: the next due sweep (injected clock past
            # the interval) stamps the honest failure AND is loud about it
            state["ok"] = False
            later = now + timedelta(seconds=601)
            from app.db import AsyncSessionLocal

            async with AsyncSessionLocal() as session:
                out = await deploy_svc.ping_due_deployments(session, now=later)
                await session.commit()
            lost = [p for p in out["lost"] if p["domain"] == "ping.v104-test.com"]
            assert lost and lost[0]["ok"] is False, out
            assert "connection refused" in lost[0]["detail"], out

            r = await client.get(f"/systems/{system['id']}/operations")
            ops = [o for o in r.json()["operations"] if o["verb"] == "deployment_ping_lost"]
            assert ops and ops[0]["detail"]["domain"] == "ping.v104-test.com", ops

            # the dot moved to attention - the system may be green inside,
            # but its people cannot reach it
            r = await client.get("/systems/health/overview")
            row = next(row for row in r.json()["systems"] if row["id"] == system["id"])
            assert row["status"] == "attention", row
            assert row["deployment"]["unreachable"] is True, row

            # already-dark: a second failing sweep updates evidence SILENTLY
            even_later = later + timedelta(seconds=601)
            async with AsyncSessionLocal() as session:
                out = await deploy_svc.ping_due_deployments(session, now=even_later)
                await session.commit()
            assert not [p for p in out["lost"] if p["domain"] == "ping.v104-test.com"], out
            r = await client.get(f"/systems/{system['id']}/operations")
            assert len([o for o in r.json()["operations"]
                        if o["verb"] == "deployment_ping_lost"]) == 1, r.text

            # RECOVERY: the domain answers again - loud, and the dot goes green
            state["ok"] = True
            recovered_at = even_later + timedelta(seconds=601)
            async with AsyncSessionLocal() as session:
                out = await deploy_svc.ping_due_deployments(session, now=recovered_at)
                await session.commit()
            rec = [p for p in out["recovered"] if p["domain"] == "ping.v104-test.com"]
            assert rec and rec[0]["ok"] is True, out
            r = await client.get(f"/systems/{system['id']}/operations")
            assert [o for o in r.json()["operations"]
                    if o["verb"] == "deployment_ping_recovered"], r.text
            r = await client.get("/systems/health/overview")
            row = next(row for row in r.json()["systems"] if row["id"] == system["id"])
            assert row["status"] == "running" and row["deployment"]["unreachable"] is False, row

            # a dark (paused) deployment is never swept - the door is dark BY DESIGN
            await client.post(f"/systems/{system['id']}/deployment/pause")
            dark_at = recovered_at + timedelta(seconds=601)
            async with AsyncSessionLocal() as session:
                out = await deploy_svc.ping_due_deployments(session, now=dark_at)
                await session.commit()
            assert all(p["domain"] != "ping.v104-test.com"
                       for p in out["lost"] + out["recovered"]), out

    _sync(_wrap(_run()))


# ---------------------------------------------------------------------------
# 3. the system API keys - the machine identity of the deployed surface
# ---------------------------------------------------------------------------

def test_v104_system_keys(monkeypatch):
    from app.services import system_deployment as deploy_svc

    async def _fast_ok(url: str) -> dict:
        return {"ok": True, "ms": 33, "code": 200, "detail": "HTTP 200"}

    async def _run():
        async with _client() as client:
            owner = await _mk_user(client, "owner")
            stranger = await _mk_user(client, "stranger")

            system = await _mk_system(client, _auth(owner["token"]), "Keyed v104")
            sid = system["id"]
            foreign = await _mk_system(client, _auth(stranger["token"]), "Foreign v104")

            # MINT: owner-only, full key shown exactly once
            r = await client.post(f"/systems/{sid}/keys", headers=_auth(owner["token"]),
                                  json={"name": "ERP push job"})
            assert r.status_code == 201, r.text
            body = r.json()
            assert body["key"].startswith("py8n_sys_"), body
            assert body["prefix"].startswith("py8n_sys_"), body
            assert body["scopes"] == ["read", "write"], body
            write_key = body["key"]

            r = await client.post(f"/systems/{sid}/keys", headers=_auth(owner["token"]),
                                  json={"name": "watchdog", "scopes": ["read"]})
            assert r.status_code == 201, r.text
            read_key = r.json()["key"]

            # non-owners cannot mint (stranger is not a member at all -> 404)
            r = await client.post(f"/systems/{sid}/keys", headers=_auth(stranger["token"]),
                                  json={"name": "nope"})
            assert r.status_code == 404, r.text

            # the masked list never carries a full key
            r = await client.get(f"/systems/{sid}/keys", headers=_auth(owner["token"]))
            keys = r.json()["keys"]
            assert len(keys) == 2 and all("key" not in k for k in keys), keys
            assert all(k["prefix"].startswith("py8n_sys_") for k in keys), keys
            assert {k["read_only"] for k in keys} == {True, False}, keys
            write_prefix = body["prefix"]

            # THE KEY SPEAKS AS THE SYSTEM: its role is its scopes, spelled out
            r = await client.get(f"/systems/{sid}", headers=_syskey(write_key))
            assert r.status_code == 200, r.text
            assert r.json()["my_role"] == "editor", r.text
            r = await client.get(f"/systems/{sid}", headers=_syskey(read_key))
            assert r.status_code == 200 and r.json()["my_role"] == "viewer", r.text

            # THE BOUNDARY: a foreign system is 404 THROUGH THE KEY - even
            # though an anonymous (auth-off) caller would be an owner there.
            # The key must never inherit the anonymous fall-through.
            r = await client.get(f"/systems/{foreign['id']}", headers=_syskey(write_key))
            assert r.status_code == 404, r.text
            # the contrast that proves the branch: anonymous still sees it (auth-off)
            r = await client.get(f"/systems/{foreign['id']}")
            assert r.status_code == 200, r.text

            # A WRITE DOOR: ping with the write key rides the editor role
            await client.put(f"/systems/{sid}/deployment", json={"domain": "keyed.v104-test.com"})
            monkeypatch.setattr(deploy_svc, "probe_url", _fast_ok)
            r = await client.post(f"/systems/{sid}/deployment/ping", headers=_syskey(write_key))
            assert r.status_code == 200 and r.json()["ping"]["ok"] is True, r.text

            # a read-only key cannot write (the router-level scope gate)
            r = await client.post(f"/systems/{sid}/deployment/ping", headers=_syskey(read_key))
            assert r.status_code == 403, r.text

            # OWNER DOORS ARE HUMAN-ONLY: the key tops out at editor
            r = await client.post(f"/systems/{sid}/keys", headers=_syskey(write_key),
                                  json={"name": "machine self-mint"})
            assert r.status_code == 403, r.text
            r = await client.get(f"/systems/{sid}/keys", headers=_syskey(write_key))
            assert r.status_code == 403, r.text
            r = await client.post(f"/systems/{sid}/members", headers=_syskey(write_key),
                                  json={"email": stranger["email"], "role": "viewer"})
            assert r.status_code == 403, r.text

            # REVOCATION: the next request carrying the key resolves to nothing
            key_id = next(k["id"] for k in keys if k["prefix"] == write_prefix)
            r = await client.delete(f"/systems/{sid}/keys/{key_id}", headers=_auth(owner["token"]))
            assert r.status_code == 204, r.text
            monkeypatch.setattr(settings, "require_auth", True)
            try:
                r = await client.get(f"/systems/{sid}", headers=_auth(owner["token"]))
                assert r.status_code == 200, r.text  # the human still walks in
                r = await client.get(f"/systems/{sid}", headers=_syskey(write_key))
                assert r.status_code == 401, r.text  # the revoked key is nobody
            finally:
                monkeypatch.setattr(settings, "require_auth", False)

            # v41 user keys are untouched: the owner's own key still speaks
            r = await client.post("/keys", headers=_auth(owner["token"]),
                                  json={"name": "user key"})
            assert r.status_code == 201, r.text
            user_key = r.json()["key"]
            assert user_key.startswith("py8n_") and not user_key.startswith("py8n_sys_"), user_key
            r = await client.get(f"/systems/{sid}", headers=_syskey(user_key))
            assert r.status_code == 200 and r.json()["my_role"] == "owner", r.text

    _sync(_wrap(_run()))


# ---------------------------------------------------------------------------
# 4. units + the version pin
# ---------------------------------------------------------------------------

def test_v104_units_and_version():
    from app.services import system_keys as keys_svc
    from app.services import system_deployment as deploy_svc

    # the role ladder derives from scopes - and tops out at editor
    assert keys_svc.role_for(["read", "write"]) == "editor"
    assert keys_svc.role_for(["read"]) == "viewer"
    assert keys_svc.role_for(None) == "viewer"

    # mint validates loudly
    with pytest.raises(keys_svc.SystemKeyError):
        keys_svc.mint("s1", name="  ", scopes=None, created_by=None)
    with pytest.raises(keys_svc.SystemKeyError):
        keys_svc.mint("s1", name="k", scopes=["admin"], created_by=None)

    # the route sheet: an empty estate renders headers only - and no crash
    sheet = deploy_svc.routes_sheet([])
    assert "0 live, 0 dark" in sheet, sheet

    assert settings.version == "1.105.0"
