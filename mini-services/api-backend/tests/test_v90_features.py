"""v90 tests - the digest lands in the inbox, and the chains span three
departments.

v89 gave the door its quiet rhythms (one digest per window, snoozed
acks) and its first cross-operator handoff (a won deal opening an
onboarding case). v90 closes the loop on both:

* DIGEST OVER A BOUND ENDPOINT: the digest no longer skips honestly at
  the last mile - a REAL email endpoint (email_inbound, SMTP out) is
  bound and the summary crosses the actual wire into a real inbox
  (scripts/dev_smtp_sink.py - a minimal RFC 5321 server on a real
  socket; smtplib, the exact client the SMTP transport uses, does the
  talking). Email is long-form: the subject line now carries the scan
  line (digest + knock), the body carries the detail.
* THREE-OPERATOR JOURNEY CHAINS: legs thread into chains that span three
  departments, one ref riding every leg, each opened leg carrying its
  own SLA promise (journey open.due_in_seconds - validated, refused
  loud) so the door and its digests watch it from birth:
    the REVENUE chain: Sales --won--> Operations --handed_off--> Finance
    the SUPPLY chain:  Procurement --ordered--> Logistics --delivered--> Finance
    the CARE chain:    Clinic --billed--> Finance
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from dev_smtp_sink import SmtpDevSink  # noqa: E402

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


@pytest.fixture()
def sink():
    s = SmtpDevSink().start()
    yield s
    s.stop()


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
        "email": f"v90-{tag}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v90 {tag}",
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


async def _mk_email_endpoint(client: httpx.AsyncClient, h: dict,
                             sink: SmtpDevSink) -> dict:
    """A REAL email channel endpoint whose SMTP out is the dev sink."""
    res = await client.post("/channels/endpoints", headers=h, json={
        "name": "Dev inbox",
        "provider": "email_inbound",
        "config": {"secret": "dev-secret",
                   "smtp_host": "127.0.0.1", "smtp_port": str(sink.port),
                   "smtp_user": "dev", "smtp_pass": "dev",
                   "from_address": "py8n@py8n.test"}})
    assert res.status_code == 201, res.text
    return res.json()


# ---------------------------------------------------------------------------
# 1. the digest over a bound endpoint - the summary crosses the real wire
# ---------------------------------------------------------------------------

def test_v90_digest_over_bound_endpoint(sink):
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "inbox")
            h = _auth(user["token"])
            ep = await _mk_email_endpoint(client, h, sink)

            res = await client.post("/processes", headers=h, json={
                "name": "Billing digest machine",
                "definition": {**MACHINE, "escalation_policy": {
                    "channel": "email", "to": "billing@py8n.test",
                    "mode": "digest", "digest_every_seconds": 60,
                    "max_repeats": 1}}})
            assert res.status_code == 201, res.text
            pid = res.json()["id"]
            for ref in ("INV-1", "INV-2"):
                res = await client.post(f"/processes/{pid}/instances",
                                        headers=h,
                                        json={"ref": ref, "title": f"inv {ref}",
                                              "due_in_seconds": 1})
                assert res.status_code == 201, res.text
            base = datetime.now(timezone.utc)

            # the window opens: candidates, no summary yet
            report = await _door(user["id"], now=base + timedelta(seconds=10))
            assert report["digest"]["sent"] == [] and \
                report["digest"]["pending"][0]["items"] == 2, report

            # the window elapses: ONE summary - and it LANDS in the inbox
            report = await _door(user["id"], now=base + timedelta(seconds=80))
            sent = report["digest"]["sent"]
            assert len(sent) == 1 and sent[0]["items"] == 2, report
            assert sent[0]["delivery"] == "delivered", report
            assert "smtp accepted" in sent[0]["detail"], report
            assert sent[0]["to"] == "billing@py8n.test", report

            # the inbox: exactly one message, the scan-line subject, both
            # refs in the body, the endpoint's from_address on the envelope
            assert sink.count == 1, sink.messages
            msg = sink.last()
            assert msg["to"] == ["billing@py8n.test"], msg
            assert msg["subject"] == ("[py8n] Escalation digest - Billing "
                                      "digest machine (2 item(s) past SLA)"), msg
            assert "INV-1" in msg["text"] and "INV-2" in msg["text"], msg
            assert "digest 1" in msg["text"], msg

            # the event carries the delivery - endpoint, provider, subject
            res = await client.get("/events", headers=h,
                                   params={"type": "business.escalation_digest"})
            ev = res.json()["events"][0]
            assert ev["payload"]["delivery"] == "delivered", ev
            assert ev["payload"]["endpoint"] == "Dev inbox", ev
            assert ev["payload"]["provider"] == "email_inbound", ev
            assert ev["payload"]["subject"].startswith(
                "[py8n] Escalation digest"), ev
            assert ev["payload"]["to"] == "billing@py8n.test", ev

            # the per-item receipts name the delivery too
            res = await client.get(f"/processes/{pid}/instances", headers=h)
            row = res.json()["instances"][0]
            res = await client.get(
                f"/processes/{pid}/instances/{row['id']}", headers=h)
            digest_rows = [j for j in res.json()["journey"]
                           if j["transition"] == "escalation_digest"]
            assert digest_rows and "delivered" in digest_rows[-1]["note"], \
                digest_rows

            # a knock machine rides the same bound endpoint - with ITS
            # subject line (the scan line per attempt)
            res = await client.post("/processes", headers=h, json={
                "name": "Knock machine",
                "definition": {**MACHINE, "escalation_policy": {
                    "channel": "email", "to": "ops@py8n.test",
                    "repeat_every_seconds": 60, "max_repeats": 5}}})
            kpid = res.json()["id"]
            res = await client.post(f"/processes/{kpid}/instances", headers=h,
                                    json={"ref": "K-1", "title": "the stuck one",
                                          "due_in_seconds": 1})
            assert res.status_code == 201, res.text

            report = await _door(user["id"], now=base + timedelta(seconds=90))
            mine = [e for e in report["recorded"] if e["instance_id"]]
            assert mine and mine[0]["delivery"] == "delivered", report
            assert sink.count == 2, sink.messages
            knock = sink.last()
            assert knock["to"] == ["ops@py8n.test"], knock
            assert knock["subject"] == ("[py8n] Knock machine: 'the stuck one' "
                                        "(ref K-1) past SLA in 'a' (attempt 1)"), knock
            res = await client.get("/events", headers=h,
                                   params={"type": "business.escalated"})
            ev = res.json()["events"][0]
            assert ev["payload"]["delivery"] == "delivered", ev
            assert ev["payload"]["subject"].startswith("[py8n] Knock machine"), ev

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 2. honest skip preserved - a bound endpoint WITHOUT credentials still
#    reports the truth (v87-89 semantics untouched)
# ---------------------------------------------------------------------------

def test_v90_digest_without_credentials_skips_honestly():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "bare")
            h = _auth(user["token"])
            # the endpoint exists but carries NO smtp credentials
            res = await client.post("/channels/endpoints", headers=h, json={
                "name": "Bare inbox", "provider": "email_inbound",
                "config": {"secret": "s"}})
            assert res.status_code == 201, res.text

            res = await client.post("/processes", headers=h, json={
                "name": "Bare digest machine",
                "definition": {**MACHINE, "escalation_policy": {
                    "channel": "email", "to": "billing@py8n.test",
                    "mode": "digest", "digest_every_seconds": 60,
                    "max_repeats": 1}}})
            pid = res.json()["id"]
            res = await client.post(f"/processes/{pid}/instances", headers=h,
                                    json={"ref": "B-1", "due_in_seconds": 1})
            base = datetime.now(timezone.utc)

            # first sight books the episode (the window opens here)
            report = await _door(user["id"], now=base + timedelta(seconds=10))
            assert report["digest"]["sent"] == [] and \
                report["digest"]["pending"][0]["items"] == 1, report

            # the window elapses - the summary renders but the last mile
            # reports the truth: no credentials, no delivery
            report = await _door(user["id"], now=base + timedelta(seconds=80))
            sent = report["digest"]["sent"]
            assert len(sent) == 1 and sent[0]["delivery"] == "skipped", report
            assert "credential" in sent[0]["detail"], report

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 3. the REVENUE chain - Sales --won--> Operations --handed_off--> Finance
# ---------------------------------------------------------------------------

def test_v90_revenue_chain_sales_to_finance():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "chain-a")
            h = _auth(user["token"])

            # loud refusals first: the leg's SLA must be a positive integer
            res = await client.post("/processes", headers=h, json={
                "name": "bad due 0", "definition": {**MACHINE, "journeys": [
                    {"on_state": "done", "open": {"process": "X",
                                                  "due_in_seconds": 0}}]}})
            assert res.status_code == 400 and "must be > 0" in res.json()["detail"]
            res = await client.post("/processes", headers=h, json={
                "name": "bad due str", "definition": {**MACHINE, "journeys": [
                    {"on_state": "done", "open": {"process": "X",
                                                  "due_in_seconds": "soon"}}]}})
            assert res.status_code == 400 and "must be an integer" \
                in res.json()["detail"]

            # install all three departments of the revenue chain
            for slug in ("operations-operator", "sales-operator",
                         "finance-operator"):
                res = await client.post(f"/operators/{slug}/install",
                                        headers=h, json={})
                assert res.status_code == 200, res.text

            res = await client.get("/processes", headers=h)
            procs = {p["name"]: p["id"] for p in res.json()["processes"]}
            lead_pid = procs["Lead pipeline"]
            onb_pid = procs["Customer onboarding"]
            inv_pid = procs["Invoice lifecycle"]

            # the middle machine carries the chain's second leg
            res = await client.get(f"/processes/{onb_pid}", headers=h)
            journeys = res.json()["journeys"]
            assert journeys and journeys[0]["on_state"] == "handed_off", journeys
            assert journeys[0]["open"]["process"] == "Invoice lifecycle"
            assert journeys[0]["open"]["due_in_seconds"] == 5 * 24 * 3600

            # a fresh lead walks the pipeline ... and WINS - the onboarding
            # case opens itself at kickoff (the v89 leg)
            res = await client.post(f"/processes/{lead_pid}/instances",
                                    headers=h, json={
                                        "ref": "+15557770123",
                                        "title": "Chain Deal",
                                        "context": {"company": "Chain Co"}})
            iid = res.json()["id"]
            for move in ("reach_out", "qualify", "book_demo", "run_demo",
                         "send_proposal", "negotiate", "win"):
                res = await client.post(
                    f"/processes/{lead_pid}/instances/{iid}/advance",
                    headers=h, json={"transition": move})
                assert res.status_code == 200, res.text

            # the onboarding case walks to handoff - and the invoice opens
            # ITSELF at 'received' with its own SLA
            res = await client.get(f"/processes/{onb_pid}/instances", headers=h)
            case = next(r for r in res.json()["instances"]
                        if r["ref"] == "+15557770123")
            for move in ("provision", "train", "go_live", "hand_off"):
                res = await client.post(
                    f"/processes/{onb_pid}/instances/{case['id']}/advance",
                    headers=h, json={"transition": move})
                assert res.status_code == 200, res.text
                if move == "hand_off":
                    opened = res.json().get("journeys_opened")
                    assert opened and opened[0]["opened"] is True, res.text
                    target = opened[0]["target"]
                    assert target["process_name"] == "Invoice lifecycle"
                    assert target["state"] == "received"
                    assert target["due_at"], opened  # the leg's own SLA

            # the third leg EXISTS: same ref, billing title, journey link
            res = await client.get(f"/processes/{inv_pid}/instances", headers=h)
            inv = next(r for r in res.json()["instances"]
                       if r["ref"] == "+15557770123")
            assert inv["state"] == "received", inv
            assert inv["title"] == "Billing - Onboarding - Chain Deal", inv
            assert inv["context"]["via"] == "handed-off journey", inv
            assert inv["context"]["journey"]["from_process"] == \
                "Customer onboarding", inv
            assert inv["due_at"], inv

            # the leg is on the record on the CASE's correlation thread
            res = await client.get("/events", headers=h,
                                   params={"type": "business.journey_opened",
                                           "correlation_id": case["id"]})
            evs = res.json()["events"]
            assert evs and evs[0]["payload"]["target"]["process_name"] == \
                "Invoice lifecycle", evs
            assert evs[0]["payload"]["target"]["due_at"], evs

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 4. the SUPPLY chain - Procurement --ordered--> Logistics --delivered--> Finance
# ---------------------------------------------------------------------------

def test_v90_supply_chain_procurement_to_finance():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "chain-b")
            h = _auth(user["token"])

            for slug in ("procurement-operator", "logistics-operator",
                         "finance-operator"):
                res = await client.post(f"/operators/{slug}/install",
                                        headers=h, json={})
                assert res.status_code == 200, res.text

            res = await client.get("/processes", headers=h)
            procs = {p["name"]: p["id"] for p in res.json()["processes"]}
            pur_pid = procs["Purchase lifecycle"]
            dlv_pid = procs["Delivery pipeline"]
            inv_pid = procs["Invoice lifecycle"]

            # the seeded purchase already sits at 'approved' - one move
            # lands it on the fire-state
            res = await client.get(f"/processes/{pur_pid}/instances", headers=h)
            purchase = next(r for r in res.json()["instances"]
                            if r["state"] == "approved")
            ref = purchase["ref"]

            res = await client.post(
                f"/processes/{pur_pid}/instances/{purchase['id']}/advance",
                headers=h, json={"transition": "order"})
            assert res.status_code == 200, res.text
            opened = res.json().get("journeys_opened")
            assert opened and opened[0]["opened"] is True, res.text
            assert opened[0]["target"]["process_name"] == "Delivery pipeline"
            assert opened[0]["target"]["state"] == "placed"

            # the delivery exists at 'placed' with the SAME ref
            res = await client.get(f"/processes/{dlv_pid}/instances", headers=h)
            delivery = next(r for r in res.json()["instances"] if r["ref"] == ref)
            assert delivery["title"] == f"Delivery - {purchase['title']}", delivery
            assert delivery["due_at"], delivery  # the leg carries its own SLA

            # the goods travel ... and land - the invoice opens ITSELF
            for move in ("pick", "dispatch", "depart", "deliver"):
                res = await client.post(
                    f"/processes/{dlv_pid}/instances/{delivery['id']}/advance",
                    headers=h, json={"transition": move})
                assert res.status_code == 200, res.text
                if move == "deliver":
                    opened = res.json().get("journeys_opened")
                    assert opened and opened[0]["opened"] is True, res.text
                    assert opened[0]["target"]["process_name"] == \
                        "Invoice lifecycle"

            # the third leg: goods received -> payable, same ref again
            res = await client.get(f"/processes/{inv_pid}/instances", headers=h)
            inv = next(r for r in res.json()["instances"] if r["ref"] == ref)
            assert inv["state"] == "received", inv
            assert inv["title"] == f"Bill - Delivery - {purchase['title']}", inv
            assert inv["context"]["via"] == "delivered journey", inv
            assert inv["context"]["journey"]["from_process"] == \
                "Delivery pipeline", inv

            # ONE ref now threads THREE departments
            res = await client.get(f"/processes/{pur_pid}/instances", headers=h)
            assert any(r["ref"] == ref for r in res.json()["instances"])
            # ...and each hop is on the record on its own thread
            for thread_id, target_name in ((purchase["id"], "Delivery pipeline"),
                                           (delivery["id"], "Invoice lifecycle")):
                res = await client.get("/events", headers=h,
                                       params={"type": "business.journey_opened",
                                               "correlation_id": thread_id})
                evs = res.json()["events"]
                assert evs and evs[0]["payload"]["target"]["process_name"] == \
                    target_name, (thread_id, evs)

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 5. the CARE chain - Clinic --billed--> Finance
# ---------------------------------------------------------------------------

def test_v90_clinic_billed_visit_opens_invoice():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "chain-c")
            h = _auth(user["token"])

            for slug in ("clinic-operator", "finance-operator"):
                res = await client.post(f"/operators/{slug}/install",
                                        headers=h, json={})
                assert res.status_code == 200, res.text

            res = await client.get("/processes", headers=h)
            procs = {p["name"]: p["id"] for p in res.json()["processes"]}
            appt_pid = procs["Appointment journey"]
            inv_pid = procs["Invoice lifecycle"]

            # a seeded appointment walks to 'billed' - the visit's invoice
            # opens itself (ref = the patient's phone)
            res = await client.get(f"/processes/{appt_pid}/instances", headers=h)
            appt = next(r for r in res.json()["instances"]
                        if r["ref"] == "+15550002111")
            for move in ("confirm", "check_in", "begin_consult", "bill"):
                res = await client.post(
                    f"/processes/{appt_pid}/instances/{appt['id']}/advance",
                    headers=h, json={"transition": move})
                assert res.status_code == 200, res.text
                if move == "bill":
                    opened = res.json().get("journeys_opened")
                    assert opened and opened[0]["opened"] is True, res.text
                    assert opened[0]["target"]["process_name"] == \
                        "Invoice lifecycle"

            res = await client.get(f"/processes/{inv_pid}/instances", headers=h)
            inv = next(r for r in res.json()["instances"]
                       if r["ref"] == "+15550002111")
            assert inv["state"] == "received", inv
            assert inv["title"] == f"Visit - {appt['title']}", inv
            assert inv["context"]["via"] == "billed visit journey", inv
            assert inv["context"]["journey"]["from_process"] == \
                "Appointment journey", inv
            assert inv["due_at"], inv

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 6. version pin
# ---------------------------------------------------------------------------

def test_v90_version_pin():
    assert settings.version == "1.90.0"
