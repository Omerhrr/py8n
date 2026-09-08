"""v89 tests - the digest, the snooze, and the handoff between operators.

v88 closed the agents' loop (read, write-the-memory, move, open, onboard)
and the escalation story's human loop (the receipt + the rotation). v89
rounds the door itself:

* SNOOZE on acks: an ack may carry snooze_hours - the hold becomes a
  LOAN, and when the snooze runs out the door RE-KNOCKS on its cadence
  (attempt count continuing inside the same episode and its cap);
  re-acking replaces the loan; an ack without a snooze still owns the
  rest of the stint (v88 semantics untouched).
* ESCALATION DIGEST: policy mode="digest" replaces the N knocks with ONE
  summary per window - the door lists the stuck items in one
  business.escalation_digest event + one channel message per
  digest_every_seconds, honestly skipping until an endpoint is bound,
  capping per item after 1 + max_repeats appearances.
* CROSS-OPERATOR JOURNEYS: journeys ride the process definition - when
  the machine lands on the fire-state the next leg OPENS ITSELF on the
  target machine (a won deal opening an onboarding case), resolved by
  name at fire time, linked in the opened context, on the record both
  ways (business.journey_opened / business.journey_skipped), never
  double-tracking. The Sales operator ships won -> Customer onboarding;
  the Operations operator ships the landing machine.
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
        "email": f"v89-{tag}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v89 {tag}",
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
# 1. snooze on acks - the hold is a loan, the door re-knocks
# ---------------------------------------------------------------------------

def test_v89_snooze_on_ack():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "snooze")
            h = _auth(user["token"])

            res = await client.post("/processes", headers=h, json={
                "name": "Loan machine",
                "definition": {**MACHINE, "escalation_policy": {
                    "channel": "email", "to": "ops@py8n.test",
                    "repeat_every_seconds": 60, "max_repeats": 5}}})
            pid = res.json()["id"]
            res = await client.post(f"/processes/{pid}/instances", headers=h,
                                    json={"ref": "SNZ-1", "title": "the loaned one",
                                          "due_in_seconds": 1})
            iid = res.json()["id"]
            base = datetime.now(timezone.utc)

            # a negative snooze refuses at the door of the API
            res = await client.post(
                f"/processes/{pid}/instances/{iid}/escalations/ack",
                headers=h, json={"by": "early", "snooze_hours": -1})
            assert res.status_code == 422, res.text

            # the door knocks once, then the human takes it ON A LOAN
            # (1 hour - the door's injected clock walks past it)
            report = await _door(user["id"], now=base + timedelta(seconds=15))
            mine = [e for e in report["recorded"] if e["instance_id"] == iid]
            assert len(mine) == 1 and mine[0]["attempt"] == 1, report

            res = await client.post(
                f"/processes/{pid}/instances/{iid}/escalations/ack",
                headers=h, json={"by": "amara", "note": "on it",
                                 "snooze_hours": 1})
            assert res.status_code == 200, res.text
            ack = res.json()["ack"]
            assert ack["snooze_hours"] == 1.0
            assert ack["snooze_until"], ack
            book = res.json()["instance"]["context"]["escalations"]
            assert book["acked"]["snooze_until"] == ack["snooze_until"]

            # the receipt names the loan: the journey row + the event
            rows = [j for j in res.json()["instance"]["journey"]
                    if j["transition"] == "escalation_acknowledged"]
            assert rows and rows[-1]["payload"].get("snooze_until") == ack["snooze_until"]
            res = await client.get("/events", headers=h,
                                   params={"type": "business.escalation_acknowledged",
                                           "correlation_id": iid})
            evs = res.json()["events"]
            assert evs and evs[0]["payload"].get("snooze_hours") == 1.0

            # within the snooze the door holds - WITH the loan visible
            report = await _door(user["id"], now=base + timedelta(seconds=200))
            held = [x for x in report["held"] if x["instance_id"] == iid]
            assert held and held[0]["reason"] == "acknowledged", report
            assert held[0]["snooze_until"] == ack["snooze_until"]
            assert held[0]["snooze_remaining_seconds"] > 0

            # the snooze ran out (door clock: 4000s > the 1h loan) - the
            # door RE-KNOCKS, the attempt continuing inside the episode
            report = await _door(user["id"], now=base + timedelta(seconds=4000))
            mine = [e for e in report["recorded"] if e["instance_id"] == iid]
            assert len(mine) == 1 and mine[0]["attempt"] == 2, report

            # re-acking replaces the loan (now 2h) - the door holds again
            res = await client.post(
                f"/processes/{pid}/instances/{iid}/escalations/ack",
                headers=h, json={"by": "amara", "snooze_hours": 2})
            assert res.status_code == 200, res.text
            report = await _door(user["id"], now=base + timedelta(seconds=5000))
            held = [x for x in report["held"] if x["instance_id"] == iid]
            assert held and held[0]["reason"] == "acknowledged", report
            # and 9000s walks past the 2h loan - attempt 3
            report = await _door(user["id"], now=base + timedelta(seconds=9000))
            mine = [e for e in report["recorded"] if e["instance_id"] == iid]
            assert len(mine) == 1 and mine[0]["attempt"] == 3, report

            # an ack WITHOUT a snooze owns the rest of the stint (v88)
            res = await client.post(
                f"/processes/{pid}/instances/{iid}/escalations/ack",
                headers=h, json={"by": "bao"})
            assert res.status_code == 200 and "snooze_until" not in res.json()["ack"]
            report = await _door(user["id"], now=base + timedelta(seconds=50000))
            held = [x for x in report["held"] if x["instance_id"] == iid]
            assert held and held[0]["reason"] == "acknowledged", report
            assert "snooze_until" not in held[0]
            res = await client.get("/events", headers=h,
                                   params={"type": "business.escalated",
                                           "correlation_id": iid})
            assert len(res.json()["events"]) == 3  # nothing snuck past the take

            # a state change starts a fresh episode - the door may knock again
            res = await client.post(f"/processes/{pid}/instances/{iid}/advance",
                                    headers=h, json={"transition": "go"})
            assert res.status_code == 200
            report = await _door(user["id"], now=base + timedelta(seconds=50100))
            fresh = [e for e in report["recorded"] if e["instance_id"] == iid]
            assert len(fresh) == 1 and fresh[0]["attempt"] == 1, report

            # the node carries the snooze too - the handler's own reply
            res = await client.post(f"/processes/{pid}/instances", headers=h,
                                    json={"ref": "SNZ-2", "due_in_seconds": 1})
            iid2 = res.json()["id"]
            await _door(user["id"], now=base + timedelta(seconds=50120))
            res = await client.post("/workflows", headers=h, json={
                "name": "Snooze reply", "is_active": False,
                "graph": {"nodes": [
                    {"id": "t", "type": "manual_trigger", "name": "T",
                     "position": {"x": 0, "y": 0}, "parameters": {}},
                    {"id": "ack", "type": "business_ack", "name": "Take it",
                     "position": {"x": 1, "y": 0},
                     "parameters": {"process": "Loan machine", "ref": "SNZ-2",
                                    "by": "the-sms-reply", "snooze_hours": 3}},
                ], "edges": [
                    {"id": "e1", "source": "t", "target": "ack",
                     "sourceHandle": "main", "targetHandle": "main"}]}})
            assert res.status_code == 201, res.text
            detail = await _run_wf(client, h, res.json()["id"])
            assert detail.get("status") == "success", detail
            runs = {r["node_id"]: r for r in detail["node_runs"]}
            out = runs["ack"]["output"]
            assert out["acknowledged"] is True and out["snooze_until"]

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 2. the escalation digest - one summary per window instead of N knocks
# ---------------------------------------------------------------------------

def test_v89_escalation_digest():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "digest")
            h = _auth(user["token"])

            async def _post(policy):
                return await client.post("/processes", headers=h, json={
                    "name": "digest probe",
                    "definition": {**MACHINE, "escalation_policy": policy}})

            # loud refusals: a bogus mode, both clocks, a stray window key
            r = await _post({"channel": "email", "mode": "hourly"})
            assert r.status_code == 400 and "not a mode" in r.json()["detail"]
            r = await _post({"channel": "email", "mode": "digest",
                             "repeat_every_seconds": 60})
            assert r.status_code == 400 and "both clocks" in r.json()["detail"]
            r = await _post({"channel": "email", "digest_every_seconds": 60})
            assert r.status_code == 400 and "only means something" in r.json()["detail"]
            r = await _post({"channel": "email", "mode": "digest",
                             "digest_every_seconds": 10})
            assert r.status_code == 400 and "digest_every_seconds must be >= 60" \
                in r.json()["detail"]

            # the digest machine: one summary per 60s window
            res = await client.post("/processes", headers=h, json={
                "name": "Digest machine",
                "definition": {**MACHINE, "escalation_policy": {
                    "channel": "email", "to": "billing@py8n.test",
                    "mode": "digest", "digest_every_seconds": 60,
                    "max_repeats": 1}}})
            assert res.status_code == 201, res.text
            pid = res.json()["id"]
            assert "digest over email" in res.json()["escalation_summary"]
            pol = res.json()["escalation_policy"]
            assert pol["mode"] == "digest" and pol["digest_every_seconds"] == 60

            for ref in ("INV-1", "INV-2"):
                res = await client.post(f"/processes/{pid}/instances", headers=h,
                                        json={"ref": ref, "title": f"invoice {ref}",
                                              "due_in_seconds": 1})
                assert res.status_code == 201, res.text
            base = datetime.now(timezone.utc)

            # first sight: both items are CANDIDATES - the breach is one
            # event each (fresh), the window just opened, no knocks yet
            report = await _door(user["id"], now=base + timedelta(seconds=10))
            assert report["escalated"] == [] and report["recorded"] == [], report
            assert report["digest"]["sent"] == [], report
            pend = report["digest"]["pending"]
            assert len(pend) == 1 and pend[0]["items"] == 2, report
            res = await client.get("/events", headers=h,
                                   params={"type": "business.stuck"})
            assert len(res.json()["events"]) == 2  # one breach fact per item
            res = await client.get("/events", headers=h,
                                   params={"type": "business.escalated"})
            assert res.json()["events"] == []  # the knock is GONE

            # mid-window: still pending (the oldest item has waited 30s)
            report = await _door(user["id"], now=base + timedelta(seconds=40))
            assert report["digest"]["sent"] == [] and \
                report["digest"]["pending"][0]["items"] == 2, report

            # the window elapses: ONE summary covers BOTH items
            report = await _door(user["id"], now=base + timedelta(seconds=80))
            sent = report["digest"]["sent"]
            assert len(sent) == 1 and sent[0]["items"] == 2, report
            assert sent[0]["delivery"] == "skipped"  # no endpoint bound - honest
            assert "endpoint" in sent[0]["detail"]
            res = await client.get("/events", headers=h,
                                   params={"type": "business.escalation_digest"})
            evs = res.json()["events"]
            assert len(evs) == 1 and evs[0]["payload"]["item_count"] == 2
            assert evs[0]["payload"]["to"] == "billing@py8n.test"
            assert evs[0]["payload"]["window_seconds"] == 60
            assert {it["ref"] for it in evs[0]["payload"]["items"]} == {"INV-1", "INV-2"}
            # the receipts ride the instances (digest paperwork, counted apart)
            res = await client.get(f"/processes/{pid}/analytics", headers=h)
            assert res.json()["digests"] == 2 and res.json()["escalations"] == 0

            # the window re-arms from the send; an ACK quietes one item
            report = await _door(user["id"], now=base + timedelta(seconds=100))
            assert report["digest"]["sent"] == [] and \
                report["digest"]["pending"][0]["items"] == 2, report
            rows = (await client.get(f"/processes/{pid}/instances", headers=h,
                                     params={"state": "a"})).json()["instances"]
            inv1 = next(r for r in rows if r["ref"] == "INV-1")
            res = await client.post(
                f"/processes/{pid}/instances/{inv1['id']}/escalations/ack",
                headers=h, json={"by": "amara"})
            assert res.status_code == 200, res.text

            # next window: the digest lists ONLY the un-acked item
            report = await _door(user["id"], now=base + timedelta(seconds=150))
            sent = report["digest"]["sent"]
            assert len(sent) == 1 and sent[0]["items"] == 1, report
            res = await client.get("/events", headers=h,
                                   params={"type": "business.escalation_digest"})
            evs = res.json()["events"]
            assert len(evs) == 2 and evs[0]["payload"]["item_count"] == 1
            assert evs[0]["payload"]["items"][0]["ref"] == "INV-2"
            assert evs[0]["payload"]["items"][0]["attempt"] == 2

            # the cap: after 1 + max_repeats appearances the item stops
            report = await _door(user["id"], now=base + timedelta(seconds=220))
            held = {x["instance_id"]: x["reason"] for x in report["held"]}
            assert len(held) == 2, report
            assert set(held.values()) == {"acknowledged", "episode_complete"}
            assert report["digest"]["sent"] == [] and report["digest"]["pending"] == []

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 3. cross-operator journeys - the machine lands, the next leg opens
# ---------------------------------------------------------------------------

def test_v89_cross_operator_journeys():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "journey")
            h = _auth(user["token"])

            # loud refusals: unknown fire-state, no target, unknown keys,
            # two journeys on one state
            res = await client.post("/processes", headers=h, json={
                "name": "bad fire", "definition": {**MACHINE, "journeys": [
                    {"on_state": "weird", "open": {"process": "X"}}]}})
            assert res.status_code == 400 and "not a state" in res.json()["detail"]
            res = await client.post("/processes", headers=h, json={
                "name": "bad open", "definition": {**MACHINE, "journeys": [
                    {"on_state": "done", "open": {}}]}})
            assert res.status_code == 400 and "opens nothing" in res.json()["detail"]
            res = await client.post("/processes", headers=h, json={
                "name": "bad key", "definition": {**MACHINE, "journeys": [
                    {"on_state": "done", "open": {"process": "X", "webhook": "y"}}]}})
            assert res.status_code == 400 and "unknown key(s)" in res.json()["detail"]
            res = await client.post("/processes", headers=h, json={
                "name": "bad dup", "definition": {**MACHINE, "journeys": [
                    {"on_state": "done", "open": {"process": "X"}},
                    {"on_state": "done", "open": {"process": "Y"}}]}})
            assert res.status_code == 400 and "duplicate journey" in res.json()["detail"]

            # the landing machine + the deal machine with a journey on won
            res = await client.post("/processes", headers=h, json={
                "name": "Onboarding machine v89", "definition": {
                    "states": ["kickoff", "provisioning", "done"],
                    "initial": "kickoff",
                    "transitions": [
                        {"name": "provision", "from": "kickoff", "to": "provisioning"},
                        {"name": "finish", "from": "provisioning", "to": "done"}]}})
            assert res.status_code == 201, res.text
            res = await client.post("/processes", headers=h, json={
                "name": "Deal machine v89", "definition": {
                    "states": ["lead", "won", "lost"], "initial": "lead",
                    "transitions": [
                        {"name": "win", "from": "lead", "to": "won"},
                        {"name": "lose", "from": "lead", "to": "lost"}],
                    "journeys": [{"on_state": "won", "open": {
                        "process": "Onboarding machine v89",
                        "title_template": "Onboarding - {title}",
                        "memory": {"via": "v89-test"}}}]}})
            assert res.status_code == 201, res.text
            deal_pid = res.json()["id"]
            assert res.json()["journeys"][0]["open"]["process"] == \
                "Onboarding machine v89"

            # the deal wins - the onboarding case OPENS ITSELF
            res = await client.post(f"/processes/{deal_pid}/instances", headers=h,
                                    json={"ref": "D-1", "title": "Big deal"})
            iid = res.json()["id"]
            res = await client.post(f"/processes/{deal_pid}/instances/{iid}/advance",
                                    headers=h, json={"transition": "win",
                                                     "actor": "the-closer"})
            assert res.status_code == 200, res.text
            opened = res.json().get("journeys_opened")
            assert opened and opened[0]["opened"] is True, res.text
            assert opened[0]["target"]["process_name"] == "Onboarding machine v89"
            assert opened[0]["target"]["ref"] == "D-1"
            assert opened[0]["target"]["title"] == "Onboarding - Big deal"
            assert opened[0]["target"]["state"] == "kickoff"

            # the opened leg: linked memory + its own life
            res = await client.get("/processes", headers=h)
            onb_pid = next(p["id"] for p in res.json()["processes"]
                           if p["name"] == "Onboarding machine v89")
            res = await client.get(f"/processes/{onb_pid}/instances", headers=h)
            case = next(r for r in res.json()["instances"] if r["ref"] == "D-1")
            assert case["title"] == "Onboarding - Big deal"
            assert case["context"]["via"] == "v89-test"
            assert case["context"]["journey"]["from_instance"] == iid
            assert case["context"]["journey"]["from_state"] == "won"

            # on the record both ways: business.journey_opened on the deal's
            # correlation thread
            res = await client.get("/events", headers=h,
                                   params={"type": "business.journey_opened",
                                           "correlation_id": iid})
            evs = res.json()["events"]
            assert len(evs) == 1
            assert evs[0]["payload"]["target"]["process_name"] == "Onboarding machine v89"
            assert evs[0]["payload"]["target"]["ref"] == "D-1"
            assert evs[0]["payload"]["on_state"] == "won"

            # the honest skip: a journey whose target was never built
            res = await client.post("/processes", headers=h, json={
                "name": "Ghost deal machine", "definition": {
                    "states": ["lead", "won"], "initial": "lead",
                    "transitions": [{"name": "win", "from": "lead", "to": "won"}],
                    "journeys": [{"on_state": "won", "open": {
                        "process": "Ghost onboarding"}}]}})
            gpid = res.json()["id"]
            res = await client.post(f"/processes/{gpid}/instances", headers=h,
                                    json={"ref": "G-1"})
            giid = res.json()["id"]
            res = await client.post(f"/processes/{gpid}/instances/{giid}/advance",
                                    headers=h, json={"transition": "win"})
            assert res.status_code == 200, res.text
            opened = res.json().get("journeys_opened")
            assert opened and opened[0]["opened"] is False
            assert "not found" in opened[0]["reason"]
            res = await client.get("/events", headers=h,
                                   params={"type": "business.journey_skipped",
                                           "correlation_id": giid})
            evs = res.json()["events"]
            assert evs and "Ghost onboarding" in evs[0]["payload"]["reason"]

            # never double-tracks: another D-1 winning skips - the case
            # is already open on the landing machine
            res = await client.post(f"/processes/{deal_pid}/instances", headers=h,
                                    json={"ref": "D-1", "title": "Big deal again"})
            iid2 = res.json()["id"]
            res = await client.post(f"/processes/{deal_pid}/instances/{iid2}/advance",
                                    headers=h, json={"transition": "win"})
            assert res.status_code == 200, res.text
            opened = res.json().get("journeys_opened")
            assert opened and opened[0]["opened"] is False
            assert "already carries ref" in opened[0]["reason"]

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 4. the operator story - a won deal opening an onboarding case
# ---------------------------------------------------------------------------

def test_v89_won_deal_opens_onboarding_case():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "handoff")
            h = _auth(user["token"])

            # the landing operator first, the selling operator second -
            # the journey resolves at FIRE time, so the order never matters
            res = await client.post("/operators/operations-operator/install",
                                    headers=h, json={})
            assert res.status_code == 200, res.text
            built_ops = res.json()
            onb = next(p for p in built_ops["processes"]
                       if p["name"] == "Customer onboarding")
            assert onb["seeded_instances"] == 2  # the desk's own import
            assert len(built_ops["workflows"]) == 5  # 3 intakes + 2 on-ramps

            res = await client.post("/operators/sales-operator/install",
                                    headers=h, json={})
            assert res.status_code == 200, res.text
            lead = next(p for p in res.json()["processes"]
                        if p["name"] == "Lead pipeline")

            # the built machine carries the cross-operator journey
            res = await client.get(f"/processes/{lead['id']}", headers=h)
            journeys = res.json()["journeys"]
            assert journeys and journeys[0]["on_state"] == "won"
            assert journeys[0]["open"]["process"] == "Customer onboarding"

            # a fresh lead walks the pipeline ... and WINS
            res = await client.post(f"/processes/{lead['id']}/instances",
                                    headers=h, json={
                                        "ref": "+15557770123",
                                        "title": "Journey Lead",
                                        "context": {"company": "Journey Co"}})
            iid = res.json()["id"]
            for move in ("reach_out", "qualify", "book_demo", "run_demo",
                         "send_proposal", "negotiate", "win"):
                res = await client.post(
                    f"/processes/{lead['id']}/instances/{iid}/advance",
                    headers=h, json={"transition": move})
                assert res.status_code == 200, res.text
                if move == "win":
                    opened = res.json().get("journeys_opened")
                    assert opened and opened[0]["opened"] is True, res.text

            # the onboarding case EXISTS - at kickoff, linked to the deal
            res = await client.get(f"/processes/{onb['id']}/instances", headers=h)
            rows = res.json()["instances"]
            case = next(r for r in rows if r["ref"] == "+15557770123")
            assert case["state"] == "kickoff"
            assert case["title"] == "Onboarding - Journey Lead"
            assert case["context"]["journey"]["from_process"] == "Lead pipeline"
            assert case["context"]["journey"]["from_ref"] == "+15557770123"
            assert case["context"]["via"] == "won-deal journey"
            counts = (await client.get(f"/processes/{onb['id']}", headers=h)
                      ).json()["instance_counts"]
            assert counts.get("kickoff") == 2 and counts.get("training") == 1

            # and the handoff is on the record
            res = await client.get("/events", headers=h,
                                   params={"type": "business.journey_opened",
                                           "correlation_id": iid})
            evs = res.json()["events"]
            assert evs and evs[0]["payload"]["target"]["process_name"] == \
                "Customer onboarding"

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 5. version pin
# ---------------------------------------------------------------------------

def test_v89_version_pin():
    assert settings.version == "1.100.0"
