#!/usr/bin/env python3
"""Standalone v44 smoke: dataset versions, notification webhooks, retention
sweep, tag vocabulary, bulk move. LLM-free - runnable while the shared LLM
gateway is throttled. Mirrors the v44 section of smoke_test.py exactly;
scripts/smoke_test.py stays the single source of truth for the full pass."""

import json
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

BASE = "http://127.0.0.1:8000/api/v1"


def req(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


class _Hook(BaseHTTPRequestHandler):
    hits = []

    def do_POST(self):
        n = int(self.headers.get("content-length", 0) or 0)
        _Hook.hits.append(json.loads(self.rfile.read(n) or b"{}"))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


def main() -> None:
    status, health = req("GET", "/health")
    assert status == 200 and health["version"] >= "1.44", health
    tag = uuid.uuid4().hex[:6]

    # -- dataset version timeline
    status, ds = req("POST", "/datasets", {"name": f"smoke44v vers {tag}", "rows": [{"city": "lima", "t": 19}], "tags": ["smoke44v"]})
    assert status == 201 and ds["tags"] == ["smoke44v"], ds
    did = ds["id"]
    status, _ = req("POST", f"/datasets/{did}/rows", {"rows": [{"city": "oslo", "t": 4}]})
    assert status == 200, status  # rows append returns the updated dataset (200)
    status, vers = req("GET", f"/datasets/{did}/versions")
    assert [v["version"] for v in vers] == [2, 1] and vers[0]["source"] == "append" and vers[0]["current"], vers
    status, prev = req("GET", f"/datasets/{did}/versions/1/rows")
    assert prev["rows"] == [{"city": "lima", "t": 19}], prev
    status, restored = req("POST", f"/datasets/{did}/versions/1/restore")
    assert status == 200 and restored["row_count"] == 1, restored
    status, vers = req("GET", f"/datasets/{did}/versions")
    assert [v["version"] for v in vers] == [3, 2, 1] and vers[0]["source"] == "restore", vers
    status, rows = req("GET", f"/datasets/{did}/rows")
    assert len(rows["rows"]) == 1 and rows["rows"][0]["city"] == "lima", rows
    status, _ = req("DELETE", f"/datasets/{did}")
    assert status == 204, status

    # -- notification rules: real webhook on a failing run + test fire
    server = HTTPServer(("127.0.0.1", 0), _Hook)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    hook = f"http://127.0.0.1:{server.server_address[1]}/hook"
    _Hook.hits = []
    status, boom = req("POST", "/workflows", {"name": f"SMOKE44V boom {tag}",
        "graph": {"nodes": [
            {"id": "t", "type": "manual_trigger", "name": "t", "position": {"x": 0, "y": 0}, "parameters": {}},
            {"id": "c", "type": "code", "name": "c", "position": {"x": 100, "y": 0}, "parameters": {"code": "result = 1 / 0"}},
        ], "edges": [{"id": "e1", "source": "t", "target": "c", "sourceHandle": "main", "targetHandle": "main"}]}})
    assert status == 201, boom
    status, rule = req("POST", "/notifications", {"name": f"smoke44v rule {tag}", "events": ["execution_failed"], "webhook_url": hook})
    assert status == 201, rule
    status, acc = req("POST", f"/workflows/{boom['id']}/run", {"payload": {}})
    exec_id = acc["execution_id"]
    for _ in range(80):
        status, run = req("GET", f"/executions/{exec_id}")
        if run["status"] != "running":
            break
        time.sleep(0.25)
    assert run["status"] == "error", run
    time.sleep(1.5)
    assert len(_Hook.hits) == 1, _Hook.hits
    assert _Hook.hits[0]["event"] == "execution_failed" and _Hook.hits[0]["status"] == "error", _Hook.hits[0]
    status, rules = req("GET", "/notifications")
    row = next(r for r in rules if r["id"] == rule["id"])
    assert row["fire_count"] == 1 and row["last_status"] == "ok", row
    status, test = req("POST", f"/notifications/{rule['id']}/test")
    assert status == 200 and test["ok"] is True and len(_Hook.hits) == 2, test
    req("DELETE", f"/notifications/{rule['id']}")
    req("DELETE", f"/workflows/{boom['id']}")
    server.shutdown()

    # -- retention sweep reporting
    status, purge = req("POST", "/settings/retention/purge")
    assert status == 200 and "artifacts_deleted" in purge, purge

    # -- tags vocabulary across workflows + datasets
    status, tagwf = req("POST", "/workflows", {"name": f"SMOKE44V tagged {tag}", "graph": {"nodes": [], "edges": []}, "tags": ["smoke44vtag"]})
    assert status == 201, tagwf
    status, tagds = req("POST", "/datasets", {"name": f"smoke44v tagged {tag}", "rows": [{"x": 1}], "tags": ["smoke44vtag"]})
    assert status == 201, tagds
    status, inv = req("GET", "/tags")
    entry = next(e for e in inv if e["name"] == "smoke44vtag")
    assert entry["workflows"] == 1 and entry["datasets"] == 1, entry
    status, ren = req("PUT", "/tags/rename", {"from": "smoke44vtag", "to": "smoke44vrenamed"})
    assert status == 200 and ren["workflows"] == 1 and ren["datasets"] == 1, ren
    status, _ = req("DELETE", "/tags/smoke44vrenamed")
    assert status == 200, status
    req("DELETE", f"/workflows/{tagwf['id']}")
    req("DELETE", f"/datasets/{tagds['id']}")

    # -- folders bulk move
    status, fold = req("POST", "/folders", {"name": f"SMOKE44V folder {tag}"})
    assert status == 201, fold
    moved = []
    for i in range(2):
        status, w = req("POST", "/workflows", {"name": f"SMOKE44V move {i} {tag}", "graph": {"nodes": [], "edges": []}})
        moved.append(w["id"])
    status, mv = req("POST", f"/folders/{fold['id']}/move", {"workflow_ids": moved + ["bogus"]})
    assert status == 200 and len(mv["moved"]) == 2 and len(mv["skipped"]) == 1, mv
    status, unfiled = req("POST", "/folders/root/move", {"workflow_ids": moved})
    assert status == 200 and len(unfiled["moved"]) == 2, unfiled
    for wid in moved:
        req("DELETE", f"/workflows/{wid}")
    req("DELETE", f"/folders/{fold['id']}")

    print("v44 standalone smoke ALL PASS (versions+restore, real webhook, retention sweep, tags, bulk move)")


if __name__ == "__main__":
    main()
