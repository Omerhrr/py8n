"""v95 tests - the cross-machine view: the chain map with per-leg
history (the operator-detail chain's live overlay) and the cross-machine
escalation heatmap.

v91-v94 gave the operator the boards (the attention feed, the live
chains on the installed system, the ack on the chain nodes, the
per-machine sparkline). v95 adds the two views that were still missing:

* THE CHAIN MAP: GET /processes/chains derives the owner-wide chains
  from what is INSTALLED (every definition's journeys becomes a leg,
  resolved by the fire's own resolution - an uninstalled partner shows
  as an honest pending leg, never hidden). The shelf's three canonical
  threads keep their names; every node carries the machine's live
  operation (open / stuck / the systems binding it); every leg carries
  the ride counts (opened / open_now / stuck) and the recent traversals
  - the HISTORY the operator-detail chain overlays: ref, title, where
  the item is now, when the leg opened it, what the door knows.
* THE CROSS-MACHINE ESCALATION HEATMAP: GET
  /processes/escalation-history reads the door's history off the
  transition log - per machine, per day, escalations / digests / acks
  - the estate-wide grid (v94's sparkline is per-machine inside the
  analytics; this is the machines-x-days view beside it).
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402

API = "http://testserver/api/v1"

MACHINE = {
    "states": ["a", "b", "done"],
    "initial": "a",
    "transitions": [{"name": "go", "from": "a", "to": "b"},
                    {"name": "finish", "from": "b", "to": "done"}],
}


@pytest.fixture(autouse=True)
def _clean_event_subscribers():
    from app.services import system_events as events_svc

    events_svc._subscribers.clear()
    yield
    events_svc._subscribers.clear()


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
    res = await client.post("/auth/register", json={
        "email": f"v95-{tag}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v95 {tag}",
    })
    assert res.status_code == 201, res.text
    return {"token": res.json()["token"], "id": res.json()["user"]["id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _door(user_id: str, *, now: datetime | None = None) -> dict:
    from app.db import AsyncSessionLocal
    from app.services import business_processes as bp_svc

    async with AsyncSessionLocal() as session:
        report = await bp_svc.escalate_stuck(session, owner_id=user_id,
                                             actor="test", now=now)
        await session.commit()
    return report


# ---------------------------------------------------------------------------
# 1. the chain map - chains derived from what is installed, with history
# ---------------------------------------------------------------------------

def test_v95_chain_map_and_leg_history():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "chains")
            h = _auth(user["token"])

            # nothing installed: an honest empty map
            res = await client.get("/processes/chains", headers=h)
            assert res.status_code == 200, res.text
            assert res.json()["chains"] == [] and res.json()["nodes"] == {}, res.json()

            # install revenue chain's three departments
            for slug in ("operations-operator", "sales-operator",
                         "finance-operator"):
                res = await client.post(f"/operators/{slug}/install",
                                        headers=h, json={})
                assert res.status_code == 200, res.text

            res = await client.get("/processes/chains", headers=h)
            out = res.json()
            names = [c["name"] for c in out["chains"]]
            assert "Revenue chain" in names, names
            revenue = next(c for c in out["chains"] if c["name"] == "Revenue chain")
            assert revenue["head_name"] == "Lead pipeline", revenue
            legs = revenue["legs"]
            assert [(l["from_name"], l["on_state"], l["to_name"])
                    for l in legs] == [
                ("Lead pipeline", "won", "Customer onboarding"),
                ("Customer onboarding", "handed_off", "Invoice lifecycle")], legs
            # the shelf SLA rides the leg (v90's due_in_seconds)
            assert legs[1]["due_in_seconds"] == 5 * 24 * 3600, legs
            # nodes carry live counts + the systems binding them
            assert out["nodes"] and all(
                isinstance(n.get("open"), int) and isinstance(n.get("systems"), list)
                for n in out["nodes"].values()), out["nodes"]

            # a fresh lead WINS - the leg opens the onboarding case
            procs = {p["name"]: p["id"] for p in
                     (await client.get("/processes", headers=h)).json()["processes"]}
            lead_pid = procs["Lead pipeline"]
            res = await client.post(f"/processes/{lead_pid}/instances",
                                    headers=h, json={"ref": "+15557770101",
                                                     "title": "Heat Deal"})
            iid = res.json()["id"]
            for move in ("reach_out", "qualify", "book_demo", "run_demo",
                         "send_proposal", "negotiate", "win"):
                res = await client.post(
                    f"/processes/{lead_pid}/instances/{iid}/advance",
                    headers=h, json={"transition": move})
                assert res.status_code == 200, res.text

            # the leg's HISTORY names the traversal - ref, where it is now
            res = await client.get("/processes/chains", headers=h)
            revenue = next(c for c in res.json()["chains"]
                           if c["name"] == "Revenue chain")
            leg1 = revenue["legs"][0]
            assert leg1["opened"] == 1 and leg1["open_now"] == 1, leg1
            hist = leg1["history"]
            assert hist and hist[0]["ref"] == "+15557770101", hist
            assert hist[0]["state"] == "kickoff", hist
            assert hist[0]["opened_at"], hist
            assert hist[0]["is_stuck"] is False, hist
            # the source node's live counts moved
            assert res.json()["nodes"][lead_pid]["open"] >= 1

            # the clinic joins mid-session - the Care chain appears
            res = await client.post("/operators/clinic-operator/install",
                                    headers=h, json={})
            assert res.status_code == 200, res.text
            res = await client.get("/processes/chains", headers=h)
            names = [c["name"] for c in res.json()["chains"]]
            assert "Care chain" in names, names

            # a machine with a journey whose partner is NOT installed:
            # the honest pending leg naming the missing machine
            res = await client.post("/processes", headers=h, json={
                "name": "Lone machine",
                "definition": {**MACHINE, "journeys": [
                    {"on_state": "b",
                     "open": {"process": "Ghost machine"}}]}})
            assert res.status_code == 201, res.text
            res = await client.get("/processes/chains", headers=h)
            lone = next(c for c in res.json()["chains"]
                        if c["head_name"] == "Lone machine")
            assert lone["legs"][0]["resolved"] is False, lone
            assert lone["legs"][0]["to_name"] == "Ghost machine", lone

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 2. the cross-machine escalation heatmap - per machine, per day, off the log
# ---------------------------------------------------------------------------

def test_v95_escalation_history_grid():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "heatmap")
            h = _auth(user["token"])

            res = await client.get("/processes/escalation-history", headers=h)
            assert res.status_code == 200, res.text
            assert res.json()["machines"] == [] and res.json()["days_count"] == 14, res.json()

            res = await client.post("/processes", headers=h, json={
                "name": "Grid machine",
                "definition": {**MACHINE, "escalation_policy": {
                    "channel": "", "to": "",
                    "repeat_every_seconds": 60, "max_repeats": 3}}})
            pid = res.json()["id"]
            for ref in ("G-1", "G-2"):
                res = await client.post(f"/processes/{pid}/instances", headers=h,
                                        json={"ref": ref, "due_in_seconds": 1})
                assert res.status_code == 201, res.text
            base = datetime.now(timezone.utc)
            await _door(user["id"], now=base + timedelta(seconds=10))
            # one human ack lands in the grid too
            res = await client.get(f"/processes/{pid}/instances", headers=h)
            iid = res.json()["instances"][0]["id"]
            res = await client.post(
                f"/processes/{pid}/instances/{iid}/escalations/ack",
                headers=h, json={"by": "raf"})
            assert res.status_code == 200, res.text

            res = await client.get("/processes/escalation-history", headers=h)
            out = res.json()
            assert len(out["days"]) == 14, out
            today = datetime.now(timezone.utc).date().isoformat()
            assert out["days"][-1] == today, out
            machines = out["machines"]
            assert len(machines) == 1 and machines[0]["name"] == "Grid machine", machines
            cell = machines[0]["cells"][today]
            assert cell["escalations"] == 2, cell  # both instances knocked
            assert cell["acks"] == 1, cell
            assert machines[0]["totals"]["escalations"] == 2, machines
            assert machines[0]["totals"]["acks"] == 1, machines

            # the window is honest: a 1-day grid still holds today's cell
            res = await client.get("/processes/escalation-history", headers=h,
                                   params={"days": 1})
            out1 = res.json()
            assert out1["days_count"] == 1 and len(out1["days"]) == 1, out1
            assert out1["machines"][0]["cells"][today]["escalations"] == 2, out1

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 3. version pin
# ---------------------------------------------------------------------------

def test_v95_version_pin():
    assert settings.version == "1.107.0"
