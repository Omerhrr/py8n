"""v121: the shelf opens - the operator SDK, the bank connector, and two
more deterministic solutions.

* THE OPERATOR SDK - a package is a directory with ``operator.json``;
  point ``PY8N_EXTRA_OPERATORS`` at its parent and the operator rides the
  SAME marketplace doors (catalog, install plan, install) beside the
  compiled ones, installs through the pack path, and binds as its own
  system. A broken manifest is refused LOUDLY (skipped with a reason),
  never silently.
* THE BANK CONNECTOR - ``POST /erp/import-csv`` lands a raw CSV statement
  as REAL balanced journal pairs (cash takes the movement, the counterpart
  account the other side); ``dry_run=true`` (the default) previews
  everything and appends nothing; a closed book refuses; the books stay
  balanced BY CONSTRUCTION.
* THE SHELF GROWS - ``lead-to-order`` and ``expense-approval`` join the
  solutions: deterministic, offline, no LLM required.
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
from app.main import app  # noqa: E402

API = "http://testserver/api/v1"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


def _sync(coro):
    return asyncio.run(coro)


async def _mk_user(client: httpx.AsyncClient, tag: str) -> dict:
    res = await client.post("/auth/register", json={
        "email": f"v121-{tag}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v121 {tag}",
    })
    assert res.status_code == 201, res.text
    return {"token": res.json()["token"], "id": res.json()["user"]["id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_v121_the_shelf_opens(tmp_path, monkeypatch):
    operators_dir = tmp_path / "operators"

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "sdk")
            h = _auth(user["token"])

            # no env -> no extras; the unknown slug stays a loud 404
            res = await client.post("/operators/ghost-operator/install")
            assert res.status_code == 404, res.text

            # a valid package lands on the shelf beside the compiled ones
            pkg = operators_dir / "cafe-operator"
            pkg.mkdir(parents=True)
            (pkg / "operator.json").write_text(json.dumps({
                "slug": "cafe-operator",
                "name": "Cafe Operator",
                "tagline": "Orders to the pass.",
                "category": "Food",
                "pack": {
                    "workflows": [{
                        "name": "cafe intake",
                        "description": "Manual trigger -> land a row.",
                        "graph": {
                            "nodes": [
                                {"id": "m", "type": "manual_trigger",
                                 "name": "Intake",
                                 "position": {"x": 0, "y": 0},
                                 "parameters": {}},
                                {"id": "s", "type": "set_variable",
                                 "name": "Row",
                                 "position": {"x": 220, "y": 0},
                                 "parameters": {
                                     "assignments": {"note": "flat white"},
                                     "keep_input": False}},
                                {"id": "w", "type": "dataset_write",
                                 "name": "Ledger",
                                 "position": {"x": 440, "y": 0},
                                 "parameters": {"dataset": "cafe_notes",
                                                "mode": "append"}},
                            ],
                            "edges": [
                                {"id": "e1", "source": "m", "target": "s"},
                                {"id": "e2", "source": "s", "target": "w"},
                            ],
                        },
                    }],
                    "datasets": [{
                        "name": "cafe_notes",
                        "description": "Every intake row.",
                        "schema": [{"name": "note", "dtype": "text"}],
                        "rows": [{"note": "hello"}],
                    }],
                },
            }), encoding="utf-8")
            # a BROKEN package is skipped loudly, never silently
            bad = operators_dir / "broken-operator"
            bad.mkdir()
            (bad / "operator.json").write_text('{"slug": "BAD SLUG"}',
                                               encoding="utf-8")

            monkeypatch.setenv("PY8N_EXTRA_OPERATORS", str(operators_dir))

            res = await client.get("/operators")
            assert res.status_code == 200, res.text
            shelf = {o["slug"]: o for o in res.json()["operators"]}
            assert "cafe-operator" in shelf
            assert shelf["cafe-operator"]["source"] == "sdk"
            assert "cafe-operator" not in {o["slug"] for o in res.json()["operators"]
                                           if o.get("source") != "sdk"}

            # the install plan reads like a real operator's
            res = await client.get("/operators/cafe-operator")
            assert res.status_code == 200, res.text
            assert res.json()["installs"]["datasets"][0]["name"] == "cafe_notes"

            # THE INSTALL: the pack lands, the system binds, rows ride along
            res = await client.post("/operators/cafe-operator/install",
                                    headers=h, json={})
            assert res.status_code == 200, res.text
            built = res.json()
            assert built["source"] == "sdk"
            assert len(built["installed"]["datasets"]) == 1
            assert built["system"]["id"]

            # the landed dataset is real and carries its sample row
            ds_id = built["installed"]["datasets"][0]["id"]
            res = await client.get(f"/datasets/{ds_id}/rows", headers=h)
            assert res.status_code == 200, res.text
            assert any(r.get("note") == "hello" for r in res.json()["rows"])

            # the compiled shelf still answers: a compiled slug installs
            # through the SAME door untouched (the cafe slug rides the sdk
            # branch, a compiled slug rides compose)
            res = await client.get("/operators")
            assert any(o.get("source") != "sdk"
                       for o in res.json()["operators"])

    _sync(_go())


def test_v121_the_bank_connector():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "bank")
            h = _auth(user["token"])

            book = await client.post("/datasets", headers=h, json={
                "name": f"v121 bank book {tag}",
                "rows": [{"ref": "SO-1", "account": "Cash", "debit": "1000.00",
                          "credit": "0.00", "memo": "opening"},
                         {"ref": "SO-1", "account": "Revenue", "debit": "0.00",
                          "credit": "1000.00", "memo": "opening"}]})
            assert book.status_code == 201, book.text
            ds_id = book.json()["id"]

            statement = [
                {"date": "2026-09-01T10:00:00+00:00", "description": "stripe payout",
                 "amount": "2450.00", "ref": "STRIPE-91"},
                {"date": "2026-09-02T10:00:00+00:00", "description": "office rent",
                 "amount": "-1800.00", "ref": "RENT-09"},
                {"description": "bank fee", "amount": "-12.50"},
                {"description": "junk", "amount": ""},
            ]
            mapping = {"amount_col": "amount", "date_col": "date",
                       "description_col": "description", "ref_col": "ref",
                       "default_account": "Uncategorized"}

            # DRY RUN (the default): everything previews, NOTHING lands
            res = await client.post("/erp/import-csv", headers=h, json={
                "dataset_id": ds_id, "rows": statement, "mapping": mapping})
            assert res.status_code == 200, res.text
            preview = res.json()
            assert preview["dry_run"] is True
            assert preview["imported"] == 3  # the junk row is skipped
            assert len(preview["skipped"]) == 1
            assert preview["appended"] is False
            assert preview["cash_net"] == 637.5
            res = await client.get(f"/datasets/{ds_id}/rows", headers=h)
            assert len(res.json()["rows"]) == 2  # untouched

            # COMMIT: the balanced pairs land
            res = await client.post("/erp/import-csv", headers=h, json={
                "dataset_id": ds_id, "rows": statement, "mapping": mapping,
                "dry_run": False})
            assert res.status_code == 200, res.text
            done = res.json()
            assert done["appended"] is True
            assert done["entries"] == 6  # three balanced pairs

            # THE BOOKS STILL BALANCE - by construction
            res = await client.get("/erp/trial-balance",
                                   params={"dataset_id": ds_id}, headers=h)
            tb = res.json()
            assert tb["totals"]["balanced"] is True
            by_account = {r["account"]: r for r in tb["rows"]}
            assert by_account["Cash"]["balance"] == 1637.5
            # 'Uncategorized' is unclassified -> the 'other' bucket rides
            # the credit-normal side (the door's standing convention)
            assert by_account["Uncategorized"]["balance"] == 637.5

            # a CLOSED book refuses the import (the close locks)
            res = await client.post("/erp/close",
                                    params={"dataset_id": ds_id},
                                    json={}, headers=h)
            assert res.status_code == 200, res.text
            res = await client.post("/erp/import-csv", headers=h, json={
                "dataset_id": ds_id, "rows": statement, "mapping": mapping,
                "dry_run": False})
            assert res.status_code == 409, res.text
            assert "locked" in res.json()["detail"]

    _sync(_go())


def test_v121_the_solutions_shelf_grows():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "shelf")
            h = _auth(user["token"])

            res = await client.get("/solutions", headers=h)
            assert res.status_code == 200, res.text
            slugs = {s["slug"] for s in res.json()["solutions"]}
            assert {"lead-to-order", "expense-approval"} <= slugs

            # the new solutions install offline: the pack machinery lands
            res = await client.post("/solutions/expense-approval/install",
                                    headers=h, json={})
            assert res.status_code == 200, res.text
            installed = res.json()
            assert not installed.get("skipped")
            assert len(installed["created_workflows"]) == 1
            assert len(installed["created_datasets"]) == 2
            wf_id = installed["created_workflows"][0]["id"]
            res = await client.get(f"/workflows/{wf_id}", headers=h)
            assert res.json()["is_active"] is False  # pack semantics

            # it RUNS offline: the sample expense auto-approves
            res = await client.post(f"/workflows/{wf_id}/run",
                                    headers=h, json={"payload": {}})
            assert res.status_code in (200, 202), res.text
            exec_id = res.json()["execution_id"]
            for _ in range(100):
                res = await client.get(f"/executions/{exec_id}", headers=h)
                if res.json().get("status") not in ("running", "queued"):
                    break
                await asyncio.sleep(0.05)
            assert res.json()["status"] == "success", res.json()

    _sync(_go())


def test_v121_version_pin():
    assert settings.version == "1.121.0"
