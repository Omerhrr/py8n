"""v103 tests - the branded front door and the deployment liveness.

The identity pillar's next steps after v102: the login surface gets a
FRONT DOOR to live behind, and the deployment answers for its own
liveness.

* THE LIVENESS PING: POST /systems/{id}/deployment/ping asks "does this
  system's custom domain actually answer?" - one outbound GET to the
  derived URL, timed, the result stamped on the deployment record
  (last_ping_*) and written to the operations log. A domain that does
  NOT answer is an honest answer (ok=false + what happened), never a
  500; a deployment with no domain refuses loud. The probe NEVER raises
  - the failure IS the evidence. The projection and the estate health
  row both carry it (compact there - the full evidence lives on the
  record).
* THE BRANDED FRONT DOOR CONTRACT: GET /systems/by-domain/{domain} is
  what the new public landing page consumes - the test pins the exact
  shape the page renders (system identity + branding fallbacks to the
  system's own face + environment) and the my_role ride for a held
  token (owner for the creator, the invited member's role, None for a
  stranger).

The probe itself is monkeypatched for the wire tests (a sandbox has no
DNS); the REAL probe is exercised against a refused loopback port -
connection refused is exactly the honest answer the probe exists to
record.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
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
    email = f"v103-{tag}-{uuid.uuid4().hex[:6]}@py8n.test"
    res = await client.post("/auth/register", json={
        "email": email,
        "password": "correct-horse-battery",
        "name": f"v103 {tag}",
    })
    assert res.status_code == 201, res.text
    return {"token": res.json()["token"], "id": res.json()["user"]["id"],
            "email": email}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _mk_system(client: httpx.AsyncClient, headers: dict | None, name: str,
                     **extra) -> dict:
    res = await client.post("/systems", headers=headers or {},
                            json={"name": name, **extra})
    assert res.status_code == 201, res.text
    return res.json()


# ---------------------------------------------------------------------------
# 1. the liveness ping - the domain answers, and the answer is kept
# ---------------------------------------------------------------------------

def test_v103_deployment_ping_liveness(monkeypatch):
    from app.services import system_deployment as deploy_svc

    async def _fast_ok(url: str) -> dict:
        assert url == "https://ops.v103-test.com", url
        return {"ok": True, "ms": 42, "code": 200, "detail": "HTTP 200"}

    monkeypatch.setattr(deploy_svc, "probe_url", _fast_ok)

    async def _run():
        async with _client() as client:
            system = await _mk_system(client, None, "Acme v103", color="#38bdf8",
                                      description="The revenue line")
            sid = system["id"]

            # never asked yet: the projection answers null, not a guess
            r = await client.put(f"/systems/{sid}/deployment",
                                 json={"domain": "ops.v103-test.com"})
            assert r.status_code == 200, r.text
            assert r.json()["deployment"]["last_ping"] is None, r.text

            # the probe: ok, timed, stamped, on the record
            r = await client.post(f"/systems/{sid}/deployment/ping")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["ping"] == {"ok": True, "ms": 42, "code": 200,
                                    "detail": "HTTP 200"}, body
            dep = body["deployment"]
            assert dep["last_ping"]["ok"] is True, dep
            assert dep["last_ping"]["ms"] == 42 and dep["last_ping"]["code"] == 200, dep
            assert dep["last_ping"]["at"], dep

            # the read path carries the same evidence
            r = await client.get(f"/systems/{sid}/deployment")
            assert r.json()["deployment"]["last_ping"]["ok"] is True, r.text

            # the operations log names the probe
            r = await client.get(f"/systems/{sid}/operations")
            pings = [o for o in r.json()["operations"] if o["verb"] == "deployment_ping"]
            assert pings and pings[0]["detail"]["ok"] is True, pings

            # the estate health row wears the compact answer
            r = await client.get("/systems/health/overview")
            rows = {row["name"]: row for row in r.json()["systems"]}
            assert "Acme v103" in rows, list(rows)
            riding = rows["Acme v103"]["deployment"]
            assert riding["domain"] == "ops.v103-test.com", riding
            assert riding["last_ping"]["ok"] is True and riding["last_ping"]["ms"] == 42, riding

    _sync(_wrap(_run()))


# ---------------------------------------------------------------------------
# 2. the honest answer - refusal, failure recorded, recovery flips it
# ---------------------------------------------------------------------------

def test_v103_ping_honesty(monkeypatch):
    from app.services import system_deployment as deploy_svc

    async def _dead(url: str) -> dict:
        return {"ok": False, "ms": 900, "code": None,
                "detail": "ConnectError: connection refused"}

    async def _run():
        async with _client() as client:
            # no deployment record at all -> loud refusal, not a 500
            system = await _mk_system(client, None, "Bare v103")
            r = await client.post(f"/systems/{system['id']}/deployment/ping")
            assert r.status_code == 400, r.text
            assert "no custom domain" in r.json()["detail"], r.text

            # a record WITH a domain, but the domain is dead: the answer
            # is honest (200 carrying ok=false), and the evidence is kept
            deployed = await _mk_system(client, None, "Dark v103")
            r = await client.put(f"/systems/{deployed['id']}/deployment",
                                 json={"domain": "dark.v103-test.com"})
            assert r.status_code == 200, r.text
            monkeypatch.setattr(deploy_svc, "probe_url", _dead)
            r = await client.post(f"/systems/{deployed['id']}/deployment/ping")
            assert r.status_code == 200, r.text
            assert r.json()["ping"]["ok"] is False, r.text
            assert "connection refused" in r.json()["ping"]["detail"], r.text
            assert r.json()["deployment"]["last_ping"]["ok"] is False, r.text

            # the ops log keeps the failure too - evidence, not a flag
            r = await client.get(f"/systems/{deployed['id']}/operations")
            pings = [o for o in r.json()["operations"] if o["verb"] == "deployment_ping"]
            assert pings and pings[0]["detail"]["ok"] is False, pings

            # recovery: the next probe flips the record back to green
            async def _alive(url: str) -> dict:
                return {"ok": True, "ms": 7, "code": 200, "detail": "HTTP 200"}
            monkeypatch.setattr(deploy_svc, "probe_url", _alive)
            r = await client.post(f"/systems/{deployed['id']}/deployment/ping")
            assert r.status_code == 200 and r.json()["ping"]["ok"] is True, r.text

            # the probe target derivation honors the dev override
            monkeypatch.setattr(settings, "deploy_ping_override", "http://127.0.0.1:9/health")
            assert deploy_svc._probe_target("any.v103-test.com") == "http://127.0.0.1:9/health"
            monkeypatch.setattr(settings, "deploy_ping_override", "")
            assert deploy_svc._probe_target("ops.acme.com") == "https://ops.acme.com"

    _sync(_wrap(_run()))


def test_v103_probe_never_raises():
    """The REAL probe against a refused loopback port: the failure comes
    back as the answer, not as an exception."""
    from app.services import system_deployment as deploy_svc

    result = asyncio.run(deploy_svc.probe_url("http://127.0.0.1:1/nope"))
    assert result["ok"] is False, result
    assert result["code"] is None, result
    assert result["detail"], result
    assert result["ms"] >= 0, result


# ---------------------------------------------------------------------------
# 3. the branded front door contract - exactly what the landing consumes
# ---------------------------------------------------------------------------

def test_v103_public_identity_contract():
    async def _run():
        async with _client() as client:
            owner = await _mk_user(client, "owner")
            stranger = await _mk_user(client, "stranger")

            # the system is CLAIMED (created by the owner's token) so the
            # role ride is a real answer, not the unclaimed bootstrap
            system = await _mk_system(
                client, _auth(owner["token"]), "Acme Front Door",
                color="#22d3ee", icon="boxes",
                description="Runs the whole revenue line, end to end")
            sid = system["id"]
            r = await client.put(f"/systems/{sid}/deployment",
                                 json={"domain": "front.v103-test.com",
                                       "environment": "production"})
            assert r.status_code == 200, r.text
            r = await client.post(f"/systems/{sid}/deployment/deploy")
            assert r.status_code == 200, r.text

            # no branding set: every fallback is the system's own face
            r = await client.get("/systems/by-domain/front.v103-test.com")
            assert r.status_code == 200, r.text
            ident = r.json()
            assert ident["system"]["id"] == sid, ident
            assert ident["system"]["name"] == "Acme Front Door", ident
            assert ident["system"]["icon"] == "boxes", ident
            assert ident["branding"]["accent"] == "#22d3ee", ident
            assert ident["branding"]["tagline"] == \
                "Runs the whole revenue line, end to end", ident
            assert ident["branding"]["login_headline"] == "Acme Front Door", ident
            assert ident["branding"]["logo"] == "boxes", ident
            assert ident["environment"] == "production" and ident["status"] == "live", ident
            # auth-off convention: the anonymous door answer says owner
            assert ident["my_role"] == "owner", ident

            # the creator's token: owner rides along
            r = await client.get("/systems/by-domain/front.v103-test.com",
                                 headers=_auth(owner["token"]))
            assert r.json()["my_role"] == "owner", r.text

            # a stranger's token: None - the door does not pretend
            r = await client.get("/systems/by-domain/front.v103-test.com",
                                 headers=_auth(stranger["token"]))
            assert r.json()["my_role"] is None, r.text

            # invited as viewer: the ride says exactly that
            r = await client.post(f"/systems/{sid}/members", headers=_auth(owner["token"]),
                                  json={"email": stranger["email"], "role": "viewer"})
            assert r.status_code in (200, 201), r.text
            r = await client.get("/systems/by-domain/front.v103-test.com",
                                 headers=_auth(stranger["token"]))
            assert r.json()["my_role"] == "viewer", r.text

            # a paused deployment is dark - the landing answers 404
            r = await client.post(f"/systems/{sid}/deployment/pause")
            assert r.status_code == 200, r.text
            r = await client.get("/systems/by-domain/front.v103-test.com")
            assert r.status_code == 404, r.text

    _sync(_wrap(_run()))


# ---------------------------------------------------------------------------
# 4. units + the version pin
# ---------------------------------------------------------------------------

def test_v103_units_and_version():
    from app.services import system_deployment as deploy_svc

    # the probe target derivation: production derives from the domain,
    # the dev override wins when set (sandbox domains do not resolve)
    assert deploy_svc._probe_target("ops.acme.com") == "https://ops.acme.com"

    # the projection: never-asked deployments answer last_ping=None
    class _Row:
        system_id = "s1"; domain = "ops.acme.com"; environment = "production"
        status = "live"; branding = {}; deployed_at = None
        created_at = None; updated_at = None
        last_ping_at = None; last_ping_ok = None; last_ping_ms = None
        last_ping_code = None; last_ping_detail = None

    out = deploy_svc.deployment_out(_Row())
    assert out["last_ping"] is None and out["url"] == "https://ops.acme.com", out

    assert settings.version == "1.107.0"
