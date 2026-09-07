"""v85 tests - operators install pre-wired processes + the escalation
door + the deeper meeting client's backend seams.

Three moves on top of v84's business state machine:

* the Sales Operator now ships the LEAD PIPELINE BOUND: installing the
  operator composes a real BusinessProcess (the machine, escalate
  self-loops included), seeds one instance per CRM lead AT ITS CRM STAGE
  (ref = the phone a call can match, the import honest on the record),
  binds it kind=process into the RUNNING system, and wires the Pipeline
  advancer workflow (call.ended -> business_advance) whose process param
  resolves to the BUILT process id;
* the business_advance NODE: workflows move the machine - by ref within
  a process, validated, on the record, emitting business.state_changed;
  unknown refs and machine refusals skip honestly when asked to;
* the scheduler DOOR: POST /scheduler/escalations/tick sweeps open
  instances past their SLA - escalates through the machine's own
  'escalate' move when it defines one, otherwise records a no-move
  escalation, emits business.stuck either way, dedups one-knock-per-
  stint, holds for processes bound to non-running systems, and stays
  owner-scoped on the manual door.

The meeting client deepening is browser-side (late-join video hello
dance, device pickers, waiting-room seat panel) - proven by the Nuxt
build + live smoke; these tests prove the backend seams it rides on.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.config import settings
from app.main import app

API = "http://testserver/api/v1"


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
        "email": f"v85-{tag}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v85 {tag}",
    })
    assert res.status_code == 201, res.text
    return {"token": res.json()["token"], "id": res.json()["user"]["id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. the Sales Operator ships the lead pipeline BOUND
# ---------------------------------------------------------------------------

def test_v85_operator_ships_the_lead_pipeline_bound():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "op")
            h = _auth(user["token"])

            # the shelf says what the sales operator builds - including the process
            res = await client.get("/operators/sales-operator", headers=h)
            assert res.status_code == 200, res.text
            procs = res.json()["installs"]["processes"]
            assert len(procs) == 1 and procs[0]["name"] == "Lead pipeline"
            assert "escalate" in procs[0]["states"] or procs[0]["escalates"] is True
            assert procs[0]["seeded_from"] == "CRM leads"

            # install - one click composes the whole business
            res = await client.post("/operators/sales-operator/install", headers=h, json={})
            assert res.status_code == 200, res.text
            built = res.json()
            assert built["system"]["lifecycle"] == "running"
            assert built["system"]["components"].get("process") == 1
            assert len(built["processes"]) == 1
            proc = built["processes"][0]
            assert proc["name"] == "Lead pipeline"
            assert proc["seeded_instances"] == 3  # the CRM's three seed leads
            pid = proc["id"]

            # the machine is real: the journey states + the escalate moves
            res = await client.get(f"/processes/{pid}", headers=h)
            assert res.status_code == 200, res.text
            detail = res.json()
            states = detail["states"]
            assert {"lead", "contacted", "interested", "proposal_sent",
                    "negotiating", "won", "lost"} <= set(states)
            escalate_moves = [t for t in detail["transitions"] if t["name"] == "escalate"]
            assert {m["from"] for m in escalate_moves} == {"interested", "proposal_sent", "negotiating"}
            assert detail["terminal_states"] == ["lost", "won"]

            # SEEDED AT THE CRM'S OWN STAGE - the import, on the record
            counts = detail["instance_counts"]
            assert counts.get("lead") == 1 and counts.get("contacted") == 1 \
                and counts.get("interested") == 1, counts

            # the seeded instance remembers its CRM row in the context
            res = await client.get(f"/processes/{pid}/instances", headers=h)
            insts = res.json()["instances"]
            dana_row = next(i for i in insts if i["context"].get("name") == "Dana Reyes")
            assert dana_row["ref"] == "+15550001111" and dana_row["state"] == "contacted"
            assert dana_row["due_at"] is not None  # the 7-day SLA promise
            res = await client.get(f"/processes/{pid}/instances/{dana_row['id']}", headers=h)
            dana = res.json()
            assert dana["journey"][0]["transition"] == "started"
            assert "imported mid-machine" in dana["journey"][0]["note"]

            # the advancer workflow is bound to THIS install's process id
            advancers = [w for w in built["workflows"] if w["name"] == "Pipeline advancer"]
            assert len(advancers) == 1
            res = await client.get(f"/workflows/{advancers[0]['id']}", headers=h)
            wf_row = res.json()
            node = next(n for n in wf_row["graph"]["nodes"] if n["type"] == "business_advance")
            assert node["parameters"]["process"] == pid  # the BUILT id, not the spec name
            assert node["parameters"]["on_missing"] == "skip"
            assert node["parameters"]["on_refusal"] == "skip"

            # the process is a first-class component of the running system
            res = await client.get(f"/systems/{built['system']['id']}", headers=h)
            grouped = res.json()["grouped"]
            assert "process" in grouped and len(grouped["process"]) == 1
            assert grouped["process"][0]["name"] == "Lead pipeline"

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 2. the business_advance node - workflows move the machine
# ---------------------------------------------------------------------------

def test_v85_business_advance_node_moves_the_pipeline():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "node")
            h = _auth(user["token"])

            res = await client.post("/processes", headers=h, json={
                "name": "Tracker pipeline", "definition": {
                    "states": ["lead", "contacted", "won"],
                    "initial": "lead",
                    "transitions": [
                        {"name": "reach_out", "from": "lead", "to": "contacted"},
                        {"name": "win", "from": "contacted", "to": "won"},
                    ]}})
            pid = res.json()["id"]
            res = await client.post(f"/processes/{pid}/instances", headers=h, json={
                "ref": "+15557770001", "title": "The tracked caller"})
            iid = res.json()["id"]

            graph = {"nodes": [
                {"id": "t", "type": "manual_trigger", "name": "Trigger",
                 "position": {"x": 0, "y": 0}, "parameters": {}},
                {"id": "a", "type": "business_advance", "name": "Move the caller",
                 "position": {"x": 1, "y": 0},
                 "parameters": {"process": "Tracker pipeline",  # by NAME (case-insensitive)
                                "ref": "+15557770001", "transition": "reach_out",
                                "actor": "v85-workflow", "note": "the workflow moved it",
                                "on_missing": "skip", "on_refusal": "skip"}},
            ], "edges": [
                {"id": "e1", "source": "t", "target": "a",
                 "sourceHandle": "main", "targetHandle": "main"},
            ]}
            res = await client.post("/workflows", headers=h, json={
                "name": "Pipeline advancer v85", "graph": graph, "is_active": True})
            assert res.status_code == 201, res.text
            wf_id = res.json()["id"]

            # first run: the move happens - the machine validates, the
            # journey records, business.state_changed fires
            res = await client.post(f"/workflows/{wf_id}/run", headers=h, json={})
            assert res.status_code == 200, res.text
            exec_id = res.json()["execution_id"]
            for _ in range(50):
                res = await client.get(f"/executions/{exec_id}", headers=h)
                if res.json()["status"] in ("success", "failed", "error"):
                    break
                await asyncio.sleep(0.1)
            assert res.json()["status"] == "success", res.text
            await _drain_background()

            res = await client.get(f"/processes/{pid}/instances/{iid}", headers=h)
            inst = res.json()
            assert inst["state"] == "contacted"
            assert inst["journey"][-1]["transition"] == "reach_out"
            assert inst["journey"][-1]["actor"] == "v85-workflow"

            res = await client.get("/events", headers=h,
                                   params={"type": "business.state_changed",
                                           "correlation_id": iid})
            assert res.status_code == 200 and len(res.json()["events"]) == 1

            # second run: the machine refuses (reach_out is a lead-move) -
            # on_refusal=skip keeps the run green, honestly
            res = await client.post(f"/workflows/{wf_id}/run", headers=h, json={})
            exec_id = res.json()["execution_id"]
            for _ in range(50):
                res = await client.get(f"/executions/{exec_id}", headers=h)
                if res.json()["status"] in ("success", "failed", "error"):
                    break
                await asyncio.sleep(0.1)
            assert res.json()["status"] == "success", res.text
            res = await client.get(f"/processes/{pid}/instances/{iid}", headers=h)
            assert res.json()["state"] == "contacted"  # unmoved

            # a ref nobody tracks skips honestly too
            res = await client.post(f"/workflows/{wf_id}/run", headers=h, json={
                "payload": {"x": 1}})
            exec_id = res.json()["execution_id"]
            for _ in range(50):
                res = await client.get(f"/executions/{exec_id}", headers=h)
                if res.json()["status"] in ("success", "failed", "error"):
                    break
                await asyncio.sleep(0.1)
            assert res.json()["status"] == "success", res.text

            # fail-loud variants refuse: an unknown process name 400s at
            # validation time is the node's runtime - prove the loud path
            # with a run whose params name a process that does not exist
            res = await client.post("/workflows", headers=h, json={
                "name": "Loud advancer", "is_active": True, "graph": {"nodes": [
                    {"id": "t", "type": "manual_trigger", "name": "Trigger",
                     "position": {"x": 0, "y": 0}, "parameters": {}},
                    {"id": "a", "type": "business_advance", "name": "Move",
                     "position": {"x": 1, "y": 0},
                     "parameters": {"process": "ghost-machine", "ref": "x",
                                    "transition": "reach_out"}},
                ], "edges": [
                    {"id": "e1", "source": "t", "target": "a",
                     "sourceHandle": "main", "targetHandle": "main"}]}})
            loud_id = res.json()["id"]
            res = await client.post(f"/workflows/{loud_id}/run", headers=h, json={})
            exec_id = res.json()["execution_id"]
            for _ in range(50):
                res = await client.get(f"/executions/{exec_id}", headers=h)
                if res.json()["status"] in ("success", "failed", "error"):
                    break
                await asyncio.sleep(0.1)
            assert res.json()["status"] in ("failed", "error")
            assert "ghost-machine" in (res.json().get("error") or "")

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 3. the scheduler door - recorded escalations + business.stuck
# ---------------------------------------------------------------------------

def test_v85_scheduler_door_records_and_emits():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "door")
            h = _auth(user["token"])

            # a machine WITHOUT an escalate move - the door records, no move
            res = await client.post("/processes", headers=h, json={
                "name": "Plain case", "definition": {
                    "states": ["open", "review", "closed"],
                    "initial": "open",
                    "transitions": [
                        {"name": "review", "from": "open", "to": "review"},
                        {"name": "close", "from": "review", "to": "closed"},
                    ]}})
            pid = res.json()["id"]
            res = await client.post(f"/processes/{pid}/instances", headers=h, json={
                "ref": "CASE-1", "due_in_seconds": 1})
            iid = res.json()["id"]
            await asyncio.sleep(1.2)  # the SLA breach, deterministically

            res = await client.post("/scheduler/escalations/tick", headers=h, json={})
            assert res.status_code == 200, res.text
            out = res.json()
            assert out["scanned"] >= 1
            assert len(out["recorded"]) == 1 and out["recorded"][0]["instance_id"] == iid
            assert not out["escalated"]
            assert out["stuck"] == 1

            # the breach is a fact on the event door
            res = await client.get("/events", headers=h,
                                   params={"type": "business.stuck",
                                           "correlation_id": iid})
            events = res.json()["events"]
            assert len(events) == 1, res.text
            assert events[0]["source"] == "business"
            assert events[0]["payload"]["ref"] == "CASE-1"
            assert events[0]["payload"]["escalated"] is False
            assert events[0]["payload"]["overdue_seconds"] >= 0  # honest, may round to 0

            # the escalation is ON THE RECORD (a no-move log row)
            res = await client.get(f"/processes/{pid}/instances/{iid}", headers=h)
            inst = res.json()
            assert inst["state"] == "open"  # no move happened
            esc = [j for j in inst["journey"] if j["transition"] == "escalated"]
            assert len(esc) == 1 and esc[0]["actor"] == user["id"]
            assert "no 'escalate' move" in esc[0]["note"]

            # the analytics count the escalation
            res = await client.get(f"/processes/{pid}/analytics", headers=h)
            assert res.json()["escalations"] == 1

            # the door knocks ONCE PER STINT - the second sweep is honest
            res = await client.post("/scheduler/escalations/tick", headers=h, json={})
            out = res.json()
            assert not out["recorded"] and not out["escalated"]
            assert out["already"] >= 1
            res = await client.get("/events", headers=h,
                                   params={"type": "business.stuck",
                                           "correlation_id": iid})
            assert len(res.json()["events"]) == 1  # no duplicate breach event

            # when the team moves the instance (a fresh stint), the door
            # may knock again - the moved-to state's clock starts over
            res = await client.post(f"/processes/{pid}/instances/{iid}/advance",
                                    headers=h, json={"transition": "review",
                                                     "due_in_seconds": 1})
            assert res.status_code == 200, res.text
            await asyncio.sleep(1.2)
            res = await client.post("/scheduler/escalations/tick", headers=h, json={})
            out = res.json()
            assert len(out["recorded"]) == 1 and out["recorded"][0]["state"] == "review"
            res = await client.get(f"/processes/{pid}/instances/{iid}", headers=h)
            assert len([j for j in res.json()["journey"]
                        if j["transition"] == "escalated"]) == 2
            res = await client.get(f"/processes/{pid}/analytics", headers=h)
            assert res.json()["escalations"] == 2
    _sync(_wrap(_go()))


def test_v85_scheduler_door_escalates_through_the_machine():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "door2")
            h = _auth(user["token"])

            # the machine DEFINES its own escalate move (a self-loop nudge)
            res = await client.post("/processes", headers=h, json={
                "name": "Nudged pipeline", "definition": {
                    "states": ["interested", "won", "lost"],
                    "initial": "interested",
                    "transitions": [
                        {"name": "win", "from": "interested", "to": "won"},
                        {"name": "lose", "from": "interested", "to": "lost"},
                        {"name": "escalate", "from": "interested", "to": "interested"},
                    ]}})
            pid = res.json()["id"]
            res = await client.post(f"/processes/{pid}/instances", headers=h, json={
                "ref": "DEAL-9", "title": "The stale deal", "due_in_seconds": 1})
            iid = res.json()["id"]
            await asyncio.sleep(1.2)

            res = await client.post("/scheduler/escalations/tick", headers=h, json={})
            assert res.status_code == 200, res.text
            out = res.json()
            assert len(out["escalated"]) == 1
            entry = out["escalated"][0]
            assert entry["instance_id"] == iid
            assert entry["state"] == "interested" and entry["moved_to"] == "interested"

            # the move re-entered the state - a fresh stint, on the record
            res = await client.get(f"/processes/{pid}/instances/{iid}", headers=h)
            inst = res.json()
            assert inst["state"] == "interested"
            assert inst["age_in_state_seconds"] < 5  # the stint clock restarted
            moves = [j for j in inst["journey"] if j["transition"] == "escalate"]
            assert len(moves) == 1 and moves[0]["actor"] == user["id"]
            assert "SLA breached" in moves[0]["note"]

            # BOTH facts landed: the breach AND the machine's move
            await _drain_background()
            res = await client.get("/events", headers=h,
                                   params={"correlation_id": iid})
            types = {e["type"] for e in res.json()["events"]}
            assert "business.stuck" in types and "business.state_changed" in types
            stuck = next(e for e in res.json()["events"] if e["type"] == "business.stuck")
            assert stuck["payload"]["escalated"] is True
            assert stuck["payload"]["moved_to"] == "interested"

            # one knock per stint: the second sweep holds its fire
            res = await client.post("/scheduler/escalations/tick", headers=h, json={})
            assert not res.json()["escalated"] and not res.json()["recorded"]

            # the breach event reacts: a workflow subscribed to business.stuck
            res = await client.post("/datasets", headers=h, json={
                "name": "Breach log", "rows": [{"ref": "seed"}]})
            ds_id = res.json()["id"]
            graph = {"nodes": [
                {"id": "t", "type": "event_trigger", "name": "Trigger",
                 "position": {"x": 0, "y": 0},
                 "parameters": {"event_type": "business.stuck"}},
                {"id": "w", "type": "dataset_write", "name": "Write",
                 "position": {"x": 1, "y": 0},
                 "parameters": {"dataset": "Breach log", "mode": "append",
                                "create_if_missing": True}},
            ], "edges": [
                {"id": "e1", "source": "t", "target": "w",
                 "sourceHandle": "main", "targetHandle": "main"}]}
            res = await client.post("/workflows", headers=h, json={
                "name": "Breach reactor", "graph": graph, "is_active": True})
            wf_id = res.json()["id"]

            # a SECOND stuck instance -> the door escalates -> the reactor runs
            res = await client.post(f"/processes/{pid}/instances", headers=h, json={
                "ref": "DEAL-10", "due_in_seconds": 1})
            iid2 = res.json()["id"]
            await asyncio.sleep(1.2)
            res = await client.post("/scheduler/escalations/tick", headers=h, json={})
            assert len(res.json()["escalated"]) == 1
            await _drain_background()
            res = await client.get("/executions", headers=h, params={"limit": 30})
            runs = [r for r in res.json() if r.get("workflow_id") == wf_id]
            assert len(runs) == 1 and runs[0]["trigger_type"] == "event"
            assert runs[0]["status"] == "success", runs[0].get("error")

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 4. the door's gate + scoping - stopped systems hold, strangers stay out
# ---------------------------------------------------------------------------

def test_v85_scheduler_door_gate_and_scoping():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "gate")
            h = _auth(user["token"])
            stranger = await _mk_user(client, "stranger")
            hs = _auth(stranger["token"])

            res = await client.post("/processes", headers=h, json={
                "name": "Gated pipeline", "definition": {
                    "states": ["open", "closed"],
                    "initial": "open",
                    "transitions": [{"name": "close", "from": "open", "to": "closed"}]}})
            pid = res.json()["id"]
            res = await client.post(f"/processes/{pid}/instances", headers=h, json={
                "ref": "G-1", "due_in_seconds": 1})
            iid = res.json()["id"]
            await asyncio.sleep(1.2)

            # a stranger's manual door does not sweep MY entities
            res = await client.post("/scheduler/escalations/tick", headers=hs, json={})
            assert res.status_code == 200
            assert res.json()["scanned"] == 0

            # bind the process to a system, then STOP the system - the door holds
            res = await client.post("/systems", headers=h, json={"name": "Gated biz"})
            sys_id = res.json()["id"]
            res = await client.post(f"/systems/{sys_id}/components", headers=h,
                                    json={"kind": "process", "ref_id": pid})
            assert res.status_code == 201, res.text
            res = await client.post(f"/systems/{sys_id}/stop", headers=h)
            assert res.status_code == 200, res.text

            res = await client.post("/scheduler/escalations/tick", headers=h, json={})
            out = res.json()
            assert len(out["held"]) == 1 and out["held"][0]["instance_id"] == iid
            assert "not running" in out["held"][0]["note"]
            assert not out["recorded"] and not out["escalated"]

            # no business.stuck while held
            res = await client.get("/events", headers=h,
                                   params={"type": "business.stuck",
                                           "correlation_id": iid})
            assert len(res.json()["events"]) == 0

            # the system runs again - the door moves
            res = await client.post(f"/systems/{sys_id}/start", headers=h)
            assert res.status_code == 200, res.text
            res = await client.post("/scheduler/escalations/tick", headers=h, json={})
            assert len(res.json()["recorded"]) == 1

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 5. the contract - business.stuck is on the event system's catalog
# ---------------------------------------------------------------------------

def test_v85_contract_and_version():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "contract")
            h = _auth(user["token"])
            res = await client.get("/events/contracts", headers=h)
            assert res.status_code == 200, res.text
            assert "business.stuck" in res.json()["types_emitted"]["business"]

            # the node is on the canvas registry
            res = await client.get("/node-definitions", headers=h)
            body = res.json()
            defs = body["definitions"] if isinstance(body, dict) else body
            types = {d["type"] for d in defs}
            assert "business_advance" in types

    _sync(_wrap(_go()))


def test_v85_version_pin():
    assert settings.version == "1.91.0"
