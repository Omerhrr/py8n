"""v112 tests - THE DISPATCH: a patrol's findings walk OUT of py8n.

* THE ANSWER MAILS HOME: a patrol with dispatch_to sends its round's
  outcome over the report envelope's OWN email path (reports._send_report_email
  - zero parallel mail machinery) when the receipt reaches a terminal state.
* ONE TRIGGER: completed / exhausted / failed / refused mail; a paused
  round (waiting_approval) and a held round stay silent - the gated
  round's mail goes out when the DECISION lands, carrying what the
  decision made real.
* FAILURES ARE RECEIPTS: a broken SMTP stamps last_dispatch_status=error
  and never flips the round or wedges the sweep; unconfigured SMTP is a
  skip, no recipients is silence.
* SILENCE REFUSES AND THE REFUSAL WALKS OUT: an expired slip refuses its
  patrol round, the receipt board stops saying waiting_approval, and the
  refusal mails with the rest.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.db import AsyncSessionLocal  # noqa: E402
from app.engine.nodes.agent import AgentNode  # noqa: E402
from app.main import app  # noqa: E402
from app.models import HarnessPatrol  # noqa: E402
from app.services import reports as report_svc  # noqa: E402
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
    """Replaces AgentNode._chat with a scripted reply sequence."""

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


class _MailCapture:
    """Stands in for the real SMTP send - captures every digest the
    dispatch tries to walk out, and can simulate a dead relay."""

    def __init__(self):
        self.sent: list[dict] = []
        self._original = report_svc._send_report_email
        self.fail_with: Exception | None = None

    def __enter__(self):
        holder = self

        def _capture(**kw):
            if holder.fail_with is not None:
                raise holder.fail_with
            holder.sent.append(kw)
            return None

        report_svc._send_report_email = _capture  # type: ignore[method-assign]
        return self

    def __exit__(self, *exc):
        report_svc._send_report_email = self._original  # type: ignore[method-assign]


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


def _sync(coro):
    return asyncio.run(coro)


async def _mk_user(client: httpx.AsyncClient, tag: str) -> dict:
    email = f"v112-{tag}-{uuid.uuid4().hex[:6]}@py8n.test"
    res = await client.post("/auth/register", json={
        "email": email, "password": "correct-horse-battery", "name": f"v112 {tag}",
    })
    assert res.status_code == 201, res.text
    return {"token": res.json()["token"], "id": res.json()["user"]["id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _mk_session(client: httpx.AsyncClient, headers: dict, **over) -> dict:
    body = {"name": "Dispatch ops", "memory": "none"}
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


def _mail_cap():
    return _MailCapture()


# ---------------------------------------------------------------------------
# 1. a completed round mails the answer over the report envelope's path
# ---------------------------------------------------------------------------

def test_v112_completed_round_mails_the_answer():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "mail")
            headers = _auth(user["token"])
            sess = await _mk_session(client, headers)
            patrol = await _mk_patrol(
                client, headers, sess["id"],
                dispatch_to="ops@example.com, chief@example.com")

            assert patrol["dispatch_to"] == "ops@example.com, chief@example.com"
            assert patrol["last_dispatch_status"] is None

            old_host = settings.smtp_host
            settings.smtp_host = "smtp.py8n.test"
            try:
                with _mail_cap() as cap, _ScriptedLLM([
                    '{"answer": "Round report: the desk is quiet."}',
                ]):
                    r = await client.post(f"/harness/patrols/{patrol['id']}/run",
                                          headers=headers)
            finally:
                settings.smtp_host = old_host
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "completed", r.text

            # the digest walked out - exactly one mail, the round's answer
            assert len(cap.sent) == 1, cap.sent
            mail = cap.sent[0]
            assert mail["to"] == ["ops@example.com", "chief@example.com"]
            assert "Front desk watch" in mail["subject"]
            assert "- completed" in mail["subject"]
            assert "Round report: the desk is quiet." in mail["body"]

            # the dispatch receipt lives on the patrol row
            r = await client.get(f"/harness/patrols/{patrol['id']}", headers=headers)
            row = r.json()
            assert row["last_dispatch_status"] == "ok", row
            assert "2 recipient(s)" in row["last_dispatch_detail"]
            assert row["last_dispatch_at"] is not None
            # dispatch never flips the round itself
            assert row["last_status"] == "completed" and row["run_count"] == 1

    _sync(_go())


# ---------------------------------------------------------------------------
# 2. quiet by config: no recipients, no dispatch, not even a receipt line
# ---------------------------------------------------------------------------

def test_v112_no_recipients_is_silent():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "quiet")
            headers = _auth(user["token"])
            sess = await _mk_session(client, headers)
            patrol = await _mk_patrol(client, headers, sess["id"])

            old_host = settings.smtp_host
            settings.smtp_host = "smtp.py8n.test"
            try:
                with _mail_cap() as cap, _ScriptedLLM([
                    '{"answer": "all quiet"}',
                ]):
                    r = await client.post(f"/harness/patrols/{patrol['id']}/run",
                                          headers=headers)
            finally:
                settings.smtp_host = old_host
            assert r.json()["status"] == "completed", r.text
            assert cap.sent == []  # nothing walked out

            r = await client.get(f"/harness/patrols/{patrol['id']}", headers=headers)
            row = r.json()
            assert row["dispatch_to"] == ""
            assert row["last_dispatch_status"] is None
            assert row["last_dispatch_at"] is None

    _sync(_go())


# ---------------------------------------------------------------------------
# 3. THE GATE HOLDS THE MAIL: a paused round is silent; the decision mails
# ---------------------------------------------------------------------------

def test_v112_the_gate_holds_the_mail_until_the_decision():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "gate")
            headers = _auth(user["token"])
            machine = await _mk_machine(client, headers, "gate")
            iid = machine["instance"]["id"]
            sess = await _mk_session(client, headers)
            patrol = await _mk_patrol(
                client, headers, sess["id"],
                mission="walk anything stuck forward",
                dispatch_to="chief@example.com")

            old_host = settings.smtp_host
            settings.smtp_host = "smtp.py8n.test"
            try:
                with _mail_cap() as cap, _ScriptedLLM([
                    '{"tool": "advance_instance", "arguments": '
                    '{"instance_id": "%s", "transition": "go"}}' % iid,
                ]):
                    r = await client.post(f"/harness/patrols/{patrol['id']}/run",
                                          headers=headers)
                assert r.json()["status"] == "waiting_approval", r.text
                assert cap.sent == [], cap.sent  # a pause is not a finding

                r = await client.get("/harness/approvals?status=pending",
                                     headers=headers)
                slips = r.json()
                assert len(slips) == 1

                # the human says yes -> the move happens at decision time
                # and THE OUTCOME mails, carrying what the decision made real
                with _mail_cap() as cap2, _ScriptedLLM([
                    '{"answer": "The visitor moved with your blessing."}',
                ]):
                    r = await client.post(
                        f"/harness/approvals/{slips[0]['id']}/approve",
                        headers=headers)
                assert r.status_code == 200, r.text
                assert r.json()["status"] == "completed"

                assert len(cap2.sent) == 1, cap2.sent
                mail = cap2.sent[0]
                assert "- completed" in mail["subject"]
                assert "The visitor moved with your blessing." in mail["body"]
            finally:
                settings.smtp_host = old_host

            # the instance really moved
            r = await client.get(
                f"/processes/{machine['process']['id']}/instances/{iid}",
                headers=headers)
            assert r.json()["state"] == "b", r.text

    _sync(_go())


# ---------------------------------------------------------------------------
# 4. unconfigured SMTP is a skip; a broken relay is a receipt never a wedge
# ---------------------------------------------------------------------------

def test_v112_dispatch_failures_are_receipts():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "fail")
            headers = _auth(user["token"])
            sess = await _mk_session(client, headers)

            # SKIPPED: SMTP unset - the dispatch says so, honestly
            patrol = await _mk_patrol(client, headers, sess["id"],
                                      name="skipper",
                                      dispatch_to="ops@example.com")
            with _mail_cap() as cap, _ScriptedLLM(['{"answer": "round one"}']):
                r = await client.post(f"/harness/patrols/{patrol['id']}/run",
                                      headers=headers)
            assert r.json()["status"] == "completed", r.text
            assert cap.sent == []
            r = await client.get(f"/harness/patrols/{patrol['id']}", headers=headers)
            row = r.json()
            assert row["last_dispatch_status"] == "skipped", row
            assert "PY8N_SMTP_HOST" in row["last_dispatch_detail"]

            # ERROR: the relay is dead - the round still completed and the
            # sweep is intact (a receipt, never a wedge)
            old_host = settings.smtp_host
            settings.smtp_host = "smtp.py8n.test"
            try:
                with _mail_cap() as cap2, _ScriptedLLM(['{"answer": "round two"}']) as llm:
                    cap2.fail_with = RuntimeError("relay on fire")
                    r = await client.post(f"/harness/patrols/{patrol['id']}/run",
                                          headers=headers)
                assert r.json()["status"] == "completed", r.text  # the round lived
                assert llm.calls == 1

                r = await client.get(f"/harness/patrols/{patrol['id']}", headers=headers)
                row = r.json()
                assert row["last_dispatch_status"] == "error", row
                assert "RuntimeError" in row["last_dispatch_detail"]
                assert row["run_count"] == 2 and row["last_status"] == "completed"

                # a patrol whose mail dies never stops the next one
                healthy = await _mk_patrol(client, headers, sess["id"],
                                           name="healthy",
                                           dispatch_to="ops@example.com")
                with _mail_cap() as cap3, _ScriptedLLM(['{"answer": "healthy round"}']):
                    r = await client.post(f"/harness/patrols/{healthy['id']}/run",
                                          headers=headers)
                assert r.json()["status"] == "completed", r.text
                assert len(cap3.sent) == 1  # the healthy mail walked out
            finally:
                settings.smtp_host = old_host

    _sync(_go())


# ---------------------------------------------------------------------------
# 5. silence refuses the round and the refusal walks out (fail-closed)
# ---------------------------------------------------------------------------

def test_v112_silence_refuses_and_the_refusal_walks_out():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "ttl")
            headers = _auth(user["token"])
            machine = await _mk_machine(client, headers, "ttl")
            iid = machine["instance"]["id"]
            sess = await _mk_session(client, headers)
            patrol = await _mk_patrol(
                client, headers, sess["id"],
                mission="walk anything stuck forward",
                dispatch_to="chief@example.com")

            old_host = settings.smtp_host
            old_ttl = settings.harness_approval_ttl_seconds
            settings.smtp_host = "smtp.py8n.test"
            settings.harness_approval_ttl_seconds = 1
            try:
                with _mail_cap() as cap, _ScriptedLLM([
                    '{"tool": "advance_instance", "arguments": '
                    '{"instance_id": "%s", "transition": "go"}}' % iid,
                ]):
                    r = await client.post(f"/harness/patrols/{patrol['id']}/run",
                                          headers=headers)
                assert r.json()["status"] == "waiting_approval", r.text
                assert cap.sent == []

                # nobody answers; past the TTL the sweep refuses the round
                import anyio

                await anyio.sleep(1.2)
                with _mail_cap() as cap2:
                    r = await client.get("/harness/approvals?status=pending",
                                         headers=headers)
                assert r.json() == []  # the slip expired

                # the refusal walked out with the rest
                assert len(cap2.sent) == 1, cap2.sent
                mail = cap2.sent[0]
                assert "- refused" in mail["subject"], mail["subject"]
                assert "fail-closed" in mail["body"]

                # the receipt board stops saying waiting_approval
                r = await client.get(f"/harness/patrols/{patrol['id']}", headers=headers)
                row = r.json()
                assert row["last_status"] == "refused", row
                assert row["last_dispatch_status"] == "ok"

                # the instance never moved (fail-closed held)
                r = await client.get(
                    f"/processes/{machine['process']['id']}/instances/{iid}",
                    headers=headers)
                assert r.json()["state"] == "a", r.text
            finally:
                settings.smtp_host = old_host
                settings.harness_approval_ttl_seconds = old_ttl

    _sync(_go())


# ---------------------------------------------------------------------------
# 6. the doors: recipient validation + the dispatch (handshake) door
# ---------------------------------------------------------------------------

def test_v112_validation_and_the_dispatch_door():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "door")
            headers = _auth(user["token"])
            sess = await _mk_session(client, headers)

            # a malformed recipient list bounces at the Pydantic door
            r = await client.post("/harness/patrols", headers=headers, json={
                "session_id": sess["id"], "name": "X", "mission": "m",
                "dispatch_to": "not-an-email"})
            assert r.status_code == 422, r.text
            r = await client.post("/harness/patrols", headers=headers, json={
                "session_id": sess["id"], "name": "X", "mission": "m",
                "dispatch_to": "a@b.c, bad one"})
            assert r.status_code == 422, r.text

            # a valid list normalizes (dedupe, trim)
            patrol = await _mk_patrol(client, headers, sess["id"],
                                      dispatch_to=" a@b.c , a@b.c, d@e.f ")
            assert patrol["dispatch_to"] == "a@b.c, d@e.f", patrol

            # PATCH can wire and quiet the walk
            r = await client.patch(f"/harness/patrols/{patrol['id']}",
                                   headers=headers, json={"dispatch_to": "new@x.y"})
            assert r.json()["dispatch_to"] == "new@x.y"
            r = await client.patch(f"/harness/patrols/{patrol['id']}",
                                   headers=headers, json={"dispatch_to": ""})
            assert r.json()["dispatch_to"] == ""

            # the handshake door without recipients is a loud 400
            r = await client.post(f"/harness/patrols/{patrol['id']}/dispatch",
                                  headers=headers)
            assert r.status_code == 400, r.text

            # wiring recipients and calling before any round: the handshake
            r = await client.patch(f"/harness/patrols/{patrol['id']}",
                                   headers=headers, json={"dispatch_to": "new@x.y"})
            assert r.status_code == 200, r.text

            old_host = settings.smtp_host
            settings.smtp_host = "smtp.py8n.test"
            try:
                with _mail_cap() as cap:
                    r = await client.post(f"/harness/patrols/{patrol['id']}/dispatch",
                                          headers=headers)
                assert r.status_code == 200, r.text
                assert r.json()["last_dispatch_status"] == "ok", r.text
                assert len(cap.sent) == 1
                assert "handshake" in cap.sent[0]["subject"]
                assert "Mission:" in cap.sent[0]["body"]

                # after a real round, the door re-sends THAT round's digest
                with _ScriptedLLM(['{"answer": "the desk is quiet"}']):
                    r = await client.post(f"/harness/patrols/{patrol['id']}/run",
                                          headers=headers)
                assert r.json()["status"] == "completed"
                with _mail_cap() as cap2:
                    r = await client.post(f"/harness/patrols/{patrol['id']}/dispatch",
                                          headers=headers)
                assert r.status_code == 200, r.text
                assert len(cap2.sent) == 1
                assert "- completed" in cap2.sent[0]["subject"]
                assert "the desk is quiet" in cap2.sent[0]["body"]

                # ownership: a stranger's dispatch door looks nonexistent
                stranger = await _mk_user(client, "stranger")
                r = await client.post(f"/harness/patrols/{patrol['id']}/dispatch",
                                      headers=_auth(stranger["token"]))
                assert r.status_code == 404, r.text
            finally:
                settings.smtp_host = old_host

    _sync(_go())


# ---------------------------------------------------------------------------
# 7. the version pin
# ---------------------------------------------------------------------------

def test_v112_pin():
    assert settings.version == "1.121.0"
