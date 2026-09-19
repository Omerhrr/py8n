"""v110 tests - THE BUILDER: the harness becomes the composer AND builder.

Until v109 the harness could OPERATE the estate (read everything, move
entities, ack escalations). v110 completes the original vision - it BUILDS:

* THE MENU: build_menu shows the archetypes, the component kinds and the
  operator shelf - what py8n can build, straight from the real catalog.
* THE BLUEPRINT IS FREE: draft_machine turns a description into a validated
  spec (or validates a hand-composed one) and builds NOTHING - the proof
  runs against the real /datasets, /workflows and /systems doors.
* THE BUILD IS GATED: build_machine rides the SAME fail-closed gate as the
  moves - the slip carries the whole spec, the machine does not exist on
  the model's word alone, and approving runs the composer's REAL build path
  (datasets, workflows inactive, the running system binding them).
* THE PREFLIGHT: a malformed sensitive call never reaches a human - the
  spec-less or invalid build bounces as tool feedback without a slip.
* THE RECEIPT: a decision stamps decided_by - the slip remembers WHO.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.engine.nodes.agent import AgentNode  # noqa: E402
from app.main import app  # noqa: E402

API = "http://testserver/api/v1"


@pytest.fixture(autouse=True)
def _fresh_rate_limit():
    from app.api import _ratelimit

    _ratelimit.reset_all()
    yield
    _ratelimit.reset_all()


class _ScriptedLLM:
    """Replaces AgentNode._chat with a scripted reply sequence (the harness
    shim rides the same transport, so the same monkeypatch drives it)."""

    def __init__(self, replies):
        self._replies = list(replies)
        self._original = AgentNode._chat
        self.calls = 0

    def __enter__(self):
        holder = self

        async def _fake_chat(agent_self, messages, temperature):
            holder.calls += 1
            reply = holder._replies.pop(0) if holder._replies else '{"answer": "done"}'
            if callable(reply):
                return reply(messages)
            return reply

        AgentNode._chat = _fake_chat  # type: ignore[method-assign]
        return self

    def __exit__(self, *exc):
        AgentNode._chat = self._original  # type: ignore[method-assign]


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


def _sync(coro):
    return asyncio.run(coro)


async def _mk_user(client: httpx.AsyncClient, tag: str) -> dict:
    email = f"v110-{tag}-{uuid.uuid4().hex[:6]}@py8n.test"
    res = await client.post("/auth/register", json={
        "email": email, "password": "correct-horse-battery", "name": f"v110 {tag}",
    })
    assert res.status_code == 201, res.text
    return {"token": res.json()["token"], "id": res.json()["user"]["id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _mk_session(client: httpx.AsyncClient, headers: dict, **over) -> dict:
    body = {"name": "Builder Harness", "memory": "none"}
    body.update(over)
    res = await client.post("/harness/sessions", headers=headers, json=body)
    assert res.status_code == 201, res.text
    return res.json()


def _relay_blueprint(messages):
    """The honest agent move: read the draft's TOOL RESULT off the wire and
    hand its exact spec to build_machine (the human will see it all)."""
    for m in reversed(messages):
        if m["role"] == "user" and \
                m["content"].startswith("TOOL RESULT draft_machine: "):
            payload = json.loads(m["content"].split(": ", 1)[1])
            return json.dumps({"tool": "build_machine",
                               "arguments": {"spec": payload["spec"]}})
    return json.dumps({"answer": "I never saw the blueprint"})


# ---------------------------------------------------------------------------
# 1. the menu and the drafts - blueprints are free, nothing is built
# ---------------------------------------------------------------------------

def test_v110_build_menu_and_drafts_build_nothing():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "menu")
            headers = _auth(user["token"])
            sess = await _mk_session(client, headers)

            # the shared suite DB carries rows from other tests (unclaimed
            # rows are estate-visible) - all proofs are DELTAS, not absolutes
            r = await client.get("/datasets", headers=headers)
            ds_before = {d["id"] for d in r.json()}
            r = await client.get("/workflows", headers=headers)
            wf_before = {w["id"] for w in r.json()}
            r = await client.get("/systems", headers=headers)
            sys_before = {s["id"] for s in r.json()}

            # the menu: archetypes, kinds and the operator shelf - real data
            with _ScriptedLLM([
                '{"tool": "build_menu", "arguments": {}}',
                '{"answer": "I can draft four archetypes and install nine operators."}',
            ]):
                r = await client.post(f"/harness/sessions/{sess['id']}/turns",
                                      headers=headers, json={"message": "what can you build?"})
            turn = r.json()
            assert turn["status"] == "completed", turn
            assert turn["tool_calls"][0]["status"] == "ok"
            menu_text = turn["tool_calls"][0]["result"]
            assert "lead_desk" in menu_text and "sales-operator" in menu_text
            assert "clinic_front_desk" in menu_text and "dataset" in menu_text

            # the draft: a description becomes a validated blueprint
            with _ScriptedLLM([
                '{"tool": "draft_machine", "arguments": '
                '{"description": "a lead desk for my sales team"}}',
                '{"answer": "The blueprint has five components; nothing built yet."}',
            ]):
                r = await client.post(f"/harness/sessions/{sess['id']}/turns",
                                      headers=headers, json={"message": "draft me a lead desk"})
            turn = r.json()
            assert turn["status"] == "completed", turn
            draft_text = turn["tool_calls"][0]["result"]
            assert '"source": "archetype"' in draft_text or '"source":"archetype"' \
                in draft_text.replace(" ", ""), draft_text[:200]
            assert "blueprint only" in draft_text

            # THE PROOF OF THE DRAFT: the estate has NO new machine
            r = await client.get("/datasets", headers=headers)
            assert {d["id"] for d in r.json()} == ds_before, r.text
            r = await client.get("/workflows", headers=headers)
            assert {w["id"] for w in r.json()} == wf_before, r.text
            r = await client.get("/systems", headers=headers)
            assert {s["id"] for s in r.json()} == sys_before, r.text

            # a hand-written broken spec is feedback, not a crash
            with _ScriptedLLM([
                '{"tool": "draft_machine", "arguments": {"spec": {"name": "Broken"}}}',
                '{"answer": "The draft needs components."}',
            ]):
                r = await client.post(f"/harness/sessions/{sess['id']}/turns",
                                      headers=headers, json={"message": "draft this junk"})
            turn = r.json()
            assert turn["status"] == "completed", turn
            assert turn["tool_calls"][0]["status"] == "error"
            assert "validate" in turn["tool_calls"][0]["result"].lower()

            # no arguments at all - the tool teaches the protocol
            with _ScriptedLLM([
                '{"tool": "draft_machine", "arguments": {}}',
                '{"answer": "I need a description or a spec."}',
            ]):
                r = await client.post(f"/harness/sessions/{sess['id']}/turns",
                                      headers=headers, json={"message": "draft"})
            turn = r.json()
            assert turn["tool_calls"][0]["status"] == "error"
            assert "description" in turn["tool_calls"][0]["result"]

    _sync(_go())


# ---------------------------------------------------------------------------
# 2. THE PREFLIGHT: a malformed build never reaches a human
# ---------------------------------------------------------------------------

def test_v110_preflight_bounces_malformed_builds_before_the_gate():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "preflight")
            headers = _auth(user["token"])
            sess = await _mk_session(client, headers)

            # a spec-less build_machine: tool feedback at once, NO slip
            with _ScriptedLLM([
                '{"tool": "build_machine", "arguments": {}}',
                '{"answer": "Right - I need to draft first."}',
            ]):
                r = await client.post(f"/harness/sessions/{sess['id']}/turns",
                                      headers=headers, json={"message": "build it now"})
            turn = r.json()
            assert turn["status"] == "completed", turn
            assert turn["tool_calls"][0]["status"] == "error"
            assert "draft_machine" in turn["tool_calls"][0]["result"]
            r = await client.get("/harness/approvals?status=pending", headers=headers)
            assert r.json() == [], "a malformed call must never pause a human"

            # an invalid spec bounces the same way (validation, not a gate)
            with _ScriptedLLM([
                '{"tool": "build_machine", "arguments": '
                '{"spec": {"name": "Junk", "components": [{"kind": "widget", "name": "W"}]}}}',
                '{"answer": "The spec does not validate - widget is not a kind."}',
            ]):
                r = await client.post(f"/harness/sessions/{sess['id']}/turns",
                                      headers=headers, json={"message": "build the junk"})
            turn = r.json()
            assert turn["status"] == "completed", turn
            assert turn["tool_calls"][0]["status"] == "error"
            assert "does not validate" in turn["tool_calls"][0]["result"]

            # an unknown operator slug never bothers a human either
            with _ScriptedLLM([
                '{"tool": "install_operator", "arguments": {"slug": "time-machine"}}',
                '{"answer": "No such operator - I checked the shelf."}',
            ]):
                r = await client.post(f"/harness/sessions/{sess['id']}/turns",
                                      headers=headers, json={"message": "install time machine"})
            turn = r.json()
            assert turn["tool_calls"][0]["status"] == "error"
            assert "unknown operator" in turn["tool_calls"][0]["result"]
            assert "sales-operator" in turn["tool_calls"][0]["result"]  # the shelf listed
            r = await client.get("/harness/approvals?status=pending", headers=headers)
            assert r.json() == []

    _sync(_go())


# ---------------------------------------------------------------------------
# 3. THE BUILD GATE: the machine does not exist on the model's word alone
# ---------------------------------------------------------------------------

def test_v110_the_build_gate_approve_builds_the_machine():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "build")
            headers = _auth(user["token"])
            sess = await _mk_session(client, headers)

            # shared-DB discipline: prove the build by DELTA, not by absence
            r = await client.get("/datasets", headers=headers)
            ds_before = {d["id"] for d in r.json()}
            r = await client.get("/workflows", headers=headers)
            wf_before = {w["id"] for w in r.json()}
            r = await client.get("/systems", headers=headers)
            sys_before = {s["id"] for s in r.json()}

            # the model drafts, then relays the EXACT spec into the build
            with _ScriptedLLM([
                '{"tool": "draft_machine", "arguments": '
                '{"description": "a lead desk for my sales team"}}',
                _relay_blueprint,
                '{"answer": "The lead desk is built: dataset, intake workflow '
                'and the running system - on your word."}',
            ]):
                r = await client.post(f"/harness/sessions/{sess['id']}/turns",
                                      headers=headers,
                                      json={"message": "draft a lead desk and build it"})
            turn = r.json()
            assert turn["status"] == "waiting_approval", turn
            assert turn["waiting"] is True

            # the slip carries the WHOLE spec - that is what the human reviews
            r = await client.get("/harness/approvals?status=pending", headers=headers)
            slips = r.json()
            assert len(slips) == 1 and slips[0]["tool"] == "build_machine", slips
            spec_args = slips[0]["arguments"]["spec"]
            assert spec_args["name"] and spec_args["components"], spec_args
            kinds = [c["kind"] for c in spec_args["components"]]
            assert "dataset" in kinds and "workflow" in kinds

            # THE PROOF OF THE GATE: the model asked, NOTHING exists
            r = await client.get("/datasets", headers=headers)
            assert {d["id"] for d in r.json()} == ds_before, r.text
            r = await client.get("/workflows", headers=headers)
            assert {w["id"] for w in r.json()} == wf_before, r.text
            r = await client.get("/systems", headers=headers)
            assert {s["id"] for s in r.json()} == sys_before, r.text

            # the human says yes -> the composer's REAL build path runs NOW
            with _ScriptedLLM([
                '{"answer": "The lead desk is built: dataset, intake workflow '
                'and the running system - on your word."}',
            ]):
                r = await client.post(f"/harness/approvals/{slips[0]['id']}/approve",
                                      headers=headers)
            assert r.status_code == 200, r.text
            done = r.json()
            assert done["status"] == "completed", done
            decided = next(f for f in done["trace"] if f["event"] == "approval_decided")
            assert decided["decision"] == "approved" and decided["status"] == "ok"
            assert decided["decided_by"] == user["id"]  # the receipt

            # THE MACHINE EXISTS - the build happened at decision time
            r = await client.get("/datasets", headers=headers)
            datasets = r.json()
            new_datasets = [d for d in datasets if d["id"] not in ds_before]
            assert any(d["name"] == "Leads" for d in new_datasets), \
                [d["name"] for d in new_datasets]
            r = await client.get("/workflows", headers=headers)
            workflows = r.json()
            new_workflows = [w for w in workflows if w["id"] not in wf_before]
            intake = next((w for w in new_workflows if w["name"] == "Lead intake"), None)
            assert intake is not None, [w["name"] for w in new_workflows]
            assert intake["is_active"] is False  # the honest inactive install
            r = await client.get("/systems", headers=headers)
            new_systems = [s for s in r.json() if s["id"] not in sys_before]
            assert len(new_systems) == 1, [s["name"] for s in new_systems]
            assert new_systems[0]["lifecycle"] == "running", new_systems[0]
            assert new_systems[0]["name"] == spec_args["name"]  # the slip's spec built

            # the approval list remembers who decided
            r = await client.get("/harness/approvals", headers=headers)
            slip = next(a for a in r.json() if a["id"] == slips[0]["id"])
            assert slip["status"] == "approved" and slip["decided_by"] == user["id"]

            # deciding twice is a loud 409
            r = await client.post(f"/harness/approvals/{slips[0]['id']}/approve",
                                  headers=headers)
            assert r.status_code == 409, r.text

    _sync(_go())


# ---------------------------------------------------------------------------
# 4. THE OPERATOR GATE: a NO keeps the estate exactly as it was
# ---------------------------------------------------------------------------

def test_v110_the_operator_gate_reject_installs_nothing():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "opgate")
            headers = _auth(user["token"])
            sess = await _mk_session(client, headers)

            # shared-DB discipline: the NO proves itself by DELTA
            r = await client.get("/systems", headers=headers)
            sys_before = {s["id"] for s in r.json()}
            r = await client.get("/processes", headers=headers)
            proc_before = {p["id"] for p in r.json()["processes"]}

            with _ScriptedLLM([
                '{"tool": "install_operator", "arguments": '
                '{"slug": "sales-operator", "brain": "scaffold"}}',
                '{"answer": "Understood - the install was declined; the estate stands."}',
            ]):
                r = await client.post(f"/harness/sessions/{sess['id']}/turns",
                                      headers=headers,
                                      json={"message": "install the sales operator"})
            turn = r.json()
            assert turn["status"] == "waiting_approval", turn
            r = await client.get("/harness/approvals?status=pending", headers=headers)
            slip = r.json()[0]
            assert slip["tool"] == "install_operator"
            assert slip["arguments"]["slug"] == "sales-operator"

            # the human says NO
            with _ScriptedLLM([
                '{"answer": "Understood - the install was declined; the estate stands."}',
            ]):
                r = await client.post(f"/harness/approvals/{slip['id']}/reject",
                                      headers=headers)
            done = r.json()
            assert done["status"] == "completed", done
            decided = next(f for f in done["trace"] if f["event"] == "approval_decided")
            assert decided["decision"] == "rejected"
            assert decided["decided_by"] == user["id"]

            # THE PROOF OF THE NO: no system, no operator business, nothing
            r = await client.get("/systems", headers=headers)
            assert {s["id"] for s in r.json()} == sys_before, r.text
            r = await client.get("/processes", headers=headers)
            assert {p["id"] for p in r.json()["processes"]} == proc_before, r.text
            r = await client.get("/harness/approvals?status=pending", headers=headers)
            assert r.json() == []

    _sync(_go())


# ---------------------------------------------------------------------------
# 5. the pin
# ---------------------------------------------------------------------------

def test_v110_version_pin():
    assert settings.version == "1.111.0"
