"""v88 tests - the agents' memory door (business_annotate), the human's
receipt (escalation acknowledgement + handler rotation), and the
department onboarding loops (business_onboard + the generated
dataset-trigger loops on every operator machine).

v87 gave the agents a READ door (business_query) and the door a POLICY
(channel + repeat). v88 completes the loop:

* business_annotate: agents WRITE facts directly into an entity's
  running memory without moving the machine - on the record (an
  'annotate' journey row), emitting business.annotated so a workflow can
  react to a FACT landing; the door's bookkeeping key refuses loud.
* handler rotation: the policy's handlers roster round-robins the
  delivery by attempt (attempt N -> handlers[(N-1) % len]); 'to' and
  'handlers' are mutually exclusive - the door refuses to guess.
* acknowledgement: a named human take quiets the episode for the rest
  of the state stint (the receipt on the record + the event); a state
  change starts a fresh episode and the door may knock again.
* business_onboard: a dataset's rows become tracked instances at each
  row's own stage, idempotently - and every operator machine ships the
  loop generated from its own seed spec (the department's data on-ramp
  beside the channel intakes).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.config import settings
from app.main import app

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
        "email": f"v88-{tag}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v88 {tag}",
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


async def _run_wf(client: httpx.AsyncClient, h: dict, wf_id: str) -> dict:
    res = await client.post(f"/workflows/{wf_id}/run", headers=h,
                            json={"payload": {}})
    assert res.status_code in (200, 202), res.text
    exec_id = res.json()["execution_id"]
    detail: dict = {}
    for _ in range(80):
        detail = (await client.get(f"/executions/{exec_id}", headers=h)).json()
        if detail.get("status") != "running":
            break
        await asyncio.sleep(0.05)
    return detail


# ---------------------------------------------------------------------------
# 1. business_annotate - the agents' memory door: facts land, machine stays
# ---------------------------------------------------------------------------

def test_v88_annotate_memory_door():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "annotate")
            h = _auth(user["token"])

            # the nodes ship in the registry (68 node types)
            res = await client.get("/node-definitions", headers=h)
            types = {d["type"] for d in res.json()["definitions"]}
            assert {"business_annotate", "business_ack",
                    "business_onboard"} <= types

            res = await client.post("/processes", headers=h, json={
                "name": "Memory machine", "definition": MACHINE})
            pid = res.json()["id"]
            res = await client.post(f"/processes/{pid}/instances", headers=h,
                                    json={"ref": "ANN-1", "title": "the remembered one"})
            iid = res.json()["id"]

            # the API door: facts land, the machine does not move
            res = await client.post(
                f"/processes/{pid}/instances/{iid}/annotate", headers=h, json={
                    "context_patch": {"budget": 4200, "channel_pref": "whatsapp"},
                    "actor": "closer-agent",
                    "note": "the number the lead gave on the discovery call"})
            assert res.status_code == 200, res.text
            out = res.json()
            assert out["state"] == "a" and out["context"]["budget"] == 4200
            assert out["context"]["channel_pref"] == "whatsapp"
            ann_row = [j for j in out["journey"] if j["transition"] == "annotate"]
            assert len(ann_row) == 1 and ann_row[0]["actor"] == "closer-agent"
            assert ann_row[0]["payload"]["keys"] == ["budget", "channel_pref"]

            # the event: business.annotated on the correlation thread
            res = await client.get("/events", headers=h,
                                   params={"type": "business.annotated",
                                           "correlation_id": iid})
            evs = res.json()["events"]
            assert len(evs) == 1
            assert evs[0]["payload"]["keys"] == ["budget", "channel_pref"]
            assert evs[0]["payload"]["state"] == "a"

            # the analytics count facts separately from moves
            res = await client.get(f"/processes/{pid}/analytics", headers=h)
            a = res.json()
            assert a["annotations"] == 1 and a["acknowledgements"] == 0
            assert "annotate" not in a["advance_counts"]
            assert a["escalations"] == 0

            # loud refusals: the door's bookkeeping key, an empty patch
            res = await client.post(
                f"/processes/{pid}/instances/{iid}/annotate", headers=h,
                json={"context_patch": {"escalations": {"count": 99}}})
            assert res.status_code == 400 and "reserved" in res.json()["detail"]
            res = await client.post(
                f"/processes/{pid}/instances/{iid}/annotate", headers=h,
                json={"context_patch": {}})
            assert res.status_code == 400 and "non-empty" in res.json()["detail"]

            # a terminal entity still remembers - facts keep landing
            res = await client.post(f"/processes/{pid}/instances/{iid}/advance",
                                    headers=h, json={"transition": "kill"})
            assert res.status_code == 200 and res.json()["is_terminal"] is True
            res = await client.post(
                f"/processes/{pid}/instances/{iid}/annotate", headers=h,
                json={"context_patch": {"real_close": "2026-09-12"},
                      "note": "the date that arrived after the kill"})
            assert res.status_code == 200
            assert res.json()["context"]["real_close"] == "2026-09-12"

            # the node: an agent's workflow annotates BY REF
            res = await client.post(f"/processes/{pid}/instances", headers=h,
                                    json={"ref": "ANN-2", "title": "the other one"})
            res = await client.post("/workflows", headers=h, json={
                "name": "Agent remembers", "is_active": False,
                "graph": {"nodes": [
                    {"id": "t", "type": "manual_trigger", "name": "T",
                     "position": {"x": 0, "y": 0}, "parameters": {}},
                    {"id": "ann", "type": "business_annotate", "name": "Remember",
                     "position": {"x": 1, "y": 0},
                     "parameters": {"process": "Memory machine", "ref": "ANN-2",
                                    "context_patch": {"score": 88},
                                    "actor": "scorer-agent"}},
                ], "edges": [
                    {"id": "e1", "source": "t", "target": "ann",
                     "sourceHandle": "main", "targetHandle": "main"}]}})
            assert res.status_code == 201, res.text
            wf_id = res.json()["id"]
            detail = await _run_wf(client, h, wf_id)
            assert detail.get("status") == "success", detail
            runs = {r["node_id"]: r for r in detail["node_runs"]}
            out = runs["ann"]["output"]
            assert out["annotated"] is True and out["state"] == "a"
            assert out["keys"] == ["score"] and out["context"]["score"] == 88

            # an unknown ref skips honestly when on_missing=skip
            res = await client.post("/workflows", headers=h, json={
                "name": "Agent remembers (skip)", "is_active": False,
                "graph": {"nodes": [
                    {"id": "t", "type": "manual_trigger", "name": "T",
                     "position": {"x": 0, "y": 0}, "parameters": {}},
                    {"id": "ann", "type": "business_annotate", "name": "Remember",
                     "position": {"x": 1, "y": 0},
                     "parameters": {"process": "Memory machine", "ref": "GHOST",
                                    "context_patch": {"x": 1},
                                    "on_missing": "skip"}},
                ], "edges": [
                    {"id": "e1", "source": "t", "target": "ann",
                     "sourceHandle": "main", "targetHandle": "main"}]}})
            detail = await _run_wf(client, h, res.json()["id"])
            assert detail.get("status") == "success", detail
            runs = {r["node_id"]: r for r in detail["node_runs"]}
            out = runs["ann"]["output"]
            assert out["annotated"] is False and out["skipped"] is True

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 2. the handlers rotation - attempt N rides handlers[(N-1) % len]
# ---------------------------------------------------------------------------

def test_v88_handler_rotation_policy():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "rotation")
            h = _auth(user["token"])

            async def _post(policy):
                return await client.post("/processes", headers=h, json={
                    "name": "rotation probe",
                    "definition": {**MACHINE, "escalation_policy": policy}})

            # loud refusals: both promises set, too many handlers, bad shape
            r = await _post({"channel": "email", "to": "ops@py8n.test",
                             "handlers": ["a@py8n.test"]})
            assert r.status_code == 400 and "not both" in r.json()["detail"]
            r = await _post({"channel": "email",
                             "handlers": [f"h{i}@py8n.test" for i in range(11)]})
            assert r.status_code == 400 and "caps at 10" in r.json()["detail"]
            r = await _post({"channel": "email", "handlers": "ops@py8n.test"})
            assert r.status_code == 400 and "must be a list" in r.json()["detail"]
            r = await _post({"channel": "email", "handlers": ["a@py8n.test", "  "]})
            assert r.status_code == 400 and "non-empty" in r.json()["detail"]

            # the rotation machine: no pinned 'to' - the roster IS the promise
            res = await client.post("/processes", headers=h, json={
                "name": "Rotation machine",
                "definition": {**MACHINE, "escalation_policy": {
                    "channel": "email",
                    "handlers": ["a@py8n.test", "b@py8n.test", "c@py8n.test"],
                    "repeat_every_seconds": 60, "max_repeats": 2}}})
            assert res.status_code == 201, res.text
            pid = res.json()["id"]
            assert "rotate 3" in res.json()["escalation_summary"]
            assert "a@py8n.test" in res.json()["escalation_summary"]

            res = await client.post(f"/processes/{pid}/instances", headers=h,
                                    json={"ref": "ROT-1", "title": "the rotating one",
                                          "due_in_seconds": 1})
            iid = res.json()["id"]

            base = datetime.now(timezone.utc)
            targets = []
            for n, offset in enumerate((15, 80, 145)):
                report = await _door(user["id"],
                                     now=base + timedelta(seconds=offset))
                mine = [e for e in report["recorded"] if e["instance_id"] == iid]
                assert len(mine) == 1 and mine[0]["attempt"] == n + 1, report
                res = await client.get("/events", headers=h,
                                       params={"type": "business.escalated",
                                               "correlation_id": iid})
                evs = res.json()["events"]  # newest first
                assert len(evs) == n + 1
                ev = next(e for e in evs if e["payload"]["attempt"] == n + 1)
                assert ev["payload"]["rotated"] is True
                targets.append(ev["payload"]["to"])
            assert targets == ["a@py8n.test", "b@py8n.test", "c@py8n.test"], targets

            # the roster is walked once (3 attempts = 3 handlers); the cap holds
            report = await _door(user["id"],
                                 now=base + timedelta(seconds=600))
            held = [x for x in report["held"] if x["instance_id"] == iid]
            assert held and held[0]["reason"] == "episode_complete", report
            assert held[0]["attempts"] == 3

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 3. the acknowledgement - the human's receipt quiets the episode
# ---------------------------------------------------------------------------

def test_v88_acknowledgement_holds_the_door():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "ack")
            h = _auth(user["token"])

            res = await client.post("/processes", headers=h, json={
                "name": "Receipt machine",
                "definition": {**MACHINE, "escalation_policy": {
                    "channel": "email", "to": "ops@py8n.test",
                    "repeat_every_seconds": 60, "max_repeats": 5}}})
            pid = res.json()["id"]
            res = await client.post(f"/processes/{pid}/instances", headers=h,
                                    json={"ref": "ACK-1", "title": "the owned one",
                                          "due_in_seconds": 1})
            iid = res.json()["id"]

            # acknowledging silence refuses loud
            res = await client.post(
                f"/processes/{pid}/instances/{iid}/escalations/ack",
                headers=h, json={"by": "too-early"})
            assert res.status_code == 400
            assert "has not knocked" in res.json()["detail"]

            base = datetime.now(timezone.utc)
            report = await _door(user["id"], now=base + timedelta(seconds=15))
            mine = [e for e in report["recorded"] if e["instance_id"] == iid]
            assert len(mine) == 1 and mine[0]["attempt"] == 1, report

            # the receipt: a named human takes it
            res = await client.post(
                f"/processes/{pid}/instances/{iid}/escalations/ack",
                headers=h, json={"by": "amara", "note": "on it - calling now"})
            assert res.status_code == 200, res.text
            out = res.json()
            assert out["ack"]["by"] == "amara"
            assert out["instance"]["context"]["escalations"]["acked"]["by"] == "amara"
            ack_rows = [j for j in out["instance"]["journey"]
                        if j["transition"] == "escalation_acknowledged"]
            assert len(ack_rows) == 1 and ack_rows[0]["actor"] == "amara"

            # the event: business.escalation_acknowledged on the thread
            res = await client.get("/events", headers=h,
                                   params={"type": "business.escalation_acknowledged",
                                           "correlation_id": iid})
            evs = res.json()["events"]
            assert len(evs) == 1 and evs[0]["payload"]["acknowledged_by"] == "amara"

            # the door holds - acknowledged outranks the cadence
            report = await _door(user["id"], now=base + timedelta(seconds=200))
            held = [x for x in report["held"] if x["instance_id"] == iid]
            assert held and held[0]["reason"] == "acknowledged", report
            assert held[0]["acked_by"] == "amara"
            # and the event timeline stays at exactly one escalation
            res = await client.get("/events", headers=h,
                                   params={"type": "business.escalated",
                                           "correlation_id": iid})
            assert len(res.json()["events"]) == 1

            # the analytics count the receipt
            res = await client.get(f"/processes/{pid}/analytics", headers=h)
            assert res.json()["acknowledgements"] == 1

            # a state change starts a FRESH episode - the door may knock again
            res = await client.post(f"/processes/{pid}/instances/{iid}/advance",
                                    headers=h, json={"transition": "go"})
            assert res.status_code == 200
            report = await _door(user["id"], now=base + timedelta(seconds=300))
            fresh = [e for e in report["recorded"] if e["instance_id"] == iid]
            assert len(fresh) == 1 and fresh[0]["attempt"] == 1, report

            # a NO-policy machine can take the receipt too - the marker
            # path (the door's no-move row this stint admits the ack)
            res = await client.post("/processes", headers=h, json={
                "name": "Quiet receipt machine", "definition": MACHINE})
            qpid = res.json()["id"]
            res = await client.post(f"/processes/{qpid}/instances", headers=h,
                                    json={"ref": "ACK-2", "due_in_seconds": 1})
            qiid = res.json()["id"]
            await _door(user["id"], now=base + timedelta(seconds=310))
            res = await client.post(
                f"/processes/{qpid}/instances/{qiid}/escalations/ack",
                headers=h, json={"by": "bao"})
            assert res.status_code == 200, res.text

            # the ack rides the node too: a workflow reacting to the reply
            res = await client.post(f"/processes/{pid}/instances", headers=h,
                                    json={"ref": "ACK-3", "due_in_seconds": 1})
            iid3 = res.json()["id"]
            await _door(user["id"], now=base + timedelta(seconds=320))
            res = await client.post("/workflows", headers=h, json={
                "name": "Reply acks", "is_active": False,
                "graph": {"nodes": [
                    {"id": "t", "type": "manual_trigger", "name": "T",
                     "position": {"x": 0, "y": 0}, "parameters": {}},
                    {"id": "ack", "type": "business_ack", "name": "Take it",
                     "position": {"x": 1, "y": 0},
                     "parameters": {"process": "Receipt machine", "ref": "ACK-3",
                                    "by": "the-sms-reply", "note": "handler texted 1"}},
                ], "edges": [
                    {"id": "e1", "source": "t", "target": "ack",
                     "sourceHandle": "main", "targetHandle": "main"}]}})
            assert res.status_code == 201, res.text
            detail = await _run_wf(client, h, res.json()["id"])
            assert detail.get("status") == "success", detail
            runs = {r["node_id"]: r for r in detail["node_runs"]}
            out = runs["ack"]["output"]
            assert out["acknowledged"] is True
            assert out["acknowledged_by"] == "the-sms-reply"
            assert out["attempt"] == 1

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 4. business_onboard - the data on-ramp, idempotent by design
# ---------------------------------------------------------------------------

def test_v88_business_onboard_node():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "onboard")
            h = _auth(user["token"])

            res = await client.post("/processes", headers=h, json={
                "name": "Onboard machine", "definition": MACHINE})
            pid = res.json()["id"]
            res = await client.post("/datasets", headers=h, json={
                "name": "Onboard intake",
                "description": "the rows the department just landed",
                "rows": [
                    {"phone": "+15550001001", "who": "Dana", "stage": "a"},
                    {"phone": "+15550001002", "who": "Ari", "stage": "b"},
                    {"phone": "", "who": "NoRef", "stage": "a"},
                    {"phone": "+15550001003", "who": "Mia", "stage": "warp"},
                ]})
            assert res.status_code == 201, res.text
            ds_id = res.json()["id"]

            graph = {"nodes": [
                {"id": "t", "type": "manual_trigger", "name": "T",
                 "position": {"x": 0, "y": 0}, "parameters": {}},
                {"id": "ob", "type": "business_onboard", "name": "Onboard",
                 "position": {"x": 1, "y": 0},
                 "parameters": {"process": "Onboard machine",
                                "dataset": "Onboard intake",
                                "ref_column": "phone",
                                "state_column": "stage",
                                "title_columns": ["who"]}},
            ], "edges": [
                {"id": "e1", "source": "t", "target": "ob",
                 "sourceHandle": "main", "targetHandle": "main"}]}
            res = await client.post("/workflows", headers=h, json={
                "name": "The on-ramp", "graph": graph, "is_active": False})
            assert res.status_code == 201, res.text
            wf_id = res.json()["id"]

            detail = await _run_wf(client, h, wf_id)
            assert detail.get("status") == "success", detail
            runs = {r["node_id"]: r for r in detail["node_runs"]}
            out = runs["ob"]["output"]
            assert out["rows_scanned"] == 4, out
            assert out["started"] == 2 and out["no_ref_rows"] == 1
            assert out["refused"] == [{"ref": "+15550001003",
                                       "reason": "stage 'warp' is not a state of this machine"}]
            by_ref = {i["ref"]: i for i in out["instances"]}
            assert by_ref["+15550001001"]["state"] == "a"
            assert by_ref["+15550001002"]["state"] == "b"  # arrives AS IT IS
            assert by_ref["+15550001002"]["title"] == "Ari"

            # idempotent: a re-run (or a trigger fire) never double-tracks
            detail = await _run_wf(client, h, wf_id)
            runs = {r["node_id"]: r for r in detail["node_runs"]}
            out = runs["ob"]["output"]
            assert out["started"] == 0 and out["already_tracked"] == 2, out

            res = await client.get(f"/processes/{pid}/instances", headers=h)
            assert len(res.json()["instances"]) == 2

            # loud: an unknown dataset refuses by name
            res = await client.post("/workflows", headers=h, json={
                "name": "On-ramp missing", "is_active": False,
                "graph": {**graph, "nodes": [
                    {**n, "parameters": {**n["parameters"], "dataset": "Nope"}}
                    if n["id"] == "ob" else n for n in graph["nodes"]]}})
            detail = await _run_wf(client, h, res.json()["id"])
            assert detail["status"] in ("failed", "error")
            assert "not found" in (detail.get("error") or "").lower()

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 5. the department onboarding loops - every machine ships its data on-ramp
# ---------------------------------------------------------------------------

def test_v88_department_onboarding_loops():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "loops")
            h = _auth(user["token"])

            # the install plan names the loop per machine
            res = await client.get("/operators/support-operator", headers=h)
            wfs = {w["name"]: w for w in res.json()["installs"]["workflows"]}
            loop = wfs.get("Case lifecycle onboarding")
            assert loop is not None
            assert loop["trigger"].startswith("dataset:"), loop

            # install: the loop builds with RESOLVED params - the BUILT
            # process id and the BUILT dataset name (never the spec's)
            res = await client.post("/operators/support-operator/install",
                                    headers=h, json={"note": "v88 loops"})
            assert res.status_code == 200, res.text
            built = res.json()
            loop_wf = next(w for w in built["workflows"]
                           if w["name"] == "Case lifecycle onboarding")
            proc = built["processes"][0]
            assert proc["name"].startswith("Case lifecycle")
            ds = next(d for d in built["datasets"]
                      if d["name"].startswith("Support tickets"))

            res = await client.get(f"/workflows/{loop_wf['id']}", headers=h)
            graph = res.json()["graph"]
            step = next(n for n in graph["nodes"]
                        if n["type"] == "business_onboard")
            assert step["parameters"]["process"] == proc["id"], step
            assert step["parameters"]["dataset"] == ds["name"], step
            assert step["parameters"]["ref_column"] == "phone"

            # the seeded rows are already tracked - the loop skips them
            res = await client.post(f"/workflows/{loop_wf['id']}/run",
                                    headers=h, json={"payload": {}})
            exec_id = res.json()["execution_id"]
            for _ in range(80):
                detail = (await client.get(f"/executions/{exec_id}", headers=h)).json()
                if detail.get("status") != "running":
                    break
                await asyncio.sleep(0.05)
            assert detail.get("status") == "success", detail
            runs = {r["node_id"]: r for r in detail["node_runs"]}
            out = runs[step["id"]]["output"]
            assert out["started"] == 0 and out["already_tracked"] >= 1, out

            # a NEW row lands (the month-end spreadsheet) -> the loop onboards
            res = await client.post(f"/datasets/{ds['id']}/rows", headers=h,
                                    json={"rows": [
                                        {"phone": "+15557770001",
                                         "subject": "the portal login loop",
                                         "status": "assigned"}]})
            assert res.status_code in (200, 201), res.text
            res = await client.post(f"/workflows/{loop_wf['id']}/run",
                                    headers=h, json={"payload": {}})
            exec_id = res.json()["execution_id"]
            for _ in range(80):
                detail = (await client.get(f"/executions/{exec_id}", headers=h)).json()
                if detail.get("status") != "running":
                    break
                await asyncio.sleep(0.05)
            runs = {r["node_id"]: r for r in detail["node_runs"]}
            out = runs[step["id"]]["output"]
            assert out["started"] == 1, out
            assert out["instances"][0]["ref"] == "+15557770001"
            assert out["instances"][0]["state"] == "assigned"

            # every operator ships the loop for each of its machines
            res = await client.get("/operators", headers=h)
            shelf = {o["slug"]: o for o in res.json()["operators"]}
            assert all(o["topology"]["workflows"] >= o["topology"]["processes"]
                       for o in shelf.values())

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 6. version pin
# ---------------------------------------------------------------------------

def test_v88_version_pin():
    assert settings.version == "1.102.0"
