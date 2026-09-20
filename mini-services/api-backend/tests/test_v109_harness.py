"""v109 tests - THE HARNESS: py8n's own agentic runtime, in tandem with the
system.

* THE TOOLCHEST IS PY8N ITSELF: ten tools (estate overview, machines and
  their instances, the attention feed, the chain map, the escalation
  heatmap, read-only dataset SQL) backed by the REAL services the API
  doors call - plus three SENSITIVE moves (start / advance / acknowledge)
  that change the business.
* THE GATE IS FAIL-CLOSED: a sensitive call never runs on the model's
  word. The turn pauses (waiting_approval), the exact wire freezes into
  the row, and only an explicit approve runs the move - AT DECISION TIME.
  Silence expires into a refusal when a TTL is set.
* THE GUARD RIDES BETWEEN ROUNDS: verbatim repeat calls are blocked with
  the refusal fed to the model; the iteration budget ends a loop that
  never answers as ``exhausted``.
* THE BRAIN IS THE NODE'S OWN: AgentNode._chat is scripted exactly like
  v19/v108 - everything here runs offline; the tool execution is REAL
  (a scripted turn actually moves a real instance through a real machine).
"""

from __future__ import annotations

import asyncio
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.engine.nodes.agent import AgentNode  # noqa: E402
from app.main import app  # noqa: E402

API = "http://testserver/api/v1"

FRONT_DESK = {
    "states": ["a", "b", "done"],
    "initial": "a",
    "transitions": [
        {"name": "go", "from": "a", "to": "b"},
        {"name": "finish", "from": "b", "to": "done"},
    ],
}


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
    email = f"v109-{tag}-{uuid.uuid4().hex[:6]}@py8n.test"
    res = await client.post("/auth/register", json={
        "email": email, "password": "correct-horse-battery", "name": f"v109 {tag}",
    })
    assert res.status_code == 201, res.text
    return {"token": res.json()["token"], "id": res.json()["user"]["id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _mk_session(client: httpx.AsyncClient, headers: dict, **over) -> dict:
    body = {"name": "Ops Harness", "memory": "none"}
    body.update(over)
    res = await client.post("/harness/sessions", headers=headers, json=body)
    assert res.status_code == 201, res.text
    return res.json()


async def _mk_machine(client: httpx.AsyncClient, headers: dict, tag: str) -> dict:
    res = await client.post("/processes", headers=headers, json={
        "name": f"Front desk {tag}", "definition": FRONT_DESK})
    assert res.status_code == 201, res.text
    proc = res.json()
    res = await client.post(f"/processes/{proc['id']}/instances", headers=headers,
                            json={"ref": f"K-{tag}", "title": "the visitor"})
    assert res.status_code == 201, res.text
    return {"process": proc, "instance": res.json()}


# ---------------------------------------------------------------------------
# 1. the toolchest door + a real read loop over the real services
# ---------------------------------------------------------------------------

def test_v109_toolchest_and_a_real_read_turn():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "read")
            headers = _auth(user["token"])

            # the catalog: fourteen tools since v110 (the builder), five gated
            r = await client.get("/harness/tools", headers=headers)
            assert r.status_code == 200, r.text
            catalog = r.json()
            names = [t["name"] for t in catalog]
            assert len(names) == 14, names
            assert {"start_instance", "advance_instance",
                    "acknowledge_escalation"} <= set(names)
            assert {t["name"] for t in catalog if t["sensitive"]} == {
                "start_instance", "advance_instance", "acknowledge_escalation",
                "build_machine", "install_operator"}

            # a machine exists; the harness reads it through the REAL service
            machine = await _mk_machine(client, headers, "read")
            sess = await _mk_session(client, headers)

            with _ScriptedLLM([
                '{"tool": "find_instances", "arguments": {"process": "front desk read"}}',
                '{"answer": "One visitor is in state a on Front desk read."}',
            ]):
                r = await client.post(f"/harness/sessions/{sess['id']}/turns",
                                      headers=headers, json={"message": "who is on the desk?"})
            assert r.status_code == 200, r.text
            turn = r.json()
            assert turn["status"] == "completed", turn
            assert turn["reply"] == "One visitor is in state a on Front desk read."
            assert turn["tool_calls"][0]["tool"] == "find_instances"
            assert turn["tool_calls"][0]["status"] == "ok"
            # the tool result carried the REAL instance (id, ref, state)
            assert machine["instance"]["id"] in turn["tool_calls"][0]["result"], \
                turn["tool_calls"][0]["result"]
            events = [f["event"] for f in turn["trace"]]
            assert "tool_call" in events and events[-1] == "answer", events

            # an unknown tool is feedback, not a crash
            with _ScriptedLLM([
                '{"tool": "launch_missiles", "arguments": {}}',
                '{"answer": "I only speak harness."}',
            ]):
                r = await client.post(f"/harness/sessions/{sess['id']}/turns",
                                      headers=headers, json={"message": "do something else"})
            turn2 = r.json()
            assert turn2["status"] == "completed"
            assert turn2["tool_calls"][0]["status"] == "error"
            assert "unknown" in turn2["tool_calls"][0]["result"].lower() or \
                "no tool" in turn2["tool_calls"][0]["result"].lower()

            # health answers with the guard config
            r = await client.get("/harness/health", headers=headers)
            assert r.status_code == 200
            body = r.json()
            assert body["ok"] is True and body["sessions"] >= 1
            assert body["guard"]["repeat_limit"] >= 1

    _sync(_go())


# ---------------------------------------------------------------------------
# 2. THE GATE: advance waits for a human; approve runs the move AT DECISION TIME
# ---------------------------------------------------------------------------

def test_v109_the_gate_approve_runs_the_move():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "gate")
            headers = _auth(user["token"])
            machine = await _mk_machine(client, headers, "gate")
            iid = machine["instance"]["id"]
            sess = await _mk_session(client, headers)

            with _ScriptedLLM([
                '{"tool": "advance_instance", "arguments": '
                '{"instance_id": "%s", "transition": "go", "note": "walked in"}}' % iid,
                '{"answer": "Moved: the visitor is now in b."}',
            ]):
                r = await client.post(f"/harness/sessions/{sess['id']}/turns",
                                      headers=headers, json={"message": "walk the visitor forward"})
            turn = r.json()
            assert turn["status"] == "waiting_approval", turn
            assert turn["waiting"] is True
            assert turn["reply"] == ""

            # THE PROOF OF THE GATE: the model asked, nothing moved
            r = await client.get(f"/processes/{machine['process']['id']}/instances/{iid}",
                                 headers=headers)
            assert r.json()["state"] == "a", r.text

            # the slip sits in the queue
            r = await client.get("/harness/approvals?status=pending", headers=headers)
            slips = r.json()
            assert len(slips) == 1, slips
            slip = slips[0]
            assert slip["tool"] == "advance_instance"
            assert slip["arguments"]["instance_id"] == iid

            # the human says yes -> the move happens NOW and the loop resumes
            # (the brain stays scripted across pause -> decide -> resume)
            with _ScriptedLLM([
                '{"answer": "Moved: the visitor is now in b."}',
            ]):
                r = await client.post(f"/harness/approvals/{slip['id']}/approve",
                                      headers=headers)
            assert r.status_code == 200, r.text
            done = r.json()
            assert done["status"] == "completed", done
            assert done["reply"] == "Moved: the visitor is now in b."
            events = [f["event"] for f in done["trace"]]
            assert "approval_requested" in events and "approval_decided" in events
            decided = next(f for f in done["trace"] if f["event"] == "approval_decided")
            assert decided["decision"] == "approved" and decided["status"] == "ok"
            assert done["tool_calls"][0]["tool"] == "advance_instance"
            assert done["tool_calls"][0]["status"] == "ok"

            # the REAL machine moved - by the harness, on the human's word
            r = await client.get(f"/processes/{machine['process']['id']}/instances/{iid}",
                                 headers=headers)
            assert r.json()["state"] == "b", r.text

            # deciding twice is a loud 409
            r = await client.post(f"/harness/approvals/{slip['id']}/approve",
                                  headers=headers)
            assert r.status_code == 409, r.text

    _sync(_go())


# ---------------------------------------------------------------------------
# 3. the gate holds on a NO: the model is told, nothing moves
# ---------------------------------------------------------------------------

def test_v109_the_gate_reject_keeps_the_estate_still():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "reject")
            headers = _auth(user["token"])
            machine = await _mk_machine(client, headers, "reject")
            iid = machine["instance"]["id"]
            sess = await _mk_session(client, headers)

            with _ScriptedLLM([
                '{"tool": "advance_instance", "arguments": {"instance_id": "%s", "to_state": "b"}}' % iid,
                '{"answer": "Understood - the move was declined, nothing changed."}',
            ]):
                r = await client.post(f"/harness/sessions/{sess['id']}/turns",
                                      headers=headers, json={"message": "move it"})
            assert r.json()["status"] == "waiting_approval"

            r = await client.get("/harness/approvals?status=pending", headers=headers)
            slip = r.json()[0]
            with _ScriptedLLM([
                '{"answer": "Understood - the move was declined, nothing changed."}',
            ]):
                r = await client.post(f"/harness/approvals/{slip['id']}/reject",
                                      headers=headers)
            done = r.json()
            assert done["status"] == "completed", done
            assert done["reply"] == "Understood - the move was declined, nothing changed."

            # the machine NEVER moved
            r = await client.get(f"/processes/{machine['process']['id']}/instances/{iid}",
                                 headers=headers)
            assert r.json()["state"] == "a", r.text
            # the queue is empty
            r = await client.get("/harness/approvals?status=pending", headers=headers)
            assert r.json() == []

    _sync(_go())


# ---------------------------------------------------------------------------
# 4. FAIL-CLOSED on silence: a slip past the TTL expires and the turn refuses
# ---------------------------------------------------------------------------

def test_v109_fail_closed_expiry_refuses_on_silence():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "ttl")
            headers = _auth(user["token"])
            machine = await _mk_machine(client, headers, "ttl")
            iid = machine["instance"]["id"]
            sess = await _mk_session(client, headers)

            old_ttl = settings.harness_approval_ttl_seconds
            settings.harness_approval_ttl_seconds = 1
            try:
                with _ScriptedLLM([
                    '{"tool": "advance_instance", "arguments": {"instance_id": "%s", "transition": "go"}}' % iid,
                ]):
                    r = await client.post(f"/harness/sessions/{sess['id']}/turns",
                                          headers=headers, json={"message": "move it"})
                assert r.json()["status"] == "waiting_approval"

                time.sleep(1.15)

                # reading the queue sweeps: the slip expires, the turn refuses
                r = await client.get("/harness/approvals", headers=headers)
                slips = r.json()
                assert slips[0]["status"] == "expired", slips
                r = await client.get(f"/harness/turns/{slips[0]['turn_id']}",
                                     headers=headers)
                turn = r.json()
                assert turn["status"] == "refused", turn
                assert "fail-closed" in turn["error"]

                # deciding an expired slip is a loud 409 - silence is never consent
                r = await client.post(f"/harness/approvals/{slips[0]['id']}/approve",
                                      headers=headers)
                assert r.status_code == 409, r.text

                # and the machine still never moved
                r = await client.get(
                    f"/processes/{machine['process']['id']}/instances/{iid}",
                    headers=headers)
                assert r.json()["state"] == "a"
            finally:
                settings.harness_approval_ttl_seconds = old_ttl

    _sync(_go())


# ---------------------------------------------------------------------------
# 5. the guard: verbatim repeats are blocked; a loop that never answers exhausts
# ---------------------------------------------------------------------------

def test_v109_guard_blocks_repeats_and_exhausts_budget():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "guard")
            headers = _auth(user["token"])
            await _mk_machine(client, headers, "guard")
            sess = await _mk_session(client, headers)

            with _ScriptedLLM([
                '{"tool": "list_processes", "arguments": {}}',
                '{"tool": "list_processes", "arguments": {}}',
                '{"tool": "list_processes", "arguments": {}}',
                '{"answer": "You have one machine; the guard stopped my echo."}',
            ]):
                r = await client.post(f"/harness/sessions/{sess['id']}/turns",
                                      headers=headers, json={"message": "list it three times"})
            turn = r.json()
            assert turn["status"] == "completed", turn
            assert turn["guard_blocks"] == 1, turn
            guard_frames = [f for f in turn["trace"] if f["event"] == "guard"]
            assert guard_frames and "repeat" in guard_frames[0]["reason"]
            # the third verbatim call was blocked, not executed
            executed = [f for f in turn["trace"] if f["event"] == "tool_call"]
            assert len(executed) == 2, executed

            # budget: a loop that never answers ends EXHAUSTED, not eternal
            old_max = settings.harness_max_iterations
            settings.harness_max_iterations = 2
            try:
                def _always_tool(messages):
                    return '{"tool": "list_processes", "arguments": {}}'

                with _ScriptedLLM([_always_tool, _always_tool, _always_tool]):
                    r = await client.post(f"/harness/sessions/{sess['id']}/turns",
                                          headers=headers, json={"message": "loop forever"})
                turn = r.json()
                assert turn["status"] == "exhausted", turn
                assert "budget" in turn["error"]
                assert turn["iterations"] == 2
            finally:
                settings.harness_max_iterations = old_max

    _sync(_go())


# ---------------------------------------------------------------------------
# 6. ownership, the doors' hygiene, and the version pin
# ---------------------------------------------------------------------------

def test_v109_ownership_and_version_pin():
    async def _go():
        async with _client() as client:
            owner = await _mk_user(client, "own")
            stranger = await _mk_user(client, "stranger")
            oh = _auth(owner["token"])
            sh = _auth(stranger["token"])

            # openai_compatible without a credential refuses loudly
            r = await client.post("/harness/sessions", headers=oh,
                                  json={"name": "X", "provider": "openai_compatible"})
            assert r.status_code == 400, r.text

            sess = await _mk_session(client, oh, name="Private ops")
            # a stranger's direct access looks nonexistent; the list hides it
            r = await client.get(f"/harness/sessions/{sess['id']}", headers=sh)
            assert r.status_code == 404
            r = await client.get("/harness/sessions", headers=sh)
            assert all(s["id"] != sess["id"] for s in r.json())
            # an inactive session refuses to run with a loud 409
            r = await client.patch(f"/harness/sessions/{sess['id']}", headers=oh,
                                   json={"is_active": False})
            assert r.json()["is_active"] is False
            with _ScriptedLLM([]):
                r = await client.post(f"/harness/sessions/{sess['id']}/turns",
                                      headers=oh, json={"message": "hello"})
            assert r.status_code == 409, r.text

            # delete removes it; the second delete is a 404
            r = await client.delete(f"/harness/sessions/{sess['id']}", headers=oh)
            assert r.json()["deleted"] == sess["id"]
            r = await client.get(f"/harness/sessions/{sess['id']}", headers=oh)
            assert r.status_code == 404

    _sync(_go())


def test_v109_version_pin():
    assert settings.version == "1.116.0"
