"""v84 tests - Long-running autonomy: the business state machine.

A BusinessProcess is the MACHINE (states + named transitions); instances
are the tracked entities that REMEMBER state and context across weeks;
every advance is on the record (the transition log) and emits
business.state_changed through the v80 event door, so event-trigger
workflows react to the business moving. The lead pipeline from the
roadmap (lead -> contacted -> interested -> demo_booked -> demo_completed
-> proposal_sent -> negotiating -> won|lost) is the running example.

Proven end to end:
* the machine validates (bad definitions refuse loudly, naming the exact
  problem);
* the lifecycle: start -> advance (by transition name or by target
  state) -> context patch merging into the running memory -> terminal
  states end the journey (and refuse further moves);
* the business MOVING is observable: business.state_changed on the
  instance's correlation thread, an event-trigger workflow reacting with
  the event as payload (a real execution), the journey log on the record;
* the process, measured: instances by state, SLA stuck-ness derived from
  due_at, mean time in state, advance counts - all derived;
* a process binds to a system as a first-class component (kind=process).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.config import settings
from app.main import app

API = "http://testserver/api/v1"

LEAD_PIPELINE = {
    "states": ["lead", "contacted", "interested", "demo_booked",
               "demo_completed", "proposal_sent", "negotiating", "won", "lost"],
    "initial": "lead",
    "transitions": [
        {"name": "reach_out", "from": "lead", "to": "contacted"},
        {"name": "qualify", "from": "contacted", "to": "interested"},
        {"name": "book_demo", "from": "interested", "to": "demo_booked"},
        {"name": "run_demo", "from": "demo_booked", "to": "demo_completed"},
        {"name": "send_proposal", "from": "demo_completed", "to": "proposal_sent"},
        {"name": "negotiate", "from": "proposal_sent", "to": "negotiating"},
        {"name": "win", "from": "negotiating", "to": "won"},
        {"name": "lose", "from": "contacted", "to": "lost"},
        {"name": "lose", "from": "proposal_sent", "to": "lost"},
        {"name": "lose", "from": "negotiating", "to": "lost"},
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
        "email": f"v84-{tag}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v84 {tag}",
    })
    assert res.status_code == 201, res.text
    return {"token": res.json()["token"], "id": res.json()["user"]["id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. the machine validates - bad definitions refuse loudly
# ---------------------------------------------------------------------------

def test_v84_definition_validation():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "def")
            h = _auth(user["token"])

            def _post(definition):
                return client.post("/processes", headers=h, json={
                    "name": "broken", "definition": definition})

            r = await _post({"states": [], "initial": "a", "transitions": []})
            assert r.status_code == 400 and "non-empty" in r.json()["detail"]

            r = await _post({"states": ["a", "a"], "initial": "a", "transitions": [
                {"name": "go", "from": "a", "to": "a"}]})
            assert r.status_code == 400 and "duplicate state" in r.json()["detail"]

            r = await _post({"states": ["a"], "initial": "b", "transitions": [
                {"name": "go", "from": "a", "to": "a"}]})
            assert r.status_code == 400 and "'b' is not in states" in r.json()["detail"]

            r = await _post({"states": ["a"], "initial": "a", "transitions": []})
            assert r.status_code == 400 and "not a machine" in r.json()["detail"]

            r = await _post({"states": ["a", "b"], "initial": "a", "transitions": [
                {"name": "go", "from": "a", "to": "zebra"}]})
            assert r.status_code == 400 and "'zebra' is not in states" in r.json()["detail"]

            r = await _post({"states": ["a", "b"], "initial": "a", "transitions": [
                {"name": "go", "from": "a", "to": "b"},
                {"name": "go", "from": "a", "to": "b"}]})
            assert r.status_code == 400 and "duplicate transition" in r.json()["detail"]

            # the real pipeline defines cleanly
            r = await client.post("/processes", headers=h, json={
                "name": "Lead pipeline", "definition": LEAD_PIPELINE,
                "description": "the roadmap's own example machine"})
            assert r.status_code == 201, r.text
            p = r.json()
            assert p["states"][0] == "lead" and p["initial"] == "lead"
            assert len(p["transitions"]) == 10
            # 'won' and 'lost' are the terminal states (derived, nothing stored)
            assert sorted(p["terminal_states"]) == ["lost", "won"]

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 2. the lifecycle - start, advance, remember, finish
# ---------------------------------------------------------------------------

def test_v84_pipeline_lifecycle():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "life")
            h = _auth(user["token"])
            res = await client.post("/processes", headers=h, json={
                "name": "Lead pipeline", "definition": LEAD_PIPELINE})
            assert res.status_code == 201, res.text
            pid = res.json()["id"]

            # start - the instance remembers ref + context from day one
            res = await client.post(f"/processes/{pid}/instances", headers=h, json={
                "ref": "LEAD-1042", "title": "Globex - Dana Reyes",
                "context": {"source": "website", "plan_interest": "team"},
                "due_in_seconds": 3600})
            assert res.status_code == 201, res.text
            inst = res.json()
            assert inst["state"] == "lead" and inst["ref"] == "LEAD-1042"
            assert inst["context"]["plan_interest"] == "team"
            assert inst["is_terminal"] is False and inst["is_stuck"] is False
            iid = inst["id"]

            # advance by transition name - the context patch MERGES
            res = await client.post(f"/processes/{pid}/instances/{iid}/advance",
                                    headers=h, json={
                                        "transition": "reach_out",
                                        "note": "called, voicemail",
                                        "context_patch": {"calls_made": 1,
                                                          "best_time": "mornings"},
                                        "actor": "sdr-1"})
            assert res.status_code == 200, res.text
            inst = res.json()
            assert inst["state"] == "contacted"
            assert inst["context"]["calls_made"] == 1
            assert inst["context"]["plan_interest"] == "team", \
                "the merge keeps the running memory"
            assert inst["age_in_state_seconds"] >= 0

            # advance by target state (no transition name needed)
            res = await client.post(f"/processes/{pid}/instances/{iid}/advance",
                                    headers=h, json={"to_state": "interested"})
            assert res.status_code == 200 and res.json()["state"] == "interested"

            # an impossible move refuses loud, naming the allowed moves
            res = await client.post(f"/processes/{pid}/instances/{iid}/advance",
                                    headers=h, json={"transition": "win"})
            assert res.status_code == 400
            assert "no transition 'win' from state 'interested'" in res.json()["detail"]
            assert "book_demo" in res.json()["detail"]

            res = await client.post(f"/processes/{pid}/instances/{iid}/advance",
                                    headers=h, json={"to_state": "won"})
            assert res.status_code == 400 and "no move from" in res.json()["detail"]

            # a move without a target refuses
            res = await client.post(f"/processes/{pid}/instances/{iid}/advance",
                                    headers=h, json={})
            assert res.status_code == 400 and "name the move" in res.json()["detail"]

            # walk to the terminal state - the journey ENDS (row remembers)
            for target in ("demo_booked", "demo_completed", "proposal_sent",
                           "negotiating", "won"):
                res = await client.post(f"/processes/{pid}/instances/{iid}/advance",
                                        headers=h, json={"to_state": target,
                                                         "note": f"moving to {target}"})
                assert res.status_code == 200, res.text
            inst = res.json()
            assert inst["state"] == "won" and inst["is_terminal"] is True
            assert inst["ended_at"] is not None
            assert len(inst["journey"]) == 8  # started + 7 advances
            assert inst["journey"][0]["transition"] == "started"
            assert inst["journey"][-1]["from_state"] == "negotiating"

            # a terminal state has no outgoing moves - the machine says so
            res = await client.post(f"/processes/{pid}/instances/{iid}/advance",
                                    headers=h, json={"to_state": "lead"})
            assert res.status_code == 400 and "terminal" in res.json()["detail"]

            # the journey is readable on its own
            res = await client.get(f"/processes/{pid}/instances/{iid}", headers=h)
            assert res.status_code == 200 and len(res.json()["journey"]) == 8

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 3. the business moving is observable - events + reacting workflows
# ---------------------------------------------------------------------------

def test_v84_state_changed_events_and_reactions():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "react")
            h = _auth(user["token"])
            res = await client.post("/processes", headers=h, json={
                "name": "Lead pipeline", "definition": LEAD_PIPELINE})
            pid = res.json()["id"]

            # the reacting workflow: business.state_changed -> shape -> write
            res = await client.post("/datasets", headers=h, json={
                "name": "Follow up queue",
                "rows": [{"instance_id": "seed", "ref": "seed", "to": "seed"}]})
            assert res.status_code == 201, res.text
            ds_id = res.json()["id"]
            graph = {"nodes": [
                {"id": "t", "type": "event_trigger", "name": "Trigger",
                 "position": {"x": 0, "y": 0},
                 "parameters": {"event_type": "business.state_changed"}},
                {"id": "s", "type": "python_transform", "name": "Shape",
                 "position": {"x": 1, "y": 0},
                 "parameters": {"code": ("r = df.iloc[0] if len(df) else {}\n"
                                         "pl = r.get('payload') or {}\n"
                                         "result = [{'instance_id': r.get('correlation_id', ''), "
                                         "'ref': pl.get('ref', ''), 'to': pl.get('to', ''), "
                                         "'transition': pl.get('transition', '')}]")}},
                {"id": "w", "type": "dataset_write", "name": "Write",
                 "position": {"x": 2, "y": 0},
                 "parameters": {"dataset": "Follow up queue", "mode": "append"}},
            ], "edges": [
                {"id": "e1", "source": "t", "target": "s",
                 "sourceHandle": "main", "targetHandle": "main"},
                {"id": "e2", "source": "s", "target": "w",
                 "sourceHandle": "main", "targetHandle": "main"},
            ]}
            res = await client.post("/workflows", headers=h, json={
                "name": "Follow up on moves", "graph": graph, "is_active": True})
            assert res.status_code == 201, res.text
            wf_id = res.json()["id"]

            # the business moves -> the event fires -> the workflow reacts
            res = await client.post(f"/processes/{pid}/instances", headers=h, json={
                "ref": "LEAD-2001", "title": "Initech - Ari Cohen"})
            iid = res.json()["id"]
            res = await client.post(f"/processes/{pid}/instances/{iid}/advance",
                                    headers=h, json={"transition": "reach_out",
                                                     "actor": "sdr-2"})
            assert res.status_code == 200, res.text
            await _drain_background()

            # the event is on the platform's thread for THIS instance
            res = await client.get("/events", headers=h,
                                   params={"type": "business.state_changed",
                                           "correlation_id": iid})
            assert res.status_code == 200, res.text
            events = res.json()["events"]
            assert len(events) == 1, res.text
            ev = events[0]
            assert ev["source"] == "business"
            assert ev["payload"]["from"] == "lead" and ev["payload"]["to"] == "contacted"
            assert ev["payload"]["ref"] == "LEAD-2001"

            # the workflow REACTED - a real execution with the event as payload
            res = await client.get("/executions", headers=h, params={"limit": 30})
            runs = [r for r in res.json() if r.get("workflow_id") == wf_id]
            assert len(runs) == 1, res.text
            assert runs[0]["trigger_type"] == "event"
            assert runs[0]["status"] == "success", runs[0].get("error")
            res = await client.get(f"/datasets/{ds_id}/rows", headers=h)
            body = res.json()
            rows = body.get("rows") or body.get("records") or []
            assert len(rows) == 2, res.text  # seed + the reacted move
            assert rows[-1]["ref"] == "LEAD-2001" and rows[-1]["to"] == "contacted"

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 4. the process, measured - SLA stuck-ness, by-state, timing
# ---------------------------------------------------------------------------

def test_v84_analytics_and_stuck():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "meas")
            h = _auth(user["token"])
            res = await client.post("/processes", headers=h, json={
                "name": "Lead pipeline", "definition": LEAD_PIPELINE})
            pid = res.json()["id"]

            # one instance WITH a short SLA, two without
            res = await client.post(f"/processes/{pid}/instances", headers=h, json={
                "ref": "L-1", "due_in_seconds": 1})
            iid1 = res.json()["id"]
            await client.post(f"/processes/{pid}/instances", headers=h, json={
                "ref": "L-2"})
            await client.post(f"/processes/{pid}/instances", headers=h, json={
                "ref": "L-3"})

            # move L-1 twice so the log carries durations
            await client.post(f"/processes/{pid}/instances/{iid1}/advance",
                              headers=h, json={"transition": "reach_out"})
            import time as _t
            _t.sleep(0.05)
            await client.post(f"/processes/{pid}/instances/{iid1}/advance",
                              headers=h, json={"transition": "qualify"})

            res = await client.get(f"/processes/{pid}/analytics", headers=h)
            assert res.status_code == 200, res.text
            a = res.json()
            assert a["instances"] == 3 and a["open"] == 3
            assert a["by_state"]["lead"] == 2 and a["by_state"]["interested"] == 1
            assert a["advance_counts"]["reach_out"] == 1
            assert a["advance_counts"]["qualify"] == 1
            assert a["terminal_states"] == ["lost", "won"]

            # stuck: the 1-second SLA on L-1 expires (still open, not
            # terminal) - wait past it deterministically, then re-derive
            _t.sleep(1.2)
            res2 = await client.get(f"/processes/{pid}/analytics", headers=h)
            a2 = res2.json()
            assert a2["stuck_count"] == 1, a2
            assert a2["stuck"] == [iid1]
            assert "contacted" in a2["mean_time_in_state_seconds"]

            # the state filter reads the open pipeline honestly
            res = await client.get(f"/processes/{pid}/instances", headers=h,
                                   params={"state": "lead"})
            assert len(res.json()["instances"]) == 2

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 5. ownership + system binding
# ---------------------------------------------------------------------------

def test_v84_owner_scoping_and_system_attach():
    async def _go():
        async with _client() as client:
            owner = await _mk_user(client, "own")
            stranger = await _mk_user(client, "str")
            ho, hs = _auth(owner["token"]), _auth(stranger["token"])

            res = await client.post("/processes", headers=ho, json={
                "name": "Lead pipeline", "definition": LEAD_PIPELINE})
            pid = res.json()["id"]
            res = await client.post(f"/processes/{pid}/instances", headers=ho, json={
                "ref": "L-9"})
            iid = res.json()["id"]

            # a stranger's process looks nonexistent
            res = await client.get(f"/processes/{pid}", headers=hs)
            assert res.status_code == 404
            res = await client.post(f"/processes/{pid}/instances/{iid}/advance",
                                    headers=hs, json={"to_state": "contacted"})
            assert res.status_code in (400, 404)

            # the process binds to a system as a first-class component
            res = await client.post("/systems", headers=ho, json={
                "name": "Sales system"})
            assert res.status_code in (200, 201), res.text
            sid = res.json()["id"]
            res = await client.post(f"/systems/{sid}/components", headers=ho,
                                    json={"kind": "process", "ref_id": pid})
            assert res.status_code in (200, 201), res.text
            res = await client.get(f"/systems/{sid}", headers=ho)
            grouped = res.json()["grouped"]
            assert grouped.get("process") and grouped["process"][0]["name"] == "Lead pipeline"

            # unknown process 404s for the owner too
            res = await client.get("/processes/nope", headers=ho)
            assert res.status_code == 404

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 6. version pin
# ---------------------------------------------------------------------------

def test_v84_version_pin():
    assert settings.version == "1.101.0"
