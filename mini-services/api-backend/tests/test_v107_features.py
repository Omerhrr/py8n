"""v107 tests - pending-update chips on the estate rows, the ping rhythm
on the deployment panel, and per-machine views beneath the attention
list.

* PENDING-UPDATE CHIPS ON THE ESTATE ROWS: the estate health overview's
  rows now say WHICH systems hold an applied-but-unruled upgrade - the
  same predicate the Updates panel's PENDING banner applies
  (``_pending_from_op``), answered for the whole estate in ONE batched
  query (``pending_updates_batch``), so the estate row and the panel can
  never disagree. A settled system (nothing pending, or the ruling
  accepted) wears no chip - the absence is honest.
* THE PING RHYTHM ON THE DEPLOYMENT PANEL: the deployment projection
  carries ``ping_rhythm`` - the cadence the scheduled walk re-probes
  this domain on (the SAME ``_clamp_interval`` the walk obeys - zero
  drift), humanized ("10m"), and when the NEXT probe falls due (a live
  domain: last probe + interval; never-probed: due NOW - an unprobed
  live door is exactly the one the walk wants to hear from first; a
  dark door: not scheduled). The front door's liveness strip wears the
  same numbers (one writer).
* PER-MACHINE VIEWS BENEATH THE ATTENTION LIST: the work surface's
  machines now carry their own view - ``instances`` (the open work on
  THAT machine's board: the stuck rising to the top, the rest
  newest-first, capped at 8 with ``instances_hidden`` naming what did
  not fit) - so a department sees its pending rows without waiting for
  an SLA breach. Terminal moves retire rows from the machine view just
  like they retire them from the attention list.
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
    email = f"v107-{tag}-{uuid.uuid4().hex[:6]}@py8n.test"
    res = await client.post("/auth/register", json={
        "email": email,
        "password": "correct-horse-battery",
        "name": f"v107 {tag}",
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


def _node(nid: str, ntype: str, params: dict | None = None) -> dict:
    return {"id": nid, "type": ntype, "name": nid,
            "position": {"x": 0, "y": 0}, "parameters": params or {}}


def _pack(workflows: list[tuple[str, str]]) -> dict:
    """A minimal honest pack - each entry (name, node type) becomes a
    bound workflow on install."""
    return {
        "format": "py8n-pack",
        "pack_version": 1,
        "workflows": [
            {"name": name, "active": False,
             "graph": {"nodes": [_node("t1", ntype)], "edges": []}}
            for name, ntype in workflows
        ],
        "datasets": [],
    }


def _db():
    from app.db import AsyncSessionLocal

    return AsyncSessionLocal()


# ---------------------------------------------------------------------------
# 1. pending-update chips on the estate rows
# ---------------------------------------------------------------------------

def test_v107_pending_update_chips_on_the_estate_rows():
    async def _go():
        from sqlalchemy import select

        from app.models import Solution

        async with _client() as client:
            owner = await _mk_user(client, "estate")
            oh = _auth(owner["token"])

            # a quiet system: nothing pending, the chip is honestly absent
            quiet = await _mk_system(client, oh, "Quiet Ops v107")

            # a system installed from a solution whose pack we control
            async with _db() as db:
                sol = Solution(
                    slug=f"v107-ops-suite-{uuid.uuid4().hex[:6]}",
                    name="v107 Ops Suite", tagline="", category="Operations",
                    outcomes_json=["one workflow"], docs="",
                    pack_json=_pack([("v107 Handler", "manual_trigger")]),
                    owner_id=owner["id"])
                db.add(sol)
                await db.commit()
                slug = sol.slug
            res = await client.post(f"/solutions/{slug}/install", headers=oh,
                                    json={"as_system": True})
            assert res.status_code == 200, res.text
            sid = res.json()["system"]["id"]

            rows = (await client.get("/systems/health/overview", headers=oh)) \
                .json()["systems"]
            by_id = {r["id"]: r for r in rows}
            assert by_id[quiet["id"]]["pending_update"] is None, by_id[quiet["id"]]
            assert by_id[sid]["pending_update"] is None, by_id[sid]

            # the pack GROWS, the upgrade applies the new binding - the
            # changeset sits on the table and the ESTATE ROW says so
            async with _db() as db:
                row = (await db.execute(
                    select(Solution).where(Solution.slug == slug))).scalar_one()
                row.pack_json = _pack([("v107 Handler", "manual_trigger"),
                                       ("v107 Extra", "manual_trigger")])
                await db.commit()
            res = await client.post(f"/systems/{sid}/upgrade", headers=oh)
            assert res.status_code == 200, res.text

            rows = (await client.get("/systems/health/overview", headers=oh)) \
                .json()["systems"]
            by_id = {r["id"]: r for r in rows}
            chip = by_id[sid]["pending_update"]
            assert chip is not None, by_id[sid]
            assert chip["added_total"] == 1, chip
            assert chip["operation_id"], chip
            assert by_id[quiet["id"]]["pending_update"] is None, by_id[quiet["id"]]

            # the SAME answer the Updates panel applies (one predicate)
            prev = (await client.get(f"/systems/{sid}/update/preview",
                                     headers=oh)).json()
            assert prev["pending"] is not None, prev
            assert prev["pending"]["operation_id"] == chip["operation_id"], prev

            # the human rules - the chip leaves the estate with the ruling
            res = await client.post(f"/systems/{sid}/update/accept", headers=oh)
            assert res.status_code == 200, res.text
            rows = (await client.get("/systems/health/overview", headers=oh)) \
                .json()["systems"]
            by_id = {r["id"]: r for r in rows}
            assert by_id[sid]["pending_update"] is None, by_id[sid]

            # the batched scan and the single read agree (one predicate)
            from app.services import system_runtime

            async with _db() as db:
                batch = await system_runtime.pending_updates_batch(
                    db, [sid, quiet["id"]])
            assert batch[sid] is None and batch[quiet["id"]] is None, batch

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 2. the ping rhythm on the deployment panel
# ---------------------------------------------------------------------------

def test_v107_ping_rhythm_on_the_deployment_panel():
    async def _go():
        async with _client() as client:
            owner = await _mk_user(client, "rhythm")
            oh = _auth(owner["token"])

            live_sys = await _mk_system(client, oh, "Watched Ops v107")
            domain = f"rhythm{uuid.uuid4().hex[:6]}.acme.com"
            await _mk_live_deployment(client, live_sys["id"], domain)

            # a LIVE deployment: the rhythm is named - the default walk
            # re-probes every 600s, and a never-probed live door is due NOW
            r = await client.get(f"/systems/{live_sys['id']}/deployment",
                                 headers=oh)
            assert r.status_code == 200, r.text
            dep = r.json()["deployment"]
            rhythm = dep["ping_rhythm"]
            assert rhythm is not None, dep
            assert rhythm["interval_seconds"] == 600, rhythm
            assert rhythm["every"] == "10m", rhythm
            assert rhythm["scheduled"] is True, rhythm
            assert rhythm["next_due_at"] is not None, rhythm

            # a dark (offline) door: the walk does not probe it - the panel
            # says the rhythm, never a lying due time
            dark_sys = await _mk_system(client, oh, "Dark Ops v107")
            r = await client.put(f"/systems/{dark_sys['id']}/deployment",
                                 json={"domain": f"dark{uuid.uuid4().hex[:6]}.acme.com"})
            assert r.status_code == 200, r.text
            dep_dark = r.json()["deployment"]
            assert dep_dark["status"] == "offline", dep_dark
            assert dep_dark["ping_rhythm"]["scheduled"] is False, dep_dark
            assert dep_dark["ping_rhythm"]["next_due_at"] is None, dep_dark
            assert dep_dark["ping_rhythm"]["every"] == "10m", dep_dark

            # the cadence the walk obeys and the cadence the panel reads
            # are ONE number: clamp the setting and the projection follows
            old = settings.deploy_ping_interval_seconds
            try:
                settings.deploy_ping_interval_seconds = 45  # below the floor
                r = await client.get(f"/systems/{live_sys['id']}/deployment",
                                     headers=oh)
                rhythm = r.json()["deployment"]["ping_rhythm"]
                assert rhythm["interval_seconds"] == 60, rhythm
                assert rhythm["every"] == "1m", rhythm

                # the next due time is the LAST PROBE + interval (one truth
                # with the walk's own arithmetic)
                from datetime import datetime, timedelta, timezone

                from app.db import AsyncSessionLocal
                from app.services import system_deployment as deploy_svc

                async with AsyncSessionLocal() as session:
                    row = await deploy_svc.get_by_domain(session, domain)
                    assert row is not None
                    stamp = datetime.now(timezone.utc) - timedelta(seconds=120)
                    row.last_ping_at = stamp
                    row.last_ping_ok = True
                    row.last_ping_code = 200
                    row.last_ping_ms = 42
                    row.last_ping_detail = "HTTP 200"
                    await session.commit()
                    out = deploy_svc.deployment_out(row)
                    rhythm = out["ping_rhythm"]
                    expected = (stamp + timedelta(seconds=60)).isoformat()
                    assert rhythm["next_due_at"] == expected, rhythm

                    # the front door's liveness strip wears the SAME rhythm
                    lv = deploy_svc.liveness(row)
                    assert lv["ping_rhythm"]["every"] == "1m", lv
                    assert lv["ping_rhythm"]["next_due_at"] == expected, lv
            finally:
                settings.deploy_ping_interval_seconds = old

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 3. per-machine views beneath the attention list
# ---------------------------------------------------------------------------

def test_v107_per_machine_views_beneath_the_attention_list():
    async def _go():
        async with _client() as client:
            owner = await _mk_user(client, "boards")
            oh = _auth(owner["token"])

            system = await _mk_system(client, oh, "Acme Ops v107")
            sid = system["id"]
            domain = f"view{uuid.uuid4().hex[:6]}.acme.com"
            await _mk_live_deployment(client, sid, domain)

            machine_a = await _mk_machine(client, oh, "Front desk v107")
            machine_b = await _mk_machine(client, oh, "Back office v107")
            await _bind(client, oh, sid, machine_a["id"])
            await _bind(client, oh, sid, machine_b["id"])

            # machine A: one late row (1s SLA), one on the clock (1h SLA)
            r = await client.post(f"/processes/{machine_a['id']}/instances",
                                  headers=oh,
                                  json={"ref": "V107-1", "title": "the late one",
                                        "due_in_seconds": 1})
            assert r.status_code == 201, r.text
            late_id = r.json()["id"]
            r = await client.post(f"/processes/{machine_a['id']}/instances",
                                  headers=oh,
                                  json={"ref": "V107-2", "title": "the calm one",
                                        "due_in_seconds": 3600})
            assert r.status_code == 201, r.text
            calm_id = r.json()["id"]
            # machine B: one open row with no SLA promise - it still shows
            # up on ITS board (a view, not a second attention list)
            r = await client.post(f"/processes/{machine_b['id']}/instances",
                                  headers=oh, json={"ref": "V107-3"})
            assert r.status_code == 201, r.text
            b_id = r.json()["id"]

            # the double-clock discipline (v96): the surface reads the
            # REAL clock - let the 1s SLA pass before reading
            await asyncio.sleep(2.0)

            r = await client.get(f"/systems/by-domain/{domain}/work", headers=oh)
            assert r.status_code == 200, r.text
            work = r.json()["work"]
            by_machine = {m["process_id"]: m for m in work["machines"]}

            # machine A's view: BOTH open rows, the stuck one on top
            va = by_machine[machine_a["id"]]
            assert va["open"] == 2 and va["stuck"] == 1, va
            assert [i["instance_id"] for i in va["instances"]] == [late_id, calm_id], va
            top = va["instances"][0]
            assert top["ref"] == "V107-1" and top["stuck"] is True, top
            assert top["overdue_seconds"] > 0 and top["state"] == "a", top
            assert va["instances"][1]["stuck"] is False, va
            assert va["instances"][1]["overdue_seconds"] == 0, va
            assert va["instances_hidden"] == 0, va

            # machine B's view: its own row, honestly not stuck
            vb = by_machine[machine_b["id"]]
            assert vb["open"] == 1 and vb["stuck"] == 0, vb
            assert [i["instance_id"] for i in vb["instances"]] == [b_id], vb
            assert vb["instances"][0]["stuck"] is False, vb

            # the attention list is UNCHANGED by the views: still exactly
            # the past-SLA row (a view is not a second attention list)
            assert work["totals"]["attention"] == 1, work["totals"]
            assert [row["instance_id"] for row in work["attention"]] == [late_id]

            # the terminal move retires the row from the machine view too
            r = await client.post(
                f"/processes/{machine_a['id']}/instances/{late_id}/advance",
                headers=oh, json={"transition": "go"})
            assert r.status_code == 200, r.text
            r = await client.post(
                f"/processes/{machine_a['id']}/instances/{late_id}/advance",
                headers=oh, json={"transition": "finish"})
            assert r.status_code == 200, r.text
            r = await client.get(f"/systems/by-domain/{domain}/work", headers=oh)
            work = r.json()["work"]
            by_machine = {m["process_id"]: m for m in work["machines"]}
            va = by_machine[machine_a["id"]]
            assert [i["instance_id"] for i in va["instances"]] == [calm_id], va
            assert va["stuck"] == 0, va
            assert work["totals"]["attention"] == 0, work["totals"]

            # the cap is honest: 10 open rows on one machine show 8, and
            # the view NAMES what did not fit
            for n in range(3, 13):
                r = await client.post(f"/processes/{machine_b['id']}/instances",
                                      headers=oh, json={"ref": f"V107-B{n}"})
                assert r.status_code == 201, r.text
            r = await client.get(f"/systems/by-domain/{domain}/work", headers=oh)
            vb = {m["process_id"]: m for m in r.json()["work"]["machines"]}[machine_b["id"]]
            assert len(vb["instances"]) == 8, len(vb["instances"])
            assert vb["instances_hidden"] == vb["open"] - 8, vb
            assert all(i["stuck"] is False for i in vb["instances"]), vb

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 4. the pin
# ---------------------------------------------------------------------------

def test_v107_version_pin():
    assert settings.version == "1.107.0"
