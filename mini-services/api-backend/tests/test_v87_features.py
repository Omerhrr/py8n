"""v87 tests - the escalation policy (channel + repeat) + business_query +
the Meeting/Clinic machines.

v85's door escalates a stuck instance once per state stint, on the
record, through the machine's own escalate move (or a no-move row) - but
it cannot TELL anyone and it cannot repeat on a cadence. v87 adds the
POLICY layer: an escalation_policy rides the definition (validated
loudly), the door gates repeats through the episode bookkeeping
(attempt 1, then one per repeat_every_seconds until 1 + max_repeats, a
state change starting a fresh episode), delivers over the policy's
channel through the owner's ChannelEndpoint (an honest skip IS a
result) and lands business.escalated beside the door's business.stuck.

business_query is the agents' read door: the running entities flow into
workflows (by process / state / ref / stuck-ness, owner-scoped).

The Meeting and Clinic operators ship their machines pre-wired (the last
two packs without one): Meeting lifecycle seeded at the ended-meeting
stage from the notes, Appointment journey seeded per patient with the
phone as ref - every operator machine now carries a policy.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.config import settings
from app.main import app

API = "http://testserver/api/v1"

# no escalate self-loop - the door records the no-move row; the policy
# layer adds the delivery + the repeat cadence on top
PLAIN_MACHINE = {
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
        "email": f"v87-{tag}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v87 {tag}",
    })
    assert res.status_code == 201, res.text
    return {"token": res.json()["token"], "id": res.json()["user"]["id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _door(user_id: str, *, now: datetime | None = None) -> dict:
    """The escalation door at the service level - the injectable clock."""
    from app.db import AsyncSessionLocal
    from app.services import business_processes as bp_svc

    async with AsyncSessionLocal() as session:
        report = await bp_svc.escalate_stuck(session, owner_id=user_id,
                                             actor="test", now=now)
        await session.commit()
    return report


# ---------------------------------------------------------------------------
# 1. the policy validates - loud refusals, honest defaults, surfaced
# ---------------------------------------------------------------------------

def test_v87_policy_validation():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "policy")
            h = _auth(user["token"])

            async def _post(policy):
                return await client.post("/processes", headers=h, json={
                    "name": "policy probe",
                    "definition": {**PLAIN_MACHINE, "escalation_policy": policy}})

            r = await _post({"channel": "fax"})
            assert r.status_code == 400 and "not a deliverable channel" in r.json()["detail"]
            assert "email" in r.json()["detail"]  # names the known channels

            r = await _post({"channel": "email", "repeat_every_seconds": 10})
            assert r.status_code == 400 and "must be >= 60" in r.json()["detail"]

            r = await _post({"channel": "email", "max_repeats": -1})
            assert r.status_code == 400 and "max_repeats must be >= 0" in r.json()["detail"]

            r = await _post({"channel": "email", "explode": True})
            assert r.status_code == 400 and "unknown key" in r.json()["detail"]

            # a valid policy round-trips: normalized, defaults filled
            r = await client.post("/processes", headers=h, json={
                "name": "Escalated machine",
                "definition": {**PLAIN_MACHINE, "escalation_policy": {
                    "channel": "email", "to": "ops@py8n.test"}}})
            assert r.status_code == 201, r.text
            p = r.json()
            pol = p["escalation_policy"]
            assert pol["channel"] == "email" and pol["to"] == "ops@py8n.test"
            assert pol["repeat_every_seconds"] == 3600 and pol["max_repeats"] == 3
            assert "{ref}" in pol["message_template"]
            assert "email" in p["escalation_summary"] and "x4" in p["escalation_summary"]

            # no policy stays none
            r = await client.post("/processes", headers=h, json={
                "name": "Plain machine", "definition": PLAIN_MACHINE})
            assert r.status_code == 201
            assert r.json()["escalation_policy"] is None
            assert r.json()["escalation_summary"] == "no escalation policy"

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 2. the episode - deliver, repeat, hold (injectable clock), the v85
#    semantics intact for machines without a policy
# ---------------------------------------------------------------------------

def test_v87_escalation_episode_channel_and_repeat():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "episode")
            h = _auth(user["token"])

            # a policy machine (no escalate move - the door records the
            # no-move row; the policy delivers + repeats on cadence)
            res = await client.post("/processes", headers=h, json={
                "name": "Episode machine",
                "definition": {**PLAIN_MACHINE, "escalation_policy": {
                    "channel": "email", "to": "ops@py8n.test",
                    "repeat_every_seconds": 60, "max_repeats": 1}}})
            pid = res.json()["id"]
            res = await client.post(f"/processes/{pid}/instances", headers=h,
                                    json={"ref": "EP-1", "title": "the stuck one",
                                          "due_in_seconds": 1})
            iid = res.json()["id"]

            # a NO-policy machine beside it: one knock per stint, no channel
            res = await client.post("/processes", headers=h, json={
                "name": "Quiet machine", "definition": PLAIN_MACHINE})
            qpid = res.json()["id"]
            res = await client.post(f"/processes/{qpid}/instances", headers=h,
                                    json={"ref": "QT-1", "due_in_seconds": 1})
            qiid = res.json()["id"]

            base = datetime.now(timezone.utc)
            t1 = base + timedelta(seconds=15)

            # attempt 1: on the record (no-move row) + told (business.escalated)
            report = await _door(user["id"], now=t1)
            assert report["stuck"] == 2, report
            assert len(report["recorded"]) == 2, report  # neither machine has the move
            mine = next(e for e in report["recorded"] if e["instance_id"] == iid)
            assert mine["attempt"] == 1 and mine["delivery"] == "skipped", mine
            assert "endpoint" in mine["delivery_detail"], mine  # honest skip: no email endpoint
            theirs = next(e for e in report["recorded"] if e["instance_id"] == qiid)
            assert "attempt" not in theirs  # no policy - no delivery layer

            # the episode is remembered on the instance's running memory
            res = await client.get(f"/processes/{pid}/instances/{iid}", headers=h)
            book = res.json()["context"]["escalations"]
            assert book["count"] == 1 and book["state"] == "a"
            assert book["last_delivery"] == "skipped"

            # the events: business.stuck (the door) + business.escalated (the policy)
            res = await client.get("/events", headers=h,
                                   params={"type": "business.escalated",
                                           "correlation_id": iid})
            escalations_events = res.json()["events"]
            assert len(escalations_events) == 1
            ev = escalations_events[0]
            assert ev["payload"]["attempt"] == 1 and ev["payload"]["delivery"] == "skipped"
            assert ev["payload"]["moved_to"] is None  # the no-move path

            # too soon: 30s later the 60s cadence holds the door
            report = await _door(user["id"], now=base + timedelta(seconds=45))
            held = [x for x in report["held"] if x["instance_id"] == iid]
            assert held and held[0]["reason"] == "too_soon", report
            # ...and the no-policy machine is NOT held again (once per stint)
            assert report["already"] >= 1, report

            # repeat: 65s after the attempt -> attempt 2 (max_repeats=1 reached)
            report = await _door(user["id"], now=base + timedelta(seconds=80))
            mine = [e for e in report["recorded"] if e["instance_id"] == iid]
            assert len(mine) == 1 and mine[0]["attempt"] == 2, report

            # episode complete: the door holds - no more noise
            report = await _door(user["id"], now=base + timedelta(seconds=600))
            held = [x for x in report["held"] if x["instance_id"] == iid]
            assert held and held[0]["reason"] == "episode_complete", report
            assert held[0]["attempts"] == 2

            # the timeline is the record: exactly 2 escalated events
            res = await client.get("/events", headers=h,
                                   params={"type": "business.escalated",
                                           "correlation_id": iid})
            assert len(res.json()["events"]) == 2

            # a state change starts a FRESH episode
            res = await client.post(f"/processes/{pid}/instances/{iid}/advance",
                                    headers=h, json={"transition": "go"})
            assert res.status_code == 200
            report = await _door(user["id"], now=base + timedelta(seconds=700))
            fresh = [e for e in report["recorded"] if e["instance_id"] == iid]
            assert len(fresh) == 1 and fresh[0]["attempt"] == 1, report

            # delivery through a BOUND endpoint without credentials still
            # skips honestly - naming exactly what is missing
            res = await client.post("/channels/endpoints", headers=h, json={
                "name": "SMS gateway", "provider": "generic_sms",
                "config": {"secret": "verify-me"}})
            assert res.status_code == 201, res.text
            res = await client.post(f"/processes/{pid}/instances", headers=h,
                                    json={"ref": "EP-2", "title": "sms one",
                                          "context": {}, "due_in_seconds": 1})
            iid2 = res.json()["id"]
            # give EP-2 its own machine? no - the policy is on the process;
            # EP-2 rides the SAME email policy. Bind an EMAIL-ish skip: the
            # endpoint above is sms - the email skip stays the honest path.
            report = await _door(user["id"], now=base + timedelta(seconds=800))
            entry = next(e for e in report["recorded"] if e["instance_id"] == iid2)
            assert entry["attempt"] == 1 and entry["delivery"] == "skipped"

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 3. business_query - the agents' read door onto the operation
# ---------------------------------------------------------------------------

def test_v87_business_query_node():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "query")
            h = _auth(user["token"])

            # the node ships in the registry (65 node types)
            res = await client.get("/node-definitions", headers=h)
            types = {d["type"] for d in res.json()["definitions"]}
            assert {"business_query", "business_advance", "business_start"} <= types

            res = await client.post("/processes", headers=h, json={
                "name": "Query demo", "definition": PLAIN_MACHINE})
            pid = res.json()["id"]
            await client.post(f"/processes/{pid}/instances", headers=h,
                              json={"ref": "Q-1", "title": "the moving one"})
            res = await client.post(f"/processes/{pid}/instances", headers=h,
                                    json={"ref": "Q-2", "title": "the finished one"})
            iid2 = res.json()["id"]
            await client.post(f"/processes/{pid}/instances/{iid2}/advance",
                              headers=h, json={"transition": "kill"})

            graph = {"nodes": [
                {"id": "t", "type": "manual_trigger", "name": "Trigger",
                 "position": {"x": 0, "y": 0}, "parameters": {}},
                {"id": "q", "type": "business_query", "name": "Read the op",
                 "position": {"x": 1, "y": 0},
                 "parameters": {"process": "Query demo"}},
            ], "edges": [
                {"id": "e1", "source": "t", "target": "q",
                 "sourceHandle": "main", "targetHandle": "main"},
            ]}
            res = await client.post("/workflows", headers=h, json={
                "name": "Agent reads the operation", "graph": graph,
                "is_active": False})
            assert res.status_code == 201, res.text
            wf_id = res.json()["id"]

            res = await client.post(f"/workflows/{wf_id}/run", headers=h,
                                    json={"payload": {}})
            assert res.status_code in (200, 202), res.text
            exec_id = res.json()["execution_id"]
            detail = {}
            for _ in range(80):
                detail = (await client.get(f"/executions/{exec_id}", headers=h)).json()
                if detail.get("status") != "running":
                    break
                await asyncio.sleep(0.05)
            assert detail.get("status") == "success", detail
            runs = {r["node_id"]: r for r in detail["node_runs"]}
            out = runs["q"]["output"]
            assert out["count"] == 1 and out["process"] == "Query demo", out
            inst = out["instances"][0]
            assert inst["ref"] == "Q-1" and inst["process_name"] == "Query demo"
            assert inst["is_terminal"] is False

            # stuck_only + a state filter at the service level
            from app.db import AsyncSessionLocal
            from app.services import business_processes as bp_svc

            async with AsyncSessionLocal() as session:
                out = await bp_svc.query_instances(
                    session, owner_id=user["id"], process="Query demo",
                    state="a")
            assert out["count"] == 1 and out["instances"][0]["ref"] == "Q-1"

            # owner hiding: a stranger's NAMED query refuses loud, an
            # unnamed one reads an empty operation
            async with AsyncSessionLocal() as session:
                try:
                    await bp_svc.query_instances(
                        session, owner_id="nobody-else", process="Query demo")
                    raise AssertionError("the stranger's named query should refuse")
                except bp_svc.ProcessError:
                    pass
                out = await bp_svc.query_instances(session, owner_id="nobody-else")
            assert out["count"] == 0

            # an unknown process refuses loud through the node
            graph_bad = {**graph, "nodes": [
                {**n, "parameters": {"process": "Nope"}} if n["id"] == "q" else n
                for n in graph["nodes"]]}
            res = await client.post("/workflows", headers=h, json={
                "name": "Query missing", "graph": graph_bad, "is_active": False})
            wf2 = res.json()["id"]
            res = await client.post(f"/workflows/{wf2}/run", headers=h,
                                    json={"payload": {}})
            exec_id = res.json()["execution_id"]
            for _ in range(80):
                detail = (await client.get(f"/executions/{exec_id}", headers=h)).json()
                if detail.get("status") != "running":
                    break
                await asyncio.sleep(0.05)
            assert detail["status"] in ("failed", "error")
            assert "not found" in (detail.get("error") or "").lower()

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 4. the Meeting/Clinic machines - the last operator packs ship theirs
# ---------------------------------------------------------------------------

def test_v87_prewired_meeting_clinic_processes():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "packs")
            h = _auth(user["token"])

            # the shelf: nine operators, every one ships processes
            res = await client.get("/operators", headers=h)
            shelf = {o["slug"]: o for o in res.json()["operators"]}
            assert len(shelf) == 9
            assert all(o["topology"]["processes"] >= 1 for o in shelf.values())

            # the install plan names the policy per machine
            res = await client.get("/operators/meeting-operator", headers=h)
            proc = res.json()["installs"]["processes"][0]
            assert proc["name"] == "Meeting lifecycle"
            assert proc["escalates"] is True
            assert "email" in proc["escalation"]

            # install the meeting: the machine builds, seeded at the
            # ended-meeting stage, policy on the built definition
            res = await client.post("/operators/meeting-operator/install",
                                    headers=h, json={"note": "v87 smoke"})
            assert res.status_code == 200, res.text
            built = res.json()
            procs = built["processes"]
            assert len(procs) == 1
            proc = procs[0]
            assert proc["name"].startswith("Meeting lifecycle")
            assert len(proc["states"]) == 8 and proc["seeded_instances"] == 2
            pid = proc["id"]

            res = await client.get(f"/processes/{pid}", headers=h)
            full = res.json()
            assert full["escalation_policy"]["channel"] == "email"
            assert full["escalation_policy"]["repeat_every_seconds"] == 1800
            assert "email" in full["escalation_summary"]

            res = await client.get(f"/processes/{pid}/instances", headers=h)
            rows = res.json()["instances"]
            assert {r["ref"] for r in rows} == {"MTG-1001", "MTG-1002"}
            assert all(r["state"] == "notes_logged" for r in rows), \
                "the seeds import AT their stage, not rewound"

            # the machine moves like any other - to a terminal state
            r0 = rows[0]
            res = await client.post(
                f"/processes/{pid}/instances/{r0['id']}/advance",
                headers=h, json={"transition": "follow_up"})
            assert res.status_code == 200 and res.json()["state"] == "followed_up"
            assert res.json()["is_terminal"] is True

            # and the machine is a first-class component of the system
            sid = built["system"]["id"]
            grouped = (await client.get(f"/systems/{sid}", headers=h)).json()["grouped"]
            assert any(c["ref_id"] == pid for c in grouped.get("process", []))

            # install the clinic: the appointment journey per patient
            res = await client.post("/operators/clinic-operator/install",
                                    headers=h, json={"note": "front desk"})
            assert res.status_code == 200, res.text
            cproc = res.json()["processes"][0]
            assert cproc["name"].startswith("Appointment journey")
            assert len(cproc["states"]) == 7 and cproc["seeded_instances"] == 3
            res = await client.get(f"/processes/{cproc['id']}", headers=h)
            assert res.json()["escalation_policy"]["channel"] == "sms"
            res = await client.get(f"/processes/{cproc['id']}/instances", headers=h)
            rows = res.json()["instances"]
            assert all(r["state"] == "requested" for r in rows)
            assert all(r["ref"].startswith("+1555") for r in rows), \
                "ref = the phone the clinic speaks in"

            # the processes list surfaces the policies
            res = await client.get("/processes", headers=h)
            names = {p["name"]: p for p in res.json()["processes"]}
            assert names[cproc["name"]]["escalation_summary"].startswith("stuck -> sms")

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 5. version pin
# ---------------------------------------------------------------------------

def test_v87_version_pin():
    assert settings.version == "1.92.0"
