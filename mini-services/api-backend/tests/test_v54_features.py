"""V54 feature tests: contract version-diff, data-ownership governance on
the catalog, and PNG report per-component drilldowns.

CONTRACT REVISIONS: every contract replace (and delete) snapshots the
outgoing state into ``dataset_contract_revisions`` (cap 20/dataset, so a
hot dataset cannot grow history unbounded). GET .../contract/revisions
lists the newest-first history (current version included, full column
snapshots) and GET .../contract/diff?from=&to= returns a human-readable
change report (added / removed / changed with per-field old->new, allowed
domains compared as sets). Defaults to the two most recent versions.

OWNERSHIP GOVERNANCE: POST /datasets/{id}/claim claims an unclaimed
dataset (or transfers/releases when the owner calls it with a body),
POST/DELETE /datasets/{id}/certify manage the steward certification stamp
(owner only). Catalog entries carry certified/claimable/owner_id; the
dataset API carries owner_id + certified_at.

PNG DRILLDOWNS: GET /dashboards/{ref}/snapshot?fmt=json stamps every
rendered component with ``dataset`` + ``ref`` (``/d/{slug}?c={id}``);
``fmt=png`` prints the same as caption strips under each component;
``&component={id}`` renders ONE component standalone (the drilldown
target). Scheduled-report PNGs gain the same captions through the shared
render path.

Runs the FastAPI app in-process via httpx ASGITransport (same harness as v4-v53).
"""

from __future__ import annotations

import asyncio
import uuid

import httpx

from app.main import app
from app.services import executor as executor_mod

API = "http://testserver/api/v1"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


async def _drain_background() -> None:
    tasks = [t for t in executor_mod._background_tasks if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _mk_user(client: httpx.AsyncClient, tag: str, n: int) -> dict:
    """Register a user BEFORE datasets exist (first user claims orphans)."""
    res = await client.post("/auth/register", json={
        "email": f"v54-{tag}-u{n}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v54 u{n} {tag}",
    })
    assert res.status_code == 201, res.text
    body = res.json()
    return {"token": body["token"], "id": body["user"]["id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _mk_dataset(client: httpx.AsyncClient, name: str, rows: list[dict]) -> dict:
    res = await client.post("/datasets", json={"name": name, "rows": rows})
    assert res.status_code == 201, res.text
    return res.json()


# ===========================================================================
# 1) contract revisions + diff
# ===========================================================================
def test_v54_contract_revisions_and_diff():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            ds = await _mk_dataset(client, f"v54-contract-{tag}", [{"a": 1, "b": "x"}])
            ds_id = ds["id"]

            # v1: two columns
            res = await client.put(f"/datasets/{ds_id}/contract", json={
                "on_violation": "warn",
                "columns": [
                    {"name": "a", "dtype": "integer", "nullable": False, "allowed": None},
                    {"name": "b", "dtype": "text", "nullable": True, "allowed": ["x", "y"]},
                ],
            })
            assert res.status_code == 200 and res.json()["version"] == 1, res.text

            # v2: a becomes number+nullable, b removed, c added, allowed widened on a
            res = await client.put(f"/datasets/{ds_id}/contract", json={
                "on_violation": "error",
                "columns": [
                    {"name": "a", "dtype": "number", "nullable": True, "allowed": None},
                    {"name": "c", "dtype": "boolean", "nullable": False, "allowed": None},
                ],
            })
            assert res.status_code == 200 and res.json()["version"] == 2, res.text

            # history: current v2 first, then the v1 snapshot
            res = await client.get(f"/datasets/{ds_id}/contract/revisions")
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["current_version"] == 2
            versions = [r["version"] for r in body["revisions"]]
            assert versions == [2, 1], versions
            v1 = next(r for r in body["revisions"] if r["version"] == 1)
            assert v1["note"] == "superseded by v2"
            assert v1["on_violation"] == "warn"
            assert [c["name"] for c in v1["columns"]] == ["a", "b"]

            # default diff = v1 -> v2
            res = await client.get(f"/datasets/{ds_id}/contract/diff")
            assert res.status_code == 200, res.text
            d = res.json()
            assert d["from"] == 1 and d["to"] == 2
            assert [c["name"] for c in d["added"]] == ["c"]
            assert [c["name"] for c in d["removed"]] == ["b"]
            fields = {(c["name"], c["field"]) for c in d["changed"]}
            assert ("a", "dtype") in fields and ("a", "nullable") in fields, d
            assert d["summary"] == "1 added, 1 removed, 2 changed"

            # explicit from/to + allowed set-compare (order-insensitive)
            res = await client.put(f"/datasets/{ds_id}/contract", json={
                "on_violation": "warn",
                "columns": [{"name": "a", "dtype": "number", "nullable": True, "allowed": ["x", "y"]}],
            })
            assert res.json()["version"] == 3
            res = await client.put(f"/datasets/{ds_id}/contract", json={
                "on_violation": "warn",
                "columns": [{"name": "a", "dtype": "number", "nullable": True, "allowed": ["y", "x", "x"]}],
            })
            assert res.json()["version"] == 4
            res = await client.get(f"/datasets/{ds_id}/contract/diff?from=3&to=4")
            d34 = res.json()
            assert d34["changed"] == [] and d34["added"] == [] and d34["removed"] == [], d34
            assert d34["summary"] == "no changes"  # ["y","x"] == ["x","y"] as a domain

            # history cap: two more replaces -> v6, 6 known versions
            for _ in range(2):
                await client.put(f"/datasets/{ds_id}/contract", json={
                    "on_violation": "warn",
                    "columns": [{"name": "a", "dtype": "text", "nullable": True, "allowed": None}],
                })
            res = await client.get(f"/datasets/{ds_id}/contract/revisions")
            assert res.json()["current_version"] == 6
            assert len(res.json()["revisions"]) == 6

            # unknown version 404s; deletion snapshots the final state
            res = await client.get(f"/datasets/{ds_id}/contract/diff?from=99&to=1")
            assert res.status_code == 404
            res = await client.delete(f"/datasets/{ds_id}/contract")
            assert res.status_code == 204
            res = await client.get(f"/datasets/{ds_id}/contract/revisions")
            body = res.json()
            assert body["current_version"] == 0
            versions = [r["version"] for r in body["revisions"]]
            assert 6 in versions, versions
            removed = next(r for r in body["revisions"] if r["version"] == 6)
            assert removed["note"] == "contract removed"
            # diff still resolves across the deleted history
            res = await client.get(f"/datasets/{ds_id}/contract/diff?from=1&to=6")
            assert res.status_code == 200

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


def test_v54_diff_contract_columns_unit():
    from app.services.contracts import diff_contract_columns

    old = [
        {"name": "a", "dtype": "integer", "nullable": False, "allowed": None},
        {"name": "b", "dtype": "text", "nullable": True, "allowed": ["x"]},
    ]
    new = [
        {"name": "a", "dtype": "integer", "nullable": False, "allowed": None},
        {"name": "c", "dtype": "text", "nullable": True, "allowed": None},
        {"name": "b", "dtype": "text", "nullable": False, "allowed": ["x", "y"]},
    ]
    d = diff_contract_columns(old, new)
    assert [c["name"] for c in d["added"]] == ["c"]
    assert d["removed"] == []
    assert {"name": "b", "field": "nullable", "old": True, "new": False} in d["changed"]
    assert {"name": "b", "field": "allowed", "old": ["x"], "new": ["x", "y"]} in d["changed"]
    assert d["same"] == ["a"]


# ===========================================================================
# 2) ownership governance: claim / transfer / release / certify
# ===========================================================================
def test_v54_ownership_governance():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            # users FIRST (the first user would claim orphans otherwise)
            ua = await _mk_user(client, tag, 1)
            ub = await _mk_user(client, tag, 2)

            ds = await _mk_dataset(client, f"v54-own-{tag}", [{"x": 1}])
            ds_id = ds["id"]
            assert ds.get("owner_id") is None  # created anonymously = unclaimed

            # catalog: unclaimed + uncertified + claimable
            cat = (await client.get("/catalog")).json()["entries"]
            entry = next(e for e in cat if e["id"] == ds_id)
            assert entry["claimable"] is True and entry["certified"] is False and entry["owner"] is None

            # anonymous claim: 401
            res = await client.post(f"/datasets/{ds_id}/claim")
            assert res.status_code == 401, res.text

            # user A claims it
            res = await client.post(f"/datasets/{ds_id}/claim", json={}, headers=_auth(ua["token"]))
            assert res.status_code == 200, res.text
            assert res.json()["owner_id"] == ua["id"]

            # catalog now shows the owner's name + not claimable
            cat = (await client.get("/catalog", headers=_auth(ua["token"]))).json()["entries"]
            entry = next(e for e in cat if e["id"] == ds_id)
            assert entry["claimable"] is False and entry["owner_id"] == ua["id"]

            # certify + un-certify (owner)
            res = await client.post(f"/datasets/{ds_id}/certify", headers=_auth(ua["token"]))
            assert res.status_code == 200 and res.json()["certified_at"] is not None
            cat = (await client.get("/catalog", headers=_auth(ua["token"]))).json()["entries"]
            assert next(e for e in cat if e["id"] == ds_id)["certified"] is True

            # transfer to user B, then B releases it back
            res = await client.post(f"/datasets/{ds_id}/claim", json={"owner_id": ub["id"]}, headers=_auth(ua["token"]))
            assert res.status_code == 200 and res.json()["owner_id"] == ub["id"]
            # A no longer owns it -> 404 (estate visibility)
            res = await client.post(f"/datasets/{ds_id}/certify", headers=_auth(ua["token"]))
            assert res.status_code == 404
            res = await client.post(f"/datasets/{ds_id}/claim", json={"owner_id": None}, headers=_auth(ub["token"]))
            assert res.status_code == 200 and res.json()["owner_id"] is None

            # transfer to a nonexistent user 400s
            res = await client.post(f"/datasets/{ds_id}/claim", json={}, headers=_auth(ub["token"]))
            assert res.status_code == 200
            res = await client.post(f"/datasets/{ds_id}/claim", json={"owner_id": "no-such-user"}, headers=_auth(ub["token"]))
            assert res.status_code == 400

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


# ===========================================================================
# 3) PNG per-component drilldowns
# ===========================================================================
def test_v54_snapshot_drilldowns():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            ds = await _mk_dataset(client, f"v54-snap-{tag}", [
                {"region": "eu", "v": 10}, {"region": "us", "v": 20}, {"region": "eu", "v": 5},
            ])
            res = await client.post("/dashboards", json={
                "name": f"v54 board {tag}",
                "config": {"components": [
                    {"id": "kpi1", "type": "stat", "dataset_id": ds["id"], "label": "Total", "agg": "sum", "column": "v"},
                    {"id": "chart1", "type": "chart", "dataset_id": ds["id"], "title": "By region", "chart_type": "bar", "group_by": "region", "agg": "sum", "column": "v"},
                ]},
            })
            assert res.status_code == 201, res.text
            board = res.json()

            # JSON snapshot: every component carries dataset + ref
            res = await client.get(f"/dashboards/{board['id']}/snapshot?fmt=json")
            assert res.status_code == 200, res.text
            snap = res.json()
            by_id = {c["id"]: c for c in snap["components"]}
            assert by_id["kpi1"]["dataset"] == ds["name"]
            assert by_id["kpi1"]["ref"] == f"/d/{board['slug']}?c=kpi1"
            assert by_id["chart1"]["ref"] == f"/d/{board['slug']}?c=chart1"

            # full PNG renders (with caption strips)
            res = await client.get(f"/dashboards/{board['id']}/snapshot?fmt=png")
            assert res.status_code == 200
            assert res.headers["content-type"] == "image/png"
            full_png = res.content
            assert full_png[:8] == b"\x89PNG\r\n\x1a\n"

            # single-component PNG: the drilldown target, standalone
            res = await client.get(f"/dashboards/{board['id']}/snapshot?fmt=png&component=chart1")
            assert res.status_code == 200 and res.headers["content-type"] == "image/png"
            comp_png = res.content
            assert comp_png[:8] == b"\x89PNG\r\n\x1a\n"
            assert comp_png != full_png  # a different (smaller) figure

            # single-component JSON works too; unknown component degrades to
            # the "not found" placeholder path (still a valid image)
            res = await client.get(f"/dashboards/{board['id']}/snapshot?fmt=json&component=chart1")
            assert res.status_code == 200 and [c["id"] for c in res.json()["components"]] == ["chart1"]
            res = await client.get(f"/dashboards/{board['id']}/snapshot?fmt=png&component=ghost")
            assert res.status_code == 200 and res.headers["content-type"] == "image/png"

            # the scheduled-report path shares the same renderer (captions ride along)
            from app.db import AsyncSessionLocal
            from app.services import reports as report_svc

            async with AsyncSessionLocal() as session:
                board_row = await session.get(__import__("app.models", fromlist=["Dashboard"]).Dashboard, board["id"])
                data, ct, ext, fname = await report_svc.dashboard_snapshot(session, board_row, "png")
                assert ct == "image/png" and data[:8] == b"\x89PNG\r\n\x1a\n"

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())
