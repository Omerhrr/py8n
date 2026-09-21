"""v116: the books get a face - the trial balance summary buckets.

The trial balance door already spoke per-account (kind, debits, credits,
balance); v116 adds the per-kind SUMMARY the console's chart and badges
draw from: seven fixed buckets (asset / liability / equity / revenue /
expense / memo / other), each with its account count and net total,
always present in reading order - the balance sheet against the income
statement, without the client re-deriving the kinds.

The test seeds a small BALANCED book by hand (the posters' work is
v114/v115's proof; this round is about the report's shape): a sale,
a payroll run and a vendor bill, then reads the door and checks the
buckets to the cent.
"""

from __future__ import annotations

import asyncio
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


async def _mk_user(client: httpx.AsyncClient, tag: str) -> dict:
    res = await client.post("/auth/register", json={
        "email": f"v116-{tag}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v116 {tag}",
    })
    assert res.status_code == 201, res.text
    return {"token": res.json()["token"], "id": res.json()["user"]["id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# the seed book - every pair the posters would write, by hand:
# a sale (cash in, revenue earned), a payroll run (salary out, cash out)
# and a vendor bill (inventory in, payable owed). debits == credits.
_SEED_ROWS = [
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


def test_v116_the_trial_balance_summary_buckets():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "summary")
            h = _auth(user["token"])

            # the books, seeded by hand over the datasets door
            res = await client.post("/datasets", json={
                "name": "TB summary books",
                "description": "v116 - a balanced book to read the buckets off",
                "rows": _SEED_ROWS,
            }, headers=h)
            assert res.status_code == 201, res.text
            ds = res.json()

            # the door reads by id (and the case-insensitive name rides free)
            res = await client.get("/erp/trial-balance",
                                   params={"dataset_id": ds["id"]}, headers=h)
            assert res.status_code == 200, res.text
            tb = res.json()
            assert tb["totals"]["balanced"] is True
            assert tb["totals"]["debits"] == 1353.0
            assert tb["totals"]["credits"] == 1353.0
            assert tb["income"] == {"revenue": 1000.0, "expenses": 200.0,
                                    "net": 800.0}

            # the summary: seven fixed buckets, always, in reading order
            assert [b["kind"] for b in tb["summary"]] == [
                "asset", "liability", "equity", "revenue", "expense",
                "memo", "other"]

            buckets = {b["kind"]: b for b in tb["summary"]}
            # the balance sheet: cash 800 + inventory 153 on the owned side,
            # the 153 payable on the owed side, equity untouched
            assert buckets["asset"] == {"kind": "asset", "accounts": 2,
                                        "total": 953.0}
            assert buckets["liability"] == {"kind": "liability",
                                            "accounts": 1, "total": 153.0}
            assert buckets["equity"] == {"kind": "equity", "accounts": 0,
                                         "total": 0.0}
            # the income statement: revenue earned vs salary spent
            assert buckets["revenue"] == {"kind": "revenue", "accounts": 1,
                                          "total": 1000.0}
            assert buckets["expense"] == {"kind": "expense", "accounts": 1,
                                          "total": 200.0}
            # no memos, nothing unclassified
            assert buckets["memo"] == {"kind": "memo", "accounts": 0,
                                       "total": 0.0}
            assert buckets["other"] == {"kind": "other", "accounts": 0,
                                        "total": 0.0}

            # the per-account rows still speak exactly as before (v114
            # contract untouched - the summary rides BESIDE them)
            accounts = {r["account"]: r for r in tb["rows"]}
            assert accounts["Cash"] == {"account": "Cash", "kind": "asset",
                                        "debits": 1000.0, "credits": 200.0,
                                        "balance": 800.0}
            assert accounts["Accounts payable"] == {
                "account": "Accounts payable", "kind": "liability",
                "debits": 0.0, "credits": 153.0, "balance": 153.0}
            assert tb["line_count"] == 6

            # a book with an unclassified account still lands - the unknown
            # kind degrades into the 'other' bucket, nothing is lost
            res = await client.post("/datasets", json={
                "name": "TB summary odd",
                "rows": [
                    {"ref": "X-1", "account": "Goodwill", "debit": "10.00",
                     "credit": "0.00", "memo": "odd"},
                    {"ref": "X-1", "account": "Revenue", "debit": "0.00",
                     "credit": "10.00", "memo": "odd"},
                ],
            }, headers=h)
            assert res.status_code == 201, res.text
            res = await client.get("/erp/trial-balance",
                                   params={"dataset_id": "tb summary odd"},
                                   headers=h)
            assert res.status_code == 200, res.text
            tb = res.json()
            assert tb["totals"]["balanced"] is True
            buckets = {b["kind"]: b for b in tb["summary"]}
            # unknown accounts ride the credit-normal side (the door's
            # standing convention), so Goodwill reads -10.0
            assert buckets["other"] == {"kind": "other", "accounts": 1,
                                        "total": -10.0}
            assert buckets["revenue"] == {"kind": "revenue", "accounts": 1,
                                          "total": 10.0}

    _sync(_go())


def test_v116_version_pin():
    assert settings.version == "1.121.0"
