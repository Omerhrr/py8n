"""V52 feature tests: Google Sheets/FTP connectors, storage migration
tooling, scheduled report delivery (webhook + email).

CONNECTORS: google_sheets_source reads a sheet tab as rows - public sheets
via the no-auth gviz CSV export (fetch seam monkeypatched), private ones
via a service-account credential (token + values seams monkeypatched,
credential decrypted through a stubbed vault). ftp_source downloads a
csv/tsv file over FTP/FTPS (stdlib ftplib; the connect seam is stubbed,
the parse path is real). Both run through the real engine.

MIGRATION: one-shot blob copy-over from the CURRENT backend to a target -
enumerate (live + versions), read/verify/idempotent-skip, optional dry
run and optional source cleanup. FakeBackend proves the routing and the
report; moto proves the real S3 surface through POST /storage/migrate.

DELIVERY: a report carries webhook/email channels that fire after every
successful run; each attempt lands a ReportDeliveryEvent (ok | error |
skipped) readable via GET /reports/{id}/deliveries, oversized attachments
are omitted, a delivery failure never fails the report run, and the run
response carries the per-channel results.

Runs the FastAPI app in-process via httpx ASGITransport (same harness as v4-v51).
"""

from __future__ import annotations

import asyncio
import base64
import uuid

import httpx

from app.config import settings
from app.main import app
from app.services import datasets as ds_svc
from app.services import executor as executor_mod
from app.services import reports as report_svc
from app.services import storage as storage_svc
from app.services.storage import LocalBackend, StorageBackend

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


async def _make_workflow(client: httpx.AsyncClient, name: str, graph: dict) -> str:
    res = await client.post("/workflows", json={"name": name, "graph": graph, "is_active": False})
    assert res.status_code == 201, res.text
    return res.json()["id"]


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


class FakeRemoteBackend(StorageBackend):
    """In-memory 'object store' as a migration target."""

    kind = "s3"

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

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
        self.objects[dst_key] = self.objects[src_key]

    def delete_prefix(self, prefix: str) -> None:
        clean = prefix.strip("/") + "/"
        self.objects = {k: v for k, v in self.objects.items() if not k.startswith(clean)}


# ===========================================================================
# 1) Google Sheets helpers: URL/ID parsing, gviz URL, values -> DataFrame
# ===========================================================================
def test_v52_sheets_helpers():
    from app.engine.nodes.connectors import (
        NodeExecutionError,
        _extract_sheet_id,
        _gviz_url,
        _values_to_df,
    )

    # full URL with a gid: id + gid both picked up
    sid, gid = _extract_sheet_id("https://docs.google.com/spreadsheets/d/1AbC_deFG-hIJK/edit#gid=42")
    assert sid == "1AbC_deFG-hIJK"
    assert gid == 42
    # URL without gid
    sid2, gid2 = _extract_sheet_id("https://docs.google.com/spreadsheets/d/1AbC_deFG-hIJK/edit")
    assert sid2 == "1AbC_deFG-hIJK" and gid2 is None
    # bare id passthrough
    sid3, gid3 = _extract_sheet_id("  1AbC_deFG-hIJK  ")
    assert sid3 == "1AbC_deFG-hIJK" and gid3 is None
    # garbage refused
    try:
        _extract_sheet_id("not a sheet")
        raise AssertionError("expected refusal")
    except NodeExecutionError:
        pass

    # tab name wins over gid in the export URL; gid used when no tab
    url = _gviz_url("SID", 7, "Q4 Sales")
    assert "tqx=out:csv" in url and "sheet=Q4%20Sales" in url and "gid=" not in url
    url2 = _gviz_url("SID", 7, "")
    assert url2.endswith("gid=7")

    # Sheets values API -> DataFrame: ragged rows padded, empty header filled
    df = _values_to_df([["name", "age", ""], ["Ada", 36], ["Grace", 45, "extra"]])
    assert list(df.columns) == ["name", "age", "col_3"]
    assert len(df) == 2
    assert df.iloc[1]["age"] == 45
    # empty sheet -> empty frame, no crash
    assert len(_values_to_df([])) == 0


# ===========================================================================
# 2) google_sheets_source through the engine: public mode (fetch seam stubbed)
# ===========================================================================
def test_v52_sheets_public_mode():
    from app.engine.nodes import connectors as conn_mod

    csv_bytes = b"name,region,ltv\nAda,eu,120.5\nGrace,us,80.0\nAlan,eu,60.0\n"
    fetch_calls: list[tuple] = []

    async def _fake_fetch(sheet_id, gid, tab):
        # no asserts here - a raise inside a node task aborts the execution
        # before node_runs are recorded; capture and assert afterwards
        fetch_calls.append((sheet_id, gid, tab))
        return csv_bytes

    async def _go():
        async with _client() as client:
            graph = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("sh", "google_sheets_source", {
                        "sheet": "https://docs.google.com/spreadsheets/d/1AbC_deFG-hIJK/edit#gid=42",
                    }),
                ],
                "edges": [_edge("e1", "t", "sh")],
            }
            wf = await _make_workflow(client, f"v52-sheets-{uuid.uuid4().hex[:6]}", graph)
            run = await _run_and_wait(client, wf)
            assert run["status"] == "success", run.get("error")
            out = _find_node_run(run, "sh")["output"]
            assert out["row_count"] == 3
            assert out["mode"] == "public"
            assert out["sheet_id"] == "1AbC_deFG-hIJK"
            assert sorted(r["name"] for r in out["items"]) == ["Ada", "Alan", "Grace"]
            assert "region" in out["columns"]
            assert fetch_calls[-1] == ("1AbC_deFG-hIJK", 42, "")  # gid picked up from the URL

            # limit applies
            graph_lim = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("sh", "google_sheets_source", {"sheet": "1AbC_deFG-hIJK", "gid": 0, "limit": 2}),
                ],
                "edges": [_edge("e1", "t", "sh")],
            }
            wf2 = await _make_workflow(client, f"v52-sheets-lim-{uuid.uuid4().hex[:6]}", graph_lim)
            run2 = await _run_and_wait(client, wf2)
            assert run2["status"] == "success", run2.get("error")
            assert _find_node_run(run2, "sh")["output"]["row_count"] == 2
            assert fetch_calls[-1] == ("1AbC_deFG-hIJK", 0, "")  # explicit gid wins over nothing

            # HTTP failure surfaces as a clean node error
            async def _fail(sheet_id, gid, tab):
                from app.engine.nodes.base import NodeExecutionError

                raise NodeExecutionError("Google Sheets export returned HTTP 404")

            orig = conn_mod._fetch_public_csv
            conn_mod._fetch_public_csv = _fail
            try:
                wf3 = await _make_workflow(client, f"v52-sheets-404-{uuid.uuid4().hex[:6]}", graph)
                run3 = await _run_and_wait(client, wf3)
                assert run3["status"] == "error"
                assert "404" in str(_find_node_run(run3, "sh").get("error") or "")
            finally:
                conn_mod._fetch_public_csv = orig

    orig = conn_mod._fetch_public_csv
    conn_mod._fetch_public_csv = _fake_fetch
    try:
        asyncio.run(_go())
    finally:
        conn_mod._fetch_public_csv = orig
        asyncio.run(_drain_background())


# ===========================================================================
# 3) google_sheets_source service-account mode (vault + token + values stubbed)
# ===========================================================================
def test_v52_sheets_service_account():
    import app.services.crypto as crypto_mod
    from app.engine.nodes import connectors as conn_mod
    from app.engine.nodes.connectors import NodeExecutionError, _service_account_credentials

    values = [["name", "age"], ["Ada", "36"], ["Grace", "45"]]

    async def _fake_values(sheet_id, tab, token):
        assert token == "tok-123"
        return values

    async def _fake_decrypt(context, credential_id, owner_id=None):
        return {"type": "google_service_account", "json": '{"client_email": "sa@x.iam", "private_key": "K"}'}

    def _fake_refresh(credentials):
        # sync on purpose: the node runs it through asyncio.to_thread
        return "tok-123"

    def _fake_sa_creds(cred):
        # skip real google-auth key parsing - only the flow is under test here
        assert cred["type"] == "google_service_account"
        return {"stub": "credentials"}

    async def _go():
        async with _client() as client:
            graph = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("sh", "google_sheets_source", {
                        "sheet": "1PrivateSheet",
                        "mode": "service_account",
                        "credential_id": "cred-1",
                        "tab": "Data",
                    }),
                ],
                "edges": [_edge("e1", "t", "sh")],
            }
            wf = await _make_workflow(client, f"v52-sheets-sa-{uuid.uuid4().hex[:6]}", graph)
            run = await _run_and_wait(client, wf)
            assert run["status"] == "success", run.get("error")
            out = _find_node_run(run, "sh")["output"]
            assert out["mode"] == "service_account"
            assert out["row_count"] == 2
            assert out["items"][0]["age"] == "36"  # FORMATTED_VALUE strings

            # wrong credential type refused
            async def _decrypt_wrong(context, credential_id, owner_id=None):
                return {"type": "smtp", "host": "x"}

            orig_decrypt = crypto_mod.decrypt_credential
            crypto_mod.decrypt_credential = _decrypt_wrong
            try:
                wf2 = await _make_workflow(client, f"v52-sheets-badcred-{uuid.uuid4().hex[:6]}", graph)
                run2 = await _run_and_wait(client, wf2)
                assert run2["status"] == "error"
                assert "google_service_account" in str(_find_node_run(run2, "sh").get("error") or "")
            finally:
                crypto_mod.decrypt_credential = orig_decrypt

    # credential-shape validation without touching google-auth network calls
    try:
        _service_account_credentials({"type": "google_service_account"})
        raise AssertionError("expected refusal for empty credential")
    except NodeExecutionError:
        pass
    # a plain client_email/private_key pair is accepted as a minimal credential
    try:
        from unittest.mock import patch

        class _FakeCreds:
            def __init__(self, info, scopes):
                self.info = info

        with patch("google.oauth2.service_account.Credentials.from_service_account_info", _FakeCreds):
            built = _service_account_credentials({"client_email": "a@b.c", "private_key": "K"})
            assert built.info["client_email"] == "a@b.c"
    except ImportError:
        pass  # google-auth not installed in this env - SA mode would error cleanly

    orig_values, orig_decrypt, orig_refresh = conn_mod._fetch_sa_values, crypto_mod.decrypt_credential, conn_mod._refresh_sa_token
    orig_sa_creds = conn_mod._service_account_credentials
    conn_mod._fetch_sa_values = _fake_values
    crypto_mod.decrypt_credential = _fake_decrypt
    conn_mod._refresh_sa_token = _fake_refresh
    conn_mod._service_account_credentials = _fake_sa_creds
    try:
        asyncio.run(_go())
    finally:
        conn_mod._fetch_sa_values = orig_values
        crypto_mod.decrypt_credential = orig_decrypt
        conn_mod._refresh_sa_token = orig_refresh
        conn_mod._service_account_credentials = orig_sa_creds
        asyncio.run(_drain_background())


# ===========================================================================
# 4) ftp_source through the engine (connect seam stubbed, parse path real)
# ===========================================================================
def test_v52_ftp_source():
    from app.engine.nodes import connectors as conn_mod

    class FakeFtp:
        """Records the control flow; retrbinary streams the configured body."""

        def __init__(self, body: bytes = b"", fail: bool = False, cmd_fail: bool = False):
            self.body = body
            self.fail = fail
            self.cmd_fail = cmd_fail
            self.prot_p_called = False
            self.quit_called = False
            self.connect_args: tuple | None = None

        def connect(self, host, port, timeout=30):
            self.connect_args = (host, port, timeout)
            if self.fail:
                raise OSError("connection refused")

        def login(self, user, password):
            self.login_args = (user, password)

        def prot_p(self):
            self.prot_p_called = True

        def retrbinary(self, cmd, callback):
            if self.cmd_fail:
                from ftplib import error_perm

                raise error_perm("550 No such file")
            callback(self.body)

        def quit(self):
            self.quit_called = True

        def close(self):
            pass

    created: list[FakeFtp] = []

    def _install(body: bytes = b"", fail: bool = False, cmd_fail: bool = False) -> None:
        def _connect(host, port, username, password, secure, timeout):
            # mimics the REAL _ftp_connect contract: connect + login (+ prot_p)
            ftp = FakeFtp(body=body, fail=fail, cmd_fail=cmd_fail)
            created.append(ftp)
            ftp.connect(host, int(port), max(5, int(timeout)))
            ftp.login(username or "anonymous", password or "")
            if secure:
                ftp.prot_p()
            return ftp

        conn_mod._ftp_connect = _connect

    csv_body = b"name,age\nAda,36\nGrace,45\n"
    tsv_body = b"name\tage\nAda\t36\nGrace\t45\n"

    async def _run_graph(client: httpx.AsyncClient, params: dict, tag: str) -> dict:
        graph = {
            "nodes": [
                _node("t", "manual_trigger"),
                _node("ftp", "ftp_source", params),
            ],
            "edges": [_edge("e1", "t", "ftp")],
        }
        wf = await _make_workflow(client, f"v52-ftp-{tag}-{uuid.uuid4().hex[:6]}", graph)
        return await _run_and_wait(client, wf)

    async def _go():
        async with _client() as client:
            # 1) csv download
            _install(body=csv_body)
            run = await _run_graph(client, {
                "host": "ftp.example.com",
                "remote_path": "/exports/customers.csv",
                "username": "u",
                "password": "p",
            }, "csv")
            assert run["status"] == "success", run.get("error")
            out = _find_node_run(run, "ftp")["output"]
            assert out["row_count"] == 2 and out["fmt"] == "csv"
            assert out["host"] == "ftp.example.com" and out["path"] == "/exports/customers.csv"
            assert out["items"][1]["age"] == 45
            ftp1 = created[-1]
            assert ftp1.connect_args == ("ftp.example.com", 21, 30)
            assert ftp1.login_args == ("u", "p")
            assert ftp1.quit_called and not ftp1.prot_p_called

            # 2) tsv delimiter + FTPS flag reaches prot_p
            _install(body=tsv_body)
            run2 = await _run_graph(client, {
                "host": "ftp.example.com",
                "remote_path": "/exports/customers.tsv",
                "fmt": "tsv",
                "secure": True,
            }, "tsv")
            assert run2["status"] == "success", run2.get("error")
            out2 = _find_node_run(run2, "ftp")["output"]
            assert out2["row_count"] == 2 and out2["secure"] is True
            assert out2["items"][0]["age"] == 36
            assert created[-1].prot_p_called

            # 3) relative path refused
            _install()
            run3 = await _run_graph(client, {"host": "h", "remote_path": "exports/customers.csv"}, "bad")
            assert run3["status"] == "error"
            assert "absolute" in str(_find_node_run(run3, "ftp").get("error") or "")

            # 4) missing host refused before any network attempt
            _install()
            run4 = await _run_graph(client, {"remote_path": "/x.csv"}, "nohost")
            assert run4["status"] == "error"
            assert "host" in str(_find_node_run(run4, "ftp").get("error") or "").lower()

            # 5) connection failure surfaces as a clean node error
            _install(fail=True)
            run5 = await _run_graph(client, {"host": "dead.example.com", "remote_path": "/x.csv"}, "dead")
            assert run5["status"] == "error"
            assert "FTP download" in str(_find_node_run(run5, "ftp").get("error") or "")

            # 6) server 550 (file missing) surfaces with the FTP code
            _install(cmd_fail=True)
            run6 = await _run_graph(client, {"host": "ftp.example.com", "remote_path": "/gone.csv"}, "550")
            assert run6["status"] == "error"
            err = str(_find_node_run(run6, "ftp").get("error") or "")
            assert "FTP download" in err and "550" in err

    orig_connect = conn_mod._ftp_connect
    _install()
    try:
        asyncio.run(_go())
    finally:
        conn_mod._ftp_connect = orig_connect
        asyncio.run(_drain_background())


# ===========================================================================
# 5) registry: both connectors are live node types (UI forms auto-generate)
# ===========================================================================
def test_v52_registry_includes_connectors():
    from app.engine.registry import get_node_class

    assert get_node_class("google_sheets_source") is not None
    assert get_node_class("ftp_source") is not None

    async def _go():
        async with _client() as client:
            res = await client.get("/node-definitions")
            assert res.status_code == 200, res.text
            types = {d["type"] for d in res.json()["definitions"]}
            assert {"google_sheets_source", "ftp_source"} <= types, types

    asyncio.run(_go())


# ===========================================================================
# 6) storage migration: fake-backend routing, dry run, idempotence, cleanup
#    (service-level with an injected fake target; the endpoint e2e is test 7)
# ===========================================================================
def test_v52_migration_fake_target():
    from app.db import AsyncSessionLocal
    from app.services import storage_migration as mig_svc

    async def _go():
        target = FakeRemoteBackend()
        storage_svc.set_backend(LocalBackend())
        orig_build = mig_svc.build_target_backend
        mig_svc.build_target_backend = lambda cfg: target
        async with _client() as client:
            prefix = f"v52mig {uuid.uuid4().hex[:6]}"
            rows_in = [{"name": "Ada", "age": 36}, {"name": "Grace", "age": 45}]
            ds_id = await _mk_dataset(client, f"{prefix} users", rows_in)
            await client.post(f"/datasets/{ds_id}/rows", json={"rows": [{"name": "Alan", "age": 41}]})
            res = await client.get(f"/datasets/{ds_id}/versions")
            n_versions = len(res.json())
            assert n_versions >= 2

            live_bytes = ds_svc.read_file_bytes(ds_svc.parquet_path(ds_id))
            target_cfg = {"kind": "s3", "bucket": "fake", "prefix": "estate"}

            async def _migrate(**kw):
                async with AsyncSessionLocal() as session:
                    return await mig_svc.migrate_blobs(session, target_cfg=target_cfg, dataset_ids=[ds_id], **kw)

            try:
                # 6a) dry run: full plan, target untouched
                plan = await _migrate(dry_run=True)
                assert plan["dry_run"] is True
                assert plan["target"]["kind"] == "s3"
                assert plan["source"]["kind"] == "local"
                entry = next(d for d in plan["datasets"] if d["dataset_id"] == ds_id)
                assert entry["copied"] >= 2  # live + at least one version would move
                assert all(b["status"] == "would_copy" for b in entry["blobs"])
                assert target.objects == {}, "dry run must not write"

                # 6b) real run: bytes land on the target, identical
                body = await _migrate()
                entry = next(d for d in body["datasets"] if d["dataset_id"] == ds_id)
                assert entry["copied"] >= 2 and entry["missing"] == 0
                assert body["summary"]["bytes_copied"] > 0
                assert "PY8N_STORAGE_BACKEND" in body["cutover_hint"]
                # the fake ignores prefixes (real S3Backend._key prefixing is
                # proven by the moto test below); the raw key holds the bytes
                assert target.objects[f"{ds_id}.parquet"] == live_bytes
                assert f"versions/{ds_id}/v1.parquet" in target.objects

                # 6c) idempotent re-run: everything already there -> skipped
                body2 = await _migrate()
                entry2 = next(d for d in body2["datasets"] if d["dataset_id"] == ds_id)
                assert entry2["copied"] == 0
                assert entry2["skipped"] == entry["copied"]
                assert all(b["status"] == "skipped" for b in entry2["blobs"])

                # 6d) overwrite forces a re-copy
                body3 = await _migrate(overwrite=True)
                entry3 = next(d for d in body3["datasets"] if d["dataset_id"] == ds_id)
                assert entry3["copied"] >= 2 and entry3["skipped"] == 0

                # 6e) move: source blob removed AFTER a verified copy
                body4 = await _migrate(overwrite=True, delete_source=True)
                assert body4["summary"]["blobs_copied"] >= 2
                assert not ds_svc.file_exists(ds_svc.parquet_path(ds_id))
                assert target.objects[f"{ds_id}.parquet"] == live_bytes

                # 6f) missing blob: reported, never fatal
                body5 = await _migrate()
                entry5 = next(d for d in body5["datasets"] if d["dataset_id"] == ds_id)
                assert entry5["missing"] >= 2 and entry5["copied"] == 0
            finally:
                mig_svc.build_target_backend = orig_build

            # API-level refusals (real build_target_backend): unknown dataset -> 400
            res6 = await client.post("/storage/migrate", json={
                "target": {"kind": "s3", "bucket": "fake"},
                "dataset_ids": ["nope-not-real"],
            })
            assert res6.status_code == 400, res6.text
            # unknown target kind -> 400
            res7 = await client.post("/storage/migrate", json={
                "target": {"kind": "dropbox", "bucket": "x"},
                "dataset_ids": [ds_id],
            })
            assert res7.status_code == 400, res7.text

            await client.delete(f"/datasets/{ds_id}")

    try:
        asyncio.run(_go())
    finally:
        storage_svc.set_backend(None)
        asyncio.run(_drain_background())


# ===========================================================================
# 7) storage migration: real S3 surface via moto (local -> S3 copy-over)
# ===========================================================================
def test_v52_migration_moto_s3():
    import boto3
    from moto import mock_aws

    async def _go():
        storage_svc.set_backend(LocalBackend())
        with mock_aws():
            s3 = boto3.client("s3", region_name="us-east-1")
            s3.create_bucket(Bucket="py8n-migrate")

            async with _client() as client:
                prefix = f"v52moto {uuid.uuid4().hex[:6]}"
                ds_id = await _mk_dataset(client, f"{prefix} invoices", [{"id": 1, "amt": 250}, {"id": 2, "amt": 90}])
                live_bytes = ds_svc.read_file_bytes(ds_svc.parquet_path(ds_id))

                res = await client.post("/storage/migrate", json={
                    "target": {
                        "kind": "s3",
                        "bucket": "py8n-migrate",
                        "prefix": "tenants/acme",
                        "region": "us-east-1",
                        "access_key_id": "test",
                        "secret_access_key": "test",
                    },
                    "dataset_ids": [ds_id],
                })
                assert res.status_code == 200, res.text
                body = res.json()
                entry = next(d for d in body["datasets"] if d["dataset_id"] == ds_id)
                assert entry["copied"] >= 1 and entry["missing"] == 0

                obj = s3.get_object(Bucket="py8n-migrate", Key=f"tenants/acme/{ds_id}.parquet")
                assert obj["Body"].read() == live_bytes

                # MinIO-style endpoint_url target is accepted (listable shape)
                res2 = await client.post("/storage/migrate", json={
                    "target": {"kind": "minio", "bucket": "py8n-migrate", "endpoint_url": "http://minio:9000"},
                    "dataset_ids": [ds_id],
                    "dry_run": True,
                })
                # minio without reachable endpoint still plans from the SOURCE side only
                assert res2.status_code in (200, 502), res2.text

                await client.delete(f"/datasets/{ds_id}")

    try:
        asyncio.run(_go())
    finally:
        storage_svc.set_backend(None)
        asyncio.run(_drain_background())


# ===========================================================================
# 8) delivery validation: shape, addresses, limits
# ===========================================================================
def test_v52_delivery_validation():
    # None/empty = artifact-only
    assert report_svc.validate_delivery(None) is None
    assert report_svc.validate_delivery({}) is None
    assert report_svc.validate_delivery({"channels": []}) is None

    # webhook normalization: headers stringified, attachment default off
    norm = report_svc.validate_delivery({"channels": [
        {"type": "webhook", "url": "https://hooks.example/x", "headers": {"X-K": 5}},
    ]})
    assert norm["channels"][0] == {
        "type": "webhook", "url": "https://hooks.example/x", "headers": {"X-K": "5"}, "include_attachment": False,
    }

    # email: comma string accepted, cc optional, attachment default ON
    norm2 = report_svc.validate_delivery({"channels": [
        {"type": "email", "to": "a@b.c, d@e.f "},
    ]})
    assert norm2["channels"][0]["to"] == ["a@b.c", "d@e.f"]
    assert norm2["channels"][0]["cc"] == []
    assert norm2["channels"][0]["include_attachment"] is True

    # failures
    for bad in [
        {"channels": [{"type": "webhook", "url": "ftp://nope"}]},
        {"channels": [{"type": "webhook"}]},
        {"channels": [{"type": "email", "to": []}]},
        {"channels": [{"type": "email", "to": "not-an-email"}]},
        {"channels": [{"type": "carrier_pigeon", "to": "a@b.c"}]},
        {"channels": [{"type": "email", "to": "a@b.c"}] * 5},
        "not-a-dict",
    ]:
        try:
            report_svc.validate_delivery(bad)
            raise AssertionError(f"expected refusal for {bad!r}")
        except ValueError:
            pass

    # API view never raises on corrupt rows
    assert report_svc.delivery_out({"channels": "garbage"}) == {"channels": []}


# ===========================================================================
# 9) report delivery e2e: run fires channels, events land, failures isolated
# ===========================================================================
def test_v52_report_delivery_e2e():
    captured: list[dict] = []
    sent_emails: list[dict] = []

    async def _capture_webhook(url, headers, payload, timeout):
        captured.append({"url": url, "headers": headers, "payload": payload, "timeout": timeout})
        return True, "HTTP 200"

    def _capture_email(**kwargs):
        sent_emails.append(kwargs)

    def _boom(**kwargs):
        raise RuntimeError("smtp relay exploded")

    async def _go():
        async with _client() as client:
            tag = uuid.uuid4().hex[:6]
            ds_id = await _mk_dataset(client, f"v52rep {tag} data", [{"k": "a", "v": 1}, {"k": "b", "v": 2}])

            delivery = {
                "channels": [
                    {"type": "webhook", "url": "https://hooks.example/py8n", "include_attachment": True},
                    {"type": "email", "to": "ops@b.c, fin@b.c", "cc": "boss@b.c"},
                ]
            }
            res = await client.post("/reports", json={
                "name": f"v52 weekly {tag}",
                "source_type": "dataset",
                "source_id": ds_id,
                "fmt": "csv",
                "cron": "0 6 * * *",
                "delivery": delivery,
            })
            assert res.status_code == 201, res.text
            report = res.json()
            rid = report["id"]
            assert report["delivery"]["channels"][0]["type"] == "webhook"
            assert report["delivery"]["channels"][1]["to"] == ["ops@b.c", "fin@b.c"]

            # capture BOTH senders and fake a reachable SMTP host
            orig_hook = report_svc._post_webhook
            orig_send = report_svc._send_report_email
            orig_host = settings.smtp_host
            report_svc._post_webhook = _capture_webhook
            report_svc._send_report_email = _capture_email
            settings.smtp_host = "smtp.test"
            try:
                await _run_flow(client, ds_id, rid)
            finally:
                report_svc._post_webhook = orig_hook
                report_svc._send_report_email = orig_send
                settings.smtp_host = orig_host

            # create with invalid delivery -> 400
            bad = await client.post("/reports", json={
                "name": f"v52 bad {tag}",
                "source_type": "dataset",
                "source_id": ds_id,
                "fmt": "csv",
                "cron": "0 6 * * *",
                "delivery": {"channels": [{"type": "email", "to": []}]},
            })
            assert bad.status_code == 400, bad.text

            # PUT replaces delivery; empty clears back to artifact-only
            put = await client.put(f"/reports/{rid}", json={"delivery": None})
            assert put.status_code == 200, put.text
            assert put.json()["delivery"]["channels"] == []

            # unknown report -> 404
            assert (await client.get("/reports/nope/deliveries")).status_code == 404

            await client.delete(f"/reports/{rid}")
            await client.delete(f"/datasets/{ds_id}")

    async def _run_flow(client: httpx.AsyncClient, ds_id: str, rid: str) -> None:
        # run NOW: both channels deliver (webhook captured, email captured)
        run_res = await client.post(f"/reports/{rid}/run")
        assert run_res.status_code == 200, run_res.text
        result = run_res.json()["run"]
        assert result["ok"] is True
        delivered = result["delivery"]
        assert [d["status"] for d in delivered] == ["ok", "ok"]
        assert delivered[0]["channel"] == "webhook" and delivered[0]["attached"] is True
        assert delivered[1]["detail"] == "sent to 2 recipient(s) + 1 cc"

        # webhook envelope: event name, artifact id, base64 csv inline
        assert captured[0]["payload"]["event"] == "py8n.report.completed"
        assert captured[0]["payload"]["artifact"]["id"] == result["artifact_id"]
        assert captured[0]["headers"]["X-Py8n-Event"] == "report.completed"
        inline = base64.b64decode(captured[0]["payload"]["artifact"]["data_base64"])
        assert b"k,v" in inline and b"b,2" in inline

        # email: attachment on by default, default subject stamped
        mail = sent_emails[0]
        assert mail["to"] == ["ops@b.c", "fin@b.c"]
        assert mail["cc"] == ["boss@b.c"]
        assert mail["attach"] is True and mail["data"] == inline
        assert "[py8n]" in mail["subject"]

        # delivery trail readable
        hist = await client.get(f"/reports/{rid}/deliveries")
        assert hist.status_code == 200, hist.text
        events = hist.json()["events"]
        assert [e["status"] for e in events] == ["ok", "ok"]
        assert all(e["artifact_id"] == result["artifact_id"] for e in events)

        # email relay dies -> event 'error', run STILL ok, webhook unaffected
        report_svc._send_report_email = _boom
        try:
            run2 = await client.post(f"/reports/{rid}/run")
            assert run2.status_code == 200, run2.text
            r2 = run2.json()["run"]
            assert r2["ok"] is True
            assert [d["status"] for d in r2["delivery"]] == ["ok", "error"]
            assert "smtp relay exploded" in r2["delivery"][1]["detail"]
        finally:
            report_svc._send_report_email = _capture_email

        hist2 = await client.get(f"/reports/{rid}/deliveries")
        statuses = [e["status"] for e in hist2.json()["events"]]
        assert statuses[0] == "error" and "ok" in statuses

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


# ===========================================================================
# 10) email skipped (not errored) when SMTP is unconfigured; oversize omitted
# ===========================================================================
def test_v52_email_skip_and_attachment_cap():
    sent_emails: list[dict] = []

    async def _go():
        async with _client() as client:
            tag = uuid.uuid4().hex[:6]
            ds_id = await _mk_dataset(client, f"v52skip {tag} data", [{"k": "a", "v": 1}])

            res = await client.post("/reports", json={
                "name": f"v52 skip {tag}",
                "source_type": "dataset",
                "source_id": ds_id,
                "fmt": "json",
                "cron": "0 7 * * *",
                "delivery": {"channels": [{"type": "email", "to": "ops@b.c"}]},
            })
            rid = res.json()["id"]

            # SMTP host empty -> clean 'skipped' event, run stays ok
            assert not settings.smtp_host
            run_res = await client.post(f"/reports/{rid}/run")
            assert run_res.status_code == 200
            delivered = run_res.json()["run"]["delivery"]
            assert delivered[0]["status"] == "skipped"
            assert "PY8N_SMTP_HOST" in delivered[0]["detail"]

            # configured + attachment over the cap -> sends WITHOUT the file
            orig_host = settings.smtp_host
            orig_cap = settings.max_delivery_attachment_bytes
            settings.smtp_host = "smtp.internal"
            settings.max_delivery_attachment_bytes = 4  # absurdly small -> skip attach
            report_svc._send_report_email = (lambda **kw: sent_emails.append(kw))
            try:
                run2 = await client.post(f"/reports/{rid}/run")
                r2 = run2.json()["run"]
                assert r2["delivery"][0]["status"] == "ok"
                assert sent_emails and sent_emails[-1]["attach"] is False
                assert sent_emails[-1]["data"] == b""
            finally:
                settings.smtp_host = orig_host
                settings.max_delivery_attachment_bytes = orig_cap
                # restore original function reference
                report_svc._send_report_email = test_v52_email_skip_and_attachment_cap._orig_send  # type: ignore

            hist = await client.get(f"/reports/{rid}/deliveries")
            statuses = [e["status"] for e in hist.json()["events"]]
            assert statuses[0] == "ok" and "skipped" in statuses

            await client.delete(f"/reports/{rid}")
            await client.delete(f"/datasets/{ds_id}")

    test_v52_email_skip_and_attachment_cap._orig_send = report_svc._send_report_email  # type: ignore
    try:
        asyncio.run(_go())
    finally:
        report_svc._send_report_email = test_v52_email_skip_and_attachment_cap._orig_send  # type: ignore
        asyncio.run(_drain_background())
