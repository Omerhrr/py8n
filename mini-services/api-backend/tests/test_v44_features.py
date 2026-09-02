"""V44 feature tests: dataset versioning, notification rules, retention
artifact sweep, tags + folders deepening.

New machinery:
    dataset_versions              every mutation (create/append/replace/restore)
                                  snapshots the parquet; cap 20 per dataset;
                                  restore records the restored state as a new
                                  version so a rollback is undoable
    /datasets/{id}/versions       list (newest first, newest == current),
                                  rows preview, restore, delete one snapshot
    /notifications CRUD + test    webhook-on-event rules (execution_succeeded
                                  / _failed / _cancelled); dispatch is
                                  fire-and-forget from the executor
    retention purge sweep         artifacts orphaned by purged executions are
                                  deleted rows AND files
    /tags inventory/rename/delete one tag vocabulary across workflows + datasets
    /folders/{id}/move            bulk move with packs-style skip-with-reasons

Same harness as v4-v43: httpx ASGITransport in-process, per-test asyncio.run,
uuid-suffixed data, finally-cleanup + background drain. Notification delivery
is verified against a real local HTTP server on an ephemeral port.
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

from app.main import app

API = "http://testserver/api/v1"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


async def _drain_background() -> None:
    from app.services import executor as executor_mod

    tasks = [t for t in executor_mod._background_tasks if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _node(nid: str, ntype: str, params: dict | None = None, name: str | None = None) -> dict:
    return {"id": nid, "type": ntype, "name": name or nid, "position": {"x": 0, "y": 0}, "parameters": params or {}}


async def _register(client: httpx.AsyncClient, email: str, password: str = "s3cret-pw!") -> dict:
    res = await client.post("/auth/register", json={"email": email, "password": password, "name": "V44 Test"})
    assert res.status_code == 201, res.text
    return res.json()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _wipe_users_and_ownership() -> None:
    from sqlalchemy import delete, update

    from app.db import AsyncSessionLocal
    from app.models import ApiKey, App, Credential, Dashboard, Dataset, EnvVariable, Folder, User, Workflow

    async with AsyncSessionLocal() as session:
        await session.execute(delete(ApiKey))
        await session.execute(delete(User))
        for model in (Workflow, Dataset, Folder, Credential, EnvVariable, App, Dashboard):
            await session.execute(update(model).values(owner_id=None))
        await session.commit()


async def _run_and_wait(client: httpx.AsyncClient, wf_id: str) -> dict:
    res = await client.post(f"/workflows/{wf_id}/run", json={"payload": {}})
    assert res.status_code == 200, res.text
    exec_id = res.json()["execution_id"]
    for _ in range(120):
        run = (await client.get(f"/executions/{exec_id}")).json()
        if run["status"] != "running":
            return run
        await asyncio.sleep(0.25)
    raise AssertionError("execution did not finish in time")


class _WebhookReceiver(BaseHTTPRequestHandler):
    captured: list[dict] = []

    def do_POST(self):  # noqa: N802 (stdlib naming)
        length = int(self.headers.get("content-length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = {"raw": True}
        self.__class__.captured.append({"body": body, "headers": {k.lower(): v for k, v in self.headers.items()}})
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):  # silence
        pass


def _start_receiver() -> tuple[HTTPServer, str]:
    server = HTTPServer(("127.0.0.1", 0), _WebhookReceiver)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _WebhookReceiver.captured = []
    return server, f"http://127.0.0.1:{server.server_address[1]}/hook"


# ------------------------------------------------------------------ test 1
def test_v44_health_pin():
    """Strict version pin lives in the latest wave only (convention)."""

    async def _go():
        async with _client() as client:
            return (await client.get("/health")).json()

    body = asyncio.run(_go())
    assert body["app"] == "Py8n"
    assert body["version"] >= "1.44.0", f"expected >= 1.44.0, got {body['version']}"


# ------------------------------------------------------------------ test 2
def test_v44_dataset_versions_lifecycle():
    """create -> append -> replace builds a timeline; preview + restore +
    delete work; the cap prunes the oldest snapshots."""
    tag = uuid.uuid4().hex[:8]
    ds_ids: list[str] = []

    async def _go():
        from app.services import datasets as ds_svc

        async with _client() as client:
            res = await client.post("/datasets", json={"name": f"v44 vers {tag}", "rows": [{"city": "lima", "t": 19}], "tags": ["v44tag"]})
            assert res.status_code == 201, res.text
            ds = res.json()
            ds_ids.append(ds["id"])
            assert ds["tags"] == ["v44tag"]
            did = ds["id"]

            # v1 landed on create
            versions = (await client.get(f"/datasets/{did}/versions")).json()
            assert [v["version"] for v in versions] == [1], versions
            assert versions[0]["current"] is True and versions[0]["source"] == "api"

            await client.post(f"/datasets/{did}/rows", json={"rows": [{"city": "oslo", "t": 4}]})

            # v2 landed on append
            versions = (await client.get(f"/datasets/{did}/versions")).json()
            assert [v["version"] for v in versions] == [2, 1], versions
            assert versions[0]["source"] == "append" and versions[0]["row_count"] == 2

            # service-level replace snapshots v3
            from app.db import AsyncSessionLocal
            from app.models import Dataset

            async with AsyncSessionLocal() as session:
                ds_row = await session.get(Dataset, did)
                await ds_svc.replace_rows(session, ds_row, [{"city": "paris", "t": 12}])
                await session.commit()

            versions = (await client.get(f"/datasets/{did}/versions")).json()
            assert [v["version"] for v in versions] == [3, 2, 1], versions
            assert versions[0]["source"] == "replace" and versions[0]["current"] is True
            assert versions[1]["current"] is False

            # preview an old snapshot without restoring
            preview = (await client.get(f"/datasets/{did}/versions/1/rows")).json()
            assert preview["rows"] == [{"city": "lima", "t": 19}], preview
            assert (await client.get(f"/datasets/{did}/versions/99/rows")).status_code == 404

            # restore v1: content rolls back AND a v4 (source=restore) appears
            restored = await client.post(f"/datasets/{did}/versions/1/restore")
            assert restored.status_code == 200, restored.text
            rows_now = (await client.get(f"/datasets/{did}/rows")).json()
            assert len(rows_now["rows"]) == 1 and rows_now["rows"][0]["city"] == "lima"
            versions = (await client.get(f"/datasets/{did}/versions")).json()
            assert versions[0]["version"] == 4 and versions[0]["source"] == "restore", versions

            # delete a snapshot (not the current one)
            res = await client.delete(f"/datasets/{did}/versions/2")
            assert res.status_code == 204, res.text
            versions = (await client.get(f"/datasets/{did}/versions")).json()
            assert 2 not in [v["version"] for v in versions]
            assert not ds_svc.version_file(did, 2).exists()

            # cap: with MAX=3, more appends prune the oldest versions
            ds_svc.MAX_DATASET_VERSIONS = 3
            try:
                for i in range(3):
                    await client.post(f"/datasets/{did}/rows", json={"rows": [{"city": f"bulk{i}", "t": i}]})
                versions = (await client.get(f"/datasets/{did}/versions")).json()
                assert len(versions) == 3, versions
                assert 1 not in [v["version"] for v in versions], "oldest pruned"
                assert versions[0]["current"] is True
            finally:
                ds_svc.MAX_DATASET_VERSIONS = 20

    try:
        asyncio.run(_go())
    finally:
        async def _cleanup():
            async with _client() as client:
                for did in ds_ids:
                    await client.delete(f"/datasets/{did}")
        asyncio.run(_cleanup())


def test_v44_dataset_versions_die_with_dataset():
    """Deleting the dataset removes every snapshot file."""
    tag = uuid.uuid4().hex[:8]

    async def _go():
        from app.services import datasets as ds_svc

        async with _client() as client:
            res = await client.post("/datasets", json={"name": f"v44 die {tag}", "rows": [{"x": 1}]})
            did = res.json()["id"]
            await client.post(f"/datasets/{did}/rows", json={"rows": [{"x": 2}]})
            assert ds_svc.version_dir(did).exists() and ds_svc.version_file(did, 1).exists()

            res = await client.delete(f"/datasets/{did}")
            assert res.status_code == 204, res.text
            assert not ds_svc.version_dir(did).exists(), "versions dir must be removed"

    asyncio.run(_go())


# ------------------------------------------------------------------ test 3
def test_v44_notification_rules_end_to_end():
    """Rules fire webhooks on real runs (failed + succeeded), honour scoping,
    headers and enable-state; the test endpoint delivers synchronously."""
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []
    rule_ids: list[str] = []

    async def _go():
        from app.services import notifications as notif_svc

        server, url = _start_receiver()
        try:
            async with _client() as client:
                # a workflow that always fails + one that always succeeds
                bad = await client.post("/workflows", json={
                    "name": f"v44 boom {tag}",
                    "graph": {"nodes": [_node("t", "manual_trigger"), _node("c", "code", {"code": "result = 1 / 0"})],
                              "edges": [{"id": "e1", "source": "t", "target": "c", "sourceHandle": "main", "targetHandle": "main"}]},
                })
                assert bad.status_code == 201, bad.text
                bad_id = bad.json()["id"]
                wf_ids.append(bad_id)

                good = await client.post("/workflows", json={
                    "name": f"v44 ok {tag}",
                    "graph": {"nodes": [_node("t", "manual_trigger")], "edges": []},
                })
                assert good.status_code == 201, good.text
                good_id = good.json()["id"]
                wf_ids.append(good_id)

                # validation first
                r = await client.post("/notifications", json={"name": "bad event", "events": ["nope"], "webhook_url": url})
                assert r.status_code == 400, r.text
                r = await client.post("/notifications", json={"name": "bad url", "events": ["execution_failed"], "webhook_url": "ftp://x"})
                assert r.status_code == 400, r.text
                r = await client.post("/notifications", json={"name": "bad wf", "events": ["execution_failed"], "webhook_url": url, "workflow_id": "missing"})
                assert r.status_code == 404, r.text

                # failed-runs rule scoped to the bad workflow, with a header
                r = await client.post("/notifications", json={
                    "name": f"v44 fail rule {tag}", "events": ["execution_failed"],
                    "webhook_url": url, "headers": {"X-Token": "s3cret"}, "workflow_id": bad_id,
                })
                assert r.status_code == 201, r.text
                rule_ids.append(r.json()["id"])
                assert r.json()["fire_count"] == 0

                events = (await client.get("/notifications/events")).json()
                assert set(events["events"]) >= {"execution_succeeded", "execution_failed", "execution_cancelled", "drift_detected"}  # v48: +drift

                run = await _run_and_wait(client, bad_id)
                assert run["status"] == "error", run
                await notif_svc.adrain_pending()
                assert len(_WebhookReceiver.captured) == 1, _WebhookReceiver.captured
                hit = _WebhookReceiver.captured[0]
                assert hit["body"]["event"] == "execution_failed"
                assert hit["body"]["workflow_id"] == bad_id
                assert hit["body"]["status"] == "error"
                assert hit["headers"].get("x-token") == "s3cret"
                listing = (await client.get("/notifications")).json()
                assert listing[0]["fire_count"] == 1 and listing[0]["last_status"] == "ok"

                # a successful run must NOT trip the failed-only rule
                _WebhookReceiver.captured.clear()
                await _run_and_wait(client, good_id)
                await notif_svc.adrain_pending()
                assert _WebhookReceiver.captured == [], "success fired a failed-only rule"

                # success rule + disabled rule
                r2 = await client.post("/notifications", json={
                    "name": f"v44 ok rule {tag}", "events": ["execution_succeeded"], "webhook_url": url, "enabled": False,
                })
                rule_ids.append(r2.json()["id"])
                await client.put(f"/notifications/{rule_ids[-1]}", json={"enabled": True})
                await _run_and_wait(client, good_id)
                await notif_svc.adrain_pending()
                assert len(_WebhookReceiver.captured) == 1 and _WebhookReceiver.captured[0]["body"]["event"] == "execution_succeeded"

                # synchronous test fire
                _WebhookReceiver.captured.clear()
                tr = await client.post(f"/notifications/{rule_ids[-1]}/test")
                assert tr.status_code == 200 and tr.json()["ok"] is True, tr.text
                assert _WebhookReceiver.captured[0]["body"]["event"] == "test"

        finally:
            server.shutdown()

    try:
        asyncio.run(_go())
    finally:
        async def _cleanup():
            async with _client() as client:
                for rid in rule_ids:
                    await client.delete(f"/notifications/{rid}")
                for wid in wf_ids:
                    await client.delete(f"/workflows/{wid}")
        asyncio.run(_cleanup())
        asyncio.run(_drain_background())


def test_v44_notification_cross_user_guard():
    """Alice's catch-all rule never fires on bob's runs."""
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []
    rule_ids: list[str] = []

    async def _go():
        from app.services import notifications as notif_svc

        server, url = _start_receiver()
        try:
            async with _client() as client:
                alice = await _register(client, f"v44-alice-{tag}@py8n.test")
                bob = await _register(client, f"v44-bob-{tag}@py8n.test")

                wf = await client.post("/workflows", json={
                    "name": f"v44 bob wf {tag}", "graph": {"nodes": [_node("t", "manual_trigger")], "edges": []},
                }, headers=_auth(bob["token"]))
                assert wf.status_code == 201, wf.text
                bob_wf = wf.json()
                wf_ids.append(bob_wf["id"])

                r = await client.post("/notifications", json={
                    "name": "alice rule", "events": ["execution_succeeded"], "webhook_url": url,
                }, headers=_auth(alice["token"]))
                rule_ids.append(r.json()["id"])
                assert r.json()["workflow_name"] is None

                await _run_and_wait(client, bob_wf["id"])
                await notif_svc.adrain_pending()
                assert _WebhookReceiver.captured == [], "cross-user dispatch leaked"

                # alice's own run fires her rule
                wf = await client.post("/workflows", json={
                    "name": f"v44 alice wf {tag}", "graph": {"nodes": [_node("t", "manual_trigger")], "edges": []},
                }, headers=_auth(alice["token"]))
                alice_wf = wf.json()
                wf_ids.append(alice_wf["id"])
                await _run_and_wait(client, alice_wf["id"])
                await notif_svc.adrain_pending()
                assert len(_WebhookReceiver.captured) == 1

                # bob cannot see or delete alice's rule
                assert (await client.put(f"/notifications/{rule_ids[0]}", json={"enabled": False}, headers=_auth(bob["token"]))).status_code == 404
                assert (await client.delete(f"/notifications/{rule_ids[0]}", headers=_auth(bob["token"]))).status_code == 404

        finally:
            server.shutdown()

    try:
        asyncio.run(_go())
    finally:
        async def _cleanup():
            async with _client() as client:
                for rid in rule_ids:
                    await client.delete(f"/notifications/{rid}")
                for wid in wf_ids:
                    await client.delete(f"/workflows/{wid}")
                await _wipe_users_and_ownership()
        asyncio.run(_cleanup())
        asyncio.run(_drain_background())


# ------------------------------------------------------------------ test 4
def test_v44_retention_artifact_sweep():
    """Purging executions now also removes their orphaned artifacts (rows + files)."""
    from pathlib import Path

    from app.db import AsyncSessionLocal
    from app.models import Artifact, ExecutionLog
    from app.services import artifacts as art_svc
    from app.services import retention as ret_svc

    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []

    async def _go():
        old = datetime.now(timezone.utc) - timedelta(days=365)
        art_file: Path | None = None

        # ExecutionLog.workflow_id is NOT NULL - anchor the fake run to a real workflow
        async with _client() as client:
            wf = await client.post("/workflows", json={
                "name": f"v44 purge anchor {tag}", "graph": {"nodes": [_node("t", "manual_trigger")], "edges": []},
            })
            assert wf.status_code == 201, wf.text
            wf_id = wf.json()["id"]
            wf_ids.append(wf_id)

        async with AsyncSessionLocal() as session:
            log = ExecutionLog(id=f"v44purge{uuid.uuid4().hex[:8]}", workflow_id=wf_id, status="error",
                               trigger_type="manual", started_at=old, finished_at=old)
            session.add(log)
            await session.flush()
            art = Artifact(kind="chart", filename="purge.png", content_type="image/png", size_bytes=3,
                           execution_id=log.id, meta={})
            session.add(art)
            await session.flush()
            art_file = art_svc.artifact_path(art.id, art.content_type)
            art_file.parent.mkdir(parents=True, exist_ok=True)
            art_file.write_bytes(b"png")
            exec_id, art_id = log.id, art.id
            await session.commit()

        result = await ret_svc.purge_execution_data()
        assert result["artifacts_deleted"] >= 1, result

        async with AsyncSessionLocal() as session:
            assert await session.get(ExecutionLog, exec_id) is None, "old execution purged"
            assert await session.get(Artifact, art_id) is None, "orphan artifact row gone"
        assert art_file is not None and not art_file.exists(), "artifact file swept"

    try:
        asyncio.run(_go())
    finally:
        async def _cleanup():
            async with _client() as client:
                for wid in wf_ids:
                    await client.delete(f"/workflows/{wid}")
        asyncio.run(_cleanup())


# ------------------------------------------------------------------ test 5
def test_v44_tags_inventory_rename_delete():
    """One tag vocabulary across workflows + datasets; rename sweeps both."""
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []
    ds_ids: list[str] = []

    async def _go():
        async with _client() as client:
            wf = await client.post("/workflows", json={
                "name": f"v44 tagged wf {tag}", "graph": {"nodes": [], "edges": []}, "tags": ["alpha", "beta"],
            })
            assert wf.status_code == 201, wf.text
            wf_ids.append(wf.json()["id"])

            ds = await client.post("/datasets", json={"name": f"v44 tagged ds {tag}", "rows": [{"x": 1}], "tags": ["beta", "gamma"]})
            assert ds.status_code == 201, ds.text
            ds_ids.append(ds.json()["id"])

            inv = (await client.get("/tags")).json()
            by_name = {e["name"]: e for e in inv}
            assert by_name["alpha"]["workflows"] == 1 and by_name["alpha"]["datasets"] == 0
            assert by_name["beta"]["workflows"] == 1 and by_name["beta"]["datasets"] == 1
            assert by_name["gamma"]["datasets"] == 1

            # dataset tag filter
            filtered = (await client.get("/datasets", params={"tag": "BETA"})).json()
            assert [d["id"] for d in filtered] == ds_ids

            # rename beta -> delta sweeps both kinds
            ren = await client.put("/tags/rename", json={"from": "beta", "to": "delta"})
            assert ren.status_code == 200 and ren.json()["workflows"] == 1 and ren.json()["datasets"] == 1, ren.text
            inv = {e["name"]: e for e in (await client.get("/tags")).json()}
            assert "beta" not in inv and inv["delta"]["workflows"] == 1 and inv["delta"]["datasets"] == 1
            wf_tags = (await client.get(f"/workflows/{wf_ids[0]}")).json()["tags"]
            assert wf_tags == ["alpha", "delta"], wf_tags

            # deleting gamma leaves the dataset tagless
            dele = await client.delete("/tags/gamma")
            assert dele.status_code == 200 and dele.json()["datasets"] == 1, dele.text
            ds_tags = (await client.get(f"/datasets/{ds_ids[0]}")).json()["tags"]
            assert ds_tags == ["delta"], ds_tags

            # PUT tags [] clears; tag rename to an existing tag merges (dedupe)
            await client.put(f"/datasets/{ds_ids[0]}", json={"tags": []})
            assert (await client.get(f"/datasets/{ds_ids[0]}")).json()["tags"] == []
            ren = await client.put("/tags/rename", json={"from": "delta", "to": "ALPHA"})
            assert ren.status_code == 200, ren.text
            wf_tags = (await client.get(f"/workflows/{wf_ids[0]}")).json()["tags"]
            assert wf_tags.count("alpha") + wf_tags.count("ALPHA") == 1, "merge dedupes"

    try:
        asyncio.run(_go())
    finally:
        async def _cleanup():
            async with _client() as client:
                for wid in wf_ids:
                    await client.delete(f"/workflows/{wid}")
                for did in ds_ids:
                    await client.delete(f"/datasets/{did}")
        asyncio.run(_cleanup())


# ------------------------------------------------------------------ test 6
def test_v44_folder_bulk_move():
    """Bulk move with skip-with-reasons; root unfiles; foreign folders 404."""
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []
    folder_ids: list[str] = []

    async def _go():
        async with _client() as client:
            alice = await _register(client, f"v44-fold-a-{tag}@py8n.test")
            bob = await _register(client, f"v44-fold-b-{tag}@py8n.test")
            a = _auth(alice["token"])
            b = _auth(bob["token"])

            folder = (await client.post("/folders", json={"name": f"v44 folder {tag}"}, headers=a)).json()
            folder_ids.append(folder["id"])

            wf_ids.clear()
            for i in range(3):
                wf = (await client.post("/workflows", json={
                    "name": f"v44 move wf {i} {tag}", "graph": {"nodes": [], "edges": []},
                }, headers=a)).json()
                wf_ids.append(wf["id"])

            res = await client.post(f"/folders/{folder['id']}/move",
                                    json={"workflow_ids": [wf_ids[0], wf_ids[1], "bogus-id"]}, headers=a)
            assert res.status_code == 200, res.text
            body = res.json()
            assert len(body["moved"]) == 2 and len(body["skipped"]) == 1
            assert body["skipped"][0]["reason"] == "workflow not found"
            for wid in wf_ids[:2]:
                assert (await client.get(f"/workflows/{wid}", headers=a)).json()["folder_id"] == folder["id"]

            # root unfiles
            res = await client.post("/folders/root/move", json={"workflow_ids": [wf_ids[0]]}, headers=a)
            assert res.status_code == 200 and res.json()["moved"][0]["id"] == wf_ids[0]
            assert (await client.get(f"/workflows/{wf_ids[0]}", headers=a)).json()["folder_id"] is None

            # foreign folder -> 404 for bob
            assert (await client.post(f"/folders/{folder['id']}/move", json={"workflow_ids": ["x"]}, headers=b)).status_code == 404
            # empty list -> 422 (min_length=1)
            assert (await client.post(f"/folders/{folder['id']}/move", json={"workflow_ids": []}, headers=a)).status_code == 422

    try:
        asyncio.run(_go())
    finally:
        async def _cleanup():
            async with _client() as client:
                for wid in wf_ids:
                    await client.delete(f"/workflows/{wid}")
                for fid in folder_ids:
                    await client.delete(f"/folders/{fid}")
                await _wipe_users_and_ownership()
        asyncio.run(_cleanup())
