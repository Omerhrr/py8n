"""V38 feature tests: the node resilience pack + the live execution queue.

New machinery:
    NodeSettings.timeout_ms      - runner-enforced wall-clock cap per attempt
                                   (asyncio.wait_for; a timeout is an ordinary
                                   failure, so retry_on_fail applies to it)
    node_retry event             - emitted before every retry attempt so the
                                   live streams can show the loop working
    NodeSettings.fallback_enabled + fallback_value
                                 - on final failure the configured value is
                                   emitted on the main handle and the flow
                                   keeps going (takes precedence over
                                   continue_on_fail; the node record is still
                                   an error and carries fallback_used=True)
    GET /executions/queue        - every running / waiting execution merged
                                   with the executor's in-memory progress map
                                   (nodes done / total, current node)

Same harness as v4-v37: httpx ASGITransport in-process, per-test asyncio.run,
uuid-suffixed data, finally-cleanup + background drain. Delay + Code nodes
make every scenario deterministic and fully offline.
"""

from __future__ import annotations

import asyncio
import time
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


def _graph(nodes: list[dict], links: list[tuple[str, str]] | None = None) -> dict:
    edges = [
        {"id": f"e{i}", "source": s, "target": t, "sourceHandle": "main", "targetHandle": "main"}
        for i, (s, t) in enumerate(links or [])
    ]
    return {"nodes": nodes, "edges": edges}


def _node(nid: str, ntype: str, name: str, parameters: dict | None = None, settings: dict | None = None) -> dict:
    spec = {"id": nid, "type": ntype, "name": name, "position": {"x": 0, "y": 0}, "parameters": parameters or {}}
    if settings is not None:
        spec["settings"] = settings
    return spec


async def _run_and_wait(client: httpx.AsyncClient, workflow_id: str) -> dict:
    res = await client.post(f"/workflows/{workflow_id}/run", json={"payload": {}})
    assert res.status_code in (200, 202), res.text
    exec_id = res.json()["execution_id"]
    for _ in range(200):
        res = await client.get(f"/executions/{exec_id}")
        assert res.status_code == 200, res.text
        if res.json()["status"] != "running":
            return res.json()
        await asyncio.sleep(0.05)
    raise AssertionError("execution never left the running state")


def test_v38_health_pin():
    """Strict version pin lives in the latest wave only (convention)."""

    async def _go():
        async with _client() as client:
            res = await client.get("/health")
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["app"] == "Py8n" and body["version"] >= "1.38.0", body

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


def test_v38_settings_roundtrip():
    """The new NodeSettings fields persist through create + read untouched."""

    async def _go():
        tag = uuid.uuid4().hex[:8]
        graph = _graph(
            [
                _node("t", "manual_trigger", "Trigger"),
                _node(
                    "a",
                    "code",
                    "Fragile",
                    {"code": "result = input_data"},
                    {"retry_on_fail": True, "max_retries": 3, "retry_wait_ms": 250,
                     "timeout_ms": 1500, "fallback_enabled": True, "fallback_value": "keep going"},
                ),
            ]
        )
        async with _client() as client:
            res = await client.post("/workflows", json={"name": f"tmp v38 cfg {tag}", "graph": graph})
            assert res.status_code == 201, res.text
            wf = res.json()
            try:
                got = (await client.get(f"/workflows/{wf['id']}")).json()
                settings = got["graph"]["nodes"][1]["settings"]
                assert settings["timeout_ms"] == 1500, settings
                assert settings["fallback_enabled"] is True, settings
                assert settings["fallback_value"] == "keep going", settings
                assert settings["retry_wait_ms"] == 250, settings
            finally:
                await client.delete(f"/workflows/{wf['id']}")

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


def test_v38_timeout_and_retries():
    """timeout_ms aborts a hung node mid-attempt; retry_on_fail re-runs it.

    A Delay node set to sleep 5s is capped at 250ms per attempt and retried
    once: the run fails fast (well under the 5s sleep) with 2 recorded
    attempts and a 'timed out after 0.25s' error.
    """

    async def _go():
        tag = uuid.uuid4().hex[:8]
        graph = _graph(
            [
                _node("t", "manual_trigger", "Trigger"),
                _node(
                    "slow",
                    "delay",
                    "Delay",
                    {"seconds": 5},
                    {"retry_on_fail": True, "max_retries": 1, "retry_wait_ms": 10, "timeout_ms": 250},
                ),
            ],
            [("t", "slow")],
        )
        async with _client() as client:
            res = await client.post("/workflows", json={"name": f"tmp v38 timeout {tag}", "graph": graph})
            assert res.status_code == 201, res.text
            wf = res.json()
            try:
                started = time.monotonic()
                detail = await _run_and_wait(client, wf["id"])
                elapsed = time.monotonic() - started
                assert detail["status"] == "error", detail
                assert "timed out after 0.25s" in (detail.get("error") or ""), detail.get("error")
                runs = {r["node_id"]: r for r in detail["node_runs"]}
                slow = runs["slow"]
                assert slow["status"] == "error", slow
                assert slow.get("attempts") == 2, slow
                assert "timed out after 0.25s" in (slow.get("error") or ""), slow
                # the whole loop (2 capped attempts + wait) must stay far below
                # one uncapped 5s sleep - proof the timeout actually cut in
                assert elapsed < 4, f"timeout did not cut in: {elapsed:.1f}s"
            finally:
                await client.delete(f"/workflows/{wf['id']}")

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


def test_v38_fallback_feeds_downstream():
    """fallback_enabled keeps the flow alive: the configured value lands on
    the main handle, the node record stays an error with fallback_used, and
    downstream nodes consume the fallback payload as their input."""

    async def _go():
        tag = uuid.uuid4().hex[:8]
        graph = _graph(
            [
                _node("t", "manual_trigger", "Trigger"),
                _node(
                    "boom",
                    "code",
                    "Boom",
                    {"code": "result = 1 / 0"},  # ZeroDivisionError is a C-level raise, sandbox-proof
                    {"fallback_enabled": True, "fallback_value": {"answer": 42}},
                ),
                _node("echo", "code", "Echo", {"code": "result = input_data"}),
            ],
            [("t", "boom"), ("boom", "echo")],
        )
        async with _client() as client:
            res = await client.post("/workflows", json={"name": f"tmp v38 fallback {tag}", "graph": graph})
            assert res.status_code == 201, res.text
            wf = res.json()
            try:
                detail = await _run_and_wait(client, wf["id"])
                # the fallback swallowed the failure - the run itself succeeds
                assert detail["status"] == "success", detail.get("error")
                runs = {r["node_id"]: r for r in detail["node_runs"]}
                boom = runs["boom"]
                assert boom["status"] == "error", boom  # node knows it failed
                assert boom.get("fallback_used") is True, boom
                assert "Code error" in (boom.get("error") or ""), boom
                assert boom.get("output") == {"answer": 42}, boom
                echo = runs["echo"]
                assert echo["status"] == "success", echo
                assert echo.get("output") == {"result": {"answer": 42}}, echo
            finally:
                await client.delete(f"/workflows/{wf['id']}")

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


def test_v38_queue_endpoint():
    """GET /executions/queue lists running executions with live progress and
    they leave the queue when cancelled."""

    async def _go():
        tag = uuid.uuid4().hex[:8]
        graph = _graph(
            [
                _node("t", "manual_trigger", "Trigger"),
                _node("d", "delay", "Delay", {"seconds": 4}),
            ],
            [("t", "d")],
        )
        async with _client() as client:
            res = await client.post("/workflows", json={"name": f"tmp v38 queue {tag}", "graph": graph})
            assert res.status_code == 201, res.text
            wf = res.json()
            try:
                res = await client.post(f"/workflows/{wf['id']}/run", json={"payload": {}})
                assert res.status_code in (200, 202), res.text
                exec_id = res.json()["execution_id"]

                await asyncio.sleep(0.4)  # let the trigger finish, delay start
                res = await client.get("/executions/queue")
                assert res.status_code == 200, res.text
                body = res.json()
                assert body["total"] >= 1 and isinstance(body["items"], list), body
                item = next((i for i in body["items"] if i["execution_id"] == exec_id), None)
                assert item is not None, "run missing from the queue"
                assert item["status"] == "running", item
                assert item["workflow_name"] == wf["name"], item
                assert item["nodes_total"] == 2, item
                assert (item["nodes_done"] or 0) >= 1, item  # trigger already done
                assert item["current_node"] == "Delay", item

                # cancel: the item leaves the queue and the run is cancelled
                res = await client.post(f"/executions/{exec_id}/cancel")
                assert res.status_code == 202, res.text
                for _ in range(40):
                    res = await client.get(f"/executions/{exec_id}")
                    if res.json()["status"] != "running":
                        break
                    await asyncio.sleep(0.1)
                assert res.json()["status"] == "cancelled", res.json().get("status")
                res = await client.get("/executions/queue")
                ids = [i["execution_id"] for i in res.json()["items"]]
                assert exec_id not in ids, ids
            finally:
                await client.delete(f"/workflows/{wf['id']}")

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())
