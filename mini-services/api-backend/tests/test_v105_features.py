"""v105 tests - the system keys ride the process doors, and the front
door gains the system's own work surface.

The identity pillar's fourth stride: the credentials and the people of a
DEPLOYED system can actually operate it.

* SYSTEM KEYS ON THE PROCESS DOORS: a ``py8n_sys_`` key answers the
  machine doors - advance / ack / start / reads - for the machines bound
  to its system (SystemComponent kind=process), with the role its scopes
  spell (write -> editor, else viewer). The BOUNDARY holds at the doors:
  a key on an unbound (or nonexistent) machine looks nonexistent (404) -
  never the anonymous fall-through, because auth-off's anonymous is an
  owner and a machine credential must not inherit that. Its moves name
  the key (the journey receipt says ``key:<name>``). The estate-wide
  views (list / attention / chains / csv / chain-report / heatmap grid)
  are the OWNER's cross-machine picture: a key gets an honest 403. The
  advance door grew the instance-process cross-check every other
  instance door already kept - authority decided on one machine must
  never move another machine's entity through a mismatched path.
* THE SYSTEM'S PEOPLE: a human who is not the machine's owner may act
  when they hold editor+ (writes) / viewer+ (reads) on a system that
  BINDS the machine - the system's users operate the system's machines,
  the authority the front door's work surface stands on. A member below
  the move's role gets an honest 403; a stranger keeps the old behavior
  (a foreign machine looks nonexistent).
* THE FRONT DOOR'S WORK SURFACE: GET /systems/by-domain/{domain}/work -
  the live deployment resolves to the system's pending work: every bound
  machine with its live operation (open / stuck) and the attention rows
  (open instances past their SLA, most-overdue first, the escalation
  book per row). The system's key reads its own work (foreign key 404);
  the system's people read it with their token; enforced-mode anonymous
  is told to sign in (401); a signed-in stranger is honestly refused
  (403); auth-off's anonymous is the owner (platform convention); a
  paused deployment is dark (404).
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

MACHINE = {
    "states": ["a", "b", "done", "dead"],
    "initial": "a",
    "transitions": [
        {"name": "go", "from": "a", "to": "b"},
        {"name": "finish", "from": "b", "to": "done"},
        {"name": "kill", "from": "a", "to": "dead"},
    ],
}

KNOCKING_MACHINE = {**MACHINE, "escalation_policy": {
    "channel": "email", "to": "ops@py8n.test",
    "repeat_every_seconds": 60, "max_repeats": 5}}


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
    email = f"v105-{tag}-{uuid.uuid4().hex[:6]}@py8n.test"
    res = await client.post("/auth/register", json={
        "email": email,
        "password": "correct-horse-battery",
        "name": f"v105 {tag}",
    })
    assert res.status_code == 201, res.text
    return {"token": res.json()["token"], "id": res.json()["user"]["id"],
            "email": email}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _syskey(key: str) -> dict:
    return {"X-API-Key": key}


async def _mk_system(client: httpx.AsyncClient, headers: dict | None,
                     name: str, **extra) -> dict:
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


async def _knock(owner_id: str | None, *, now: datetime) -> dict:
    """The escalation door at the service level - the injectable clock."""
    from app.db import AsyncSessionLocal
    from app.services import business_processes as bp_svc

    async with AsyncSessionLocal() as session:
        report = await bp_svc.escalate_stuck(session, owner_id=owner_id,
                                             actor="test", now=now)
        await session.commit()
    return report


# ---------------------------------------------------------------------------
# 1. system keys ride the process doors
# ---------------------------------------------------------------------------

def test_v105_system_keys_ride_the_process_doors():
    async def _go():
        async with _client() as client:
            owner = await _mk_user(client, "keys")
            h = _auth(owner["token"])

            system = await _mk_system(client, h, "Acme Ops v105")
            sid = system["id"]
            r = await client.post(f"/systems/{sid}/keys", headers=h,
                                  json={"name": "ERP push job"})
            assert r.status_code == 201, r.text
            key = r.json()["key"]
            r = await client.post(f"/systems/{sid}/keys", headers=h,
                                  json={"name": "read-only eye",
                                        "scopes": ["read"]})
            assert r.status_code == 201, r.text
            ro_key = r.json()["key"]

            bound = await _mk_machine(client, h, "Lead pipeline v105")
            foreign = await _mk_machine(client, h, "Unbound machine v105")
            await _bind(client, h, sid, bound["id"])

            # the key starts work on its own machine - the company's
            # software pushes an entity in AS the system
            r = await client.post(f"/processes/{bound['id']}/instances",
                                  headers=_syskey(key),
                                  json={"ref": "ERP-1", "title": "pushed in",
                                        "due_in_seconds": 3600})
            assert r.status_code == 201, r.text
            iid = r.json()["id"]

            # the key advances the bound machine - the named door
            r = await client.post(
                f"/processes/{bound['id']}/instances/{iid}/advance",
                headers=_syskey(key), json={"transition": "go"})
            assert r.status_code == 200, r.text
            assert r.json()["state"] == "b", r.text

            # the receipt names the key, not a spoofable actor
            r = await client.get(
                f"/processes/{bound['id']}/instances/{iid}", headers=h)
            actors = [j.get("actor") for j in r.json().get("journey", [])]
            assert "key:ERP push job" in actors, actors

            # the key reads its own machine, but the unbound one is a 404
            r = await client.get(f"/processes/{bound['id']}", headers=_syskey(key))
            assert r.status_code == 200, r.text
            r = await client.get(f"/processes/{foreign['id']}", headers=_syskey(key))
            assert r.status_code == 404, r.text
            r = await client.get(f"/processes/{foreign['id']}/instances",
                                 headers=_syskey(key))
            assert r.status_code == 404, r.text

            # the read-only key may read, but the move refuses loud
            r = await client.get(f"/processes/{bound['id']}", headers=_syskey(ro_key))
            assert r.status_code == 200, r.text
            r = await client.post(
                f"/processes/{bound['id']}/instances/{iid}/advance",
                headers=_syskey(ro_key), json={"transition": "finish"})
            assert r.status_code == 403, r.text

            # the estate-wide views are human doors
            for door in ("/processes", "/processes/attention", "/processes/chains",
                         "/processes/escalation-history", "/processes/chain-report"):
                r = await client.get(door, headers=_syskey(key))
                assert r.status_code == 403, (door, r.text)

            # the mismatched path moves nothing: the cross-check every
            # other instance door keeps now holds for advance too
            r = await client.post(
                f"/processes/{foreign['id']}/instances/{iid}/advance",
                headers=h, json={"transition": "go"})
            assert r.status_code == 400, r.text

            # the knock + the key's ack - the machine voice takes the turn
            r = await client.post(f"/processes/{bound['id']}/instances",
                                  headers=h,
                                  json={"ref": "ACK-1", "title": "the late one",
                                        "due_in_seconds": 1})
            late_iid = r.json()["id"]
            base = datetime.now(timezone.utc)
            report = await _knock(owner["id"], now=base + timedelta(seconds=15))
            mine = [e for e in report["recorded"] if e["instance_id"] == late_iid]
            assert len(mine) == 1, report
            r = await client.post(
                f"/processes/{bound['id']}/instances/{late_iid}/escalations/ack",
                headers=_syskey(key),
                json={"by": "erp-job", "note": "the ERP took the turn"})
            assert r.status_code == 200, r.text
            assert r.json()["ack"]["by"] == "erp-job", r.text

            # and the door stays quiet for the key's own machine only:
            # the unbound machine's escalations are not the key's to take
            r = await client.post(f"/processes/{foreign['id']}/instances",
                                  headers=h,
                                  json={"ref": "F-1", "due_in_seconds": 1})
            f_iid = r.json()["id"]
            report = await _knock(owner["id"], now=base + timedelta(seconds=30))
            r = await client.post(
                f"/processes/{foreign['id']}/instances/{f_iid}/escalations/ack",
                headers=_syskey(key), json={"by": "erp-job"})
            assert r.status_code == 404, r.text

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 2. the system's people operate the system's machines
# ---------------------------------------------------------------------------

def test_v105_the_systems_people_operate_the_machines():
    async def _go():
        async with _client() as client:
            owner = await _mk_user(client, "owner")
            editor = await _mk_user(client, "editor")
            viewer = await _mk_user(client, "viewer")
            stranger = await _mk_user(client, "stranger")
            oh = _auth(owner["token"])

            system = await _mk_system(client, oh, "Acme Ops v105 people")
            sid = system["id"]
            r = await client.post(f"/systems/{sid}/members", headers=oh,
                                  json={"email": editor["email"], "role": "editor"})
            assert r.status_code in (200, 201), r.text
            r = await client.post(f"/systems/{sid}/members", headers=oh,
                                  json={"email": viewer["email"], "role": "viewer"})
            assert r.status_code in (200, 201), r.text

            bound = await _mk_machine(client, oh, "Pipeline v105 people")
            unbound = await _mk_machine(client, oh, "Unbound v105 people")
            await _bind(client, oh, sid, bound["id"])

            r = await client.post(f"/processes/{bound['id']}/instances",
                                  headers=oh,
                                  json={"ref": "P-1", "due_in_seconds": 3600})
            iid = r.json()["id"]

            # the EDITOR of the binding system moves the machine - even
            # though every row is owned by the creator
            r = await client.post(
                f"/processes/{bound['id']}/instances/{iid}/advance",
                headers=_auth(editor["token"]), json={"transition": "go"})
            assert r.status_code == 200, r.text

            # the VIEWER reads it, but the move refuses honestly
            r = await client.get(f"/processes/{bound['id']}",
                                 headers=_auth(viewer["token"]))
            assert r.status_code == 200, r.text
            r = await client.get(
                f"/processes/{bound['id']}/instances", headers=_auth(viewer["token"]))
            assert r.status_code == 200, r.text
            r = await client.post(
                f"/processes/{bound['id']}/instances/{iid}/advance",
                headers=_auth(viewer["token"]), json={"transition": "finish"})
            assert r.status_code == 403, r.text
            assert "viewer" in r.json()["detail"], r.text

            # a stranger keeps the old honesty: the reads hide the machine,
            # the move refuses as it always has (the row looks absent)
            r = await client.get(f"/processes/{bound['id']}",
                                 headers=_auth(stranger["token"]))
            assert r.status_code == 404, r.text
            r = await client.post(
                f"/processes/{bound['id']}/instances/{iid}/advance",
                headers=_auth(stranger["token"]), json={"transition": "finish"})
            assert r.status_code == 400, r.text
            assert "not found" in r.json()["detail"], r.text

            # membership on the system does not reach UNBOUND machines
            r = await client.get(f"/processes/{unbound['id']}",
                                 headers=_auth(editor["token"]))
            assert r.status_code == 404, r.text

            # the editor takes the escalation of a bound machine's entity
            r = await client.post(f"/processes/{bound['id']}/instances",
                                  headers=oh,
                                  json={"ref": "LATE-1", "due_in_seconds": 1})
            late_iid = r.json()["id"]
            base = datetime.now(timezone.utc)
            report = await _knock(owner["id"], now=base + timedelta(seconds=15))
            mine = [e for e in report["recorded"] if e["instance_id"] == late_iid]
            assert len(mine) == 1, report
            r = await client.post(
                f"/processes/{bound['id']}/instances/{late_iid}/escalations/ack",
                headers=_auth(editor["token"]), json={"by": "editor-amara"})
            assert r.status_code == 200, r.text
            # ... and the viewer's take refuses (the move needs editor)
            r = await client.post(f"/processes/{bound['id']}/instances",
                                  headers=oh,
                                  json={"ref": "LATE-2", "due_in_seconds": 1})
            late2_iid = r.json()["id"]
            report = await _knock(owner["id"], now=base + timedelta(seconds=30))
            r = await client.post(
                f"/processes/{bound['id']}/instances/{late2_iid}/escalations/ack",
                headers=_auth(viewer["token"]), json={"by": "viewer-sam"})
            assert r.status_code == 403, r.text

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 3. the front door gains the system's own work surface
# ---------------------------------------------------------------------------

def test_v105_the_front_door_gains_the_work_surface():
    async def _go():
        async with _client() as client:
            owner = await _mk_user(client, "door")
            stranger = await _mk_user(client, "door-stranger")
            oh = _auth(owner["token"])

            system = await _mk_system(client, oh, "Acme Ops v105 door")
            sid = system["id"]
            domain = f"work{uuid.uuid4().hex[:6]}.acme.com"
            await _mk_live_deployment(client, sid, domain)
            r = await client.post(f"/systems/{sid}/keys", headers=oh,
                                  json={"name": "door key"})
            key = r.json()["key"]
            other = await _mk_system(client, oh, "Other Ops v105")
            r = await client.post(f"/systems/{other['id']}/keys", headers=oh,
                                  json={"name": "other key"})
            other_key = r.json()["key"]

            late = await _mk_machine(client, oh, "Late machine v105",
                                     definition=KNOCKING_MACHINE)
            clean = await _mk_machine(client, oh, "Clean machine v105")
            away = await _mk_machine(client, oh, "Machine elsewhere v105")
            await _bind(client, oh, sid, late["id"])
            await _bind(client, oh, sid, clean["id"])
            # `away` is owned by the same operator but NOT this system's

            r = await client.post(f"/processes/{late['id']}/instances",
                                  headers=oh,
                                  json={"ref": "W-1", "title": "the late one",
                                        "due_in_seconds": 1})
            late_iid = r.json()["id"]
            r = await client.post(f"/processes/{away['id']}/instances",
                                  headers=oh,
                                  json={"ref": "W-2", "due_in_seconds": 1})
            away_iid = r.json()["id"]
            base = datetime.now(timezone.utc)
            report = await _knock(owner["id"], now=base + timedelta(seconds=15))
            # the estate sweep knocks BOTH overdue machines (they belong to
            # the same operator) - the work SURFACE is what must stay scoped
            assert any(e["instance_id"] == late_iid for e in report["recorded"]), report
            # the double-clock discipline: the knock rode the INJECTED
            # clock; the surface reads the REAL one - let the 1s SLA pass
            await asyncio.sleep(2.0)

            # the owner's token: the work surface carries the system's work
            r = await client.get(f"/systems/by-domain/{domain}/work", headers=oh)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["system"]["id"] == sid and body["domain"] == domain, body
            assert body["my_role"] == "owner", body
            work = body["work"]
            assert work["totals"]["machines"] == 2, work["totals"]
            assert work["totals"]["attention"] == 1, work["totals"]
            row = work["attention"][0]
            assert row["instance_id"] == late_iid and row["ref"] == "W-1", row
            assert row["overdue_seconds"] > 0 and row["escalation"]["count"] >= 1, row
            assert row["escalation"]["acked"] is None, row
            names = {m["name"] for m in work["machines"]}
            assert names == {"Late machine v105", "Clean machine v105"}, names

            # auth-off's anonymous caller is the owner (platform convention)
            r = await client.get(f"/systems/by-domain/{domain}/work")
            assert r.status_code == 200 and r.json()["my_role"] == "owner", r.text

            # the system's own key reads its work; the foreign key does not
            r = await client.get(f"/systems/by-domain/{domain}/work",
                                 headers=_syskey(key))
            assert r.status_code == 200 and r.json()["my_role"] == "editor", r.text
            r = await client.get(f"/systems/by-domain/{domain}/work",
                                 headers=_syskey(other_key))
            assert r.status_code == 404, r.text

            # a signed-in stranger is honestly refused
            r = await client.get(f"/systems/by-domain/{domain}/work",
                                 headers=_auth(stranger["token"]))
            assert r.status_code == 403, r.text

            # enforced mode: the anonymous caller is told to sign in
            settings.require_auth = True
            try:
                r = await client.get(f"/systems/by-domain/{domain}/work")
                assert r.status_code == 401, r.text
            finally:
                settings.require_auth = False

            # a paused deployment is dark - the work surface is not served
            r = await client.post(f"/systems/{sid}/deployment/pause", headers=oh)
            assert r.status_code == 200, r.text
            r = await client.get(f"/systems/by-domain/{domain}/work", headers=oh)
            assert r.status_code == 404, r.text
            r = await client.post(f"/systems/{sid}/deployment/deploy", headers=oh)
            assert r.status_code == 200, r.text

            # the ack from the work surface's own door: the owner takes it,
            # and the surface answers "taken by" on the next read
            r = await client.post(
                f"/processes/{late['id']}/instances/{late_iid}/escalations/ack",
                headers=oh, json={"by": "door-amara"})
            assert r.status_code == 200, r.text
            r = await client.get(f"/systems/by-domain/{domain}/work", headers=oh)
            row = r.json()["work"]["attention"][0]
            assert row["escalation"]["acked"]["by"] == "door-amara", row

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 4. the pin
# ---------------------------------------------------------------------------

def test_v105_version_pin():
    assert settings.version == "1.107.0"
