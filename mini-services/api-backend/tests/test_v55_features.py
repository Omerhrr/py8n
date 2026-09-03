"""V55 feature tests: Impact & Lineage Intelligence.

VERSION DIFF: GET /datasets/{id}/versions/diff?from=&to=[&key=] compares
two snapshots across four lenses - schema (added/removed/retyped), rows
(version-record counts + delta), quality (completeness/nulls/duplicates
per snapshot + score delta, bounded profiling), and the row-level truth
(keyed: inserted/updated/removed with sample field-level updates;
keyless: multiset full-row-hash added/removed/unchanged) - plus the
IMPACT section: what breaks if this dataset changes.

IMPACT ENGINE: services/impact.py derives the downstream graph from live
graphs and registries - active workflows whose nodes reference the
dataset (engine-resolution matching: id, name, lowercase, view-name),
dashboards charting it, apps bound to it, models trained on it (by
dataset_name, as model_train records it), and the datasets those
consumer workflows WRITE (one hop downstream, via= the producer
workflows). Ranked by risk (model > app > dashboard > workflow),
sensitivity bumps the headline severity.

GOVERNANCE LAYER: datasets carry steward / domain / classification
(public|internal|confidential|restricted) / sensitivity
(low|medium|high|critical) / retention_days; patched via
PUT /datasets/{id} {"governance": {...}} with enum validation, surfaced
through the dataset API, the catalog entries and catalog filters
(?domain= ?classification= ?sensitivity=), and propagated into the
lineage response so the chain answers "who owns this" at every hop.

Runs the FastAPI app in-process via httpx ASGITransport (same harness as v4-v54).
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


def _node(nid: str, ntype: str, params: dict | None = None, name: str | None = None) -> dict:
    return {"id": nid, "type": ntype, "name": name or nid, "position": {"x": 0, "y": 0}, "parameters": params or {}}


def _edge(eid: str, source: str, target: str) -> dict:
    return {"id": eid, "source": source, "target": target, "sourceHandle": "main", "targetHandle": "main"}


async def _mk_user(client: httpx.AsyncClient, tag: str, n: int) -> dict:
    res = await client.post("/auth/register", json={
        "email": f"v55-{tag}-u{n}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v55 u{n} {tag}",
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


async def _replace_rows(ds_id: str, rows: list[dict]) -> None:
    """Direct service replace - the rows API is append-only by design."""
    from app.db import AsyncSessionLocal
    from app.services import datasets as ds_svc

    import pandas as pd

    async with AsyncSessionLocal() as session:
        ds = await session.get(__import__("app.models", fromlist=["Dataset"]).Dataset, ds_id)
        assert ds is not None
        fresh = ds_svc.normalize_df(pd.DataFrame(rows))
        await ds_svc.replace_rows(session, ds, fresh.to_dict(orient="records"))
        await session.commit()


# ===========================================================================
# 1) version diff: schema / rows / quality / changed (keyed + keyless)
# ===========================================================================
def test_v55_version_diff():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            ds = await _mk_dataset(client, f"v55-diff-{tag}", [
                {"id": 1, "city": "berlin", "v": 10},
                {"id": 2, "city": "paris", "v": 20},
            ])
            ds_id = ds["id"]

            # v2: one new row appended
            res = await client.post(f"/datasets/{ds_id}/rows", json={"rows": [{"id": 3, "city": "rome", "v": 30}]})
            assert res.status_code in (200, 201), res.text

            # v3: schema retype + keyed changes (1 updated, 1 inserted, 1 removed)
            await _replace_rows(ds_id, [
                {"id": 1, "city": "berlin", "v": 12.5},
                {"id": 2, "city": "paris", "v": 20},
                {"id": 4, "city": "madrid", "v": 40},
            ])

            # --- keyed diff v2 -> v3
            res = await client.get(f"/datasets/{ds_id}/versions/diff?from=2&to=3&key=id")
            assert res.status_code == 200, res.text
            d = res.json()
            assert d["from"]["version"] == 2 and d["to"]["version"] == 3
            assert d["rows"] == {"from": 3, "to": 3, "delta": 0}, d["rows"]
            # v retype: integer -> number
            assert any(c["name"] == "v" and c["from"] == "integer" and c["to"] == "number" for c in d["schema"]["changed"]), d["schema"]
            ch = d["changed"]
            assert ch["key"] == "id"
            assert ch["inserted"] == 1 and ch["removed"] == 1 and ch["updated"] == 1, ch
            sample = next(s for s in ch["updated_samples"] if s["key"] == "1")
            assert any(c["column"] == "v" for c in sample["changes"]), sample

            # --- keyless diff v1 -> v2 (pure append)
            res = await client.get(f"/datasets/{ds_id}/versions/diff?from=1&to=2")
            assert res.status_code == 200
            d12 = res.json()
            assert d12["changed"]["key"] is None
            assert d12["changed"]["added"] == 1 and d12["changed"]["removed"] == 0 and d12["changed"]["unchanged"] == 2, d12["changed"]
            assert "full-row hash" in d12["changed"]["note"]
            assert d12["rows"]["delta"] == 1

            # --- keyless diff v2 -> v3: 1' and 4 added, 1 and 3 removed, 2 unchanged
            res = await client.get(f"/datasets/{ds_id}/versions/diff?from=2&to=3")
            d23 = res.json()
            assert d23["changed"]["added"] == 2 and d23["changed"]["removed"] == 2 and d23["changed"]["unchanged"] == 1, d23["changed"]

            # --- quality lens exists on both sides
            assert "score" in d["quality"]["from"] and "score" in d["quality"]["to"]
            assert d["quality"]["from"]["null_rate_pct"] is not None

            # --- impact rides along
            assert set(d["impact"]) >= {"totals", "severity", "workflows", "dashboards", "apps", "models", "downstream_datasets"}

            # --- errors: unknown version + key not in both versions
            res = await client.get(f"/datasets/{ds_id}/versions/diff?from=99&to=1")
            assert res.status_code == 404
            res = await client.get(f"/datasets/{ds_id}/versions/diff?from=1&to=2&key=nope")
            assert res.status_code == 400

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


# ===========================================================================
# 2) impact engine: workflows / dashboards / apps / models / downstream
# ===========================================================================
def test_v55_impact_engine():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            ds = await _mk_dataset(client, f"v55 impact base {tag}", [
                {"region": "eu", "v": 10}, {"region": "us", "v": 20},
            ])
            ds_id = ds["id"]
            down_name = f"v55 impact down {tag}"
            # the downstream dataset exists (the consumer workflow would create it on first run)
            await _mk_dataset(client, down_name, [{"x": 1}])

            # consumer workflow: reads the dataset, writes a downstream one
            graph = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("r", "dataset_read", {"dataset": ds["name"]}),
                    _node("w", "dataset_write", {"dataset": down_name, "mode": "append"}),
                ],
                "edges": [_edge("e1", "t", "r"), _edge("e2", "r", "w")],
            }
            res = await client.post("/workflows", json={"name": f"v55 consumer {tag}", "graph": graph, "is_active": True})
            assert res.status_code == 201, res.text

            # dashboard charting it
            res = await client.post("/dashboards", json={
                "name": f"v55 impact board {tag}",
                "config": {"components": [
                    {"id": "c1", "type": "chart", "dataset_id": ds_id, "title": "By region",
                     "chart_type": "bar", "group_by": "region", "agg": "sum", "column": "v"},
                ]},
            })
            assert res.status_code == 201, res.text

            # app bound to it
            res = await client.post("/apps", json={"name": f"v55 impact app {tag}", "dataset_id": ds_id})
            assert res.status_code == 201, res.text

            # a model trained on it (registry row seeded directly - training is v46 territory)
            from app.db import AsyncSessionLocal
            from app.models import TrainedModel

            async with AsyncSessionLocal() as session:
                session.add(TrainedModel(
                    name=f"v55 churn {tag}", version=1, algorithm="logistic_regression",
                    task="classification", target="churned", features=["v"],
                    dataset_name=ds["name"], row_count=2, active=True,
                ))
                await session.commit()

            # mark sensitivity to bump severity
            res = await client.put(f"/datasets/{ds_id}", json={"governance": {"sensitivity": "high"}})
            assert res.status_code == 200, res.text

            res = await client.get(f"/datasets/{ds_id}/impact")
            assert res.status_code == 200, res.text
            imp = res.json()

            assert imp["totals"]["workflows"] >= 1
            assert imp["totals"]["dashboards"] >= 1
            assert imp["totals"]["apps"] >= 1
            assert imp["totals"]["models"] >= 1
            assert imp["totals"]["affected"] >= 4

            wf = next(w for w in imp["workflows"] if w["name"] == f"v55 consumer {tag}")
            assert wf["active"] is True and any("r" in str(n) or "read" in str(n).lower() for n in wf["nodes"]) or wf["nodes"], wf

            # downstream: the consumer writes the second dataset
            down = next(d for d in imp["downstream_datasets"] if d["name"] == down_name)
            assert down["via"] == [f"v55 consumer {tag}"], down

            # risk ranking: the model wins
            assert imp["highest_risk"] is not None
            assert imp["highest_risk"]["kind"] == "model"
            assert imp["highest_risk"]["name"] == f"v55 churn {tag}"
            # sensitivity high + a model => critical
            assert imp["severity"] == "critical", imp["severity"]

            # governance fields echo in the impact report
            assert imp["dataset"]["sensitivity"] == "high"

            # unaffected dataset: clean bill
            other = await _mk_dataset(client, f"v55 impact lonely {tag}", [{"x": 1}])
            res = await client.get(f"/datasets/{other['id']}/impact")
            assert res.json()["totals"]["affected"] == 0
            assert res.json()["highest_risk"] is None
            assert res.json()["severity"] == "low"

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


# ===========================================================================
# 3) governance layer: fields, validation, catalog surface + filters
# ===========================================================================
def test_v55_governance_layer():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            ua = await _mk_user(client, tag, 1)
            ds = await _mk_dataset(client, f"v55 gov {tag}", [{"x": 1}])
            ds_id = ds["id"]

            # patch the governance layer
            res = await client.put(f"/datasets/{ds_id}", json={"governance": {
                "steward": "Data Office",
                "domain": "sales",
                "classification": "confidential",
                "sensitivity": "high",
                "retention_days": 365,
            }}, headers=_auth(ua["token"]))
            assert res.status_code == 200, res.text
            g = res.json()["governance"]
            assert g == {"steward": "Data Office", "domain": "sales", "classification": "confidential",
                         "sensitivity": "high", "retention_days": 365}, g

            # enum validation
            res = await client.put(f"/datasets/{ds_id}", json={"governance": {"classification": "top-secret"}})
            assert res.status_code == 400
            res = await client.put(f"/datasets/{ds_id}", json={"governance": {"sensitivity": "meh"}})
            assert res.status_code == 400

            # dataset API surface (unclaimed read works anonymously)
            res = await client.get(f"/datasets/{ds_id}")
            assert res.json()["governance"]["domain"] == "sales"

            # catalog entries + filters
            cat = (await client.get("/catalog")).json()["entries"]
            entry = next(e for e in cat if e["id"] == ds_id)
            assert entry["governance"]["classification"] == "confidential"
            assert entry["governance"]["steward"] == "Data Office"

            res = await client.get("/catalog?sensitivity=high")
            assert any(e["id"] == ds_id for e in res.json()["entries"])
            res = await client.get("/catalog?sensitivity=low")
            assert not any(e["id"] == ds_id for e in res.json()["entries"])
            res = await client.get("/catalog?domain=SALES")
            assert any(e["id"] == ds_id for e in res.json()["entries"])
            res = await client.get("/catalog?classification=restricted")
            assert not any(e["id"] == ds_id for e in res.json()["entries"])

            # lineage propagates ownership
            res = await client.get(f"/datasets/{ds_id}/lineage")
            assert res.status_code == 200
            assert res.json()["governance"]["steward"] == "Data Office"
            assert res.json()["governance"]["domain"] == "sales"

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())
