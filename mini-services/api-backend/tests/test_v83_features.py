"""v83 tests - Marketplace Operators: "Install a business operator".

The operators shelf over solutions: one click composes the WHOLE business
into real primitives - datasets, event-reactive workflows (event_trigger
-> shape -> dataset write), the AI agent with knowledge bound to the BUILT
FAQ, the video-first room, the waiting-room queue, the outbound campaign,
and the staff dashboard generated over the built datasets - all bound into
a RUNNING Py8nSystem with the durable installed operation and the
system.installed event.

Proven end to end:
* the meeting operator's scribe reacts to a REAL meeting end (the room
  ends, the meeting.ended event fires, the row lands in Meeting notes);
* the sales operator's scorer reacts to a real call.ended event and the
  scored evidence lands in Lead events; the campaign composes EMPTY with
  the retry/AMD defaults filled;
* the clinic operator's receptionist is grounded in the built FAQ (the
  knowledge search answers from it) and the intake workflow lands every
  inbound SMS as an appointment request.
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
    """The live-tail hub is process state - keep scenarios isolated."""
    from app.services import system_events as events_svc

    events_svc._subscribers.clear()
    yield
    events_svc._subscribers.clear()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


async def _drain_background() -> None:
    from app.services import executor as executor_mod
    from app.services import system_events as events_svc

    # the after-commit dispatch tasks first (they spawn the executor's runs)
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
        "email": f"v83-{tag}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v83 {tag}",
    })
    assert res.status_code == 201, res.text
    return {"token": res.json()["token"], "id": res.json()["user"]["id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _boot_system(client: httpx.AsyncClient, headers: dict, system_id: str) -> None:
    """The boot door: stop -> start(activate_workflows) flips the operator's
    reactive workflows ON loudly (v81 semantics - never a silent activation)."""
    res = await client.post(f"/systems/{system_id}/stop", headers=headers, json={})
    assert res.status_code == 200, res.text
    res = await client.post(f"/systems/{system_id}/start", headers=headers,
                            json={"activate_workflows": True})
    assert res.status_code == 200, res.text


async def _ds_rows(client: httpx.AsyncClient, headers: dict, ds_id: str) -> list[dict]:
    res = await client.get(f"/datasets/{ds_id}/rows", headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    return body.get("rows") or body.get("records") or []


# ---------------------------------------------------------------------------
# 1. the shelf - businesses, not workflow templates
# ---------------------------------------------------------------------------

def test_v83_catalog_and_detail():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "shelf")
            h = _auth(user["token"])

            res = await client.get("/operators", headers=h)
            assert res.status_code == 200, res.text
            shelf = res.json()["operators"]
            # v86: the shelf grew to nine - the founding three still lead it
            assert [o["slug"] for o in shelf[:3]] == ["meeting-operator",
                                                      "sales-operator",
                                                      "clinic-operator"]
            assert len(shelf) == 9
            meeting = shelf[0]
            assert meeting["topology"] == {"datasets": 1, "workflows": 2, "agents": 1,
                                           "rooms": 1, "queues": 1, "campaign": 0,
                                           "processes": 1, "dashboard": 1}  # v87: the machine ships pre-wired; v88: + the onboarding loop
            sales = shelf[1]
            assert sales["topology"]["campaign"] == 1
            assert sales["topology"]["datasets"] == 3  # CRM + Lead events + Sales FAQ
            assert sales["topology"]["processes"] == 1  # v85: the lead pipeline ships bound
            clinic = shelf[2]
            assert clinic["topology"]["queues"] == 1 and clinic["topology"]["campaign"] == 0

            # the install plan names every primitive it will build
            res = await client.get("/operators/meeting-operator", headers=h)
            assert res.status_code == 200, res.text
            plan = res.json()
            assert plan["installs"]["workflows"][0]["trigger"] == "meeting.ended"
            assert plan["installs"]["rooms"][0]["modality"] == "video"
            assert plan["installs"]["dashboard"] == "Meeting Operator Board"
            assert plan["notes"], "the wiring notes ship with the plan"

            res = await client.get("/operators/nope", headers=h)
            assert res.status_code == 404
            res = await client.post("/operators/nope/install", headers=h, json={})
            assert res.status_code == 404

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 2. the meeting operator - a video meeting business, RUNNING
# ---------------------------------------------------------------------------

def test_v83_meeting_operator_install():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "meet")
            h = _auth(user["token"])

            res = await client.post("/operators/meeting-operator/install", headers=h,
                                    json={"note": "our weekly standups"})
            assert res.status_code == 200, res.text
            built = res.json()

            # the topology built (dataset names suffix honestly when a prior
            # install of the operator already took the name - global uniqueness)
            assert built["datasets"][0]["name"].startswith("Meeting notes")
            assert built["datasets"][0]["rows"] == 2  # the seed rows came along
            scribe = built["workflows"][0]
            assert scribe["name"] == "Meeting scribe" and scribe["trigger"] == "meeting.ended"
            assert scribe["active"] is False, "reactive workflows install INACTIVE - honest"
            assert built["rooms"][0]["modality"] == "video"
            queue = built["queues"][0]
            assert queue["config"]["announce"]["enabled"] is True
            assert queue["config"]["sms"]["enabled"] is True
            assert queue["config"]["callback"]["enabled"] is True

            # the board generated over the BUILT dataset (the name suffixes
            # honestly when a prior install of the operator already took it)
            assert built["dashboard"]["name"].startswith("Meeting Operator Board")
            assert built["dashboard"]["components"] > 0

            # the system: RUNNING with the whole estate bound
            system = built["system"]
            assert system["lifecycle"] == "running"
            assert system["components"]["dataset"] == 1
            assert system["components"]["workflow"] >= 2  # scribe + the agent's handler
            assert system["components"]["voice_agent"] == 1
            assert system["components"]["meeting"] == 1
            assert system["components"]["queue"] == 1
            assert system["components"]["dashboard"] == 1

            # the room is video-first for the media runtime
            res = await client.get(f"/voice/meetings/{built['rooms'][0]['id']}", headers=h)
            assert res.status_code == 200, res.text
            room = res.json()
            assert (room.get("context") or {}).get("modality") == "audio+video"

            # the durable operation + event are on the record
            res = await client.get(f"/systems/{system['id']}/operations", headers=h)
            assert res.status_code == 200, res.text
            ops = res.json() if isinstance(res.json(), list) else res.json().get("operations", [])
            assert any(o.get("verb") == "installed" or o.get("operation") == "installed"
                       for o in ops), res.text

    _sync(_wrap(_go()))


def test_v83_meeting_operator_reactive_scribe():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "scribe")
            h = _auth(user["token"])

            res = await client.post("/operators/meeting-operator/install", headers=h, json={})
            assert res.status_code == 200, res.text
            built = res.json()
            system_id = built["system"]["id"]
            scribe_id = built["workflows"][0]["id"]
            room_id = built["rooms"][0]["id"]
            notes_id = built["datasets"][0]["id"]

            # the boot door opens the reactive path
            await _boot_system(client, h, system_id)

            # a REAL meeting ends - the room fires meeting.ended itself
            res = await client.post(f"/voice/meetings/{room_id}/end", headers=h, json={})
            assert res.status_code == 200, res.text
            await _drain_background()

            # the scribe reacted: a real execution, then the row in the notes
            res = await client.get("/executions", headers=h, params={"limit": 30})
            runs = [r for r in res.json() if r.get("workflow_id") == scribe_id]
            assert len(runs) == 1, res.text
            assert runs[0]["trigger_type"] == "event"
            assert runs[0]["status"] == "success", runs[0].get("error")

            rows = await _ds_rows(client, h, notes_id)
            assert len(rows) == 3, res.text  # 2 seeds + the ended meeting
            logged = rows[-1]
            assert logged.get("title") == "Operator meeting room"
            assert logged.get("meeting_id") == room_id

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 3. the sales operator - CRM, campaign, and calls that score themselves
# ---------------------------------------------------------------------------

def test_v83_sales_operator_campaign_scorer():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "sales")
            h = _auth(user["token"])

            res = await client.post("/operators/sales-operator/install", headers=h, json={})
            assert res.status_code == 200, res.text
            built = res.json()
            system_id = built["system"]["id"]
            scorer_id = built["workflows"][0]["id"]
            events_ds = next(d for d in built["datasets"] if d["name"].startswith("Lead events"))

            # the campaign composed EMPTY (create_campaign refuses empty target
            # lists BY DESIGN) with the retry/AMD defaults filled
            camp = built["campaign"]
            assert camp is not None and camp["targets"] == 0
            cfg = camp["config"]
            assert cfg.get("retry_schedule") or cfg.get("retry"), cfg
            assert "amd" in cfg or "answer_machine" in cfg or cfg.get("amd_enabled") is not None, cfg

            # the CRM seeds came along
            crm = next(d for d in built["datasets"] if d["name"].startswith("CRM leads"))
            rows = await _ds_rows(client, h, crm["id"])
            assert len(rows) == 3 and rows[0].get("stage") == "contacted"

            # the SDR is grounded in the built Sales FAQ
            agent_id = built["agents"][0]["id"]
            res = await client.post(f"/voice/agents/{agent_id}/knowledge/search",
                                    headers=h, json={"query": "What does it cost"})
            assert res.status_code == 200, res.text
            hits = res.json().get("matches") or res.json().get("results") or []
            assert hits and "twenty dollars" in str(hits[0]), res.text

            # boot, then a REAL-shaped call.ended event: the lead scores itself
            await _boot_system(client, h, system_id)
            res = await client.post("/events", headers=h, json={
                "type": "call.ended", "source": "voice",
                "payload": {"end_reason": "completed", "state": "ended"}})
            assert res.status_code == 201, res.text
            await _drain_background()

            rows = await _ds_rows(client, h, events_ds["id"])
            assert len(rows) == 1, res.text
            assert rows[0].get("end_reason") == "completed"
            assert int(rows[0].get("score") or 0) == 60

            res = await client.get("/executions", headers=h, params={"limit": 30})
            runs = [r for r in res.json() if r.get("workflow_id") == scorer_id]
            assert len(runs) == 1 and runs[0]["trigger_type"] == "event"

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 4. the clinic operator - a grounded receptionist and texts that book
# ---------------------------------------------------------------------------

def test_v83_clinic_operator_receptionist_intake():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "clinic")
            h = _auth(user["token"])

            res = await client.post("/operators/clinic-operator/install", headers=h, json={})
            assert res.status_code == 200, res.text
            built = res.json()
            system_id = built["system"]["id"]
            intake_id = built["workflows"][0]["id"]
            appts_id = next(d["id"] for d in built["datasets"] if d["name"].startswith("Clinic appointments"))

            # the receptionist's knowledge is bound to the BUILT FAQ dataset
            agent = built["agents"][0]
            faq = next(d for d in built["datasets"] if d["name"].startswith("Clinic FAQ"))
            assert (agent.get("knowledge") or {}).get("dataset_id") == faq["id"]
            res = await client.post(f"/voice/agents/{agent['id']}/knowledge/search",
                                    headers=h, json={"query": "Do you take walk-ins"})
            assert res.status_code == 200, res.text
            hits = res.json().get("matches") or res.json().get("results") or []
            assert hits and "front desk queue" in str(hits[0]), res.text

            # the front desk queue: SMS backchannel pre-enabled
            queue = built["queues"][0]
            assert queue["name"] == "Clinic front desk"
            assert queue["config"]["sms"]["enabled"] is True

            # boot, then an inbound SMS event: the appointment request lands
            await _boot_system(client, h, system_id)
            res = await client.post("/events", headers=h, json={
                "type": "sms.received", "source": "sms",
                "payload": {"queue_id": queue["id"], "queue_name": queue["name"],
                            "keyword": "1", "text": "1 - I need to see a doctor tomorrow",
                            "auto_answer_action": "left_queue"}})
            assert res.status_code == 201, res.text
            await _drain_background()

            rows = await _ds_rows(client, h, appts_id)
            assert len(rows) == 1, res.text
            assert rows[0].get("keyword") == "1"
            assert "doctor tomorrow" in str(rows[0].get("text"))
            assert rows[0].get("queue_id") == queue["id"]

            res = await client.get("/executions", headers=h, params={"limit": 30})
            runs = [r for r in res.json() if r.get("workflow_id") == intake_id]
            assert len(runs) == 1 and runs[0]["status"] == "success", runs[0].get("error")

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 5. refusals + double install - honest about what cannot happen
# ---------------------------------------------------------------------------

def test_v83_install_refusals_and_double_install():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "refuse")
            h = _auth(user["token"])

            # ai_agent brain without a credential refuses loudly
            res = await client.post("/operators/sales-operator/install", headers=h,
                                    json={"brain": "ai_agent"})
            assert res.status_code == 400
            assert "llm_credential_id" in res.json()["detail"]

            # unknown brain
            res = await client.post("/operators/sales-operator/install", headers=h,
                                    json={"brain": "quantum"})
            assert res.status_code == 400

            # a double install builds a SECOND system - and the second
            # install's datasets suffix so the reactive write lands on what
            # THAT system owns (never the first install's notes)
            res = await client.post("/operators/meeting-operator/install", headers=h, json={})
            assert res.status_code == 200, res.text
            first = res.json()
            res = await client.post("/operators/meeting-operator/install", headers=h, json={})
            assert res.status_code == 200, res.text
            second = res.json()
            assert first["system"]["id"] != second["system"]["id"]
            assert second["datasets"][0]["name"] != first["datasets"][0]["name"]
            assert second["datasets"][0]["name"].startswith("Meeting notes")

            # the scribe of the SECOND install writes into the SECOND notes:
            # boot it and end ITS room
            await _boot_system(client, h, second["system"]["id"])
            res = await client.post(f"/voice/meetings/{second['rooms'][0]['id']}/end",
                                    headers=h, json={})
            assert res.status_code == 200, res.text
            await _drain_background()
            rows_second = await _ds_rows(client, h, second["datasets"][0]["id"])
            assert len(rows_second) == 3, rows_second  # seeds + its own meeting
            rows_first = await _ds_rows(client, h, first["datasets"][0]["id"])
            assert len(rows_first) == 2, rows_first  # untouched seeds

            # operator systems refuse the solution-pack upgrade honestly
            res = await client.post(f"/systems/{first['system']['id']}/upgrade",
                                    headers=h, json={})
            assert res.status_code == 400
            assert "OPERATOR" in res.json()["detail"]

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 6. version pin
# ---------------------------------------------------------------------------

def test_v83_version_pin():
    assert settings.version == "1.106.0"
