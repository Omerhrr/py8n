"""v106 tests - advance actions beside the acks on the surface,
per-domain TLS notes on the route sheet, and the deployment liveness
riding the surface.

* ADVANCE BESIDE THE ACKS: the front door's work surface rows carry the
  moves the machine allows FROM their current state (``transitions`` -
  resolved by the SAME ``_allowed_from`` the advance door runs, so the
  buttons can never offer a move the door would refuse), and the move
  rides the v105 authority doors exactly like the take does: the owner
  or an editor advances, the viewer's move refuses honestly (403), and
  the journey receipt names the human. An advanced-but-still-late row
  STAYS on the attention list wearing its new state's transitions (the
  clock never lied); the terminal move retires the row entirely.
* PER-DOMAIN TLS NOTES ON THE ROUTE SHEET: every live site block on
  GET /systems/deployment/routes.caddy carries its domain's own TLS
  note - the posture (production leans on Caddy's automatic HTTPS,
  staging rehearses on the ACME staging issuer) followed by what the
  domain's liveness probe last SAW: "certificate live" with the probe's
  numbers when it answered, CHECK THE CERTIFICATE with the detail when
  it did not, and an honest "no probe evidence yet" when never asked.
* THE DEPLOYMENT LIVENESS RIDING THE SURFACE: the work surface answers
  ``liveness`` - the domain, the status, the environment and the last
  probe's evidence (or an honest null when it has never been asked) -
  so the people on a custom domain can see their own address is being
  watched, with the same evidence columns the estate's health row reads.
"""

from __future__ import annotations

import asyncio
import socket
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402

API = "http://testserver/api/v1"

MACHINE = {
    "states": ["a", "b", "done", "dead"],
    "initial": "a",
    "transitions": [
        {"name": "go", "from": "a", "to": "b"},
        {"name": "finish", "from": "b", "to": "done"},
        {"name": "kill", "from": "a", "to": "dead"},
    ],
}


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
    email = f"v106-{tag}-{uuid.uuid4().hex[:6]}@py8n.test"
    res = await client.post("/auth/register", json={
        "email": email,
        "password": "correct-horse-battery",
        "name": f"v106 {tag}",
    })
    assert res.status_code == 201, res.text
    return {"token": res.json()["token"], "id": res.json()["user"]["id"],
            "email": email}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _mk_system(client: httpx.AsyncClient, headers: dict | None,
                     name: str, **extra) -> dict:
    res = await client.post("/systems", headers=headers or {},
                            json={"name": name, **extra})
    assert res.status_code == 201, res.text
    return res.json()


async def _mk_live_deployment(client: httpx.AsyncClient, system_id: str,
                              domain: str, environment: str = "production") -> dict:
    """PUT the domain, then deploy - the loud path to a LIVE deployment."""
    r = await client.put(f"/systems/{system_id}/deployment",
                         json={"domain": domain, "environment": environment})
    assert r.status_code == 200, r.text
    r = await client.post(f"/systems/{system_id}/deployment/deploy")
    assert r.status_code == 200, r.text
    return r.json()["deployment"]


async def _mk_machine(client: httpx.AsyncClient, headers: dict | None,
                      name: str, definition: dict | None = None) -> dict:
    res = await client.post("/processes", headers=headers or {},
                            json={"name": name,
                                  "definition": definition or MACHINE})
    assert res.status_code == 201, res.text
    return res.json()


async def _bind(client: httpx.AsyncClient, headers: dict | None,
                system_id: str, process_id: str) -> None:
    r = await client.post(f"/systems/{system_id}/components",
                          headers=headers or {},
                          json={"kind": "process", "ref_id": process_id})
    assert r.status_code in (200, 201), r.text


def _closed_port() -> int:
    """A port with nothing listening - the probe's honest failure."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _stamp_probe(domain: str, *, ok: bool, code: int | None,
                       ms: int | None, detail: str) -> None:
    """Stamp the probe evidence columns directly - the SAME projection
    every reader wears (the sheet, the estate row, the surface)."""
    from app.db import AsyncSessionLocal
    from app.services import system_deployment as deploy_svc

    async with AsyncSessionLocal() as session:
        row = await deploy_svc.get_by_domain(session, domain)
        assert row is not None, domain
        row.last_ping_at = datetime.now(timezone.utc)
        row.last_ping_ok = ok
        row.last_ping_code = code
        row.last_ping_ms = ms
        row.last_ping_detail = detail[:200]
        await session.commit()


async def _real_ping(system_id: str) -> dict:
    """The v103 ping door at the service level - the honest probe."""
    from app.db import AsyncSessionLocal
    from app.models import Py8nSystem
    from app.services import system_deployment as deploy_svc

    async with AsyncSessionLocal() as session:
        system = await session.get(Py8nSystem, system_id)
        row, result = await deploy_svc.ping_deployment(session, system)
        await session.commit()
    return result


# ---------------------------------------------------------------------------
# 1. advance actions beside the acks on the surface
# ---------------------------------------------------------------------------

def test_v106_advance_beside_the_acks_on_the_surface():
    async def _go():
        async with _client() as client:
            owner = await _mk_user(client, "owner")
            viewer = await _mk_user(client, "viewer")
            oh = _auth(owner["token"])

            system = await _mk_system(client, oh, "Acme Ops v106")
            sid = system["id"]
            r = await client.post(f"/systems/{sid}/members", headers=oh,
                                  json={"email": viewer["email"], "role": "viewer"})
            assert r.status_code in (200, 201), r.text
            domain = f"adv{uuid.uuid4().hex[:6]}.acme.com"
            await _mk_live_deployment(client, sid, domain)

            machine = await _mk_machine(client, oh, "Lead pipeline v106")
            await _bind(client, oh, sid, machine["id"])
            r = await client.post(f"/processes/{machine['id']}/instances",
                                  headers=oh,
                                  json={"ref": "ADV-1", "title": "the late one",
                                        "due_in_seconds": 1})
            assert r.status_code == 201, r.text
            iid = r.json()["id"]
            # the double-clock discipline (v96): the surface reads the
            # REAL clock - let the 1s SLA pass before reading
            await asyncio.sleep(2.0)

            # the row wears the moves the machine allows FROM its state
            r = await client.get(f"/systems/by-domain/{domain}/work", headers=oh)
            assert r.status_code == 200, r.text
            work = r.json()["work"]
            assert work["totals"]["attention"] == 1, work["totals"]
            row = work["attention"][0]
            assert row["instance_id"] == iid and row["state"] == "a", row
            assert row["transitions"] == [
                {"name": "go", "to": "b"}, {"name": "kill", "to": "dead"}], row

            # the viewer's move refuses honestly - the surface's control
            # rides the SAME authority door the ack rides
            r = await client.post(
                f"/processes/{machine['id']}/instances/{iid}/advance",
                headers=_auth(viewer["token"]), json={"transition": "go"})
            assert r.status_code == 403, r.text
            assert "viewer" in r.json()["detail"], r.text

            # the owner advances from the surface's door - the journey
            # receipt names the human
            r = await client.post(
                f"/processes/{machine['id']}/instances/{iid}/advance",
                headers=oh, json={"transition": "go", "actor": "door-amara",
                                  "note": "picked it up"})
            assert r.status_code == 200, r.text
            assert r.json()["state"] == "b", r.text
            actors = [j.get("actor") for j in r.json().get("journey", [])]
            assert "door-amara" in actors, actors

            # still late, still on the list - but wearing state b's moves
            r = await client.get(f"/systems/by-domain/{domain}/work", headers=oh)
            row = r.json()["work"]["attention"][0]
            assert row["state"] == "b", row
            assert row["transitions"] == [{"name": "finish", "to": "done"}], row

            # the terminal move retires the row from the attention list
            r = await client.post(
                f"/processes/{machine['id']}/instances/{iid}/advance",
                headers=oh, json={"transition": "finish"})
            assert r.status_code == 200, r.text
            r = await client.get(f"/systems/by-domain/{domain}/work", headers=oh)
            assert r.json()["work"]["totals"]["attention"] == 0, r.text
            assert r.json()["work"]["attention"] == [], r.text

            # a state with no outgoing moves would wear an empty list -
            # the surface hides the control (pinned at the unit level via
            # _allowed_from, since the feed skips terminal rows by design)
            from app.services.business_processes import _allowed_from

            assert _allowed_from(MACHINE, "done") == []

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 2. per-domain TLS notes on the route sheet
# ---------------------------------------------------------------------------

def test_v106_tls_notes_on_the_route_sheet():
    async def _go():
        async with _client() as client:
            owner = await _mk_user(client, "sheet")
            oh = _auth(owner["token"])

            sys_ok = await _mk_system(client, oh, "Answering Ops v106")
            d_ok = f"ok{uuid.uuid4().hex[:6]}.acme.com"
            await _mk_live_deployment(client, sys_ok["id"], d_ok)
            await _stamp_probe(d_ok, ok=True, code=200, ms=123, detail="HTTP 200")

            sys_dead = await _mk_system(client, oh, "Dark Ops v106")
            d_dead = f"dead{uuid.uuid4().hex[:6]}.acme.com"
            await _mk_live_deployment(client, sys_dead["id"], d_dead)
            old = settings.deploy_ping_override
            settings.deploy_ping_override = f"http://127.0.0.1:{_closed_port()}"
            try:
                result = await _real_ping(sys_dead["id"])
                assert result["ok"] is False and result["code"] is None, result
            finally:
                settings.deploy_ping_override = old

            sys_stage = await _mk_system(client, oh, "Rehearsal Ops v106")
            d_stage = f"stage{uuid.uuid4().hex[:6]}.acme.com"
            await _mk_live_deployment(client, sys_stage["id"], d_stage,
                                      environment="staging")

            r = await client.get("/systems/deployment/routes.caddy", headers=oh)
            assert r.status_code == 200, r.text
            sheet = r.text

            # the header carries the TLS policy
            assert "# tls: every site block relies on Caddy's automatic HTTPS" in sheet, sheet

            # the answered domain: the certificate is live, with the numbers
            block_ok = sheet.split(f"{d_ok} {{")[1].split("\n}")[0]
            assert "certificate live: last probe answered HTTP 200 in 123 ms" in block_ok, block_ok
            assert f"rewrite * /go/{d_ok}" in block_ok  # the v104 contract intact

            # the failed domain: CHECK, with the probe's honest detail
            block_dead = sheet.split(f"{d_dead} {{")[1].split("\n}")[0]
            assert "CHECK THE CERTIFICATE: last probe failed" in block_dead, block_dead
            assert "Connect" in block_dead, block_dead

            # the staging domain: the rehearsal issuer + the honest unknown
            block_stage = sheet.split(f"{d_stage} {{")[1].split("\n}")[0]
            assert "staging - rehearse on the ACME staging issuer" in block_stage, block_stage
            assert "no probe evidence yet" in block_stage, block_stage

            # dark deployments stay comments-only (v104 unchanged)
            await client.post(f"/systems/{sys_stage['id']}/deployment/pause",
                              headers=oh)
            r = await client.get("/systems/deployment/routes.caddy", headers=oh)
            sheet = r.text
            live_block = sheet.split("# dark deployments")[0]
            assert f"{d_stage} {{" not in live_block, sheet
            assert d_stage in sheet  # still named, as a comment

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 3. the deployment liveness rides the surface
# ---------------------------------------------------------------------------

def test_v106_deployment_liveness_rides_the_surface():
    async def _go():
        async with _client() as client:
            owner = await _mk_user(client, "live")
            oh = _auth(owner["token"])

            system = await _mk_system(client, oh, "Watched Ops v106")
            sid = system["id"]
            domain = f"live{uuid.uuid4().hex[:6]}.acme.com"
            await _mk_live_deployment(client, sid, domain)
            machine = await _mk_machine(client, oh, "Quiet machine v106")
            await _bind(client, oh, sid, machine["id"])

            # never probed: an honest unknown, never a lying green
            r = await client.get(f"/systems/by-domain/{domain}/work", headers=oh)
            assert r.status_code == 200, r.text
            lv = r.json()["liveness"]
            assert lv["domain"] == domain and lv["status"] == "live", lv
            assert lv["environment"] == "production" and lv["last_ping"] is None, lv

            # the real probe door, honest failure: the surface wears it
            old = settings.deploy_ping_override
            settings.deploy_ping_override = f"http://127.0.0.1:{_closed_port()}"
            try:
                result = await _real_ping(sid)
                assert result["ok"] is False, result
            finally:
                settings.deploy_ping_override = old
            r = await client.get(f"/systems/by-domain/{domain}/work", headers=oh)
            lv = r.json()["liveness"]
            assert lv["last_ping"]["ok"] is False, lv
            assert "Connect" in lv["last_ping"]["detail"], lv
            assert lv["last_ping"]["code"] is None, lv

            # the evidence columns are the one truth: stamp a recovery and
            # the surface answers green with the probe's numbers
            await _stamp_probe(domain, ok=True, code=200, ms=87, detail="HTTP 200")
            r = await client.get(f"/systems/by-domain/{domain}/work", headers=oh)
            lv = r.json()["liveness"]
            assert lv["last_ping"]["ok"] is True and lv["last_ping"]["code"] == 200, lv
            assert lv["last_ping"]["ms"] == 87, lv

            # the estate's own read keeps the same evidence (one truth)
            r = await client.get(f"/systems/{sid}/deployment", headers=oh)
            assert r.json()["deployment"]["last_ping"]["ok"] is True, r.text

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 4. the pin
# ---------------------------------------------------------------------------

def test_v106_version_pin():
    assert settings.version == "1.107.0"
