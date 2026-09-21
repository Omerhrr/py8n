"""v119: the agent reads the books - the ERP joins the harness toolchest.

The harness had fourteen tools for the ESTATE (machines, attention,
chains, escalations, the builder); the BOOKS lived behind the /erp doors
alone. v119 hands the agent the same ledger:

* erp_books        - list the datasets, flag the ones that look like books;
* erp_statements   - trial balance + income statement + balance sheet;
* erp_gl           - the journal lines, newest first, filtered;
* erp_aging        - the AR/AP buckets;
* erp_cash_flow    - the direct-method movement of the money;
* erp_health       - THE BOOKS PATROL CHECK: imbalance, unclassified
                     money, overdrawn cash, receivables past 90 - the
                     findings a patrol's round mails home;
* erp_close (SENSITIVE) - the period close behind the fail-closed gate:
                     the slip waits, the human decides, the closing
                     entries land AT DECISION TIME through the SAME
                     erp_books service the doors serve.

Every tool reads the one service (zero parallel book paths), so the
agent's numbers and the console's numbers can never disagree.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402

API = "http://testserver/api/v1"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


def _sync(coro):
    return asyncio.run(coro)


class _ScriptedLLM:
    """Replaces AgentNode._chat with a scripted reply sequence (the harness
    shim rides the same transport, so the same monkeypatch drives it)."""

    def __init__(self, replies):
        self._replies = list(replies)
        self._original = None

    def __enter__(self):
        from app.engine.nodes.agent import AgentNode
        holder = self
        self._original = AgentNode._chat

        async def _fake_chat(agent_self, messages, temperature):
            reply = holder._replies.pop(0) if holder._replies \
                else '{"answer": "done"}'
            if callable(reply):
                return reply(messages)
            return reply

        AgentNode._chat = _fake_chat
        return self

    def __exit__(self, *exc):
        from app.engine.nodes.agent import AgentNode
        AgentNode._chat = self._original


async def _mk_user(client: httpx.AsyncClient, tag: str) -> dict:
    res = await client.post("/auth/register", json={
        "email": f"v119-{tag}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v119 {tag}",
    })
    assert res.status_code == 201, res.text
    return {"token": res.json()["token"], "id": res.json()["user"]["id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _mk_dataset(client: httpx.AsyncClient, h: dict,
                      name: str, rows: list[dict]) -> dict:
    res = await client.post("/datasets", json={"name": name, "rows": rows},
                            headers=h)
    assert res.status_code == 201, res.text
    return res.json()


async def _mk_session(client: httpx.AsyncClient, headers: dict) -> dict:
    res = await client.post("/harness/sessions", headers=headers,
                            json={"name": "The books desk", "memory": "none"})
    assert res.status_code == 201, res.text
    return res.json()


async def _turn(client: httpx.AsyncClient, headers: dict, sess: dict,
                message: str) -> dict:
    res = await client.post(f"/harness/sessions/{sess['id']}/turns",
                            headers=headers, json={"message": message})
    assert res.status_code in (200, 201), res.text
    return res.json()


def _result(call: dict) -> dict:
    """The tool's result rides the wire as truncated text - parse it."""
    raw = call.get("result")
    if isinstance(raw, dict):
        return raw
    return json.loads(raw)


# the operating book: sale 1000, payroll 200, vendor bill 153 - net 800
_OPERATING_ROWS = [
    {"ref": "SO-2001", "account": "Cash", "debit": "1000.00",
     "credit": "0.00", "memo": "sale settled"},
    {"ref": "SO-2001", "account": "Revenue", "debit": "0.00",
     "credit": "1000.00", "memo": "sale settled"},
    {"ref": "PR-2026-09", "account": "Salary expense", "debit": "200.00",
     "credit": "0.00", "memo": "payroll run"},
    {"ref": "PR-2026-09", "account": "Cash", "debit": "0.00",
     "credit": "200.00", "memo": "payroll run"},
    {"ref": "PO-3001", "account": "Inventory", "debit": "153.00",
     "credit": "0.00", "memo": "vendor bill matched"},
    {"ref": "PO-3001", "account": "Accounts payable", "debit": "0.00",
     "credit": "153.00", "memo": "vendor bill matched"},
]


def test_v119_the_toolchest_reads_the_books():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "reads")
            h = _auth(user["token"])

            # the chest: 21 tools, and the books' names ride the catalog
            r = await client.get("/harness/tools", headers=h)
            assert r.status_code == 200, r.text
            names = {t["name"] for t in r.json()}
            assert {"erp_books", "erp_statements", "erp_gl", "erp_aging",
                    "erp_cash_flow", "erp_health", "erp_close"} <= names

            ds = await _mk_dataset(client, h, "v119 books", _OPERATING_ROWS)
            sess = await _mk_session(client, h)

            # ---- erp_books lists the book, flagged as one ---------------
            with _ScriptedLLM([
                '{"tool": "erp_books", "arguments": {}}',
                '{"answer": "the books are listed"}',
            ]):
                turn = await _turn(client, h, sess, "which books do we keep?")
            assert turn["status"] == "completed", turn
            call = turn["tool_calls"][0]
            assert call["tool"] == "erp_books" and call["status"] == "ok"
            text = json.dumps(call.get("result") or "")
            assert "v119 books" in text

            # ---- erp_statements reads the numbers to the cent -----------
            with _ScriptedLLM([
                '{"tool": "erp_statements", "arguments": '
                '{"dataset": "v119 books"}}',
                '{"answer": "net income 800, the books balance"}',
            ]):
                turn = await _turn(client, h, sess, "how are the numbers?")
            assert turn["status"] == "completed", turn
            call = turn["tool_calls"][0]
            assert call["tool"] == "erp_statements" and call["status"] == "ok"
            result = _result(call)
            assert result["income_statement"]["net"] == 800.0
            assert result["trial_balance"]["balanced"] is True
            assert result["balance_sheet"]["balanced"] is True

            # ---- erp_gl drills to one account ---------------------------
            with _ScriptedLLM([
                '{"tool": "erp_gl", "arguments": '
                '{"dataset": "v119 books", "account": "Cash"}}',
                '{"answer": "cash moved twice: +1000 and -200"}',
            ]):
                turn = await _turn(client, h, sess, "show me the cash lines")
            call = turn["tool_calls"][0]
            assert call["tool"] == "erp_gl" and call["status"] == "ok"
            result = _result(call)
            assert result["returned_lines"] == 2
            assert {line["ref"] for line in result["lines"]} == {"SO-2001",
                                                                 "PR-2026-09"}

            # ---- erp_cash_flow sections the money -----------------------
            with _ScriptedLLM([
                '{"tool": "erp_cash_flow", "arguments": '
                '{"dataset": "v119 books"}}',
                '{"answer": "operating +800, the rest quiet"}',
            ]):
                turn = await _turn(client, h, sess, "where did the money go?")
            call = turn["tool_calls"][0]
            assert call["tool"] == "erp_cash_flow" and call["status"] == "ok"
            result = _result(call)
            sections = {s["name"]: s for s in result["sections"]}
            assert sections["operating"]["net"] == 800.0
            assert result["net_cash_movement"] == 800.0

            # ---- erp_health on an UNBALANCED book cries honest wolf -----
            await _mk_dataset(client, h, "v119 odd books", [
                {"ref": "X-1", "account": "Cash", "debit": "10.00",
                 "credit": "0.00", "memo": "stray"},
                {"ref": "X-1", "account": "Revenue", "debit": "0.00",
                 "credit": "10.00", "memo": "sale"},
                {"ref": "X-2", "account": "Goodwill", "debit": "5.00",
                 "credit": "0.00", "memo": "unclassified stray"},
            ])
            with _ScriptedLLM([
                '{"tool": "erp_health", "arguments": '
                '{"dataset": "v119 odd books"}}',
                '{"answer": "the books do not balance and Goodwill is '
                'unclassified - raising it"}',
            ]):
                turn = await _turn(client, h, sess, "patrol the odd book")
            call = turn["tool_calls"][0]
            assert call["tool"] == "erp_health" and call["status"] == "ok"
            result = _result(call)
            assert result["healthy"] is False
            findings_text = json.dumps(result["findings"])
            assert "DO NOT balance" in findings_text
            assert "unclassified" in findings_text

            # a foreign owner's book stays invisible - a loud tool refusal
            stranger = await _mk_user(client, "stranger")
            sh = _auth(stranger["token"])
            ssess = await _mk_session(client, sh)
            with _ScriptedLLM([
                '{"tool": "erp_statements", "arguments": '
                '{"dataset": "v119 books"}}',
            ]):
                turn = await _turn(client, sh, ssess, "read their books")
            assert turn["status"] == "completed", turn
            call = turn["tool_calls"][0]
            assert call["status"] == "error"
            assert "not found" in json.dumps(call.get("result") or "")

    _sync(_go())


def test_v119_the_close_gate_holds_the_books():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "gate")
            h = _auth(user["token"])
            ds = await _mk_dataset(client, h, "v119 close books",
                                   _OPERATING_ROWS)
            sess = await _mk_session(client, h)

            # a malformed close (no dataset) is PREFLIGHT feedback - the
            # model self-corrects, no human is ever paused for syntax
            with _ScriptedLLM([
                '{"tool": "erp_close", "arguments": {}}',
                '{"tool": "erp_close", "arguments": '
                '{"dataset": "v119 close books", "period": "2026-08"}}',
            ]):
                turn = await _turn(client, h, sess, "close the month")
            assert turn["status"] == "waiting_approval", turn
            calls = turn["tool_calls"]
            # the preflight bounce rides back as tool feedback (the model
            # self-corrected and named the book on its second call)
            assert calls[0]["tool"] == "erp_close"
            assert calls[0]["status"] == "error"
            assert "which book" in calls[0]["result"]

            # NOTHING closed while unwatched
            r = await client.get("/erp/close", params={"dataset_id": ds["id"]},
                                 headers=h)
            assert r.json()["closed"] is False

            # the slip waits on the board
            r = await client.get("/harness/approvals", headers=h)
            slips = [s for s in r.json() if s["status"] == "pending"
                     and s.get("tool") == "erp_close"]
            assert len(slips) == 1, r.text
            assert slips[0]["arguments"] == {"dataset": "v119 close books",
                                             "period": "2026-08"}

            # THE DECISION: the close runs at decision time, through the
            # REAL service - the entries land, the book locks
            with _ScriptedLLM([
                '{"answer": "The period is closed: net 800 into retained '
                'earnings, the book is locked."}',
            ]):
                r = await client.post(
                    f"/harness/approvals/{slips[0]['id']}/approve", headers=h)
            assert r.status_code == 200, r.text
            done = r.json()
            assert done["status"] == "completed", done
            close_call = done["tool_calls"][-1]
            assert close_call["tool"] == "erp_close"
            assert close_call["status"] == "ok"
            closed = _result(close_call)
            assert closed["net"] == 800.0
            assert closed["locked"] is True

            # THE BOOKS MOVED - exactly once
            r = await client.get("/erp/close", params={"dataset_id": ds["id"]},
                                 headers=h)
            hist = r.json()
            assert hist["closed"] is True
            assert len(hist["closes"]) == 1
            assert hist["closes"][0]["net"] == 800.0

            # deciding twice is a loud 409
            r = await client.post(
                f"/harness/approvals/{slips[0]['id']}/approve", headers=h)
            assert r.status_code == 409, r.text

    _sync(_go())


def test_v119_version_pin():
    assert settings.version == "1.120.0"
