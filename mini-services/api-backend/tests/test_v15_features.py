"""V15 feature tests: global environment variables ({{ env.KEY }}).

Covers: env-vars CRUD API (key normalization to UPPER_SNAKE_CASE, duplicate
409 case-insensitive, invalid-key 400, secret masking — values never echoed,
"__keep__" write-only semantics, is_secret flips, 404s) and the engine
integration (a run resolves {{ env.* }} in node parameters, including the
default filter for unknown keys, while the persisted context snapshot never
carries a standalone "env" dump).

Runs the FastAPI app in-process via httpx ASGITransport against the dev
SQLite DB (same harness as v4-v14). All assertions scope to variables and
workflows created here (uuid-suffixed keys) so dev data never flakes.
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


async def _cleanup(workflow_ids: list[str], env_ids: list[str]) -> None:
    async with _client() as client:
        for wid in workflow_ids:
            try:
                await client.delete(f"/workflows/{wid}")
            except Exception:
                pass
        for eid in env_ids:
            try:
                await client.delete(f"/env-vars/{eid}")
            except Exception:
                pass
    await _drain_background()


# ------------------------------------------------------------------ API tests
def test_env_vars_crud_masking_and_validation():
    tag = uuid.uuid4().hex[:8]
    env_ids: list[str] = []

    async def _go():
        async with _client() as client:
            # create: key stored EXACTLY as typed (Jinja access is case-sensitive)
            res = await client.post("/env-vars", json={
                "key": f"v15_plain_{tag}", "value": "hello-env", "description": "plain demo",
            })
            assert res.status_code == 201, res.text
            plain = res.json()
            env_ids.append(plain["id"])
            assert plain["key"] == f"v15_plain_{tag}", plain
            assert plain["value"] == "hello-env" and plain["is_secret"] is False

            # create secret: value NEVER echoed back
            res = await client.post("/env-vars", json={
                "key": f"V15_TOKEN_{tag}", "value": "tk-secret-42", "is_secret": True,
            })
            assert res.status_code == 201, res.text
            secret = res.json()
            env_ids.append(secret["id"])
            assert secret["value"] is None and secret["is_secret"] is True

            # duplicate key: case-insensitive → 409 (exact + case-variant)
            res = await client.post("/env-vars", json={"key": f"v15_plain_{tag}", "value": "x"})
            assert res.status_code == 409, res.text
            res = await client.post("/env-vars", json={"key": f"V15_PLAIN_{tag}", "value": "x"})
            assert res.status_code == 409, res.text

            # invalid keys → 400 (digit start, hyphen, empty)
            for bad in ("1ABC", f"{tag}-BAD", "  "):
                res = await client.post("/env-vars", json={"key": bad, "value": "x"})
                assert res.status_code == 400, (bad, res.text)

            # list shows both; only the plaintext value visible
            res = await client.get("/env-vars")
            assert res.status_code == 200, res.text
            rows = {r["id"]: r for r in res.json()}
            assert rows[plain["id"]]["value"] == "hello-env"
            assert rows[secret["id"]]["value"] is None

            # PUT plaintext replace
            res = await client.put(f"/env-vars/{plain['id']}", json={"value": "hello-env-2"})
            assert res.status_code == 200 and res.json()["value"] == "hello-env-2", res.text

            # secret write-only: "__keep__" preserves the stored value
            res = await client.put(f"/env-vars/{secret['id']}", json={"value": "__keep__"})
            assert res.status_code == 200 and res.json()["value"] is None, res.text

            # unmasking a secret (is_secret=False, no value sent) reveals the kept value
            res = await client.put(f"/env-vars/{secret['id']}", json={"is_secret": False})
            assert res.status_code == 200 and res.json()["value"] == "tk-secret-42", res.text
            # and back to secret
            res = await client.put(f"/env-vars/{secret['id']}", json={"is_secret": True})
            assert res.json()["value"] is None

            # description-only update leaves value untouched
            res = await client.put(f"/env-vars/{plain['id']}", json={"description": "updated desc"})
            assert res.json()["value"] == "hello-env-2" and res.json()["description"] == "updated desc"

            # 404 guards
            assert (await client.get("/env-vars/does-not-exist")).status_code == 404
            assert (await client.put("/env-vars/does-not-exist", json={"value": "x"})).status_code == 404
            assert (await client.delete("/env-vars/does-not-exist")).status_code == 404

            # delete + confirm gone
            res = await client.delete(f"/env-vars/{plain['id']}")
            assert res.status_code == 204, res.text
            env_ids.remove(plain["id"])
            assert (await client.get(f"/env-vars/{plain['id']}")).status_code == 404
    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup([], env_ids))


# ------------------------------------------------------------------ engine test
def test_env_vars_resolve_in_runs_and_never_dump_to_snapshot():
    tag = uuid.uuid4().hex[:8]
    env_ids: list[str] = []
    created: list[str] = []

    async def _go():
        async with _client() as client:
            # seed one plaintext + one secret variable
            for body in (
                {"key": f"V15_RUN_PLAIN_{tag}", "value": "plain-value-15"},
                {"key": f"V15_RUN_TOKEN_{tag}", "value": "token-value-15", "is_secret": True},
            ):
                res = await client.post("/env-vars", json=body)
                assert res.status_code == 201, res.text
                env_ids.append(res.json()["id"])

            graph = {
                "nodes": [
                    {"id": "t1", "type": "manual_trigger", "name": "Manual", "parameters": {}},
                    {
                        "id": "s1",
                        "type": "set_variable",
                        "name": "Read env",
                        "parameters": {
                            "assignments": {
                                "plain": "{{ env." + f"V15_RUN_PLAIN_{tag}" + " }}",
                                "token": "{{ env." + f"V15_RUN_TOKEN_{tag}" + " }}",
                                "missing": "{{ env." + f"V15_RUN_NOPE_{tag}" + " | default('fallback') }}",
                            },
                            "keep_input": False,
                        },
                    },
                ],
                "edges": [{"id": "e1", "source": "t1", "target": "s1", "sourceHandle": "main", "targetHandle": "main"}],
            }
            res = await client.post("/workflows", json={"name": f"v15 env run {tag}", "graph": graph})
            assert res.status_code == 201, res.text
            wf_id = res.json()["id"]
            created.append(wf_id)

            res = await client.post(f"/workflows/{wf_id}/run", json={"payload": {"hello": "v15"}})
            assert res.status_code in (200, 202), res.text
            exec_id = res.json()["execution_id"]
            await _drain_background()  # inline dispatch runs as a background task

            detail = (await client.get(f"/executions/{exec_id}")).json()
            assert detail["status"] == "success", detail.get("error")
            s1 = next(r for r in detail["node_runs"] if r["node_id"] == "s1")
            out = s1["output"]
            assert out["plain"] == "plain-value-15", out
            assert out["token"] == "token-value-15", out  # secrets resolve server-side
            assert out["missing"] == "fallback", out

            # the persisted snapshot never carries a standalone env dump
            assert "env" not in (detail.get("context_snapshot") or {}), detail.get("context_snapshot", {}).keys()
    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(created, env_ids))
