"""V51 feature tests: storage backends, data-DAG policies, dashboard audit.

STORAGE BACKENDS: dataset parquet blobs move behind a pluggable backend
(local | s3/minio | gcs) while DuckDB stays the compute engine - the
service addresses blobs by PATH, the backend decides where bytes live.
A fake in-memory backend proves the ROUTING (writes land in the backend
and not on disk, versions copy server-side, deletes clear prefixes,
run_sql stages downloads); moto proves the S3 API surface; the status
endpoint reports kind + liveness.

DATA-DAG POLICIES: a workflow-level execution policy (retries, backoff
with multiplier, per-attempt timeout, retry_on=all|transient) becomes the
default for every node without its own settings - transient failures
(connection/timeout/5xx messages) retry, permanent ones (validation,
data-contract ValueErrors, wrapped NodeExecutionError causes) fail fast;
node-level settings always win; every node run records the policy it ran
under.

DASHBOARD SHARE AUDIT: parity with the v49 apps grant log - protected
boards log every allowed /d/{slug} render and every rejected attempt
(missing/invalid token) BEFORE the 403, open boards are never logged,
and the owner reads the trail via GET /dashboards/{id}/share/audit.

Runs the FastAPI app in-process via httpx ASGITransport (same harness as v4-v50).
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import httpx
import pandas as pd

from app.main import app
from app.services import executor as executor_mod
from app.services import storage as storage_svc
from app.services import datasets as ds_svc
from app.services.storage import S3Backend, LocalBackend, StorageBackend, StorageError

API = "http://testserver/api/v1"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


async def _drain_background() -> None:
    tasks = [t for t in executor_mod._background_tasks if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _node(nid: str, ntype: str, params: dict | None = None, name: str | None = None, settings: dict | None = None) -> dict:
    out = {"id": nid, "type": ntype, "name": name or nid, "position": {"x": 0, "y": 0}, "parameters": params or {}}
    if settings is not None:
        out["settings"] = settings
    return out


def _edge(eid: str, source: str, target: str) -> dict:
    return {"id": eid, "source": source, "target": target, "sourceHandle": "main", "targetHandle": "main"}


async def _run_and_wait(client: httpx.AsyncClient, workflow_id: str, payload: dict | None = None) -> dict:
    res = await client.post(f"/workflows/{workflow_id}/run", json={"payload": payload or {}})
    assert res.status_code in (200, 202), res.text
    exec_id = res.json()["execution_id"]
    for _ in range(200):
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


async def _mk_dataset(client: httpx.AsyncClient, name: str, rows: list[dict]) -> str:
    res = await client.post("/datasets", json={"name": name, "rows": rows})
    assert res.status_code == 201, res.text
    return res.json()["id"]


async def _cleanup_datasets(client: httpx.AsyncClient, prefix: str) -> None:
    res = await client.get("/datasets")
    for row in res.json():
        if str(row["name"]).startswith(prefix):
            await client.delete(f"/datasets/{row['id']}")


# ===========================================================================
# 1) fake backend: the ROUTING contract of the dataset service
# ===========================================================================
class FakeRemoteBackend(StorageBackend):
    """In-memory 'object store' - proves the service routes through the
    backend instead of touching the local datasets dir."""

    kind = "s3"  # behaves like a remote: service must not keep local files

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.copied: list[tuple[str, str]] = []

    def read_bytes(self, key: str) -> bytes:
        if key not in self.objects:
            raise FileNotFoundError(key)
        return self.objects[key]

    def write_bytes(self, key: str, data: bytes) -> None:
        self.objects[key] = data

    def exists(self, key: str) -> bool:
        return key in self.objects

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    def copy(self, src_key: str, dst_key: str) -> None:
        if src_key not in self.objects:
            raise FileNotFoundError(src_key)
        self.copied.append((src_key, dst_key))
        self.objects[dst_key] = self.objects[src_key]

    def delete_prefix(self, prefix: str) -> None:
        clean = prefix.strip("/") + "/"
        self.objects = {k: v for k, v in self.objects.items() if not k.startswith(clean)}


def test_v51_fake_backend_routing():
    async def _go():
        prefix = f"v51route {uuid.uuid4().hex[:6]}"
        backend = FakeRemoteBackend()
        storage_svc.set_backend(backend)
        try:
            async with _client() as client:
                await _cleanup_datasets(client, prefix)
                rows_in = [{"name": "Ada", "age": 36}, {"name": "Grace", "age": 45}]
                ds_id = await _mk_dataset(client, f"{prefix} users", rows_in)

                # 1. the blob lives in the BACKEND, not on local disk
                assert f"{ds_id}.parquet" in backend.objects, sorted(backend.objects)
                assert not ds_svc.parquet_path(ds_id).exists(), "remote-backed dataset must not keep a local file"

                # 2. rows read back THROUGH the backend
                res = await client.get(f"/datasets/{ds_id}/rows")
                assert res.status_code == 200, res.text
                assert res.json()["rows"][0]["name"] == "Ada"

                # 3. versions copy via the backend (server-side copy path)
                res = await client.post(f"/datasets/{ds_id}/rows", json={"rows": [{"name": "Alan", "age": 41}]})
                assert res.status_code == 200, res.text
                res = await client.get(f"/datasets/{ds_id}/versions")
                versions = res.json()
                assert len(versions) == 2, versions
                assert all(v["file_exists"] for v in versions), versions
                v1, v2 = versions[0], versions[1]  # list is newest-first? verify order
                if v1["version"] > v2["version"]:
                    v1, v2 = v2, v1
                assert f"versions/{ds_id}/v1.parquet" in backend.objects
                assert f"versions/{ds_id}/v2.parquet" in backend.objects
                assert any(s == f"{ds_id}.parquet" for s, _ in backend.copied), backend.copied

                # 4. snapshot preview reads through the backend
                res = await client.get(f"/datasets/{ds_id}/versions/{v1['version']}/rows")
                assert res.status_code == 200, res.text
                assert len(res.json()["rows"]) == 2

                # 5. time travel restores through the backend
                res = await client.post(f"/datasets/{ds_id}/versions/{v1['version']}/restore")
                assert res.status_code == 200, res.text
                assert res.json()["row_count"] == 2

                # 6. run_sql stages remote blobs (download -> duckdb view)
                res = await client.post("/datasets/query", json={"sql": "SELECT COUNT(*) AS n FROM " + ds_svc.view_name(f"{prefix} users")})
                assert res.status_code == 200, res.text
                assert res.json()["rows"][0]["n"] == 2, res.text

                # 7. parquet export streams the backend blob verbatim
                res = await client.get(f"/datasets/{ds_id}/export?fmt=parquet")
                assert res.status_code == 200, res.text
                assert len(res.content) > 100

                # 8. dataset delete clears the live blob AND the version prefix
                res = await client.delete(f"/datasets/{ds_id}")
                assert res.status_code == 204, res.text
                live_keys = [k for k in backend.objects if not k.startswith(f"versions/{ds_id}/")]
                assert f"{ds_id}.parquet" not in live_keys, sorted(backend.objects)
                assert not any(k.startswith(f"versions/{ds_id}/") for k in backend.objects), sorted(backend.objects)
        finally:
            storage_svc.set_backend(None)
            # safety: the dataset may still exist if an assertion fired early
            async with _client() as client:
                await _cleanup_datasets(client, prefix)
                await _drain_background()

    asyncio.run(_go())


def test_v51_storage_status_and_local_default():
    async def _go():
        async with _client() as client:
            res = await client.get("/storage")
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["kind"] == "local", body
            assert body["ping"] is True, body
            assert "root" in body, body
    asyncio.run(_go())


def test_v51_storage_key_guard():
    # keys must stay inside the datasets root (defence in depth)
    inside = ds_svc.parquet_path("abc")
    assert ds_svc.storage_key(inside) == "abc.parquet"
    assert ds_svc.storage_key(ds_svc.version_file("abc", 3)) == "versions/abc/v3.parquet"
    try:
        ds_svc.storage_key(Path("/etc/passwd"))
        raise AssertionError("path escape not rejected")
    except StorageError:
        pass
    # LocalBackend rejects traversal keys
    backend = LocalBackend()
    for bad in ("../escape.parquet", "/abs.parquet", ""):
        try:
            backend.read_bytes(bad)
            raise AssertionError(f"bad key {bad!r} accepted")
        except (StorageError, FileNotFoundError):
            pass


def test_v51_s3_backend_with_moto():
    moto = pytest_import_moto()
    import boto3

    with moto.mock_aws():
        os_set_aws_creds()
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="py8n-test")

        backend = S3Backend(bucket="py8n-test", prefix="datasets")
        assert backend.describe() == {"kind": "s3", "bucket": "py8n-test", "prefix": "datasets"}

        # raw backend verbs
        backend.write_bytes("abc.parquet", b"hello-parquet")
        assert backend.read_bytes("abc.parquet") == b"hello-parquet"
        assert backend.exists("abc.parquet")
        assert not backend.exists("nope.parquet")
        backend.copy("abc.parquet", "versions/abc/v1.parquet")
        assert backend.read_bytes("versions/abc/v1.parquet") == b"hello-parquet"
        backend.delete("abc.parquet")
        assert not backend.exists("abc.parquet")
        backend.write_bytes("versions/abc/v2.parquet", b"x")
        backend.delete_prefix("versions/abc")
        assert not backend.exists("versions/abc/v1.parquet")
        assert not backend.exists("versions/abc/v2.parquet")
        assert backend.ping() is True

        try:
            backend.read_bytes("nope.parquet")
            raise AssertionError("missing key read did not raise")
        except FileNotFoundError:
            pass

        # full dataset round trip THROUGH the S3 backend (service routing)
        async def _roundtrip():
            prefix = f"v51s3 {uuid.uuid4().hex[:6]}"
            storage_svc.set_backend(S3Backend(bucket="py8n-test", prefix="datasets"))
            try:
                async with _client() as client:
                    await _cleanup_datasets(client, prefix)
                    ds_id = await _mk_dataset(client, f"{prefix} users", [{"k": "v"}])
                    head = s3.head_object(Bucket="py8n-test", Key=f"datasets/{ds_id}.parquet")
                    assert head["ContentLength"] > 100
                    assert not ds_svc.parquet_path(ds_id).exists()
                    res = await client.post(f"/datasets/{ds_id}/rows", json={"rows": [{"k": "v2"}]})
                    assert res.status_code == 200, res.text
                    res = await client.get(f"/datasets/{ds_id}/versions")
                    assert all(v["file_exists"] for v in res.json())
                    res = await client.get(f"/datasets/{ds_id}/rows")
                    assert len(res.json()["rows"]) == 2
                    await client.delete(f"/datasets/{ds_id}")
            finally:
                storage_svc.set_backend(None)

        asyncio.run(_roundtrip())


def pytest_import_moto():
    try:
        import moto

        return moto
    except ImportError as exc:  # pragma: no cover - moto is in requirements
        raise AssertionError("moto must be installed for the S3 backend tests") from exc


def os_set_aws_creds() -> None:
    import os

    os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")


def test_v51_build_backend_from_env(monkeypatch):
    # unknown backend name -> clean error; minio aliases s3; gcs needs bucket
    monkeypatch.setattr(storage_svc.settings, "storage_backend", "nonsense")
    try:
        storage_svc.build_backend()
        raise AssertionError("unknown backend accepted")
    except StorageError:
        pass
    monkeypatch.setattr(storage_svc.settings, "storage_backend", "minio")
    monkeypatch.setattr(storage_svc.settings, "s3_bucket", "bkt")
    monkeypatch.setattr(storage_svc.settings, "s3_endpoint_url", "http://minio:9000")
    backend = storage_svc.build_backend()
    assert backend.kind == "s3" and backend.describe()["endpoint_url"] == "http://minio:9000"
    monkeypatch.setattr(storage_svc.settings, "storage_backend", "gcs")
    monkeypatch.setattr(storage_svc.settings, "gcs_bucket", "gs-bkt")
    assert storage_svc.build_backend().describe()["bucket"] == "gs-bkt"
    # missing bucket -> error at construction
    monkeypatch.setattr(storage_svc.settings, "gcs_bucket", "")
    try:
        storage_svc.build_backend()
        raise AssertionError("bucketless gcs backend accepted")
    except StorageError:
        pass


# ===========================================================================
# 2) data-DAG execution policies
# ===========================================================================
def _policy_graph(code: str, node_settings: dict | None = None) -> dict:
    return {
        "nodes": [
            _node("t1", "manual_trigger"),
            _node("c1", "code", {"code": code}, name="victim", settings=node_settings),
        ],
        "edges": [_edge("e1", "t1", "c1")],
    }


def test_v51_policy_retries_transient_vs_permanent():
    async def _go():
        async with _client() as client:
            # transient: a timeout-flavored failure retries to exhaustion
            # (sandbox-safe: a NameError whose message matches the transient
            # classifier - no exception classes inside the sandbox)
            res = await client.post("/workflows", json={
                "name": f"v51 pol transient {uuid.uuid4().hex[:6]}",
                "graph": _policy_graph("timeout_variable"),
                "policy": {"retries": 2, "backoff_ms": 5, "backoff_multiplier": 2, "retry_on": "transient"},
            })
            assert res.status_code == 201, res.text
            wf_id = res.json()["id"]
            assert res.json()["policy"]["retries"] == 2

            run = await _run_and_wait(client, wf_id)
            assert run["status"] == "error"
            victim = _find_node_run(run, "victim")
            assert victim is not None
            assert victim["attempts"] == 3, victim  # 1 + 2 retries
            assert victim["policy"]["source"] == "workflow"
            assert victim["policy"]["retry_on"] == "transient"
            assert "timeout_variable" in (victim["error"] or "")
            await client.delete(f"/workflows/{wf_id}")

            # permanent: a ValueError (validation/contract class) fails FAST
            # (sandbox-safe: int('abc') raises ValueError natively)
            res = await client.post("/workflows", json={
                "name": f"v51 pol permanent {uuid.uuid4().hex[:6]}",
                "graph": _policy_graph("int('abc')"),
                "policy": {"retries": 4, "backoff_ms": 5, "retry_on": "transient"},
            })
            assert res.status_code == 201, res.text
            wf_id = res.json()["id"]
            run = await _run_and_wait(client, wf_id)
            assert run["status"] == "error"
            victim = _find_node_run(run, "victim")
            assert victim is not None
            assert "attempts" not in victim or victim.get("attempts", 1) == 1, victim
            assert "permanent error, retries skipped" in (victim["error"] or ""), victim
            assert "invalid literal" in (victim["error"] or ""), victim
            await client.delete(f"/workflows/{wf_id}")
            await _drain_background()

    asyncio.run(_go())


def test_v51_policy_node_settings_win():
    async def _go():
        async with _client() as client:
            # workflow policy says 3 retries; the node pins its own 1
            # (sandbox-safe throttling-flavored NameError for the classifier)
            res = await client.post("/workflows", json={
                "name": f"v51 pol node-win {uuid.uuid4().hex[:6]}",
                "graph": _policy_graph(
                    "throttling_error",
                    node_settings={"retry_on_fail": True, "max_retries": 1, "retry_wait_ms": 1},
                ),
                "policy": {"retries": 3, "backoff_ms": 5},
            })
            assert res.status_code == 201, res.text
            wf_id = res.json()["id"]
            run = await _run_and_wait(client, wf_id)
            victim = _find_node_run(run, "victim")
            assert victim is not None
            assert victim["attempts"] == 2, victim  # node's max_retries=1 wins
            assert victim["policy"]["source"] == "node"
            assert victim["policy"]["retries"] == 1
            await client.delete(f"/workflows/{wf_id}")
            await _drain_background()

    asyncio.run(_go())


def test_v51_policy_timeout_on_data_chain():
    async def _go():
        async with _client() as client:
            # a hung step (delay 5s) is cut off at the policy's 1s timeout,
            # and the timeout itself is transient -> retried once
            res = await client.post("/workflows", json={
                "name": f"v51 pol timeout {uuid.uuid4().hex[:6]}",
                "graph": {
                    "nodes": [
                        _node("t1", "manual_trigger"),
                        _node("d1", "delay", {"seconds": 5}, name="victim"),
                    ],
                    "edges": [_edge("e1", "t1", "d1")],
                },
                "policy": {"retries": 1, "timeout_seconds": 1},
            })
            assert res.status_code == 201, res.text
            wf_id = res.json()["id"]
            run = await _run_and_wait(client, wf_id)
            victim = _find_node_run(run, "victim")
            assert victim is not None
            assert "timed out after 1s" in (victim["error"] or ""), victim
            assert victim["policy"]["timeout_ms"] == 1000
            assert victim["attempts"] == 2, victim
            await client.delete(f"/workflows/{wf_id}")
            await _drain_background()

    asyncio.run(_go())


def test_v51_policy_api_validation():
    async def _go():
        async with _client() as client:
            # create with policy
            res = await client.post("/workflows", json={
                "name": f"v51 pol api {uuid.uuid4().hex[:6]}",
                "graph": {"nodes": [_node("t1", "manual_trigger")], "edges": []},
                "policy": {"retries": 3, "backoff_ms": 250, "timeout_seconds": 120, "retry_on": "all"},
            })
            assert res.status_code == 201, res.text
            wf = res.json()
            assert wf["policy"]["retries"] == 3 and wf["policy"]["timeout_seconds"] == 120

            # invalid shapes rejected
            for bad in (
                {"retries": 9},
                {"nope": 1},
                {"retry_on": "sometimes"},
                {"timeout_seconds": "soon"},
            ):
                res2 = await client.put(f"/workflows/{wf['id']}", json={"policy": bad})
                assert res2.status_code == 400, (bad, res2.text)

            # clear with {}
            res2 = await client.put(f"/workflows/{wf['id']}", json={"policy": {}})
            assert res2.status_code == 200, res2.text
            assert res2.json()["policy"] is None
            # omitted = untouched
            res2 = await client.put(f"/workflows/{wf['id']}", json={"policy": {"retries": 1}})
            assert res2.json()["policy"]["retries"] == 1
            res2 = await client.put(f"/workflows/{wf['id']}", json={"description": "x"})
            assert res2.json()["policy"]["retries"] == 1
            await client.delete(f"/workflows/{wf['id']}")
            await _drain_background()

    asyncio.run(_go())


def test_v51_policy_contract_breach_fails_fast_on_data_chain():
    """The data-DAG story: a contract violation inside a dataset pipeline
    must NOT burn the retry wheel - it fails on attempt 1."""
    async def _go():
        async with _client() as client:
            prefix = f"v51pol {uuid.uuid4().hex[:6]}"
            ds_name = f"{prefix} users"
            ds_id = await _mk_dataset(client, ds_name, [{"status": "active"}])
            try:
                res = await client.put(f"/datasets/{ds_id}/contract", json={
                    "columns": [{"name": "status", "dtype": "text", "allowed": ["active", "inactive"]}],
                    "on_violation": "error",
                })
                assert res.status_code in (200, 201), res.text

                # manual trigger -> code feeder (emits the rows) -> dataset_write
                graph = {
                    "nodes": [
                        _node("t1", "manual_trigger"),
                        _node("s1", "code", {"code": "result = [{'status': 'BROKEN'}]"}, name="feeder"),
                        _node("w1", "dataset_write", {"dataset": ds_name, "mode": "append"}, name="write"),
                    ],
                    "edges": [_edge("e1", "t1", "s1"), _edge("e2", "s1", "w1")],
                }
                res = await client.post("/workflows", json={
                    "name": f"{prefix} chain",
                    "graph": graph,
                    "policy": {"retries": 3, "backoff_ms": 5, "retry_on": "transient"},
                })
                assert res.status_code == 201, res.text
                wf_id = res.json()["id"]
                run = await _run_and_wait(client, wf_id)
                assert run["status"] == "error"
                write_run = _find_node_run(run, "write")
                assert write_run is not None
                assert write_run.get("attempts", 1) == 1, write_run
                assert "permanent error, retries skipped" in (write_run["error"] or ""), write_run
                await client.delete(f"/workflows/{wf_id}")
            finally:
                await client.delete(f"/datasets/{ds_id}")
                await _drain_background()

    asyncio.run(_go())


# ===========================================================================
# 3) dashboard share audit (parity with apps)
# ===========================================================================
def test_v51_dashboard_share_audit():
    async def _go():
        async with _client() as client:
            tag = uuid.uuid4().hex[:6]
            ds_id = await _mk_dataset(client, f"v51 audit ds {tag}", [
                {"region": "eu", "amount": 10}, {"region": "us", "amount": 20},
            ])
            try:
                # protected board
                res = await client.post("/dashboards", json={
                    "name": f"v51 audit board {tag}", "dataset_ids": [ds_id],
                })
                assert res.status_code == 201, res.text
                board = res.json()
                await client.post(f"/dashboards/{board['id']}/publish")
                res = await client.put(f"/dashboards/{board['id']}/share", json={"enabled": True})
                assert res.status_code == 200, res.text
                token = res.json()["share_token"]
                assert token

                slug = board["slug"]
                # allowed render
                res = await client.get(f"/dashboards/{slug}/runtime?t={token}")
                assert res.status_code == 200, res.text
                # missing token -> 403 + audited
                res = await client.get(f"/dashboards/{slug}/runtime")
                assert res.status_code == 403
                # invalid token -> 403 + audited
                res = await client.get(f"/dashboards/{slug}/runtime?t=wrong-token")
                assert res.status_code == 403

                res = await client.get(f"/dashboards/{board['id']}/share/audit")
                assert res.status_code == 200, res.text
                audit = res.json()
                assert audit["protected"] is True
                assert audit["total"] == 3, audit
                actions = [(e["outcome"], e["action"], e["detail"]) for e in audit["events"]]
                assert ("allowed", "view_dashboard", None) in actions, actions
                denied = [d for o, _, d in actions if o == "denied"]
                assert "missing token" in denied and "invalid token" in denied, actions

                # open boards are NEVER logged
                res = await client.post("/dashboards", json={
                    "name": f"v51 open board {tag}", "dataset_ids": [ds_id],
                })
                open_board = res.json()
                await client.post(f"/dashboards/{open_board['id']}/publish")
                res = await client.get(f"/dashboards/{open_board['slug']}/runtime")
                assert res.status_code == 200
                res = await client.get(f"/dashboards/{open_board['id']}/share/audit")
                assert res.json()["protected"] is False
                assert res.json()["total"] == 0, res.json()

                await client.delete(f"/dashboards/{board['id']}")
                await client.delete(f"/dashboards/{open_board['id']}")
            finally:
                await client.delete(f"/datasets/{ds_id}")
                await _drain_background()

    asyncio.run(_go())
