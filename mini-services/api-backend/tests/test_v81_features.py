"""V81 feature tests: the system runtime - systems as RUNNING entities.

* LIFECYCLE: install -> start/pause/resume/stop as a validated state
  machine (invalid transitions fail loud 409 with the current state).
  Every accepted verb writes a SystemOperation row (the durable audit)
  and emits a system.* event through the v80 door on the system's own
  correlation thread.
* THE GATE: a system that is paused/stopped holds its workflows on every
  REACTIVE path - event triggers stop dispatching, schedule ticks return
  early, webhook hits are refused 409 - WITHOUT touching the workflows'
  own is_active. A workflow bound to several systems reacts while at
  least one binding is running. Manual runs stay open (the builder's
  debugging door) and start's activate_workflows door boots pack-installed
  inactive workflows loudly, on the record.
* COMPONENTS: the interaction layer joins the estate (voice_agent |
  queue | meeting), resolved against the live tables with owner scoping.
* SOLUTION PROVENANCE + UPGRADE: installing support-line-system with
  as_system binds the whole topology (workflows + datasets + agent +
  queue + meeting), stamps source_solution_slug and records the install;
  upgrade re-applies the pack and reconciles honestly (idempotent when
  everything is already bound, selective binding when something is new).
* STATE + METRICS: the runtime snapshot (gate, live interactions, last
  activity) and the window counters are derived at read time.

Runs the FastAPI app in-process (httpx ASGITransport). No network egress.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.main import app

API = "http://testserver/api/v1"


@pytest.fixture(autouse=True)
def _fresh_rate_limit():
    from app.api import _ratelimit

    _ratelimit.reset_all()
    yield
    _ratelimit.reset_all()


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


async def _mk_user(client: httpx.AsyncClient, tag: str, n: int = 1) -> dict:
    res = await client.post("/auth/register", json={
        "email": f"v81-{tag}-u{n}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v81 u{n} {tag}",
    })
    assert res.status_code == 201, res.text
    return {"token": res.json()["token"], "id": res.json()["user"]["id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _node(nid: str, ntype: str, params: dict | None = None) -> dict:
    return {"id": nid, "type": ntype, "name": nid,
            "position": {"x": 0, "y": 0}, "parameters": params or {}}


def _edge(eid: str, source: str, target: str) -> dict:
    return {"id": eid, "source": source, "target": target,
            "sourceHandle": "main", "targetHandle": "main"}


async def _mk_system(client: httpx.AsyncClient, headers: dict, name: str) -> dict:
    res = await client.post("/systems", headers=headers, json={"name": name})
    assert res.status_code == 201, res.text
    return res.json()


async def _runs(client: httpx.AsyncClient, headers: dict, wf_id: str) -> list[dict]:
    res = await client.get("/executions", headers=headers, params={"limit": 50})
    assert res.status_code == 200, res.text
    return [r for r in res.json() if r.get("workflow_id") == wf_id]


# ---------------------------------------------------------------------------
# 1. the lifecycle state machine + operations + events
# ---------------------------------------------------------------------------

def test_v81_lifecycle_state_machine():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "lifecycle")
            h = _auth(user["token"])

            s = await _mk_system(client, h, "support line runtime")
            # pre-v81 honesty: a system has been operating since creation
            assert s["lifecycle"] == "running"
            assert s["source_solution_slug"] is None

            sid = s["id"]
            # invalid transitions fail loud with the current state named
            res = await client.post(f"/systems/{sid}/start", headers=h, json={})
            assert res.status_code == 409, res.text
            assert "running" in res.json()["detail"] and "start" in res.json()["detail"]
            res = await client.post(f"/systems/{sid}/resume", headers=h, json={})
            assert res.status_code == 409
            res = await client.post(f"/systems/{sid}/shred", headers=h)
            assert res.status_code in (404, 405)

            # running -> paused -> resume -> running
            res = await client.post(f"/systems/{sid}/pause", headers=h, json={})
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["verb"] == "pause" and body["from"] == "running"
            assert body["lifecycle"] == "paused" and body["operation_id"]

            res = await client.post(f"/systems/{sid}/pause", headers=h, json={})
            assert res.status_code == 409

    async def _go2():
        async with _client() as client:
            user = await _mk_user(client, "lifecycle2")
            h = _auth(user["token"])
            s = await _mk_system(client, h, "careful machine")
            sid = s["id"]

            res = await client.post(f"/systems/{sid}/pause", headers=h, json={})
            assert res.status_code == 200
            # paused IS a valid start source (the boot-from-pause door)
            res = await client.post(f"/systems/{sid}/start", headers=h, json={})
            assert res.status_code == 200, res.text
            assert res.json()["lifecycle"] == "running"

            # stop from running; resume now refuses (resume applies to paused)
            res = await client.post(f"/systems/{sid}/stop", headers=h, json={})
            assert res.status_code == 200 and res.json()["lifecycle"] == "stopped"
            res = await client.post(f"/systems/{sid}/resume", headers=h, json={})
            assert res.status_code == 409
            # stop from stopped refuses too
            res = await client.post(f"/systems/{sid}/stop", headers=h, json={})
            assert res.status_code == 409

            # the operations log holds every accepted verb, newest first
            res = await client.get(f"/systems/{sid}/operations", headers=h)
            verbs = [o["verb"] for o in res.json()["operations"]]
            assert verbs == ["stop", "start", "pause"], verbs
            op = res.json()["operations"][0]
            assert op["detail"]["from"] == "running" and op["detail"]["to"] == "stopped"
            assert op["actor"] == user["id"]

            # every verb emitted a system.* event on the system's correlation
            # thread - and the system's own event view shows the thread
            res = await client.get(f"/systems/{sid}/events", headers=h)
            types = [e["type"] for e in res.json()["events"]]
            assert types == ["system.stopped", "system.started", "system.paused"], types
            assert all(e["correlation_id"] == sid for e in res.json()["events"])
            assert all(e["source"] == "system" for e in res.json()["events"])
            # the global event door sees the same thread
            res = await client.get("/events", headers=h,
                                   params={"correlation_id": sid, "type": "system.*"})
            assert len(res.json()["events"]) == 3

    _sync(_wrap(_go()))
    _sync(_wrap(_go2()))


# ---------------------------------------------------------------------------
# 2. the gate: event triggers hold on a stopped system
# ---------------------------------------------------------------------------

def test_v81_event_trigger_gated():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "gate")
            h = _auth(user["token"])

            graph = {"nodes": [
                _node("t", "event_trigger", {"event_type": "crm.*"}),
                _node("c", "code", {"mode": "run_once_for_each_item",
                                    "jsCode": "return {saw: input.event.type};"}),
            ], "edges": [_edge("e1", "t", "c")]}
            res = await client.post("/workflows", headers=h, json={
                "name": "react to crm", "graph": graph, "is_active": True})
            assert res.status_code == 201, res.text
            wf_id = res.json()["id"]

            s = await _mk_system(client, h, "crm ops")
            res = await client.post(f"/systems/{s['id']}/components", headers=h,
                                    json={"kind": "workflow", "ref_id": wf_id})
            assert res.status_code == 201, res.text
            comp_id = res.json()["component_id"]
            # the membership change is on the record and on the wire
            res = await client.get(f"/systems/{s['id']}/events", headers=h)
            assert any(e["type"] == "system.component_added"
                       and e["payload"].get("ref_id") == wf_id
                       for e in res.json()["events"])

            # running: the event dispatches
            res = await client.post("/events", headers=h, json={
                "type": "crm.lead_won", "source": "user", "payload": {"v": 1}})
            assert res.status_code == 201
            await _drain_background()
            assert len(await _runs(client, h, wf_id)) == 1

            # stop: the same event dispatches NOTHING (the gate holds)
            res = await client.post(f"/systems/{s['id']}/stop", headers=h, json={})
            assert res.status_code == 200
            res = await client.post("/events", headers=h, json={
                "type": "crm.lead_lost", "source": "user", "payload": {"v": 2}})
            assert res.status_code == 201
            await _drain_background()
            assert len(await _runs(client, h, wf_id)) == 1  # unchanged

            # the workflow's own is_active was NEVER touched
            res = await client.get(f"/workflows/{wf_id}", headers=h)
            assert res.json()["is_active"] is True

            # pause holds too; start reopens
            res = await client.post(f"/systems/{s['id']}/start", headers=h, json={})
            assert res.status_code == 200
            res = await client.post(f"/systems/{s['id']}/pause", headers=h, json={})
            assert res.status_code == 200
            res = await client.post("/events", headers=h, json={
                "type": "crm.note_added", "source": "user", "payload": {"v": 3}})
            await _drain_background()
            assert len(await _runs(client, h, wf_id)) == 1
            res = await client.post(f"/systems/{s['id']}/resume", headers=h, json={})
            assert res.status_code == 200
            res = await client.post("/events", headers=h, json={
                "type": "crm.note_added", "source": "user", "payload": {"v": 4}})
            await _drain_background()
            assert len(await _runs(client, h, wf_id)) == 2

            # multi-system honesty: bind the workflow to a SECOND system,
            # stop one of them - the running one keeps it alive
            s2 = await _mk_system(client, h, "second desk")
            await client.post(f"/systems/{s2['id']}/components", headers=h,
                              json={"kind": "workflow", "ref_id": wf_id})
            res = await client.post(f"/systems/{s['id']}/stop", headers=h, json={})
            assert res.status_code == 200
            res = await client.post("/events", headers=h, json={
                "type": "crm.note_added", "source": "user", "payload": {"v": 5}})
            await _drain_background()
            assert len(await _runs(client, h, wf_id)) == 3
            # stop the second one: now every binding holds it still
            res = await client.post(f"/systems/{s2['id']}/stop", headers=h, json={})
            assert res.status_code == 200
            res = await client.post("/events", headers=h, json={
                "type": "crm.note_added", "source": "user", "payload": {"v": 6}})
            await _drain_background()
            assert len(await _runs(client, h, wf_id)) == 3

            # unbinding clears the gate: both stopped bindings must go
            for sys_row in (s, s2):
                res = await client.get(f"/systems/{sys_row['id']}", headers=h)
                comps = res.json()["grouped"]["workflow"]
                cid = next(c["component_id"] for c in comps if c["ref_id"] == wf_id)
                res = await client.delete(f"/systems/{sys_row['id']}/components/{cid}", headers=h)
                assert res.status_code == 204
            res = await client.post("/events", headers=h, json={
                "type": "crm.note_added", "source": "user", "payload": {"v": 7}})
            await _drain_background()
            assert len(await _runs(client, h, wf_id)) == 4
            res = await client.get(f"/systems/{s['id']}/events", headers=h)
            assert any(e["type"] == "system.component_removed" for e in res.json()["events"])

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 3. the gate: webhooks refuse 409, schedules hold, manual runs stay open
# ---------------------------------------------------------------------------

def test_v81_webhook_and_schedule_gated():
    async def _go():
        async with _client() as client:
            from app.services import scheduler as sched

            user = await _mk_user(client, "hooks")
            h = _auth(user["token"])

            # webhook-trigger workflow
            wgraph = {"nodes": [
                _node("w", "webhook_trigger", {"response_mode": "immediately"}),
                _node("c", "code", {"mode": "run_once_for_each_item",
                                    "jsCode": "return {got: input.payload != null};"}),
            ], "edges": [_edge("e1", "w", "c")]}
            res = await client.post("/workflows", headers=h, json={
                "name": "hooked", "graph": wgraph, "is_active": True})
            assert res.status_code == 201, res.text
            wf_hook = res.json()["id"]

            # schedule-trigger workflow (5s interval)
            sgraph = {"nodes": [
                _node("s", "schedule_trigger", {"mode": "interval", "interval_seconds": 300}),
                _node("c", "code", {"mode": "run_once_for_each_item",
                                    "jsCode": "return {tick: true};"}),
            ], "edges": [_edge("e1", "s", "c")]}
            res = await client.post("/workflows", headers=h, json={
                "name": "ticker", "graph": sgraph, "is_active": True})
            assert res.status_code == 201, res.text
            wf_tick = res.json()["id"]
            node_id = "s"

            # manual-run workflow (the builder's debugging door)
            mgraph = {"nodes": [
                _node("m", "manual_trigger", {}),
                _node("c", "code", {"mode": "run_once_for_each_item",
                                    "jsCode": "return {manual: true};"}),
            ], "edges": [_edge("e1", "m", "c")]}
            res = await client.post("/workflows", headers=h, json={
                "name": "by hand", "graph": mgraph, "is_active": True})
            assert res.status_code == 201, res.text
            wf_manual = res.json()["id"]

            s = await _mk_system(client, h, "the line")
            for wid in (wf_hook, wf_tick, wf_manual):
                res = await client.post(f"/systems/{s['id']}/components", headers=h,
                                        json={"kind": "workflow", "ref_id": wid})
                assert res.status_code == 201

            # RUNNING: webhook serves, the tick fires, manual runs
            res = await client.post(f"/webhooks/{wf_hook}", headers=h, json={"x": 1})
            assert res.status_code == 202, res.text
            await sched._fire_scheduled_workflow(wf_tick, node_id)
            await _drain_background()
            assert len(await _runs(client, h, wf_tick)) == 1

            res = await client.post(f"/systems/{s['id']}/stop", headers=h, json={})
            assert res.status_code == 200

            # the webhook hit is refused loudly - the caller learns the
            # system is not operating instead of the hit vanishing
            res = await client.post(f"/webhooks/{wf_hook}", headers=h, json={"x": 2})
            assert res.status_code == 409, res.text
            assert "not running" in res.json()["detail"]

            # the schedule tick returns honestly without firing
            await sched._fire_scheduled_workflow(wf_tick, node_id)
            await _drain_background()
            assert len(await _runs(client, h, wf_tick)) == 1  # unchanged

            # manual runs stay open: the builder's explicit door, not the
            # system operating - and the state endpoint says so
            res = await client.post(f"/workflows/{wf_manual}/run", headers=h,
                                    json={"payload": {"q": 1}})
            assert res.status_code == 200, res.text
            await _drain_background()
            runs = await _runs(client, h, wf_manual)
            assert len(runs) == 1 and runs[0]["status"] == "success"

            res = await client.get(f"/systems/{s['id']}/state", headers=h)
            st = res.json()
            assert st["lifecycle"] == "stopped"
            assert st["workflows"] == {"bound": 3, "active": 3, "gate_blocked": 3}
            assert "debugging door" in st["gate"]["note"]
            assert st["last_operation_verb"] == "stop"

            # start (without activate_workflows): the gate reopens; the
            # is_active flags were never touched so nothing else changes
            res = await client.post(f"/systems/{s['id']}/start", headers=h, json={})
            assert res.status_code == 200
            assert res.json()["workflows_activated"] == 0
            res = await client.post(f"/webhooks/{wf_hook}", headers=h, json={"x": 3})
            assert res.status_code == 202
            await sched._fire_scheduled_workflow(wf_tick, node_id)
            await _drain_background()
            assert len(await _runs(client, h, wf_tick)) == 2

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 4. the interaction layer as components + role enforcement
# ---------------------------------------------------------------------------

def test_v81_interaction_components_and_roles():
    async def _go():
        async with _client() as client:
            owner = await _mk_user(client, "owner")
            viewer = await _mk_user(client, "owner", n=2)
            stranger = await _mk_user(client, "owner", n=3)
            ho, hv = _auth(owner["token"]), _auth(viewer["token"])
            hs = _auth(stranger["token"])

            agent = (await client.post("/voice/agents", headers=ho, json={
                "name": "line persona", "scaffold_handler": True})).json()
            meeting = (await client.post("/voice/meetings", headers=ho, json={
                "title": "the room", "agent_id": agent["id"]})).json()
            queue = (await client.post("/voice/queues", headers=ho, json={
                "name": "the line", "meeting_id": meeting["id"]})).json()

            s = await _mk_system(client, ho, "voice estate")
            for kind, ref in (("voice_agent", agent["id"]),
                              ("meeting", meeting["id"]),
                              ("queue", queue["id"])):
                res = await client.post(f"/systems/{s['id']}/components", headers=ho,
                                        json={"kind": kind, "ref_id": ref})
                assert res.status_code == 201, res.text
            # duplicates refuse 409
            res = await client.post(f"/systems/{s['id']}/components", headers=ho,
                                    json={"kind": "queue", "ref_id": queue["id"]})
            assert res.status_code == 409
            # unknown kinds + foreign refs refuse as before
            res = await client.post(f"/systems/{s['id']}/components", headers=ho,
                                    json={"kind": "sip_trunk", "ref_id": "x"})
            assert res.status_code == 400
            res = await client.post(f"/systems/{s['id']}/components", headers=ho,
                                    json={"kind": "queue", "ref_id": "no-such"})
            assert res.status_code == 404

            # the detail groups the interaction layer
            res = await client.get(f"/systems/{s['id']}", headers=ho)
            grouped = res.json()["grouped"]
            assert len(grouped["voice_agent"]) == 1 and len(grouped["queue"]) == 1
            assert len(grouped["meeting"]) == 1

            # a stranger cannot even see it; a viewer reads but cannot operate
            res = await client.post(f"/systems/{s['id']}/stop", headers=hs)
            assert res.status_code == 404
            res = await client.get(f"/systems/{s['id']}/state", headers=hs)
            assert res.status_code == 404
            res = await client.post(f"/systems/{s['id']}/members", headers=ho,
                                    json={"email": f"v81-owner-u2@py8n.test", "role": "viewer"})
            assert res.status_code == 201, res.text
            res = await client.get(f"/systems/{s['id']}/state", headers=hv)
            assert res.status_code == 200
            res = await client.post(f"/systems/{s['id']}/stop", headers=hv)
            assert res.status_code == 403  # above the viewer's role

            # the owner operates; live interactions show in state
            res = await client.post(f"/systems/{s['id']}/pause", headers=ho, json={})
            assert res.status_code == 200
            res = await client.get(f"/systems/{s['id']}/state", headers=ho)
            st = res.json()
            assert st["lifecycle"] == "paused"
            assert st["live"]["live_meetings"] == 1  # the bound room is live
            assert st["components"] == 3

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 5. solution install -> a full system; the activate_workflows boot door
# ---------------------------------------------------------------------------

def test_v81_solution_install_binds_a_running_system():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "install")
            h = _auth(user["token"])

            res = await client.post("/solutions/support-line-system/install", headers=h,
                                    json={"as_system": True, "as_voice_agent": True,
                                          "as_support_line": True})
            assert res.status_code in (200, 201), res.text
            body = res.json()
            sysref = body["system"]
            assert sysref["lifecycle"] == "running"
            counts = sysref["components"]
            assert counts.get("workflow", 0) >= 1  # the pack handler (the brain's own)
            assert counts.get("dataset", 0) >= 1
            assert counts.get("voice_agent") == 1
            assert counts.get("queue") == 1
            assert counts.get("meeting") == 1

            sid = sysref["id"]
            res = await client.get(f"/systems/{sid}", headers=h)
            detail = res.json()
            assert detail["source_solution_slug"] == "support-line-system"

            # the install is on the record: an operation + the system.installed event
            res = await client.get(f"/systems/{sid}/operations", headers=h)
            ops = res.json()["operations"]
            assert ops and ops[-1]["verb"] == "installed"
            assert ops[-1]["actor"] == user["id"]
            assert ops[-1]["detail"]["solution"] == "support-line-system"
            res = await client.get(f"/systems/{sid}/events", headers=h)
            assert any(e["type"] == "system.installed" for e in res.json()["events"])

            # the pack pipeline honesty: workflows land INACTIVE, the gate open
            res = await client.get(f"/systems/{sid}/state", headers=h)
            st = res.json()
            assert st["workflows"]["active"] == 0
            assert st["workflows"]["gate_blocked"] == 0
            assert st["live"]["live_meetings"] == 1  # the installed room

            # the boot door: start with activate_workflows turns them on,
            # loudly, on the record
            res = await client.post(f"/systems/{sid}/stop", headers=h, json={})
            assert res.status_code == 200
            res = await client.post(f"/systems/{sid}/start", headers=h,
                                    json={"activate_workflows": True})
            assert res.status_code == 200
            assert res.json()["workflows_activated"] >= 1
            res = await client.get(f"/systems/{sid}/state", headers=h)
            assert res.json()["workflows"]["active"] >= 1
            res = await client.get(f"/systems/{sid}/operations", headers=h)
            started = next(o for o in res.json()["operations"] if o["verb"] == "start")
            assert started["detail"]["activate_workflows"] >= 1

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 6. upgrade: idempotent when everything is bound, selective when not
# ---------------------------------------------------------------------------

def test_v81_upgrade_reconciles():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "upgrade")
            h = _auth(user["token"])

            res = await client.post("/solutions/support-line-system/install", headers=h,
                                    json={"as_system": True, "as_voice_agent": True,
                                          "as_support_line": True})
            sysref = res.json()["system"]
            sid = sysref["id"]

            res = await client.get(f"/systems/{sid}", headers=h)
            before = res.json()
            wf_refs_before = {c["ref_id"] for c in before["grouped"]["workflow"]}
            ds_refs_before = {c["ref_id"] for c in before["grouped"]["dataset"]}

            # the pack offers nothing the system doesn't already bind ->
            # the upgrade is IDEMPOTENT: nothing imported, nothing rewritten
            res = await client.post(f"/systems/{sid}/upgrade", headers=h)
            assert res.status_code == 200, res.text
            up = res.json()
            assert up["solution"] == "support-line-system"
            assert up["imports_skipped"] is True
            assert up["added"] == {"workflow": 0, "dataset": 0}
            assert up["same_name_left_untouched"] == []
            assert "already bound" in up["note"]

            res = await client.get(f"/systems/{sid}", headers=h)
            after = res.json()
            assert {c["ref_id"] for c in after["grouped"]["workflow"]} == wf_refs_before
            assert {c["ref_id"] for c in after["grouped"]["dataset"]} == ds_refs_before
            assert after["upgraded_at"]

            res = await client.get(f"/systems/{sid}/operations", headers=h)
            assert res.json()["operations"][0]["verb"] == "upgraded"
            res = await client.get(f"/systems/{sid}/events", headers=h)
            assert any(e["type"] == "system.upgraded" for e in res.json()["events"])

            # a system with no solution provenance refuses loudly
            s2 = await _mk_system(client, h, "hand-built")
            res = await client.post(f"/systems/{s2['id']}/upgrade", headers=h)
            assert res.status_code == 400
            assert "as_system" in res.json()["detail"]

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 7. metrics: derived counters over a window
# ---------------------------------------------------------------------------

def test_v81_metrics_and_events_view():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "metrics")
            h = _auth(user["token"])

            mgraph = {"nodes": [
                _node("m", "manual_trigger", {}),
                _node("c", "code", {"mode": "run_once_for_each_item",
                                    "jsCode": "return {n: 1};"}),
            ], "edges": [_edge("e1", "m", "c")]}
            res = await client.post("/workflows", headers=h, json={
                "name": "counted", "graph": mgraph, "is_active": True})
            wf_id = res.json()["id"]

            s = await _mk_system(client, h, "measured")
            await client.post(f"/systems/{s['id']}/components", headers=h,
                              json={"kind": "workflow", "ref_id": wf_id})

            # run it twice (one succeeds), pause/stop for lifecycle events
            for i in range(2):
                res = await client.post(f"/workflows/{wf_id}/run", headers=h,
                                        json={"payload": {"i": i}})
                assert res.status_code == 200
            await _drain_background()
            await client.post(f"/systems/{s['id']}/pause", headers=h, json={})
            await client.post(f"/systems/{s['id']}/resume", headers=h, json={})

            res = await client.get(f"/systems/{s['id']}/metrics", headers=h)
            m = res.json()
            assert m["window_hours"] == 24
            assert m["executions"]["total"] == 2
            assert m["executions"]["success"] == 2
            assert m["executions"]["failure_rate"] == 0.0
            assert m["events"]["total"] >= 2
            assert m["events"]["by_type"].get("system.paused") == 1
            assert m["events"]["by_type"].get("system.resumed") == 1
            assert m["operations"] >= 2

            # the events view matches by correlation AND by target binding:
            # an event whose target IS a bound object shows up here
            queue = (await client.post("/voice/queues", headers=h,
                                       json={"name": "event queue"})).json()
            res = await client.post("/events", headers=h, json={
                "type": "queue.someone_waiting", "source": "user",
                "target_type": "queue", "target_id": queue["id"]})
            assert res.status_code == 201, res.text
            # not visible before the queue is bound
            res = await client.get(f"/systems/{s['id']}/events", headers=h)
            assert not any(e["type"] == "queue.someone_waiting"
                           for e in res.json()["events"])
            res = await client.post(f"/systems/{s['id']}/components", headers=h,
                                    json={"kind": "queue", "ref_id": queue["id"]})
            assert res.status_code == 201, res.text
            res = await client.get(f"/systems/{s['id']}/events", headers=h)
            types = [e["type"] for e in res.json()["events"]]
            assert "queue.someone_waiting" in types

            # viewer-visible metrics (a read) - foreign users get 404
            stranger = await _mk_user(client, "metrics", n=2)
            res = await client.get(f"/systems/{s['id']}/metrics",
                                   headers=_auth(stranger["token"]))
            assert res.status_code == 404

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 8. the version pin
# ---------------------------------------------------------------------------

def test_v81_version():
    from app.config import settings

    assert settings.version == "1.86.0"
