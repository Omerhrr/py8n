"""v117: the statements speak - the income statement + the balance sheet.

The trial balance door already proves the books balance per account;
v117 shapes the SAME book into the two documents a company closes with:

* the INCOME STATEMENT - the revenue lines, the expense lines, the net;
* the BALANCE SHEET - the asset, liability and equity lines, retained
  earnings implied by the period's net (no close entries exist yet), and
  the ACCOUNTING EQUATION check: assets == liabilities + equity +
  retained - the chip that proves the books tie out end to end.

Both reports read the dataset through the SAME one-pass reader, so they
can never disagree. The kinds map gains the classic equity accounts
(owner's equity, retained earnings, common stock, paid-in capital) so a
hand-kept capital book ties out instead of crying wolf - the posters
never post them, but the door reads them honestly when they appear.

The test seeds two small books by hand: the operating book (a sale, a
payroll run, a vendor bill - the equation ties through retained
earnings) and the capital book (a cash infusion against owner's equity
- the equation ties through the equity line), then reads the door and
checks every number to the cent.
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
        "email": f"v117-{tag}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v117 {tag}",
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


# the operating book - a sale, a payroll run, a vendor bill:
# 1353.0 on both sides, the equation ties through retained earnings
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

# the capital book - the owner puts cash in, the equity line takes it:
# 5000.0 on both sides, the equation ties through the equity account
_CAPITAL_ROWS = [
    {"ref": "CAP-1", "account": "Cash", "debit": "5000.00",
     "credit": "0.00", "memo": "owner puts cash in"},
    {"ref": "CAP-1", "account": "Owner's equity", "debit": "0.00",
     "credit": "5000.00", "memo": "owner puts cash in"},
]


def test_v117_the_statements_speak():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "statements")
            h = _auth(user["token"])

            # the unknown dataset is a loud 404 - same as the trial balance
            res = await client.get("/erp/statements",
                                   params={"dataset_id": "no-such-dataset"},
                                   headers=h)
            assert res.status_code == 404, res.text

            # ---- the operating book ------------------------------------
            ds = await _mk_dataset(client, h, "Statements books",
                                   _OPERATING_ROWS)
            res = await client.get("/erp/statements",
                                   params={"dataset_id": ds["id"]}, headers=h)
            assert res.status_code == 200, res.text
            st = res.json()
            assert st["dataset"]["name"] == "Statements books"
            assert st["line_count"] == 6

            # the income statement: the revenue line, the expense line,
            # the net to the cent
            assert st["income"]["revenue"] == [{"account": "Revenue",
                                                "balance": 1000.0}]
            assert st["income"]["expenses"] == [{"account": "Salary expense",
                                                 "balance": 200.0}]
            assert st["income"]["total_revenue"] == 1000.0
            assert st["income"]["total_expenses"] == 200.0
            assert st["income"]["net"] == 800.0

            # the balance sheet: assets carry the owned side, the payable
            # the owed side, and the equation ties through the retained net
            assert st["balance"]["assets"] == [
                {"account": "Cash", "balance": 800.0},
                {"account": "Inventory", "balance": 153.0}]
            assert st["balance"]["liabilities"] == [
                {"account": "Accounts payable", "balance": 153.0}]
            assert st["balance"]["equity"] == []
            assert st["balance"]["memos"] == []
            assert st["balance"]["total_assets"] == 953.0
            assert st["balance"]["total_liabilities"] == 153.0
            assert st["balance"]["total_equity"] == 0.0
            assert st["balance"]["retained_earnings"] == 800.0
            assert st["balance"]["equation_side"] == 953.0
            assert st["balance"]["balanced"] is True

            # ---- the capital book --------------------------------------
            cap = await _mk_dataset(client, h, "Statements capital",
                                    _CAPITAL_ROWS)
            res = await client.get("/erp/statements",
                                   params={"dataset_id": cap["id"]}, headers=h)
            assert res.status_code == 200, res.text
            st = res.json()
            # the equity line takes the infusion (credit-normal), the net
            # is zero, and the equation ties through the equity account
            assert st["income"] == {"revenue": [], "expenses": [],
                                    "total_revenue": 0.0,
                                    "total_expenses": 0.0, "net": 0.0}
            assert st["balance"]["assets"] == [{"account": "Cash",
                                                "balance": 5000.0}]
            assert st["balance"]["equity"] == [{"account": "Owner's equity",
                                                "balance": 5000.0}]
            assert st["balance"]["total_assets"] == 5000.0
            assert st["balance"]["total_equity"] == 5000.0
            assert st["balance"]["retained_earnings"] == 0.0
            assert st["balance"]["equation_side"] == 5000.0
            assert st["balance"]["balanced"] is True

            # ---- an unclassified account cries honest wolf --------------
            odd = await _mk_dataset(client, h, "Statements odd", [
                {"ref": "X-1", "account": "Goodwill", "debit": "10.00",
                 "credit": "0.00", "memo": "unclassified"},
                {"ref": "X-1", "account": "Revenue", "debit": "0.00",
                 "credit": "10.00", "memo": "unclassified"},
            ])
            res = await client.get("/erp/statements",
                                   params={"dataset_id": odd["id"]}, headers=h)
            assert res.status_code == 200, res.text
            st = res.json()
            # Goodwill is not in the kinds map - the door will not pretend:
            # the money sits unplaced in the memos bucket (credit-normal)
            # and the equation DOES NOT tie out
            assert st["balance"]["memos"] == [{"account": "Goodwill",
                                               "balance": -10.0}]
            assert st["balance"]["total_assets"] == 0.0
            assert st["balance"]["equation_side"] == 10.0
            assert st["balance"]["balanced"] is False

    _sync(_go())


def test_v117_version_pin():
    assert settings.version == "1.121.0"
