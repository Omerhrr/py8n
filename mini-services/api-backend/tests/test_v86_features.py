"""v86 tests - the shelf grows to NINE operators + the intake loop closes.

Two moves on top of v85's pre-wired operators:

* SIX more department operators ship bound: support, operations, hr,
  finance, procurement, logistics - each with its machine seeded from
  its dataset (one instance per row AT ITS OWN STAGE), an advancer that
  moves the tracked entity when a real call ends, escalate self-loops
  for the scheduler door, and (finance, logistics) the empty dialer.
* the business_start NODE: the intake side of the machine - workflows
  open NEW tracked instances (ref = the external key the business
  speaks: the sender's phone), so an operator's reactive path now opens
  the ticket row AND starts the tracked case; the machine watches every
  entity from the moment it exists and never doubles one (on_duplicate).

The full reactive loop is proven END TO END: a real sms.received event
opens a row and a tracked instance; a real call.ended from a seeded
phone moves the seeded case; the scheduler door escalates the new
machines through their own escalate moves.
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
        "email": f"v86-{tag}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v86 {tag}",
    })
    assert res.status_code == 201, res.text
    return {"token": res.json()["token"], "id": res.json()["user"]["id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _run_to_end(client: httpx.AsyncClient, h: dict, wf_id: str) -> dict:
    res = await client.post(f"/workflows/{wf_id}/run", headers=h, json={})
    assert res.status_code == 200, res.text
    exec_id = res.json()["execution_id"]
    for _ in range(50):
        res = await client.get(f"/executions/{exec_id}", headers=h)
        if res.json()["status"] in ("success", "failed", "error"):
            break
        await asyncio.sleep(0.1)
    return res.json()


# ---------------------------------------------------------------------------
# 1. the shelf: nine operators, each declaring its topology honestly
# ---------------------------------------------------------------------------

def test_v86_catalog_shows_nine_operators():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "shelf")
            h = _auth(user["token"])

            res = await client.get("/operators", headers=h)
            assert res.status_code == 200, res.text
            ops = {o["slug"]: o for o in res.json()["operators"]}
            assert len(ops) == 9
            assert {"support-operator", "operations-operator", "hr-operator",
                    "finance-operator", "procurement-operator",
                    "logistics-operator"} <= set(ops)

            # the topology counts are the truth of what an install builds
            assert ops["support-operator"]["topology"]["processes"] == 1
            assert ops["support-operator"]["topology"]["workflows"] == 4  # v88: + the generated onboarding loop
            assert ops["hr-operator"]["topology"]["processes"] == 2
            assert ops["finance-operator"]["topology"]["campaign"] == 1
            assert ops["logistics-operator"]["topology"]["campaign"] == 1
            assert ops["procurement-operator"]["topology"]["datasets"] == 3

            # the install plan names the machine, its seeds, its door moves
            res = await client.get("/operators/finance-operator", headers=h)
            assert res.status_code == 200, res.text
            detail = res.json()
            proc = detail["installs"]["processes"][0]
            assert proc["name"] == "Invoice lifecycle"
            assert proc["seeded_from"] == "Invoices"
            assert proc["escalates"] is True
            triggers = {w["trigger"] for w in detail["installs"]["workflows"]}
            assert {"sms.received", "call.ended"} <= triggers

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 2. the six install bound: machines seeded, advancers + intakes resolved
# ---------------------------------------------------------------------------

def test_v86_six_operators_install_bound_and_seeded():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "six")
            h = _auth(user["token"])

            # slug -> (process name, seed state counts, workflow count, campaign)
            expectations = {
                "support-operator": ("Case lifecycle",
                                     {"opened": 1, "assigned": 1, "investigating": 1},
                                     4, False),
                "operations-operator": (None,  # two machines since v89 - the
                                # Customer onboarding desk the won deals land on
                                {"Request lifecycle": {"submitted": 1, "in_review": 1, "approved": 1},
                                 "Customer onboarding": {"kickoff": 1, "training": 1}},
                                5, False),
                "hr-operator": (None,  # two machines - asserted separately
                                {"Onboarding pipeline": {"day_one": 1, "accepted": 1, "buddied": 1},
                                 "Leave pipeline": {"requested": 1, "manager_review": 1, "approved": 1}},
                                4, False),
                "finance-operator": ("Invoice lifecycle",
                                     {"received": 1, "matched": 1, "approved": 1},
                                     4, True),
                "procurement-operator": ("Purchase lifecycle",
                                         {"requested": 1, "quoted": 1, "approved": 1},
                                         4, False),
                "logistics-operator": ("Delivery pipeline",
                                       {"placed": 1, "dispatched": 1, "in_transit": 1},
                                       4, True),
            }

            for slug, (pname, seed_counts, n_wfs, has_campaign) in expectations.items():
                res = await client.post(f"/operators/{slug}/install", headers=h, json={})
                assert res.status_code == 200, res.text
                built = res.json()
                assert built["system"]["lifecycle"] == "running"

                # the machine(s) arrived seeded AT THE ROWS' OWN STAGES
                procs = {p["name"]: p for p in built["processes"]}
                if pname is not None:
                    assert list(procs) == [pname]
                    assert procs[pname]["seeded_instances"] == sum(seed_counts.values())
                    pid = procs[pname]["id"]
                    res2 = await client.get(f"/processes/{pid}", headers=h)
                    counts = res2.json()["instance_counts"]
                    for state, n in seed_counts.items():
                        assert counts.get(state) == n, f"{slug}: {counts}"
                    esc = [t for t in res2.json()["transitions"] if t["name"] == "escalate"]
                    assert len(esc) >= 3, f"{slug}: the door's moves are missing"
                else:
                    for pname2, counts2 in seed_counts.items():
                        assert procs[pname2]["seeded_instances"] == sum(counts2.values())
                        res2 = await client.get(f"/processes/{procs[pname2]['id']}", headers=h)
                        got = res2.json()["instance_counts"]
                        for state, n in counts2.items():
                            assert got.get(state) == n, f"{slug}/{pname2}: {got}"

                # the reactive wiring: exactly the spec workflows, inactive
                assert len(built["workflows"]) == n_wfs
                assert all(w["active"] is False for w in built["workflows"])
                assert (built["campaign"] is not None) is has_campaign

                # advancers and intakes are bound to THIS install's process ids
                for w in built["workflows"]:
                    res2 = await client.get(f"/workflows/{w['id']}", headers=h)
                    for node in res2.json()["graph"]["nodes"]:
                        if node["type"] in ("business_advance", "business_start"):
                            hit_name = node["parameters"]["process"]
                            assert hit_name in {p["id"] for p in built["processes"]}, (
                                f"{slug}: {node['type']} bound to {hit_name!r}, not a built id")

                # the process is a first-class component of the running system
                res2 = await client.get(f"/systems/{built['system']['id']}", headers=h)
                grouped = res2.json()["grouped"]
                assert len(grouped["process"]) == (1 if pname else 2)
                assert grouped["dashboard"] and grouped["voice_agent"]

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 3. the business_start node - workflows open tracked entities, never doubles
# ---------------------------------------------------------------------------

def test_v86_business_start_node_tracks_and_skips_honestly():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "start")
            h = _auth(user["token"])

            res = await client.post("/processes", headers=h, json={
                "name": "Intake machine v86", "definition": {
                    "states": ["new", "tracked", "closed"],
                    "initial": "new",
                    "transitions": [
                        {"name": "track", "from": "new", "to": "tracked"},
                        {"name": "close", "from": "tracked", "to": "closed"},
                    ]}})
            pid = res.json()["id"]

            def _graph(on_duplicate: str) -> dict:
                return {"nodes": [
                    {"id": "t", "type": "manual_trigger", "name": "Trigger",
                     "position": {"x": 0, "y": 0}, "parameters": {}},
                    {"id": "s", "type": "business_start", "name": "Open the case",
                     "position": {"x": 1, "y": 0},
                     "parameters": {"process": "Intake machine v86", "ref": "NEW-1",
                                    "title": "The inbound case",
                                    "context": {"src": "test"}, "due_in_seconds": 3600,
                                    "actor": "v86-workflow",
                                    "on_duplicate": on_duplicate}},
                ], "edges": [
                    {"id": "e1", "source": "t", "target": "s",
                     "sourceHandle": "main", "targetHandle": "main"},
                ]}

            # first run: the entity lands tracked, the SLA promise lands
            res = await client.post("/workflows", headers=h, json={
                "name": "Skip starter", "graph": _graph("skip"), "is_active": True})
            wf_id = res.json()["id"]
            out = await _run_to_end(client, h, wf_id)
            assert out["status"] == "success", out.get("error")

            res = await client.get(f"/processes/{pid}/instances", headers=h)
            insts = res.json()["instances"]
            assert len(insts) == 1
            assert insts[0]["ref"] == "NEW-1" and insts[0]["state"] == "new"
            assert insts[0]["due_at"] is not None
            res = await client.get(f"/processes/{pid}/instances/{insts[0]['id']}", headers=h)
            journey0 = res.json()["journey"][0]
            assert journey0["transition"] == "started" and journey0["actor"] == "v86-workflow"

            # second run: on_duplicate=skip - honest pass, no double
            out = await _run_to_end(client, h, wf_id)
            assert out["status"] == "success", out.get("error")
            res = await client.get(f"/processes/{pid}/instances", headers=h)
            assert len(res.json()["instances"]) == 1

            # the loud variant refuses a duplicate - and the refusal NAMES it
            res = await client.post("/workflows", headers=h, json={
                "name": "Loud starter", "graph": _graph("error"), "is_active": True})
            loud_id = res.json()["id"]
            out = await _run_to_end(client, h, loud_id)
            assert out["status"] in ("failed", "error")
            assert "already carries ref" in (out.get("error") or "")

            # a fresh ref starts loud and clean
            res = await client.put(f"/workflows/{loud_id}", headers=h, json={
                "graph": {"nodes": _graph("error")["nodes"][:-1]
                          + [dict(_graph("error")["nodes"][-1],
                                  parameters={"process": "Intake machine v86",
                                              "ref": "NEW-2", "on_duplicate": "error"})],
                           "edges": _graph("error")["edges"]}})
            assert res.status_code == 200, res.text
            out = await _run_to_end(client, h, loud_id)
            assert out["status"] == "success", out.get("error")
            res = await client.get(f"/processes/{pid}/instances", headers=h)
            assert {i["ref"] for i in res.json()["instances"]} == {"NEW-1", "NEW-2"}

            # the node is on the canvas registry
            res = await client.get("/node-definitions", headers=h)
            body = res.json()
            defs = body["definitions"] if isinstance(body, dict) else body
            assert "business_start" in {d["type"] for d in defs}

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 4. the full reactive loop - a REAL event opens a row AND a tracked case
# ---------------------------------------------------------------------------

def test_v86_intake_reactive_path_end_to_end():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "loop")
            h = _auth(user["token"])

            res = await client.post("/operators/support-operator/install",
                                    headers=h, json={})
            assert res.status_code == 200, res.text
            built = res.json()
            pid = built["processes"][0]["id"]
            ds_id = next(d["id"] for d in built["datasets"] if d["name"].startswith("Support tickets"))
            opener = next(w for w in built["workflows"] if w["name"] == "Ticket opener")
            advancer = next(w for w in built["workflows"] if w["name"] == "Case advancer")

            # boot: the reactive workflows go active (the system runs already)
            for w in (opener, advancer):
                res = await client.put(f"/workflows/{w['id']}", headers=h,
                                       json={"is_active": True})
                assert res.status_code == 200, res.text

            # an UNTRACKED phone texts in -> row AND tracked case, by themselves
            res = await client.post("/events", headers=h, json={
                "type": "sms.received", "source": "sms", "actor": "+15550009999",
                "payload": {"text": "my portal is down since this morning"}})
            assert res.status_code == 201, res.text
            await _drain_background()

            res = await client.get(f"/processes/{pid}/instances", headers=h)
            insts = res.json()["instances"]
            fresh = [i for i in insts if i["ref"] == "+15550009999"]
            assert len(fresh) == 1, insts
            assert fresh[0]["state"] == "opened"
            res = await client.get(f"/datasets/{ds_id}/rows", headers=h)
            rows = res.json() if isinstance(res.json(), list) else res.json().get("rows", [])
            assert any(r.get("phone") == "+15550009999" for r in rows), rows

            # the SAME phone texts again - another row, never a double case
            res = await client.post("/events", headers=h, json={
                "type": "sms.received", "source": "sms", "actor": "+15550009999",
                "payload": {"text": "still down - any news?"}})
            assert res.status_code == 201, res.text
            await _drain_background()
            res = await client.get(f"/processes/{pid}/instances", headers=h)
            assert len([i for i in res.json()["instances"]
                        if i["ref"] == "+15550009999"]) == 1

            # a SEEDED phone calls and hangs up -> the seeded case moves itself
            res = await client.post("/events", headers=h, json={
                "type": "call.ended", "source": "voice", "actor": "+15550003333",
                "payload": {"end_reason": "completed"},
                "session_id": "sess-v86-1"})
            assert res.status_code == 201, res.text
            await _drain_background()

            res = await client.get(f"/processes/{pid}/instances", headers=h)
            june = next(i for i in res.json()["instances"]
                        if i["ref"] == "+15550003333")
            assert june["state"] == "assigned"  # opened -> triage -> assigned
            res = await client.get(f"/processes/{pid}/instances/{june['id']}", headers=h)
            last = res.json()["journey"][-1]
            assert last["transition"] == "triage" and last["actor"] == "support-operator"

            # the advancer's move is a fact on the event door
            res = await client.get("/events", headers=h,
                                   params={"type": "business.state_changed",
                                           "correlation_id": june["id"]})
            assert res.status_code == 200 and len(res.json()["events"]) == 1

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 5. the door escalates the new machines through their own escalate moves
# ---------------------------------------------------------------------------

def test_v86_scheduler_door_escalates_the_new_machines():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "door")
            h = _auth(user["token"])

            # v89: the finance machine walks the DIGEST beat now (its policy
            # is the daily-digest flagship) - the knock-beat door walk uses
            # the procurement machine, same shape (escalate self-loop on
            # the initial state)
            res = await client.post("/operators/procurement-operator/install",
                                    headers=h, json={})
            assert res.status_code == 200, res.text
            pid = res.json()["processes"][0]["id"]

            # the seeded purchases sit far inside their SLA - the door passes
            res = await client.post("/scheduler/escalations/tick", headers=h, json={})
            out = res.json()
            assert out["scanned"] >= 3 and not out["escalated"] and not out["recorded"]

            # a fresh purchase due in one second breaches - the machine's own
            # escalate move (requested -> requested) re-arms the stint
            res = await client.post(f"/processes/{pid}/instances", headers=h, json={
                "ref": "+15550007999", "title": "The stuck purchase",
                "due_in_seconds": 1})
            iid = res.json()["id"]
            await asyncio.sleep(1.2)

            res = await client.post("/scheduler/escalations/tick", headers=h, json={})
            out = res.json()
            assert len(out["escalated"]) == 1, out
            entry = out["escalated"][0]
            assert entry["instance_id"] == iid
            assert entry["state"] == "requested" and entry["moved_to"] == "requested"

            # the stint restarted, on the record
            res = await client.get(f"/processes/{pid}/instances/{iid}", headers=h)
            inst = res.json()
            assert inst["state"] == "requested"
            assert inst["age_in_state_seconds"] < 5
            moves = [j for j in inst["journey"] if j["transition"] == "escalate"]
            assert len(moves) == 1 and moves[0]["actor"] == user["id"]

            # BOTH facts landed on the event door
            await _drain_background()
            res = await client.get("/events", headers=h,
                                   params={"correlation_id": iid})
            types = {e["type"] for e in res.json()["events"]}
            assert {"business.stuck", "business.state_changed"} <= types
            stuck = next(e for e in res.json()["events"] if e["type"] == "business.stuck")
            assert stuck["payload"]["escalated"] is True

            # one knock per stint
            res = await client.post("/scheduler/escalations/tick", headers=h, json={})
            assert not res.json()["escalated"] and not res.json()["recorded"]

            # the analytics count it
            res = await client.get(f"/processes/{pid}/analytics", headers=h)
            assert res.json()["escalations"] == 1

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 6. the version pin
# ---------------------------------------------------------------------------

def test_v86_version_pin():
    assert settings.version == "1.98.0"
