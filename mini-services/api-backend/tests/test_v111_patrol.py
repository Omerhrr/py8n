"""v111 tests - THE PATROL: the harness scheduling its own rounds.

* THE SYSTEM FIRES THE ROUNDS: a patrol binds a mission to a session and
  a rhythm; the sweep (or the run-now door) turns every due mission into
  a REAL harness turn through the SAME start_turn path - same loop, same
  guard, same fail-closed gate. Nothing about the discipline changes
  when nobody is watching.
* THE RECEIPT BOARD: run_count, last_run_at, last_status, last_run_turn_id
  and last_error live on the patrol row, stamped BEFORE the round runs
  (the rhythm stays honest across a crash) with the terminal state after.
* THE GATE HOLDS WITHOUT A HUMAN WATCHING: a patrol round that wants to
  move the business parks a slip; the decision's outcome lands back on
  the patrol receipt.
* FAILURES ARE RECEIPTS, NEVER WEDGES: a missing or inactive session is
  HELD (never counted), a crashed brain is a failed receipt, and one
  patrol's failure never stops the sweep from firing the next.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.db import AsyncSessionLocal  # noqa: E402
from app.engine.nodes.agent import AgentNode  # noqa: E402
from app.main import app  # noqa: E402
from app.models import HarnessPatrol  # noqa: E402
from app.services.harness import patrol as patrol_svc  # noqa: E402

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
    email = f"v111-{tag}-{uuid.uuid4().hex[:6]}@py8n.test"
    res = await client.post("/auth/register", json={
        "email": email, "password": "correct-horse-battery", "name": f"v111 {tag}",
    })
    assert res.status_code == 201, res.text
    return {"token": res.json()["token"], "id": res.json()["user"]["id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _mk_session(client: httpx.AsyncClient, headers: dict, **over) -> dict:
    body = {"name": "Patrol ops", "memory": "none"}
    body.update(over)
    res = await client.post("/harness/sessions", headers=headers, json=body)
    assert res.status_code == 201, res.text
    return res.json()


async def _mk_patrol(client: httpx.AsyncClient, headers: dict, session_id: str,
                     **over) -> dict:
    body = {"session_id": session_id, "name": "Front desk watch",
            "mission": "read the estate and summarize",
            "interval_seconds": 3600}
    body.update(over)
    res = await client.post("/harness/patrols", headers=headers, json=body)
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
# 1. CRUD + the doors' hygiene
# ---------------------------------------------------------------------------

def test_v111_patrol_crud_and_validation():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "crud")
            headers = _auth(user["token"])
            sess = await _mk_session(client, headers)

            # validation: unknown session, empty mission, sub-floor rhythm
            r = await client.post("/harness/patrols", headers=headers,
                                  json={"session_id": "nope", "name": "X",
                                        "mission": "m"})
            assert r.status_code == 404, r.text
            r = await client.post("/harness/patrols", headers=headers,
                                  json={"session_id": sess["id"], "name": "X",
                                        "mission": "  "})
            assert r.status_code == 422, r.text
            r = await client.post("/harness/patrols", headers=headers,
                                  json={"session_id": sess["id"], "name": "X",
                                        "mission": "m", "interval_seconds": 1})
            assert r.status_code == 422, r.text

            patrol = await _mk_patrol(client, headers, sess["id"])
            assert patrol["session_name"] == "Patrol ops"
            assert patrol["is_active"] is True and patrol["run_count"] == 0
            assert patrol["last_status"] is None and patrol["last_run_at"] is None

            # the list carries the session name; PATCH edits mission/rhythm
            r = await client.get("/harness/patrols", headers=headers)
            assert any(p["id"] == patrol["id"] for p in r.json())
            r = await client.patch(f"/harness/patrols/{patrol['id']}", headers=headers,
                                   json={"mission": "watch the desk",
                                         "interval_seconds": 600, "is_active": False})
            body = r.json()
            assert body["mission"] == "watch the desk"
            assert body["interval_seconds"] == 600 and body["is_active"] is False

            # ownership: a stranger's direct access looks nonexistent
            stranger = await _mk_user(client, "stranger")
            r = await client.get(f"/harness/patrols/{patrol['id']}",
                                 headers=_auth(stranger["token"]))
            assert r.status_code == 404, r.text
            r = await client.post(f"/harness/patrols/{patrol['id']}/run",
                                  headers=_auth(stranger["token"]))
            assert r.status_code == 404, r.text

            # delete removes it; the second delete is a 404
            r = await client.delete(f"/harness/patrols/{patrol['id']}", headers=headers)
            assert r.json()["deleted"] == patrol["id"]
            r = await client.get(f"/harness/patrols/{patrol['id']}", headers=headers)
            assert r.status_code == 404

    _sync(_go())


# ---------------------------------------------------------------------------
# 2. run-now: one round is a REAL harness turn through the SAME path
# ---------------------------------------------------------------------------

def test_v111_patrol_run_now_is_a_real_round():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "runnow")
            headers = _auth(user["token"])
            machine = await _mk_machine(client, headers, "runnow")
            sess = await _mk_session(client, headers)
            patrol = await _mk_patrol(
                client, headers, sess["id"],
                mission=f"who is on the desk of "
                        f"{machine['process']['name']}?",
                interval_seconds=3600)

            # the receipt board BEFORE: nothing has run
            assert patrol["run_count"] == 0 and patrol["last_status"] is None

            with _ScriptedLLM([
                '{"tool": "find_instances", "arguments": '
                '{"process": "%s"}}' % machine["process"]["name"],
                '{"answer": "Round report: one visitor in state a."}',
            ]):
                r = await client.post(f"/harness/patrols/{patrol['id']}/run",
                                      headers=headers)
            assert r.status_code == 200, r.text
            receipt = r.json()
            assert receipt["status"] == "completed", receipt
            assert receipt["turn_id"]

            # the receipt board AFTER: one honest round
            r = await client.get(f"/harness/patrols/{patrol['id']}", headers=headers)
            row = r.json()
            assert row["run_count"] == 1
            assert row["last_status"] == "completed"
            assert row["last_run_turn_id"] == receipt["turn_id"]
            assert row["last_run_at"] is not None and row["last_error"] == ""

            # the round IS a harness turn: patrol_id set, patrol_start frame,
            # real tool call through the real service, full trace
            r = await client.get(f"/harness/patrols/{patrol['id']}/runs", headers=headers)
            runs = r.json()
            assert len(runs) == 1 and runs[0]["id"] == receipt["turn_id"]
            turn = runs[0]
            assert turn["patrol_id"] == patrol["id"]
            assert turn["status"] == "completed"
            assert turn["tool_calls"][0]["tool"] == "find_instances"
            assert turn["tool_calls"][0]["status"] == "ok"
            assert machine["instance"]["id"] in turn["tool_calls"][0]["result"]
            events = [f["event"] for f in turn["trace"]]
            assert events[0] == "patrol_start" and events[-1] == "answer", events
            assert turn["trace"][0]["patrol"] == patrol["name"]

            # the same round is on the session transcript - one memory
            r = await client.get(f"/harness/sessions/{sess['id']}/turns", headers=headers)
            assert any(t["id"] == receipt["turn_id"] for t in r.json())

    _sync(_go())


# ---------------------------------------------------------------------------
# 3. THE GATE WITHOUT A HUMAN WATCHING: a patrol round parks a slip and waits
# ---------------------------------------------------------------------------

def test_v111_patrol_respects_the_gate():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "gate")
            headers = _auth(user["token"])
            machine = await _mk_machine(client, headers, "gate")
            iid = machine["instance"]["id"]
            sess = await _mk_session(client, headers)
            patrol = await _mk_patrol(client, headers, sess["id"],
                                      mission="walk anything stuck forward")

            with _ScriptedLLM([
                '{"tool": "advance_instance", "arguments": '
                '{"instance_id": "%s", "transition": "go", "note": "patrol says move"}}' % iid,
            ]):
                r = await client.post(f"/harness/patrols/{patrol['id']}/run",
                                      headers=headers)
            receipt = r.json()
            assert receipt["status"] == "waiting_approval", receipt

            # THE PROOF: the model asked through a patrol - nothing moved
            r = await client.get(f"/processes/{machine['process']['id']}/instances/{iid}",
                                 headers=headers)
            assert r.json()["state"] == "a", r.text

            # the receipt board shows the pause; the slip is pending
            r = await client.get(f"/harness/patrols/{patrol['id']}", headers=headers)
            assert r.json()["last_status"] == "waiting_approval"
            r = await client.get("/harness/approvals?status=pending", headers=headers)
            slips = r.json()
            assert len(slips) == 1 and slips[0]["tool"] == "advance_instance"

            # the human says yes -> the move happens at DECISION TIME and
            # the outcome lands back on the patrol receipt
            with _ScriptedLLM([
                '{"answer": "The patrol moved it with your blessing."}',
            ]):
                r = await client.post(f"/harness/approvals/{slips[0]['id']}/approve",
                                      headers=headers)
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "completed"

            r = await client.get(f"/processes/{machine['process']['id']}/instances/{iid}",
                                 headers=headers)
            assert r.json()["state"] == "b", r.text
            r = await client.get(f"/harness/patrols/{patrol['id']}", headers=headers)
            row = r.json()
            assert row["last_status"] == "completed", row
            assert row["last_run_turn_id"] == slips[0]["turn_id"]

    _sync(_go())


# ---------------------------------------------------------------------------
# 4. THE SWEEP: only due patrols fire; inactive ones never do
# ---------------------------------------------------------------------------

def test_v111_patrol_sweep_fires_only_due():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "sweep")
            headers = _auth(user["token"])
            sess = await _mk_session(client, headers)
            other = await _mk_session(client, headers)
            await client.patch(f"/harness/sessions/{other['id']}", headers=headers,
                               json={"is_active": False})

            due_a = await _mk_patrol(client, headers, sess["id"], name="A never ran")
            due_b = await _mk_patrol(client, headers, sess["id"], name="B ran long ago")
            asleep = await _mk_patrol(client, headers, sess["id"], name="C inactive",
                                      is_active=False)
            held = await _mk_patrol(client, headers, other["id"], name="D held")

            # B ran an hour ago -> due again; the manipulation is honest DB state
            async with AsyncSessionLocal() as db:
                row = await db.get(HarnessPatrol, due_b["id"])
                row.last_run_at = datetime.now(timezone.utc) - timedelta(hours=1)
                await db.commit()

            async def _sweep():
                async with AsyncSessionLocal() as db:
                    out = await patrol_svc.run_due(db)
                    await db.commit()
                    return out

            with _ScriptedLLM([
                '{"answer": "round report A"}',
                '{"answer": "round report B"}',
            ]):
                out = await _sweep()

            assert out["due"] == 3, out  # A (never ran), B (stale), D (session off)
            by_id = {r["patrol_id"]: r for r in out["receipts"]}
            assert by_id[due_a["id"]]["status"] == "completed"
            assert by_id[due_b["id"]]["status"] == "completed"
            assert by_id[held["id"]]["status"] == "held"
            assert "inactive" in by_id[held["id"]]["error"]
            assert asleep["id"] not in by_id  # an inactive patrol never fires

            # the held round was NOT counted - only real attempts are
            r = await client.get(f"/harness/patrols/{held['id']}", headers=headers)
            assert r.json()["run_count"] == 0 and r.json()["last_status"] == "held"
            r = await client.get(f"/harness/patrols/{due_a['id']}", headers=headers)
            assert r.json()["run_count"] == 1

            # an immediate second sweep: A and B are inside their rhythm,
            # C stays inactive, D is held again (its rhythm never stamps)
            with _ScriptedLLM([]):
                out2 = await _sweep()
            ids2 = {r["patrol_id"]: r["status"] for r in out2["receipts"]}
            assert ids2 == {held["id"]: "held"}, ids2
            r = await client.get(f"/harness/patrols/{due_a['id']}", headers=headers)
            assert r.json()["run_count"] == 1  # no double-fire inside the rhythm

    _sync(_go())


# ---------------------------------------------------------------------------
# 5. failures are receipts, never wedges: a dead brain or a dead session
# ---------------------------------------------------------------------------

def test_v111_patrol_failures_are_receipts():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "fail")
            headers = _auth(user["token"])
            sess = await _mk_session(client, headers)
            patrol = await _mk_patrol(client, headers, sess["id"], name="doomed")

            # a brain that dies mid-round: the receipt says failed, the turn
            # is marked failed (never a zombie 'running'), the door holds
            async def _boom(agent_self, messages, temperature):
                raise RuntimeError("the bridge fell over")

            original = AgentNode._chat
            AgentNode._chat = _boom  # type: ignore[method-assign]
            try:
                r = await client.post(f"/harness/patrols/{patrol['id']}/run",
                                      headers=headers)
            finally:
                AgentNode._chat = original  # type: ignore[method-assign]
            assert r.status_code == 200, r.text
            receipt = r.json()
            assert receipt["status"] == "failed", receipt
            assert "RuntimeError" in receipt["error"], receipt

            r = await client.get(f"/harness/patrols/{patrol['id']}", headers=headers)
            row = r.json()
            assert row["last_status"] == "failed" and "RuntimeError" in row["last_error"]
            assert row["run_count"] == 1  # a real attempt was made and recorded

            # the failed round is still an honest turn on the runs list
            r = await client.get(f"/harness/patrols/{patrol['id']}/runs", headers=headers)
            runs = r.json()
            assert len(runs) == 1 and runs[0]["status"] == "failed"
            assert "drive failed" in runs[0]["error"]

            # a patrol whose session is GONE: failed receipt, nothing wedged
            doomed_session = await _mk_session(client, headers)
            orphan = await _mk_patrol(client, headers, doomed_session["id"],
                                      name="orphan")
            r = await client.delete(f"/harness/sessions/{doomed_session['id']}",
                                    headers=headers)
            assert r.status_code == 200, r.text
            r = await client.post(f"/harness/patrols/{orphan['id']}/run", headers=headers)
            receipt = r.json()
            assert receipt["status"] == "failed" and "gone" in receipt["error"]
            assert receipt["turn_id"] is None

            # and one patrol's failure never stops the sweep: a healthy
            # patrol after a broken one still fires in the same sweep
            healthy = await _mk_patrol(client, headers, sess["id"], name="healthy")
            async with AsyncSessionLocal() as db:
                broken = await db.get(HarnessPatrol, orphan["id"])
                broken.is_active = True
                broken.session_id = "ghost-session"
                await db.commit()

            async def _sweep():
                async with AsyncSessionLocal() as db:
                    out = await patrol_svc.run_due(db)
                    await db.commit()
                    return out

            with _ScriptedLLM(['{"answer": "all quiet"}']):
                out = await _sweep()
            by_id = {r["patrol_id"]: r["status"] for r in out["receipts"]}
            assert by_id[orphan["id"]] == "failed"
            assert by_id[healthy["id"]] == "completed", by_id

    _sync(_go())


# ---------------------------------------------------------------------------
# 6. the sweep's registration honors the env knob, and the version pin
# ---------------------------------------------------------------------------

def test_v111_patrol_sweep_registration_and_pin():
    from app.services import scheduler as sched_mod

    class _FakeSched:
        def __init__(self):
            self.jobs = []

        def add_job(self, fn, trigger=None, id=None, **kw):
            self.jobs.append(id)

    fake = _FakeSched()
    old = os.environ.get("PY8N_PATROL_TICK_SECONDS")
    try:
        os.environ["PY8N_PATROL_TICK_SECONDS"] = "0"
        sched_mod._register_patrol_sweep(fake)
        assert fake.jobs == []  # 0 disables the sweep entirely

        os.environ["PY8N_PATROL_TICK_SECONDS"] = "45"
        sched_mod._register_patrol_sweep(fake)
        assert fake.jobs == ["harness:patrols"]

        os.environ["PY8N_PATROL_TICK_SECONDS"] = "1"
        sched_mod._register_patrol_sweep(fake)
        assert fake.jobs == ["harness:patrols", "harness:patrols"]  # floor applies
    finally:
        if old is None:
            os.environ.pop("PY8N_PATROL_TICK_SECONDS", None)
        else:
            os.environ["PY8N_PATROL_TICK_SECONDS"] = old

    assert settings.version == "1.120.0"
