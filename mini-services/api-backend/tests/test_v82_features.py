"""V82 feature tests: the AI System Composer - describe a system, get one BUILT.

* CATALOG: the honest inventory - composable kinds, trigger types, the
  composable node types (every one registered in this build), the
  archetypes, the limits, the compose-don't-generate notes.
* PROPOSE (deterministic): the archetype composer turns a description
  into a VALIDATED spec with zero credentials - support line, meeting
  operator, lead desk, clinic front desk, fallback support line.
* PROPOSE (LLM-first): the model composes the spec through a REAL
  openai_compatible credential (transport-injected); an unreachable model
  or a prose-only reply fails LOUD; a parseable-but-impossible spec
  (hallucinated node type) is refused with the exact reason.
* BUILD: the spec becomes real primitives through the same services the
  API uses - datasets (with seed rows), composed workflow graphs
  (imported inactive), knowledge-bound voice agents, audio/video meeting
  rooms, channel queues with the backchannel wiring - all bound into a
  RUNNING Py8nSystem with a durable "build" operation and a system.built
  event on the platform's correlation thread.
* COMPOSITION PROOF: the built event-trigger workflow (booted through the
  system's start door) REACTS to a real event and the write lands in the
  built dataset - primitives composing into a system, not code generation.
* REFUSALS: unknown kinds, unknown node types, dangling references,
  duplicate names, a brain without its credential - every one loud.

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
        "email": f"v82-{tag}-u{n}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v82 u{n} {tag}",
    })
    assert res.status_code == 201, res.text
    return {"token": res.json()["token"], "id": res.json()["user"]["id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


SUPPORT_DESC = ("Build me a customer support system: a phone line that answers "
                "from our FAQ, keeps callers in a queue with announcements, "
                "and logs every ended call.")


async def _propose(client: httpx.AsyncClient, headers: dict, description: str) -> dict:
    res = await client.post("/ai-composer/propose", headers=headers,
                            json={"description": description})
    assert res.status_code == 200, res.text
    return res.json()


# ---------------------------------------------------------------------------
# 1. the catalog - the honest inventory
# ---------------------------------------------------------------------------

def test_v82_catalog():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "catalog")
            h = _auth(user["token"])
            res = await client.get("/ai-composer/catalog", headers=h)
            assert res.status_code == 200, res.text
            cat = res.json()
            for kind in ("dataset", "workflow", "voice_agent", "meeting_room", "queue"):
                assert kind in cat["kinds"], kind
            # every composable node type is registered in THIS build
            from app.engine.registry import get_node_class

            for nt in cat["node_types"]["composable"]:
                assert get_node_class(nt) is not None, nt
            # triggers are a subset of the composable set
            for t in cat["trigger_types"]:
                assert t in cat["node_types"]["composable"], t
            assert {a["id"] for a in cat["archetypes"]} == {
                "support_line", "meeting_operator", "lead_desk", "clinic_front_desk"}
            assert any("never generates" in n for n in cat["notes"])
    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 2. deterministic propose - every archetype validates
# ---------------------------------------------------------------------------

def test_v82_propose_deterministic():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "det")
            h = _auth(user["token"])

            out = await _propose(client, h, SUPPORT_DESC)
            spec = out["spec"]
            assert out["validated"] is True
            assert spec["mode"] == "deterministic"
            assert spec["archetype"] == "support_line"
            kinds = [c["kind"] for c in spec["components"]]
            assert "voice_agent" in kinds and "queue" in kinds
            assert "meeting_room" in kinds and "dataset" in kinds
            assert "workflow" in kinds

            # the meeting operator archetype (video room)
            out = await _propose(client, h,
                                 "I need a zoom-like meeting room system for standups")
            assert out["spec"]["archetype"] == "meeting_operator"
            room = next(c for c in out["spec"]["components"]
                        if c["kind"] == "meeting_room")
            assert room["modality"] == "video"

            # the lead desk archetype
            out = await _propose(client, h,
                                 "a sales lead intake and follow up desk for my pipeline")
            assert out["spec"]["archetype"] == "lead_desk"

            # the clinic archetype
            out = await _propose(client, h,
                                 "a clinic front desk for appointments and patient calls")
            assert out["spec"]["archetype"] == "clinic_front_desk"

            # the fallback is the support line
            out = await _propose(client, h, "something for my team, whatever fits")
            assert out["spec"]["archetype"] == "support_line"
    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 3. LLM-first propose - the model composes, py8n validates, failures are loud
# ---------------------------------------------------------------------------

_VALID_LLM_SPEC = {
    "name": "Clinic line",
    "description": "a phone line for a small clinic",
    "components": [
        {"kind": "dataset", "name": "Clinic FAQ",
         "columns": ["question", "answer"],
         "rows": [{"question": "hours?", "answer": "8 to 5"}]},
        {"kind": "voice_agent", "name": "Desk agent",
         "greeting": "Good day", "brain": "scaffold",
         "knowledge": {"dataset": "Clinic FAQ", "text_column": "question",
                       "answer_column": "answer"}},
        {"kind": "meeting_room", "name": "Desk room", "agent": "Desk agent"},
        {"kind": "queue", "name": "Patient line", "meeting": "Desk room",
         "agent": "Desk agent", "max_size": 10},
    ],
}


def _openai_reply(content: str) -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "model": "gpt-test",
    })


def test_v82_propose_llm_first():
    async def _go():
        from app.services import llm_routing

        async with _client() as client:
            user = await _mk_user(client, "llm")
            h = _auth(user["token"])
            cred = await client.post("/credentials", headers=h, json={
                "name": "composer brain", "type": "openai_compatible",
                "data": {"provider": "openai", "api_key": "sk-test",
                         "base_url": "https://api.openai.com/v1",
                         "model": "gpt-test"}})
            assert cred.status_code == 201, cred.text
            cred_id = cred.json()["id"]

            import json as _json

            class _Transport(httpx.MockTransport):
                def __init__(self, content: str):
                    super().__init__(lambda request: _openai_reply(content))

            # 1) a good spec composes through the model
            llm_routing.set_transport(_Transport(_json.dumps(_VALID_LLM_SPEC)))
            try:
                res = await client.post("/ai-composer/propose", headers=h, json={
                    "description": "a phone line for a small clinic",
                    "credential_id": cred_id})
                assert res.status_code == 200, res.text
                assert res.json()["spec"]["mode"] == "llm"
                assert res.json()["validated"] is True
                assert res.json()["spec"]["name"] == "Clinic line"
                assert any("proposed by" in n for n in res.json()["spec"]["notes"])
            finally:
                llm_routing.set_transport(None)

            # 2) an unreachable model fails LOUD (the user chose the model)
            def _boom(request):
                return httpx.Response(500, text="provider on fire")

            llm_routing.set_transport(httpx.MockTransport(_boom))
            try:
                res = await client.post("/ai-composer/propose", headers=h, json={
                    "description": "a phone line for a small clinic",
                    "credential_id": cred_id})
                assert res.status_code == 400, res.text
                assert "LLM" in res.json()["detail"] or "proposal" in res.json()["detail"]
            finally:
                llm_routing.set_transport(None)

            # 3) prose without JSON fails loud
            llm_routing.set_transport(
                _Transport("I would build a lovely phone system for you!"))
            try:
                res = await client.post("/ai-composer/propose", headers=h, json={
                    "description": "a phone line for a small clinic",
                    "credential_id": cred_id})
                assert res.status_code == 400, res.text
                assert "JSON" in res.json()["detail"]
            finally:
                llm_routing.set_transport(None)

            # 4) a parseable-but-impossible spec (hallucinated node type)
            #    is refused with the exact reason - AI proposes, py8n disposes
            bad = _json.dumps({
                "name": "Bad", "components": [
                    {"kind": "workflow", "name": "w", "trigger": {"type": "manual_trigger"},
                     "steps": [{"type": "quantum_compute"}]}]})
            llm_routing.set_transport(_Transport(bad))
            try:
                res = await client.post("/ai-composer/propose", headers=h, json={
                    "description": "a phone line for a small clinic",
                    "credential_id": cred_id})
                assert res.status_code == 422, res.text
                assert "quantum_compute" in res.json()["detail"]
            finally:
                llm_routing.set_transport(None)

            # 5) a stranger's credential is not found
            other = await _mk_user(client, "llm", n=2)
            llm_routing.set_transport(_Transport(_json.dumps(_VALID_LLM_SPEC)))
            try:
                res = await client.post("/ai-composer/propose", headers=_auth(other["token"]),
                                        json={"description": "a phone line for a clinic",
                                              "credential_id": cred_id})
                assert res.status_code == 404, res.text
            finally:
                llm_routing.set_transport(None)
    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 4. build - the spec becomes real primitives bound into a RUNNING system
# ---------------------------------------------------------------------------

def test_v82_build_support_line():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "build")
            h = _auth(user["token"])

            out = await _propose(client, h, SUPPORT_DESC)
            spec = out["spec"]

            res = await client.post("/ai-composer/build", headers=h, json={"spec": spec})
            assert res.status_code == 201, res.text
            built = res.json()
            assert built["spec"]["mode"] == "deterministic"

            # datasets carry the seed rows
            assert len(built["datasets"]) == 2
            faq = next(d for d in built["datasets"] if "FAQ" in d["name"])
            assert faq["rows"] == 3
            res = await client.get(f"/datasets/{faq['id']}", headers=h)
            assert res.status_code == 200
            assert res.json()["row_count"] == 3

            # the composed workflow is REAL and registered
            assert len(built["workflows"]) == 1
            wf = built["workflows"][0]
            assert wf["trigger"] == "event_trigger"
            res = await client.get(f"/workflows/{wf['id']}", headers=h)
            assert res.status_code == 200
            nodes = res.json()["graph"]["nodes"]
            assert [n["type"] for n in nodes] == ["event_trigger", "python_transform", "dataset_write"]
            assert nodes[0]["parameters"]["event_type"] == "call.ended"
            assert nodes[2]["parameters"]["dataset"]  # a REAL dataset name was wired

            # the agent's knowledge is bound to the BUILT dataset
            assert len(built["voice_agents"]) == 1
            agent = built["voice_agents"][0]
            assert agent["knowledge"]["dataset_id"] == faq["id"]
            assert agent["handler_workflow_id"]  # the scaffold is real

            # the room + the queue with the honest backchannel wiring
            assert built["meeting_rooms"][0]["modality"] == "audio"
            queue = built["queues"][0]
            assert queue["config"]["announce"]["enabled"] is True
            assert queue["config"]["sms"]["enabled"] is True
            assert queue["config"]["callback"]["enabled"] is True

            # THE SYSTEM: running from day one, everything bound, on the record
            system = built["system"]
            assert system["lifecycle"] == "running"
            assert system["components"] == 6
            res = await client.get(f"/systems/{system['id']}", headers=h)
            assert res.status_code == 200
            sys_view = res.json()
            grouped = sys_view["grouped"]
            assert len(grouped.get("dataset", [])) == 2
            assert len(grouped.get("workflow", [])) == 1
            assert len(grouped.get("voice_agent", [])) == 1
            assert len(grouped.get("meeting", [])) == 1
            assert len(grouped.get("queue", [])) == 1

            # the build operation + the system.built event on the thread
            res = await client.get(f"/systems/{system['id']}/operations", headers=h)
            assert res.status_code == 200
            verbs = [o["verb"] for o in res.json()["operations"]]
            assert "build" in verbs
            res = await client.get(
                f"/events", headers=h,
                params={"type": "system.built",
                        "correlation_id": system["id"]})
            assert res.status_code == 200, res.text
            types = [e["type"] for e in res.json()["events"]]
            assert "system.built" in types
    _sync(_wrap(_go()))


def test_v82_build_video_room():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "video")
            h = _auth(user["token"])
            out = await _propose(client, h,
                                 "a zoom-like meeting system for our weekly standup")
            res = await client.post("/ai-composer/build", headers=h,
                                    json={"spec": out["spec"]})
            assert res.status_code == 201, res.text
            built = res.json()
            room = built["meeting_rooms"][0]
            assert room["modality"] == "video"
            # the video-first modality lives where the room describes itself
            res = await client.get(f"/voice/meetings/{room['id']}", headers=h)
            assert res.status_code == 200
            assert res.json()["context"]["modality"] == "audio+video"
            assert res.json()["context"]["media_session_kind"] == "video"
    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 5. composition proof - the built workflow REACTS (boot -> event -> row)
# ---------------------------------------------------------------------------

def test_v82_built_workflow_reacts():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "react")
            h = _auth(user["token"])

            out = await _propose(client, h, SUPPORT_DESC)
            res = await client.post("/ai-composer/build", headers=h,
                                    json={"spec": out["spec"]})
            assert res.status_code == 201, res.text
            built = res.json()
            wf = built["workflows"][0]
            system = built["system"]
            log_ds = next(d for d in built["datasets"] if "calls" in d["name"].lower())

            # workflows install INACTIVE (honest); the system's start door
            # boots them loudly - stop first (running -> stopped)
            res = await client.post(f"/systems/{system['id']}/stop", headers=h, json={})
            assert res.status_code == 200, res.text
            res = await client.post(f"/systems/{system['id']}/start", headers=h,
                                    json={"activate_workflows": True})
            assert res.status_code == 200, res.text
            assert res.json()["workflows_activated"] == 1, res.text

            # a REAL call.ended event -> the built workflow reacts
            res = await client.post("/events", headers=h, json={
                "type": "call.ended", "source": "voice",
                "session_id": built["voice_agents"][0]["id"],
                "payload": {"reason": "caller hung up"}})
            assert res.status_code == 201, res.text
            await _drain_background()

            res = await client.get("/executions", headers=h, params={"limit": 20})
            runs = [r for r in res.json() if r.get("workflow_id") == wf["id"]]
            assert len(runs) == 1, res.text
            assert runs[0]["trigger_type"] == "event"
            assert runs[0]["status"] == "success", runs[0].get("error")

            # the composed write LANDED in the built dataset - the system works
            res = await client.get(f"/datasets/{log_ds['id']}/rows", headers=h)
            assert res.status_code == 200, res.text
            body = res.json()
            rows = body.get("rows") or body.get("records") or []
            assert len(rows) == 1, res.text
            assert rows[0].get("event_type") == "call.ended"
    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 6. refusals - every impossible proposal fails loud
# ---------------------------------------------------------------------------

def test_v82_refusals():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "refuse")
            h = _auth(user["token"])

            base_dataset = {"kind": "dataset", "name": "D", "columns": ["a"]}

            # unknown kind
            res = await client.post("/ai-composer/build", headers=h, json={"spec": {
                "name": "X", "components": [{"kind": "quantum_oracle", "name": "Q"}]}})
            assert res.status_code == 400, res.text
            assert "quantum_oracle" in res.json()["detail"]

            # hallucinated node type
            res = await client.post("/ai-composer/build", headers=h, json={"spec": {
                "name": "X", "components": [
                    base_dataset,
                    {"kind": "workflow", "name": "w", "trigger": {"type": "manual_trigger"},
                     "steps": [{"type": "teleport_data"}]}]}})
            assert res.status_code == 400, res.text
            assert "teleport_data" in res.json()["detail"]

            # a trigger used as a STEP
            res = await client.post("/ai-composer/build", headers=h, json={"spec": {
                "name": "X", "components": [
                    base_dataset,
                    {"kind": "workflow", "name": "w", "trigger": {"type": "manual_trigger"},
                     "steps": [{"type": "webhook_trigger"}]}]}})
            assert res.status_code == 400, res.text
            assert "trigger" in res.json()["detail"]

            # dangling queue -> meeting reference
            res = await client.post("/ai-composer/build", headers=h, json={"spec": {
                "name": "X", "components": [
                    base_dataset,
                    {"kind": "queue", "name": "q", "meeting": "nowhere"}]}})
            assert res.status_code == 400, res.text
            assert "nowhere" in res.json()["detail"]

            # knowledge columns must exist on the referenced dataset
            res = await client.post("/ai-composer/build", headers=h, json={"spec": {
                "name": "X", "components": [
                    base_dataset,
                    {"kind": "voice_agent", "name": "a", "brain": "scaffold",
                     "knowledge": {"dataset": "D", "text_column": "nope",
                                   "answer_column": "a"}}]}})
            assert res.status_code == 400, res.text
            assert "nope" in res.json()["detail"]

            # duplicate names
            res = await client.post("/ai-composer/build", headers=h, json={"spec": {
                "name": "X", "components": [base_dataset, base_dataset]}})
            assert res.status_code == 400, res.text
            assert "duplicate" in res.json()["detail"]

            # a brain without its credential
            res = await client.post("/ai-composer/build", headers=h, json={"spec": {
                "name": "X", "components": [
                    base_dataset,
                    {"kind": "voice_agent", "name": "a", "brain": "ai_agent"}]}})
            assert res.status_code == 400, res.text
            assert "llm_credential_id" in res.json()["detail"]

            # an empty description proposes nothing
            res = await client.post("/ai-composer/propose", headers=h,
                                    json={"description": "short"})
            assert res.status_code == 422, res.text
    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 7. the generate door - describe -> deploy in one call, owner-scoped
# ---------------------------------------------------------------------------

def test_v82_generate_and_ownership():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "gen")
            other = await _mk_user(client, "gen", n=2)
            h = _auth(user["token"])
            oh = _auth(other["token"])

            res = await client.post("/ai-composer/generate", headers=h,
                                    json={"description": SUPPORT_DESC})
            assert res.status_code == 201, res.text
            built = res.json()
            assert built["system"]["lifecycle"] == "running"
            system_id = built["system"]["id"]

            # owner sees it; a stranger does not
            res = await client.get("/systems", headers=h)
            assert any(s["id"] == system_id for s in res.json())
            res = await client.get("/systems", headers=oh)
            assert not any(s["id"] == system_id for s in res.json())

            # two builds of the same description coexist (dataset suffixing)
            res = await client.post("/ai-composer/generate", headers=h,
                                    json={"description": SUPPORT_DESC})
            assert res.status_code == 201, res.text
            names = [d["name"] for d in res.json()["datasets"]]
            assert len(names) == len(set(names)), names
    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 8. version pin
# ---------------------------------------------------------------------------

def test_v82_version():
    from app.config import settings

    assert settings.version == "1.106.0"
