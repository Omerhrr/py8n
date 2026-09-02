"""V48 feature tests: production operations II.

Drift alerts: the drift_check node fires ``drift_detected`` notification
rules (both warn and error modes, before an error-mode run fails), so the
Notifications page rules UI hears about model drift with a full payload
(model, overall PSI, drifted features).
Scheduled reports: cron-driven exports of datasets (csv/xlsx/json/parquet)
and dashboards (JSON snapshot of every rendered component) land as regular
Artifacts, with run-now, fire previews and last-run stats on the row.
Row-level app permissions: named AppShareGrants pair a token with a row
filter - scoped viewers compute/render/page/write inside their slice, can
never touch rows outside it (404, fail closed), ``eq`` grants stamp the
scope column onto created records, and revocation kills links instantly.

Runs the FastAPI app in-process via httpx ASGITransport (same harness as v4-v47).
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

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


def _edge(eid: str, source: str, target: str, source_handle: str = "main", target_handle: str = "main") -> dict:
    return {"id": eid, "source": source, "target": target, "sourceHandle": source_handle, "targetHandle": target_handle}


async def _make_workflow(client: httpx.AsyncClient, name: str, graph: dict) -> str:
    res = await client.post("/workflows", json={"name": name, "graph": graph, "is_active": False})
    assert res.status_code == 201, res.text
    return res.json()["id"]


async def _run_and_wait(client: httpx.AsyncClient, workflow_id: str) -> dict:
    res = await client.post(f"/workflows/{workflow_id}/run", json={"payload": {}})
    assert res.status_code in (200, 202), res.text
    exec_id = res.json()["execution_id"]
    for _ in range(300):
        res = await client.get(f"/executions/{exec_id}")
        assert res.status_code == 200, res.text
        if res.json()["status"] != "running":
            return res.json()
        await asyncio.sleep(0.05)
    raise AssertionError("execution did not finish in time")


async def _mk_dataset(client: httpx.AsyncClient, name: str, rows: list[dict]) -> str:
    res = await client.post("/datasets", json={"name": name, "rows": rows})
    assert res.status_code == 201, res.text
    return res.json()["id"]


def _clf_rows(n: int = 80, shift: float = 0.0, seed: int = 7) -> list[dict]:
    import random

    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        group = rng.choice(["A", "B"])
        rows.append({
            "x1": round(rng.gauss(2 + shift if group == "A" else -2 + shift, 1.0), 3),
            "x2": round(rng.gauss(0, 1.0), 3),
            "city": rng.choice(["lagos", "cairo", "nairobi"]),
            "tier": group,
        })
    return rows


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


def _start_receiver() -> tuple[HTTPServer, str, list[dict]]:
    """Fresh receiver per call: each server gets its OWN captured list."""

    class _Receiver(_WebhookReceiver):
        captured: list[dict] = []

    server = HTTPServer(("127.0.0.1", 0), _Receiver)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/hook", _Receiver.captured


# ---------------------------------------------------------------------------
# 1) definitions: strict pins live HERE (newest wave), drift event in catalog
# ---------------------------------------------------------------------------
def test_v48_definitions():
    async def _go():
        async with _client() as client:
            res = await client.get("/health")
            assert res.json()["version"] == "1.48.0", res.json()
            res = await client.get("/node-definitions")
            defs = res.json()["definitions"]
            assert len(defs) == 47, f"v48 has no new nodes; expected 47 types, got {len(defs)}"
            res = await client.get("/notifications/events")
            events = res.json()["events"]
            assert "drift_detected" in events, events
            assert "execution_failed" in events

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


# ---------------------------------------------------------------------------
# 2) drift alerts fire notification rules (warn + error + quiet + scoping)
# ---------------------------------------------------------------------------
def test_v48_drift_alert_notifications():
    tag = uuid.uuid4().hex[:8]
    model_name = f"alert-{tag}"
    wf_ids, ds_refs, model_ids, rule_ids = [], [], [], []

    async def _go():
        from app.services import notifications as notif_svc

        server_a, url_a, captured_a = _start_receiver()
        server_b, url_b, captured_b = _start_receiver()
        try:
            async with _client() as client:
                ds_id = await _mk_dataset(client, f"v48-train-{tag}", _clf_rows(80))
                ds_refs.append(f"v48-train-{tag}")
                ds_shift = await _mk_dataset(client, f"v48-shift-{tag}", _clf_rows(60, shift=8.0))
                ds_refs.append(f"v48-shift-{tag}")

                # retrain first: the model must exist with reference stats
                train_graph = {
                    "nodes": [
                        _node("t", "manual_trigger"),
                        _node("d", "dataset_read", {"dataset": f"v48-train-{tag}", "limit": 80}),
                        _node("tr", "model_train", {
                            "model": "random_forest_classifier", "task": "classification",
                            "target": "tier", "model_name": model_name, "register": True,
                        }),
                    ],
                    "edges": [_edge("e1", "t", "d"), _edge("e2", "d", "tr")],
                }
                wf_id = await _make_workflow(client, f"v48-train-{tag}", train_graph)
                wf_ids.append(wf_id)
                exec_row = await _run_and_wait(client, wf_id)
                assert exec_row["status"] in ("success", "succeeded"), exec_row.get("error")

                res = await client.get("/models")
                model = next(m for m in res.json() if m["name"] == model_name)
                model_ids.append(model["id"])
                assert model["has_reference_stats"]

                # rule A: catch-all for drift + failure; rule B: scoped to a
                # workflow that will never drift (scoping is asserted later)
                res = await client.post("/notifications", json={
                    "name": f"drift-catchall-{tag}",
                    "events": ["drift_detected", "execution_failed"],
                    "webhook_url": url_a,
                })
                assert res.status_code == 201, res.text
                rule_ids.append(res.json()["id"])

                # --- warn mode: shifted batch -> drift_detected fires, run passes
                warn_graph = {
                    "nodes": [
                        _node("t", "manual_trigger"),
                        _node("d", "dataset_read", {"dataset": f"v48-shift-{tag}", "limit": 60}),
                        _node("dc", "drift_check", {"model": model_name, "threshold": 0.25, "on_drift": "warn"}),
                    ],
                    "edges": [_edge("e1", "t", "d"), _edge("e2", "d", "dc")],
                }
                warn_wf = await _make_workflow(client, f"v48-warn-{tag}", warn_graph)
                wf_ids.append(warn_wf)
                exec_row = await _run_and_wait(client, warn_wf)
                assert exec_row["status"] in ("success", "succeeded"), exec_row.get("error")
                await notif_svc.adrain_pending()
                drift_events = [c["body"] for c in captured_a if c["body"].get("event") == "drift_detected"]
                assert drift_events, "warn-mode drift must fire the drift_detected event"
                payload = drift_events[0]
                assert payload["model_name"] == model_name
                assert payload["mode"] == "warn"
                assert payload["overall_psi"] > 0.25
                assert payload["drifted_features"], payload
                assert payload["workflow_id"] == warn_wf
                assert payload["execution_id"] == exec_row["id"]
                n_after_warn = len(captured_a)

                # --- quiet: identical batch -> NO drift event
                quiet_graph = {
                    "nodes": [
                        _node("t", "manual_trigger"),
                        _node("d", "dataset_read", {"dataset": f"v48-train-{tag}", "limit": 80}),
                        _node("dc", "drift_check", {"model": model_name, "threshold": 0.25, "on_drift": "warn"}),
                    ],
                    "edges": [_edge("e1", "t", "d"), _edge("e2", "d", "dc")],
                }
                quiet_wf = await _make_workflow(client, f"v48-quiet-{tag}", quiet_graph)
                wf_ids.append(quiet_wf)
                res = await client.post("/notifications", json={
                    "name": f"drift-scoped-{tag}",
                    "events": ["drift_detected", "execution_failed"],
                    "webhook_url": url_b,
                    "workflow_id": quiet_wf,  # scoped: other workflows must NOT hit it
                })
                assert res.status_code == 201, res.text
                rule_ids.append(res.json()["id"])
                exec_row = await _run_and_wait(client, quiet_wf)
                assert exec_row["status"] in ("success", "succeeded")
                await notif_svc.adrain_pending()
                assert len(captured_a) == n_after_warn, "stable batch must stay quiet"
                assert not captured_b, "quiet workflow fires nothing even for its own rule"

                # --- error mode: the alert fires BEFORE the run fails, and the
                # failure ALSO raises execution_failed (two events, one webhook)
                error_graph = {
                    "nodes": [
                        _node("t", "manual_trigger"),
                        _node("d", "dataset_read", {"dataset": f"v48-shift-{tag}", "limit": 60}),
                        _node("dc", "drift_check", {"model": model_name, "threshold": 0.25, "on_drift": "error"}),
                    ],
                    "edges": [_edge("e1", "t", "d"), _edge("e2", "d", "dc")],
                }
                error_wf = await _make_workflow(client, f"v48-error-{tag}", error_graph)
                wf_ids.append(error_wf)
                exec_row = await _run_and_wait(client, error_wf)
                assert exec_row["status"] in ("failed", "error"), exec_row["status"]
                await notif_svc.adrain_pending()
                events_now = [c["body"].get("event") for c in captured_a[n_after_warn:]]
                assert "drift_detected" in events_now, events_now
                assert "execution_failed" in events_now, "error-mode drift must fail the run loudly"
                drift_payload = next(
                    c["body"] for c in captured_a[n_after_warn:] if c["body"].get("event") == "drift_detected"
                )
                assert drift_payload["mode"] == "error"
                assert "Drift detected" in (drift_payload.get("message") or "")

                # --- scoping: a drifted run in ANOTHER workflow hits rule A
                # (one more drift event) but NEVER rule B (scope = quiet_wf)
                other_graph = {
                    "nodes": [
                        _node("t", "manual_trigger"),
                        _node("d", "dataset_read", {"dataset": f"v48-shift-{tag}", "limit": 60}),
                        _node("dc", "drift_check", {"model": model_name, "threshold": 0.25, "on_drift": "warn"}),
                    ],
                    "edges": [_edge("e1", "t", "d"), _edge("e2", "d", "dc")],
                }
                other_wf = await _make_workflow(client, f"v48-other-{tag}", other_graph)
                wf_ids.append(other_wf)
                exec_row = await _run_and_wait(client, other_wf)
                assert exec_row["status"] in ("success", "succeeded")
                await notif_svc.adrain_pending()
                # error run added 2 (drift + failed), other run adds 1 (drift)
                assert len(captured_a) == n_after_warn + 3, "catch-all rule hears every drift"
                assert not captured_b, "scoped rule must not hear other workflows"
                assert captured_a[-1]["body"]["workflow_id"] == other_wf

                # rule stats bookkeeping
                res = await client.get("/notifications")
                rules = {r["id"]: r for r in res.json()}
                assert rules[rule_ids[0]]["fire_count"] >= 3
                assert rules[rule_ids[0]]["last_status"] == "ok"
                assert rules[rule_ids[1]]["fire_count"] == 0
        finally:
            server_a.shutdown()
            server_b.shutdown()

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())
        asyncio.run(_cleanup(wf_ids, ds_refs, [], [], model_ids, rule_ids))


async def _cleanup(
    workflow_ids: list[str],
    dataset_refs: list[str],
    app_refs: list[str],
    dashboard_ids: list[str],
    model_ids: list[str],
    rule_ids: list[str] | None = None,
    report_ids: list[str] | None = None,
) -> None:
    async with _client() as client:
        for rid in rule_ids or []:
            try:
                await client.delete(f"/notifications/{rid}")
            except Exception:
                pass
        for rid in report_ids or []:
            try:
                await client.delete(f"/reports/{rid}")
            except Exception:
                pass
        for wid in workflow_ids:
            try:
                await client.delete(f"/workflows/{wid}")
            except Exception:
                pass
        for ref in dataset_refs:
            try:
                await client.delete(f"/datasets/{ref}")
            except Exception:
                pass
        for ref in app_refs:
            try:
                await client.delete(f"/apps/{ref}")
            except Exception:
                pass
        for did in dashboard_ids:
            try:
                await client.delete(f"/dashboards/{did}")
            except Exception:
                pass
        for mid in model_ids:
            try:
                await client.delete(f"/models/{mid}")
            except Exception:
                pass
    await _drain_background()


# ---------------------------------------------------------------------------
# 3) scheduled reports: dataset + dashboard exports on a cron
# ---------------------------------------------------------------------------
def test_v48_scheduled_reports():
    tag = uuid.uuid4().hex[:8]
    ds_refs, dashboard_ids, report_ids, artifact_ids = [], [], [], []

    async def _go():
        async with _client() as client:
            ds_id = await _mk_dataset(client, f"v48-rep-{tag}", [
                {"region": "eu", "amount": 10 + i} for i in range(5)
            ] + [{"region": "us", "amount": 20 + i} for i in range(5)])
            ds_refs.append(f"v48-rep-{tag}")

            # validation errors first
            res = await client.post("/reports", json={
                "name": "bad cron", "source_type": "dataset", "source_id": ds_id,
                "fmt": "csv", "cron": "not-a-cron",
            })
            assert res.status_code == 400, res.text
            res = await client.post("/reports", json={
                "name": "bad fmt", "source_type": "dashboard", "source_id": ds_id,
                "fmt": "csv", "cron": "0 6 * * *",
            })
            assert res.status_code == 400, res.text
            res = await client.post("/reports", json={
                "name": "missing source", "source_type": "dataset", "source_id": "nope",
                "fmt": "csv", "cron": "0 6 * * *",
            })
            assert res.status_code == 404, res.text

            # dataset report
            res = await client.post("/reports", json={
                "name": f"weekly-csv-{tag}", "source_type": "dataset", "source_id": ds_id,
                "fmt": "csv", "cron": "0 6 * * 1",
            })
            assert res.status_code == 201, res.text
            report = res.json()
            report_ids.append(report["id"])
            assert report["source_name"] == f"v48-rep-{tag}"
            assert report["enabled"] is True and report["fire_count"] == 0

            # run NOW
            res = await client.post(f"/reports/{report['id']}/run")
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["run"]["ok"] is True, body
            artifact_ids.append(body["run"]["artifact_id"])
            assert body["last_artifact_id"] == body["run"]["artifact_id"]
            assert body["fire_count"] == 1 and body["last_status"] == "ok"
            res = await client.get(f"/artifacts/{body['run']['artifact_id']}/content")
            assert res.status_code == 200 and b"region" in res.content

            # runs preview: cron validated -> real next-fire times
            res = await client.get(f"/reports/{report['id']}/runs")
            assert res.status_code == 200, res.text
            runs = res.json()
            assert len(runs["next_runs"]) == 5 and runs["fire_count"] == 1

            # dashboard report (json snapshot)
            dash = await client.post("/dashboards", json={
                "name": f"v48-board-{tag}",
                "config": {"components": [
                    {"id": "s1", "type": "stat", "dataset_id": ds_id, "label": "Rows", "agg": "count"},
                    {"id": "c1", "type": "chart", "dataset_id": ds_id, "title": "By region",
                     "chart_type": "bar", "group_by": "region", "agg": "count"},
                ]},
            })
            assert dash.status_code == 201, dash.text
            dashboard_ids.append(dash.json()["id"])
            res = await client.post("/reports", json={
                "name": f"board-snapshot-{tag}", "source_type": "dashboard",
                "source_id": dashboard_ids[0], "fmt": "json", "cron": "30 6 * * *",
            })
            assert res.status_code == 201, res.text
            board_report = res.json()
            report_ids.append(board_report["id"])
            res = await client.post(f"/reports/{board_report['id']}/run")
            assert res.status_code == 200, res.text
            body = res.json()
            artifact_ids.append(body["run"]["artifact_id"])
            res = await client.get(f"/artifacts/{body['run']['artifact_id']}/content")
            snapshot = json.loads(res.content)
            assert snapshot["dashboard"]["name"] == f"v48-board-{tag}"
            comp_types = {c["id"]: c["type"] for c in snapshot["components"]}
            assert comp_types == {"s1": "stat", "c1": "chart"}
            assert snapshot["components"][0]["value"] == 10

            # update: rename + disable (job cleared), invalid cron rejected
            res = await client.put(f"/reports/{report['id']}", json={"name": "renamed", "enabled": False})
            assert res.status_code == 200 and res.json()["name"] == "renamed"
            res = await client.put(f"/reports/{report['id']}", json={"cron": "nope"})
            assert res.status_code == 400

            # source deleted -> run lands on the row as error, never a 500
            res = await client.post("/reports", json={
                "name": f"doomed-{tag}", "source_type": "dataset", "source_id": ds_id,
                "fmt": "csv", "cron": "0 5 * * *",
            })
            doomed = res.json()
            report_ids.append(doomed["id"])
            await client.delete(f"/datasets/{ds_id}")
            ds_refs.remove(f"v48-rep-{tag}")
            res = await client.post(f"/reports/{doomed['id']}/run")
            assert res.status_code == 400, res.text
            res = await client.get("/reports")
            row = next(r for r in res.json() if r["id"] == doomed["id"])
            assert row["last_status"] == "error" and row["last_error"], row

            # list + delete
            res = await client.get("/reports")
            assert {r["id"] for r in res.json()} >= set(report_ids)
            res = await client.delete(f"/reports/{report['id']}")
            assert res.status_code == 200
            report_ids.remove(report["id"])

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())
        asyncio.run(_cleanup([], ds_refs, [], dashboard_ids, [], [], report_ids))


# ---------------------------------------------------------------------------
# 4) row-level app permissions (grants)
# ---------------------------------------------------------------------------
def test_v48_row_level_grants():
    tag = uuid.uuid4().hex[:8]
    ds_refs, app_refs = [], []

    async def _go():
        async with _client() as client:
            rows = (
                [{"region": "eu", "name": f"eu{i}", "mrr": 10 + i} for i in range(1, 7)]
                + [{"region": "us", "name": f"us{i}", "mrr": 50 + i} for i in range(1, 7)]
            )
            ds_id = await _mk_dataset(client, f"v48-grant-{tag}", rows)
            ds_refs.append(f"v48-grant-{tag}")
            config = {"components": [
                {"id": "k1", "type": "stat", "label": "Rows", "agg": "count"},
                {"id": "t1", "type": "table", "title": "Records", "columns": ["region", "name", "mrr"], "page_size": 20},
            ]}
            res = await client.post("/apps", json={"name": f"v48-grant-{tag}", "dataset_id": ds_id, "config": config})
            assert res.status_code == 201, res.text
            app_row = res.json()
            app_refs.append(app_row["id"])
            res = await client.post(f"/apps/{app_row['id']}/publish")
            assert res.status_code == 200, res.text
            slug = app_row["slug"]

            # grants CRUD + validation
            res = await client.post(f"/apps/{app_row['id']}/grants", json={
                "name": "EU team", "column": "region", "op": "eq", "value": "eu",
            })
            assert res.status_code == 201, res.text
            eu_grant = res.json()
            assert eu_grant["row_filter"] == {"column": "region", "op": "eq", "value": "eu"}
            assert f"/run/{slug}?t=" in eu_grant["url"]

            res = await client.post(f"/apps/{app_row['id']}/grants", json={
                "name": "EU+US", "column": "region", "op": "in", "value": ["eu", "us"],
            })
            assert res.status_code == 201, res.text
            in_grant = res.json()

            res = await client.post(f"/apps/{app_row['id']}/grants", json={
                "name": "ghost", "column": "nope", "op": "eq", "value": "x",
            })
            assert res.status_code == 400, res.text
            res = await client.post(f"/apps/{app_row['id']}/grants", json={
                "name": "bad op", "column": "region", "op": "like", "value": "x",
            })
            assert res.status_code in (400, 422), res.text
            res = await client.post(f"/apps/{app_row['id']}/grants", json={
                "name": "bad in", "column": "region", "op": "in", "value": [],
            })
            assert res.status_code == 400, res.text

            # protection exists now -> anonymous without token is 403 (fail closed)
            res = await client.get(f"/apps/{slug}/runtime")
            assert res.status_code == 403, res.status_code
            res = await client.get(f"/apps/{slug}/records")
            assert res.status_code == 403

            # wrong token also 403
            res = await client.get(f"/apps/{slug}/runtime?t=wrong-token")
            assert res.status_code == 403

            # eq grant: scoped runtime + records
            tok = eu_grant["token"]
            res = await client.get(f"/apps/{slug}/runtime?t={tok}")
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["scope"]["grant"] == "EU team" and body["scope"]["column"] == "region"
            comps = {c["id"]: c for c in body["components"]}
            assert comps["k1"]["value"] == 6, "eq viewer counts only their slice"
            assert body["dataset"]["row_count"] == 6

            res = await client.get(f"/apps/{slug}/records?t={tok}")
            rows_seen = res.json()["rows"]
            assert res.json()["row_count"] == 6
            assert all(r["region"] == "eu" for r in rows_seen)

            # eq grant can create; the stamp WINS over a forged region
            res = await client.post(f"/apps/{slug}/records?t={tok}", json={
                "record": {"region": "us", "name": "forged", "mrr": 1},
            })
            assert res.status_code == 201, res.text
            res = await client.get(f"/apps/{slug}/records?t={tok}")
            names = [r["name"] for r in res.json()["rows"]]
            assert "forged" in names
            forged = next(r for r in res.json()["rows"] if r["name"] == "forged")
            assert forged["region"] == "eu", "the grant stamp must override the submitted value"

            # forged row's RAW index: last row of the dataset (13 rows now).
            # The anonymous app surface is 403 now (grants exist, fail closed),
            # so compute raw indexes via the dataset API (open-mode owner path).
            res = await client.get(f"/datasets/{ds_id}/rows?limit=100")
            all_rows = res.json()["rows"]
            assert len(all_rows) == 13
            us_raw_index = next(i for i, r in enumerate(all_rows) if r["name"] == "us1")

            # scoped viewer cannot PATCH/DELETE a row outside the slice (404)
            res = await client.patch(f"/apps/{slug}/records/{us_raw_index}?t={tok}", json={"record": {"mrr": 999}})
            assert res.status_code == 404, res.status_code
            res = await client.delete(f"/apps/{slug}/records/{us_raw_index}?t={tok}")
            assert res.status_code == 404, res.status_code

            # ...but CAN edit/delete inside it (raw eu index)
            eu_raw_index = next(i for i, r in enumerate(all_rows) if r["name"] == "eu1")
            res = await client.patch(f"/apps/{slug}/records/{eu_raw_index}?t={tok}", json={"record": {"mrr": 77}})
            assert res.status_code == 200, res.text

            # in-op grant sees everything the list matches, but is read-only
            res = await client.get(f"/apps/{slug}/runtime?t={in_grant['token']}")
            comps = {c["id"]: c for c in res.json()["components"]}
            assert comps["k1"]["value"] == 13, "in-viewer sees eu+us rows"
            res = await client.post(f"/apps/{slug}/records?t={in_grant['token']}", json={
                "record": {"region": "eu", "name": "blocked", "mrr": 5},
            })
            assert res.status_code == 403, "in grants are read-only"
            assert "read-only" in res.json()["detail"]

            # owner/legacy: with share protection ON, the full token still
            # opens the WHOLE runtime (unscoped) exactly as in v47
            res = await client.put(f"/apps/{app_row['id']}/share", json={"enabled": True})
            assert res.status_code == 200, res.text
            full_token = res.json()["share_token"]
            res = await client.get(f"/apps/{slug}/runtime?t={full_token}")
            assert res.status_code == 200
            assert res.json()["scope"] is None, "full token is not scoped"
            comps = {c["id"]: c for c in res.json()["components"]}
            assert comps["k1"]["value"] == 13

            # disable + delete the grant -> its token dies instantly
            res = await client.put(f"/apps/{app_row['id']}/grants/{eu_grant['id']}", json={"enabled": False})
            assert res.status_code == 200 and res.json()["enabled"] is False
            res = await client.get(f"/apps/{slug}/runtime?t={tok}")
            assert res.status_code == 403, "disabled grant must stop working"
            res = await client.delete(f"/apps/{app_row['id']}/grants/{eu_grant['id']}")
            assert res.status_code == 200
            res = await client.get(f"/apps/{slug}/runtime?t={tok}")
            assert res.status_code == 403

            # grants list reflects the deletion
            res = await client.get(f"/apps/{app_row['id']}/grants")
            ids = {g["id"] for g in res.json()}
            assert eu_grant["id"] not in ids and in_grant["id"] in ids

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())
        asyncio.run(_cleanup([], ds_refs, app_refs, [], []))
