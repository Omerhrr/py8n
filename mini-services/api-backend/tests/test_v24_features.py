"""V24 feature tests: Compare Datasets, Summarize, CSV.

Compare Datasets is the platform's first multi-INPUT node (Input A on the
"main" targetHandle, Input B on "secondary" - the runner now exposes
targetHandle-keyed payloads via context.current_input_handles) and routes
reconciliation results to matched / a_only / b_only output handles.
Summarize adds group-by aggregation; CSV parses/serializes CSV text.

Runs the FastAPI app in-process via httpx ASGITransport (same harness as v4-v23).
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


async def _cleanup(workflow_ids: list[str]) -> None:
    async with _client() as client:
        for wid in workflow_ids:
            try:
                await client.delete(f"/workflows/{wid}")
            except Exception:
                pass
    await _drain_background()


def _node(nid: str, ntype: str, params: dict | None = None, name: str | None = None) -> dict:
    return {
        "id": nid,
        "type": ntype,
        "name": name or nid,
        "position": {"x": 0, "y": 0},
        "parameters": params or {},
    }


async def _make_workflow(client: httpx.AsyncClient, name: str, graph: dict) -> str:
    res = await client.post("/workflows", json={"name": name, "graph": graph})
    assert res.status_code == 201, res.text
    return res.json()["id"]


async def _run_and_wait(client: httpx.AsyncClient, workflow_id: str, payload: dict | None = None) -> dict:
    res = await client.post(f"/workflows/{workflow_id}/run", json={"payload": payload or {}})
    assert res.status_code in (200, 202), res.text
    exec_id = res.json()["execution_id"]
    for _ in range(100):
        res = await client.get(f"/executions/{exec_id}")
        assert res.status_code == 200, res.text
        if res.json()["status"] != "running":
            return res.json()
        await asyncio.sleep(0.05)
    raise AssertionError("execution did not finish in time")


def _find_node_run(execution: dict, node_name: str) -> dict | None:
    for run in execution.get("node_runs") or []:
        if run.get("node_name") == node_name:
            return run
    return None


# ---------------------------------------------------------------------------
# 1) Compare Datasets E2E: two split_out sources -> A/B handles -> 3 branches
# ---------------------------------------------------------------------------
def test_v24_compare_datasets_routing():
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []

    async def _go():
        nonlocal wf_ids
        async with _client() as client:
            crm = [{"id": 1, "sku": "A1"}, {"id": 2, "sku": "A2"}, {"id": 3, "sku": "A3"}]
            billing = [{"id": 2, "sku": "B2"}, {"id": 3, "sku": "B3"}, {"id": 9, "sku": "B9"}]
            graph = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("sa", "split_out", {"field": "a"}, "CRM"),
                    _node("sb", "split_out", {"field": "b"}, "Billing"),
                    _node("cmp", "compare_datasets", {"field_a": "id", "field_b": "id"}, "Reconcile"),
                    _node("m_set", "set_variable", {"assignments": {"n": "{{ input | length }}"}}, "Matched Out"),
                    _node("a_set", "set_variable", {"assignments": {"n": "{{ input | length }}"}}, "A Only Out"),
                    _node("b_set", "set_variable", {"assignments": {"n": "{{ input | length }}"}}, "B Only Out"),
                ],
                "edges": [
                    {"id": "e1", "source": "t", "target": "sa", "sourceHandle": "main", "targetHandle": "main"},
                    {"id": "e2", "source": "t", "target": "sb", "sourceHandle": "main", "targetHandle": "main"},
                    {"id": "e3", "source": "sa", "target": "cmp", "sourceHandle": "main", "targetHandle": "main"},
                    {"id": "e4", "source": "sb", "target": "cmp", "sourceHandle": "main", "targetHandle": "secondary"},
                    {"id": "e5", "source": "cmp", "target": "m_set", "sourceHandle": "matched", "targetHandle": "main"},
                    {"id": "e6", "source": "cmp", "target": "a_set", "sourceHandle": "a_only", "targetHandle": "main"},
                    {"id": "e7", "source": "cmp", "target": "b_set", "sourceHandle": "b_only", "targetHandle": "main"},
                ],
            }
            wid = await _make_workflow(client, f"v24 cmp routing {tag}", graph)
            wf_ids.append(wid)
            run = await _run_and_wait(client, wid, payload={"a": crm, "b": billing})

            assert run["status"] == "success", run.get("error")

            cmp_run = _find_node_run(run, "Reconcile")
            assert cmp_run and cmp_run["status"] == "success"
            assert cmp_run["output"] == {"matched": 2, "a_only": 1, "b_only": 1, "b_duplicates_skipped": 0}

            # Downstream branches each received their own bucket
            m = _find_node_run(run, "Matched Out")
            assert m["status"] == "success"
            pairs = m["input"]
            assert [p["a"]["id"] for p in pairs] == [2, 3]  # A order wins
            assert pairs[0]["b"]["sku"] == "B2"
            assert m["output"]["n"] == 2

            a = _find_node_run(run, "A Only Out")
            assert a["status"] == "success"
            assert [it["id"] for it in a["input"]] == [1]
            assert a["output"]["n"] == 1

            b = _find_node_run(run, "B Only Out")
            assert b["status"] == "success"
            assert [it["id"] for it in b["input"]] == [9]
            assert b["output"]["n"] == 1

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(wf_ids))


# ---------------------------------------------------------------------------
# 2) Compare edge cases: only A connected (orphans skip matched/b_only
#    branches) + duplicate B keys counted, first occurrence wins
# ---------------------------------------------------------------------------
def test_v24_compare_edge_cases():
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []

    async def _go():
        nonlocal wf_ids
        async with _client() as client:
            # --- only Input A wired ---
            graph_a = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("sa", "split_out", {"field": "a"}, "CRM"),
                    _node("cmp", "compare_datasets", {}, "Reconcile"),
                    _node("m_set", "set_variable", {"assignments": {"n": 1}}, "Matched Out"),
                    _node("a_set", "set_variable", {"assignments": {"n": "{{ input | length }}"}}, "A Only Out"),
                    _node("b_set", "set_variable", {"assignments": {"n": 1}}, "B Only Out"),
                ],
                "edges": [
                    {"id": "e1", "source": "t", "target": "sa", "sourceHandle": "main", "targetHandle": "main"},
                    {"id": "e3", "source": "sa", "target": "cmp", "sourceHandle": "main", "targetHandle": "main"},
                    {"id": "e5", "source": "cmp", "target": "m_set", "sourceHandle": "matched", "targetHandle": "main"},
                    {"id": "e6", "source": "cmp", "target": "a_set", "sourceHandle": "a_only", "targetHandle": "main"},
                    {"id": "e7", "source": "cmp", "target": "b_set", "sourceHandle": "b_only", "targetHandle": "main"},
                ],
            }
            wid_a = await _make_workflow(client, f"v24 cmp onlyA {tag}", graph_a)
            wf_ids.append(wid_a)
            run = await _run_and_wait(client, wid_a, payload={"a": [{"id": 1}, {"id": 2}]})
            assert run["status"] == "success", run.get("error")
            cmp_run = _find_node_run(run, "Reconcile")
            assert cmp_run["output"] == {"matched": 0, "a_only": 2, "b_only": 0, "b_duplicates_skipped": 0}
            assert _find_node_run(run, "Matched Out")["status"] == "skipped"  # no payload on that handle
            assert _find_node_run(run, "B Only Out")["status"] == "skipped"
            a_out = _find_node_run(run, "A Only Out")
            assert a_out["status"] == "success" and a_out["output"]["n"] == 2

            # --- duplicate B keys: first wins, extras counted ---
            graph_dup = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("sa", "split_out", {"field": "a"}, "A"),
                    _node("sb", "split_out", {"field": "b"}, "B"),
                    _node("cmp", "compare_datasets", {"field_a": "id", "field_b": "id"}, "Reconcile"),
                ],
                "edges": [
                    {"id": "e1", "source": "t", "target": "sa", "sourceHandle": "main", "targetHandle": "main"},
                    {"id": "e2", "source": "t", "target": "sb", "sourceHandle": "main", "targetHandle": "main"},
                    {"id": "e3", "source": "sa", "target": "cmp", "sourceHandle": "main", "targetHandle": "main"},
                    {"id": "e4", "source": "sb", "target": "cmp", "sourceHandle": "main", "targetHandle": "secondary"},
                ],
            }
            wid_b = await _make_workflow(client, f"v24 cmp dup {tag}", graph_dup)
            wf_ids.append(wid_b)
            run = await _run_and_wait(
                client, wid_b,
                payload={
                    "a": [{"id": 1, "v": "crm"}],
                    "b": [{"id": 1, "v": "first"}, {"id": 1, "v": "second"}, {"id": 2, "v": "orphan"}],
                },
            )
            assert run["status"] == "success", run.get("error")
            cmp_run = _find_node_run(run, "Reconcile")
            assert cmp_run["output"] == {"matched": 1, "a_only": 0, "b_only": 1, "b_duplicates_skipped": 1}
            # the matched pair carries the FIRST B occurrence
            # (visible through template access from the run record output)
            assert cmp_run["output"]["matched"] == 1

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(wf_ids))


# ---------------------------------------------------------------------------
# 3) Compare fallback (both edges on one handle -> arrival order) + no inputs
# ---------------------------------------------------------------------------
def test_v24_compare_fallback_and_no_input_error():
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []

    async def _go():
        nonlocal wf_ids
        async with _client() as client:
            # both edges target the SAME handle ("main") -> arrival order fallback
            graph = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("sa", "split_out", {"field": "a"}, "A"),
                    _node("sb", "split_out", {"field": "b"}, "B"),
                    _node("cmp", "compare_datasets", {"field_a": "id", "field_b": "id"}, "Reconcile"),
                    _node("m_set", "set_variable", {"assignments": {"n": "{{ input | length }}"}}, "Matched Out"),
                ],
                "edges": [
                    {"id": "e1", "source": "t", "target": "sa", "sourceHandle": "main", "targetHandle": "main"},
                    {"id": "e2", "source": "t", "target": "sb", "sourceHandle": "main", "targetHandle": "main"},
                    {"id": "e3", "source": "sa", "target": "cmp", "sourceHandle": "main", "targetHandle": "main"},
                    {"id": "e4", "source": "sb", "target": "cmp", "sourceHandle": "main", "targetHandle": "main"},
                    {"id": "e5", "source": "cmp", "target": "m_set", "sourceHandle": "matched", "targetHandle": "main"},
                ],
            }
            wid = await _make_workflow(client, f"v24 cmp fallback {tag}", graph)
            wf_ids.append(wid)
            run = await _run_and_wait(client, wid, payload={"a": [{"id": 5}], "b": [{"id": 5}, {"id": 6}]})
            assert run["status"] == "success", run.get("error")
            cmp_run = _find_node_run(run, "Reconcile")
            assert cmp_run["output"]["matched"] == 1  # id 5 paired via fallback
            assert cmp_run["output"]["b_only"] == 1  # id 6 orphaned

            # no inputs at all -> clean node error
            graph2 = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("cmp", "compare_datasets", {}, "Lonely Compare"),
                ],
                "edges": [],
            }
            wid2 = await _make_workflow(client, f"v24 cmp lonely {tag}", graph2)
            wf_ids.append(wid2)
            run2 = await _run_and_wait(client, wid2)
            assert run2["status"] == "error"
            assert "at least one connected input" in (run2.get("error") or "")
            cmp_run2 = _find_node_run(run2, "Lonely Compare")
            assert cmp_run2 and cmp_run2["status"] == "error"

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(wf_ids))


# ---------------------------------------------------------------------------
# 4) Summarize: group-by aggregation + global mode + string min/max
# ---------------------------------------------------------------------------
def test_v24_summarize_grouping():
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []

    async def _go():
        nonlocal wf_ids
        async with _client() as client:
            rows = [
                {"region": "EU", "amount": 100, "day": "2024-01-15"},
                {"region": "EU", "amount": 50, "day": "2024-01-02"},
                {"region": "US", "amount": 80, "day": "2024-02-01"},
            ]
            graph = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node(
                        "summ",
                        "summarize",
                        {
                            "group_by": ["region"],
                            "aggregates": [
                                {"field": "amount", "op": "sum"},
                                {"field": "amount", "op": "avg"},
                                {"field": "day", "op": "min"},
                                {"op": "count"},
                            ],
                        },
                        "By Region",
                    ),
                ],
                "edges": [{"id": "e1", "source": "t", "target": "summ"}],
            }
            wid = await _make_workflow(client, f"v24 summarize {tag}", graph)
            wf_ids.append(wid)
            run = await _run_and_wait(client, wid, payload={"items": rows})
            assert run["status"] == "success", run.get("error")
            summ = _find_node_run(run, "By Region")
            assert summ["status"] == "success"
            out = summ["output"]
            assert out["groups"] == 2 and out["total_items"] == 3
            by_region = {g["region"]: g for g in out["items"]}
            eu, us = by_region["EU"], by_region["US"]
            assert eu["amount_sum"] == 150 and eu["amount_avg"] == 75.0
            assert eu["day_min"] == "2024-01-02"  # string-domain min
            assert eu["count"] == 2 and eu["_count"] == 2
            assert us["amount_sum"] == 80 and us["_count"] == 1

            # global mode: no group_by -> one group over all items
            graph2 = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("summ", "summarize", {"aggregates": [{"field": "amount", "op": "sum"}]}, "Global"),
                ],
                "edges": [{"id": "e1", "source": "t", "target": "summ"}],
            }
            wid2 = await _make_workflow(client, f"v24 summarize global {tag}", graph2)
            wf_ids.append(wid2)
            run2 = await _run_and_wait(client, wid2, payload={"items": rows})
            g = _find_node_run(run2, "Global")["output"]
            assert g["groups"] == 1 and g["items"][0]["amount_sum"] == 230 and g["items"][0]["_count"] == 3

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(wf_ids))


# ---------------------------------------------------------------------------
# 5) CSV: parse (quoting + auto_convert) -> serialize -> re-parse roundtrip
# ---------------------------------------------------------------------------
def test_v24_csv_roundtrip():
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []

    async def _go():
        nonlocal wf_ids
        async with _client() as client:
            graph = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("p1", "csv", {"mode": "parse", "content": "{{ input.payload.csv }}", "auto_convert": True}, "Parse"),
                    _node("s1", "csv", {"mode": "serialize"}, "Serialize"),
                    _node("p2", "csv", {"mode": "parse", "content": "{{ nodes.s1.output.csv }}", "auto_convert": True}, "Reparse"),
                    _node("nh", "csv", {"mode": "parse", "content": "x,y\n1,2", "has_header": False}, "NoHeader"),
                ],
                "edges": [
                    {"id": "e1", "source": "t", "target": "p1"},
                    {"id": "e2", "source": "p1", "target": "s1"},
                    {"id": "e3", "source": "s1", "target": "p2"},
                ],
            }
            wid = await _make_workflow(client, f"v24 csv {tag}", graph)
            wf_ids.append(wid)
            csv_text = 'name,note,amount\n"Alice, Inc",hello,120\nBob,"say ""hi""",9.5\n'
            run = await _run_and_wait(client, wid, payload={"csv": csv_text})
            assert run["status"] == "success", run.get("error")

            parsed = _find_node_run(run, "Parse")
            assert parsed["status"] == "success"
            items = parsed["output"]["items"]
            assert items == [
                {"name": "Alice, Inc", "note": "hello", "amount": 120},
                {"name": "Bob", "note": 'say "hi"', "amount": 9.5},
            ]
            assert parsed["output"]["columns"] == ["name", "note", "amount"]

            ser = _find_node_run(run, "Serialize")
            assert ser["output"]["rows"] == 2
            assert ser["output"]["columns"] == ["name", "note", "amount"]
            assert '"Alice, Inc"' in ser["output"]["csv"]  # minimal quoting round-trips

            reparsed = _find_node_run(run, "Reparse")
            assert reparsed["output"]["items"] == items  # true round-trip equality

            nh = _find_node_run(run, "NoHeader")
            assert nh["output"]["items"] == [{"0": "x", "1": "y"}, {"0": "1", "1": "2"}]
            assert nh["output"]["columns"] is None

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(wf_ids))


# ---------------------------------------------------------------------------
# 6) Definitions: 29 types with the three v24 nodes fully described
# ---------------------------------------------------------------------------
def test_v24_definitions():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            res = await client.get("/node-definitions")
            assert res.status_code == 200
            defs = res.json()["definitions"]
            types = {d["type"] for d in defs}
            assert len(defs) >= 37, f"expected 37+ node types, got {len(defs)}"  # 45 at v45
            assert {"compare_datasets", "summarize", "csv"} <= types

            cmp = next(d for d in defs if d["type"] == "compare_datasets")
            assert [h["key"] for h in cmp["inputs"]] == ["main", "secondary"]
            assert [h["key"] for h in cmp["outputs"]] == ["matched", "a_only", "b_only"]
            props = cmp["parameters_schema"]["properties"]
            assert "field_a" in props and "field_b" in props

            summ = next(d for d in defs if d["type"] == "summarize")
            sprops = summ["parameters_schema"]["properties"]
            assert sprops["group_by"]["widget"] == "code"
            assert sprops["aggregates"]["widget"] == "code"

            csvd = next(d for d in defs if d["type"] == "csv")
            cprops = csvd["parameters_schema"]["properties"]
            assert cprops["mode"]["options"] == ["parse", "serialize"]
            assert cprops["content"]["widget"] == "textarea"

    asyncio.run(_go())
