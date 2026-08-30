#!/usr/bin/env python3
"""Py8n backend smoke test: run workflow, poll execution, fire webhook, WS stream."""

import asyncio
import os
import json
import time
import urllib.request
import uuid
from urllib.parse import quote

import httpx

BASE = "http://127.0.0.1:8000/api/v1"


def req(method: str, path: str, body: dict | None = None, headers: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    r = urllib.request.Request(BASE + path, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


async def ws_test(execution_id: str, expect_nodes: int) -> list:
    import websockets

    events = []
    uri = f"ws://127.0.0.1:8000/ws/executions/{execution_id}"
    async with websockets.connect(uri) as ws:
        try:
            while True:
                frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
                events.append(frame)
                if frame.get("event") == "execution_finished":
                    break
        except (asyncio.TimeoutError, Exception):
            pass
    return events


def main() -> None:
    print("== node definitions ==")
    status, defs = req("GET", "/node-definitions")
    assert status == 200
    types = [d["type"] for d in defs["definitions"]]
    print(f"{len(types)} node types: {types}")
    assert len(types) == 30, "expected 30 node types after v25 wave"
    for t in ("loop_over_items", "email_send", "slack_message"):
        assert t in types, f"missing {t}"
    # internal batch trigger must stay hidden from the palette
    assert "_batch_trigger" not in types, "internal _batch_trigger leaked into definitions"
    schema = defs["definitions"][0]["parameters_schema"]
    assert "properties" in schema

    print("== workflows list ==")
    status, flows = req("GET", "/workflows")
    print(f"{len(flows)} workflows:", [f["name"] for f in flows])
    by_name = {f["name"]: f["id"] for f in flows}

    print("== run Quickstart (IF branch + code node) ==")
    wf_id = by_name["Hello Py8n — Quickstart"]
    status, acc = req("POST", f"/workflows/{wf_id}/run", {"payload": {"name": "Grace", "score": 99}})
    assert status == 200, acc
    exec_id = acc["execution_id"]
    print("execution:", exec_id)

    # poll until finished
    for _ in range(40):
        status, run = req("GET", f"/executions/{exec_id}")
        if run["status"] != "running":
            break
        time.sleep(0.25)
    assert run["status"] == "success", run
    statuses = {n["node_id"]: n["status"] for n in run["node_runs"]}
    print("node statuses:", statuses)
    assert statuses["win"] == "success" and statuses["tryagain"] == "skipped"

    print("== websocket live stream ==")
    status, acc = req("POST", f"/workflows/{wf_id}/run", {})
    exec_id2 = acc["execution_id"]
    events = asyncio.run(ws_test(exec_id2, 5))
    kinds = [e["event"] for e in events]
    print(f"{len(events)} WS frames:", kinds[:8], "...")
    assert "execution_finished" in kinds, kinds

    def flat_successful_nodes(events):
        finished = [e for e in events if e["event"] == "node_finished" and e.get("status") == "success"]
        for e in events:
            if e["event"] == "history":
                finished += [h for h in e["events"] if h["event"] == "node_finished" and h.get("status") == "success"]
        return finished

    assert len(flat_successful_nodes(events)) >= 3, events  # trigger + enrich + win

    print("== AI Writer workflow (LLM bridge) ==")
    ai_id = by_name["AI Writer — free LLM demo"]
    status, acc = req("POST", f"/workflows/{ai_id}/run", {})
    ai_exec = acc["execution_id"]
    for _ in range(80):
        status, ai_run = req("GET", f"/executions/{ai_exec}")
        if ai_run["status"] != "running":
            break
        time.sleep(0.5)
    print("AI writer status:", ai_run["status"])
    assert ai_run["status"] == "success", ai_run
    llm_out = next(n for n in ai_run["node_runs"] if n["node_id"] == "llm")["output"]
    print("LLM text sample:", str(llm_out["text"])[:120])
    assert llm_out["text"]

    print("== webhook (immediately mode via echo bot is last_node; test both) ==")
    echo_id = by_name["Webhook Echo Bot"]
    r = httpx.post(f"{BASE}/webhooks/{echo_id}", json={"msg": "hello py8n", "n": 7}, timeout=35)
    print("webhook status:", r.status_code)
    body = r.json()
    assert r.status_code == 200, body
    assert body["status"] == "success" and body["last_output"]["result"]["echo"] == {"msg": "hello py8n", "n": 7}, body
    print("echo output:", json.dumps(body["last_output"]["result"], ensure_ascii=False))

    print("== webhook 202 mode (create temp wf) ==")
    graph = {
        "nodes": [
            {"id": "h", "type": "webhook_trigger", "name": "Hook", "position": {"x": 0, "y": 0}, "parameters": {"response_mode": "immediately"}},
            {"id": "s", "type": "set_variable", "name": "Set", "position": {"x": 200, "y": 0}, "parameters": {"assignments": {"got": "{{ nodes.hook.output.body }}"}, "keep_input": False}},
        ],
        "edges": [{"id": "e", "source": "h", "target": "s"}],
    }
    status, tmp = req("POST", "/workflows", {"name": "tmp 202 test", "graph": graph, "is_active": True})
    r = httpx.post(f"{BASE}/webhooks/{tmp['id']}", json={"ping": True}, timeout=15)
    print("immediate webhook:", r.status_code, r.json().get("execution_id"))
    assert r.status_code == 202
    # cleanup
    req("DELETE", f"/workflows/{tmp['id']}")

    print("== cycle rejection ==")
    bad = {"nodes": [{"id": "a", "type": "manual_trigger", "parameters": {}}, {"id": "b", "type": "set_variable", "parameters": {}}],
           "edges": [{"id": "e1", "source": "a", "target": "b"}, {"id": "e2", "source": "b", "target": "a"}]}
    status, err = req("POST", "/workflows", {"name": "bad", "graph": bad})
    print("cycle ->", status, err)
    assert status == 400

    # ------------------------------------------------------------------ v2
    print("== v2 data pipeline (split_out → filter → aggregate) ==")
    graph = {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "name": "Start", "position": {"x": 0, "y": 0},
             "parameters": {"payload": {"data": {"orders": [
                 {"item": "keyboard", "amt": 120}, {"item": "cable", "amt": 15},
                 {"item": "monitor", "amt": 300}, {"item": "mouse pad", "amt": 10},
             ]}}}},
            {"id": "split", "type": "split_out", "name": "Split Orders", "position": {"x": 200, "y": 0}, "parameters": {"field": "data.orders"}},
            {"id": "filt", "type": "filter", "name": "Big Only", "position": {"x": 400, "y": 0},
             "parameters": {"field": "amt", "operator": "greater_than", "right_value": 50}},
            {"id": "agg", "type": "aggregate", "name": "Total", "position": {"x": 600, "y": 0},
             "parameters": {"mode": "sum", "field": "amt"}},
        ],
        "edges": [
            {"id": "e0", "source": "t", "target": "split"},
            {"id": "e1", "source": "split", "target": "filt"},
            {"id": "e2", "source": "filt", "target": "agg"},
        ],
    }
    status, pipe = req("POST", "/workflows", {"name": "tmp v2 pipeline", "graph": graph, "is_active": False})
    assert status == 201, pipe
    status, acc = req("POST", f"/workflows/{pipe['id']}/run", {})
    exec_id = acc["execution_id"]
    for _ in range(40):
        status, run = req("GET", f"/executions/{exec_id}")
        if run["status"] != "running":
            break
        time.sleep(0.25)
    assert run["status"] == "success", run
    total = next(n for n in run["node_runs"] if n["node_id"] == "agg")["output"]["value"]
    print("filtered order total:", total)
    assert total == 420, run

    print("== sub-workflow (execute_workflow) ==")
    child_graph = {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "name": "Start", "position": {"x": 0, "y": 0}, "parameters": {}},
            {"id": "greet", "type": "set_variable", "name": "Greet", "position": {"x": 200, "y": 0},
             "parameters": {"assignments": {"hello": "{{ nodes.t.output.payload.name | default('stranger') }}", "doubled": "{{ nodes.t.output.payload.n * 2 }}"}, "keep_input": False}},
        ],
        "edges": [{"id": "e", "source": "t", "target": "greet"}],
    }
    status, child = req("POST", "/workflows", {"name": "tmp child wf", "graph": child_graph, "is_active": False})
    assert status == 201, child
    parent_graph = {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "name": "Start", "position": {"x": 0, "y": 0},
             "parameters": {"payload": {"name": "Ada", "n": 21}}},
            {"id": "sub", "type": "execute_workflow", "name": "Call Child", "position": {"x": 200, "y": 0},
             "parameters": {"workflow_id": child["id"], "payload": {"name": "{{ nodes.t.output.payload.name }}", "n": "{{ nodes.t.output.payload.n }}"}}},
            {"id": "wrap", "type": "set_variable", "name": "Wrap", "position": {"x": 400, "y": 0},
             "parameters": {"assignments": {"greeting": "{{ nodes.sub.output.output.hello }}", "from": "{{ nodes.sub.output.subworkflow.name }}"}, "keep_input": False}},
        ],
        "edges": [
            {"id": "e0", "source": "t", "target": "sub"},
            {"id": "e1", "source": "sub", "target": "wrap"},
        ],
    }
    status, parent = req("POST", "/workflows", {"name": "tmp parent wf", "graph": parent_graph, "is_active": False})
    assert status == 201, parent
    status, acc = req("POST", f"/workflows/{parent['id']}/run", {})
    exec_id = acc["execution_id"]
    for _ in range(40):
        status, run = req("GET", f"/executions/{exec_id}")
        if run["status"] != "running":
            break
        time.sleep(0.25)
    assert run["status"] == "success", run
    sub_out = next(n for n in run["node_runs"] if n["node_id"] == "wrap")["output"]
    print("sub-workflow result:", sub_out)
    assert sub_out["greeting"] == "Ada" and sub_out["from"] == "tmp child wf", run

    print("== export / import / duplicate ==")
    status, doc = req("GET", f"/workflows/{parent['id']}/export")
    assert status == 200 and doc["format"] == "py8n-workflow" and doc["graph"]["nodes"], doc
    status, imported = req("POST", "/workflows/import", {"data": doc})
    assert status == 201 and imported["name"] == doc["name"] and imported["is_active"] is False, imported
    status, dup = req("POST", f"/workflows/{parent['id']}/duplicate")
    assert status == 201 and dup["name"] == "tmp parent wf (copy)", dup
    status, bad_import = req("POST", "/workflows/import", {"name": "broken", "graph": bad})
    assert status == 400, bad_import
    print("import copy:", imported["id"], "· duplicate:", dup["id"], "· bad import ->", 400)

    # ------------------------------------------------------------------ v3
    print("== v3 loop: seeded Batch Orders Digest (loop → code per batch → aggregate) ==")
    assert "Batch Orders Digest — loop demo" in by_name, "seed top-up missing"
    digest_id = by_name["Batch Orders Digest — loop demo"]
    status, acc = req("POST", f"/workflows/{digest_id}/run", {})
    exec_id = acc["execution_id"]
    for _ in range(40):
        status, run = req("GET", f"/executions/{exec_id}")
        if run["status"] != "running":
            break
        time.sleep(0.25)
    assert run["status"] == "success", run
    body_runs = [n for n in run["node_runs"] if n["node_id"] == "sum"]
    assert [n["batch_index"] for n in body_runs] == [0, 1, 2, 3], body_runs
    revenues = [n["output"]["result"]["revenue"] for n in body_runs]
    print("batch revenues:", revenues)
    assert revenues == [19.75, 140.5, 68.75, 28.9]
    report = next(n for n in run["node_runs"] if n["node_id"] == "report")["output"]
    print("report:", report)
    assert abs(report["total_revenue"] - 257.9) < 0.001
    assert report["batches"] == 4 and report["orders"] == 7

    print("== v3 loop: closure violation rejected at save (400) ==")
    bad_loop = {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "name": "T", "position": {"x": 0, "y": 0}, "parameters": {}},
            {"id": "side", "type": "set_variable", "name": "Side", "position": {"x": 200, "y": -100}, "parameters": {"assignments": {"x": 1}, "keep_input": False}},
            {"id": "lp", "type": "loop_over_items", "name": "Loop", "position": {"x": 200, "y": 0}, "parameters": {}},
            {"id": "body", "type": "set_variable", "name": "Body", "position": {"x": 400, "y": 0}, "parameters": {"assignments": {"y": 2}, "keep_input": False}},
        ],
        "edges": [
            {"id": "e1", "source": "t", "target": "lp"},
            {"id": "e2", "source": "lp", "target": "body", "sourceHandle": "loop"},
            {"id": "e3", "source": "side", "target": "body"},
        ],
    }
    status, err = req("POST", "/workflows", {"name": "tmp bad loop", "graph": bad_loop})
    print("bad loop ->", status, str(err)[:120])
    assert status == 400 and "outside the loop body" in err["detail"], err

    print("== v3 loop: dry-run-free integrations chain (email → slack, both dry_run) ==")
    integ_graph = {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "name": "Start", "position": {"x": 0, "y": 0},
             "parameters": {"payload": {"customer": "Ada", "total": 42}}},
            {"id": "mail", "type": "email_send", "name": "Confirm email", "position": {"x": 200, "y": 0},
             "parameters": {"to": "ada@example.com", "subject": "Order confirmed",
                            "body": "Thanks {{ nodes.t.output.payload.customer }} — total {{ nodes.t.output.payload.total }} EUR",
                            "dry_run": True}},
            {"id": "alert", "type": "slack_message", "name": "Slack alert", "position": {"x": 400, "y": 0},
             "parameters": {"webhook_url": "https://hooks.slack.com/services/T000/B000/XXXX",
                            "text": "New order from {{ nodes.t.output.payload.customer }}", "dry_run": True}},
        ],
        "edges": [
            {"id": "e1", "source": "t", "target": "mail"},
            {"id": "e2", "source": "mail", "target": "alert"},
        ],
    }
    status, integ = req("POST", "/workflows", {"name": "tmp v3 integrations", "graph": integ_graph, "is_active": False})
    assert status == 201, integ
    status, acc = req("POST", f"/workflows/{integ['id']}/run", {})
    exec_id = acc["execution_id"]
    for _ in range(40):
        status, run = req("GET", f"/executions/{exec_id}")
        if run["status"] != "running":
            break
        time.sleep(0.25)
    assert run["status"] == "success", run
    outs = {n["node_id"]: n["output"] for n in run["node_runs"]}
    assert outs["mail"]["delivered"] is False and outs["mail"]["message"]["to"] == ["ada@example.com"]
    assert "Ada" in outs["mail"]["message"]["body"]
    assert outs["alert"]["mode"] == "webhook" and "Ada" in outs["alert"]["payload"]["text"]
    print("email preview:", json.dumps(outs["mail"]["message"], ensure_ascii=False))
    print("slack preview:", json.dumps(outs["alert"]["payload"], ensure_ascii=False))

    print("== v3 slack live loopback through a real Py8n webhook ==")
    hook_graph = {
        "nodes": [
            {"id": "h", "type": "webhook_trigger", "name": "Hook", "position": {"x": 0, "y": 0}, "parameters": {"response_mode": "last_node"}},
            {"id": "s", "type": "set_variable", "name": "Catch", "position": {"x": 200, "y": 0},
             "parameters": {"assignments": {"text": "{{ nodes.h.output.body.text }}", "channel": "{{ nodes.h.output.body.channel | default('') }}"}, "keep_input": False}},
        ],
        "edges": [{"id": "e", "source": "h", "target": "s"}],
    }
    status, hook = req("POST", "/workflows", {"name": "tmp slack loopback hook", "graph": hook_graph, "is_active": True})
    assert status == 201, hook
    integ_graph["nodes"][2]["parameters"]["webhook_url"] = f"{BASE}/webhooks/{hook['id']}"
    integ_graph["nodes"][2]["parameters"]["dry_run"] = False
    status, integ2 = req("POST", "/workflows", {"name": "tmp v3 slack live", "graph": integ_graph, "is_active": False})
    assert status == 201, integ2
    status, acc = req("POST", f"/workflows/{integ2['id']}/run", {})
    exec_id = acc["execution_id"]
    for _ in range(40):
        status, run = req("GET", f"/executions/{exec_id}")
        if run["status"] != "running":
            break
        time.sleep(0.25)
    assert run["status"] == "success", run
    alert_out = next(n for n in run["node_runs"] if n["node_id"] == "alert")["output"]
    print("slack live output:", alert_out)
    assert alert_out["delivered"] is True and alert_out["status_code"] == 200, alert_out

    print("== v4 executions observability: rerun + status filter + delete ==")
    # rerun the slack-live execution with its recorded payload
    status, rr = req("POST", f"/executions/{exec_id}/rerun")
    assert status == 202 and rr["rerun_of"] == exec_id, (status, rr)
    new_exec = rr["execution_id"]
    assert new_exec != exec_id
    run = {"status": "running"}
    for _ in range(40):
        status, run = req("GET", f"/executions/{new_exec}")
        if run["status"] != "running":
            break
        time.sleep(0.25)
    assert run["status"] == "success", run
    assert run["trigger_payload"].get("payload") is not None, run["trigger_payload"]
    rerun_alert = next(n for n in run["node_runs"] if n["node_id"] == "alert")["output"]
    assert rerun_alert["delivered"] is True, rerun_alert
    print("rerun replayed payload and re-delivered slack message:", rerun_alert["status_code"])

    # status filter returns only successes and includes the rerun
    status, ok_rows = req("GET", "/executions?status=success&limit=50")
    assert status == 200 and all(r["status"] == "success" for r in ok_rows)
    assert any(r["id"] == new_exec for r in ok_rows)
    assert all(r.get("workflow_name") for r in ok_rows), "workflow_name missing in list"
    print(f"status=success filter: {len(ok_rows)} rows, all named + success")

    # delete the rerun record
    status, body = req("DELETE", f"/executions/{new_exec}")
    assert status == 200 and body["ok"] is True
    status, _ = req("GET", f"/executions/{new_exec}")
    assert status == 404
    print("rerun record deleted (404 after delete)")

    print("== v5 wait-for-resume: suspend -> token check -> resume ==")
    wait_graph = {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "name": "Kickoff", "position": {"x": 0, "y": 0},
             "parameters": {"payload": {"ticket": "PY8N-1"}}},
            {"id": "w", "type": "wait_for_resume", "name": "Approval Gate", "position": {"x": 200, "y": 0},
             "parameters": {"resume_hint": "Manager approval required"}},
            {"id": "post", "type": "set_variable", "name": "Outcome", "position": {"x": 400, "y": 0},
             "parameters": {"assignments": {
                 "approved": "{{ nodes.w.output.approved }}",
                 "ticket": "{{ nodes.t.output.payload.ticket }}",
             }, "keep_input": False}},
        ],
        "edges": [
            {"id": "e1", "source": "t", "target": "w"},
            {"id": "e2", "source": "w", "target": "post"},
        ],
    }
    status, wfw = req("POST", "/workflows", {"name": "tmp v5 wait resume", "graph": wait_graph, "is_active": False})
    assert status == 201, wfw
    try:
        status, acc = req("POST", f"/workflows/{wfw['id']}/run", {})
        wexec = acc["execution_id"]
        detail = {"status": "running"}
        for _ in range(40):
            status, detail = req("GET", f"/executions/{wexec}")
            if detail["status"] != "running":
                break
            time.sleep(0.25)
        assert detail["status"] == "waiting", detail
        token = detail["resume"]["token"]
        assert detail["resume"]["url"] == f"/executions/{wexec}/resume"
        wait_run = next(n for n in detail["node_runs"] if n["node_id"] == "w")
        assert wait_run["output"]["resume_hint"] == "Manager approval required"
        print("suspended with token:", token[:12], "…; hint OK")

        # token guards
        status, _ = req("POST", "/executions/nonexistent/resume", {"token": "x"})
        assert status == 404, status
        status, _ = req("POST", f"/executions/{wexec}/resume", {"token": "wrong-token"})
        assert status == 403, status
        print("404 unknown id / 403 wrong token — OK")

        status, rr = req("POST", f"/executions/{wexec}/resume", {"token": token, "payload": {"approved": True}})
        assert status == 202, (status, rr)
        assert rr["execution_id"] == wexec  # same execution continues
        for _ in range(60):
            status, detail = req("GET", f"/executions/{wexec}")
            if detail["status"] != "running":
                break
            time.sleep(0.25)
        assert detail["status"] == "success", detail
        post = next(n for n in detail["node_runs"] if n["node_id"] == "post")
        assert post["output"] == {"approved": True, "ticket": "PY8N-1"}, post["output"]
        wruns = [n["status"] for n in detail["node_runs"] if n["node_id"] == "w"]
        assert wruns == ["waiting", "success"], wruns
        print("resumed to success; post output:", post["output"], "; wait records:", wruns)

        # token invalidated after resume
        status, _ = req("POST", f"/executions/{wexec}/resume", {"token": token})
        assert status == 409, status
        print("token invalidated after resume (409) — OK")
    finally:
        req("DELETE", f"/workflows/{wfw['id']}")

    print("== v6 templates + node input capture ==")
    status, tpls = req("GET", "/templates")
    assert status == 200 and len(tpls) >= 8, (status, len(tpls))
    assert all("graph" not in t for t in tpls), "template list should stay lean"
    status, twf = req("POST", "/templates/data-pipeline/use")
    assert status == 201 and twf["is_active"] is False, (status, twf)
    try:
        status, acc = req("POST", f"/workflows/{twf['id']}/run", {})
        texec = acc["execution_id"]
        for _ in range(40):
            status, run = req("GET", f"/executions/{texec}")
            if run["status"] != "running":
                break
            time.sleep(0.25)
        assert run["status"] == "success", run
        total = next(n for n in run["node_runs"] if n["node_id"] == "total")
        assert total["output"]["value"] == 340, total["output"]
        # input capture: downstream nodes record what they received
        keep = next(n for n in run["node_runs"] if n["node_id"] == "keep")
        assert "input" in keep and keep["input"]["items"], keep.get("input")
        assert keep["input"]["items"][0]["amount"] > 100, keep["input"]
        print(f"template instantiated + ran: EU total={total['output']['value']}; filter saw input {keep['input']}")
    finally:
        req("DELETE", f"/workflows/{twf['id']}")
    status, _ = req("POST", "/templates/nope/use")
    assert status == 404
    print("8+ templates listed, use->run->success, 404 unknown template")

    print("== v7 schedules: previews -> activate -> global view -> guards ==")
    sched_graph = {
        "nodes": [
            {"id": "s1", "type": "schedule_trigger", "name": "Every Minute", "position": {"x": 0, "y": 0},
             "parameters": {"mode": "cron", "cron": "* * * * *"}},
            {"id": "c1", "type": "code", "name": "Tick", "position": {"x": 200, "y": 0},
             "parameters": {"code": "result = {'fired': True}\n"}},
        ],
        "edges": [{"id": "e1", "source": "s1", "target": "c1"}],
    }
    status, swf = req("POST", "/workflows", {"name": "tmp v7 schedule", "graph": sched_graph, "is_active": False})
    assert status == 201, swf
    try:
        # introspection: 5 strictly-ascending fire previews, none while paused
        status, sched = req("GET", f"/workflows/{swf['id']}/schedule")
        assert status == 200 and sched["is_active"] is False and sched["next_run_at"] is None, sched
        entry = sched["schedules"][0]
        assert entry["summary"] == "cron * * * * *" and len(entry["next_runs"]) == 5, entry
        assert entry["next_runs"] == sorted(entry["next_runs"]) and len(set(entry["next_runs"])) == 5, entry["next_runs"]
        print("fire previews advance:", entry["next_runs"][:3])

        # activate -> next_run_at appears on workflow + detail
        status, act = req("POST", f"/workflows/{swf['id']}/activate")
        assert status == 200 and act["is_active"] is True and act["next_run_at"], act
        status, dlist = req("GET", "/workflows")
        item = next(w for w in dlist if w["id"] == swf["id"])
        assert item["schedule_summary"] == "cron * * * * *" and item["next_run_at"], item
        print("activated; list shows next run:", item["next_run_at"])

        status, rows = req("GET", "/schedules")
        assert status == 200 and any(r["workflow_id"] == swf["id"] and r["is_active"] for r in rows), rows[:2]
        print(f"global schedules view: {len(rows)} entries incl. activated workflow")

        # invalid cron cannot be saved anymore
        bad_nodes = [{**sched_graph["nodes"][0], "parameters": {"mode": "cron", "cron": "oops"}}]
        status, _ = req("PUT", f"/workflows/{swf['id']}", {"graph": {**sched_graph, "nodes": bad_nodes}})
        assert status == 400, status
        print("invalid cron rejected on save (400)")

        status, deact = req("POST", f"/workflows/{swf['id']}/deactivate")
        assert status == 200 and deact["is_active"] is False and deact["next_run_at"] is None, deact
    finally:
        req("DELETE", f"/workflows/{swf['id']}")
    print("activate/deactivate roundtrip + global view + cron guard — OK")

    # ------------------------------------------------------------- v8 wave
    print("== v8 run control: disabled nodes -> cancel -> error workflows ==")

    # 1) disabled node passes its input through
    dis_graph = {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "parameters": {"payload": {"tag": "keepme"}}},
            {"id": "off", "type": "code", "name": "Off Duty", "parameters": {"code": "result = 1/0"},
             "disabled": True},
            {"id": "after", "type": "set_variable",
             "parameters": {"assignments": {"tag": "{{ nodes.off.output.payload.tag }}"}, "keep_input": False}},
        ],
        "edges": [
            {"id": "e1", "source": "t", "target": "off"},
            {"id": "e2", "source": "off", "target": "after"},
        ],
    }
    status, dwf = req("POST", "/workflows", {"name": "tmp v8 disable", "graph": dis_graph, "is_active": False})
    assert status == 201, dwf
    try:
        status, acc = req("POST", f"/workflows/{dwf['id']}/run", {})
        exec_id = acc["execution_id"]
        for _ in range(40):
            status, run = req("GET", f"/executions/{exec_id}")
            if run["status"] != "running":
                break
            time.sleep(0.2)
        assert run["status"] == "success", run
        st = {n["node_id"]: n for n in run["node_runs"]}
        assert st["off"]["status"] == "skipped" and "disabled" in (st["off"].get("error") or ""), st["off"]
        assert st["after"]["output"] == {"tag": "keepme"}, st["after"]
        print("disabled node bypassed 1/0 code; downstream saw payload:", st["after"]["output"])
    finally:
        req("DELETE", f"/workflows/{dwf['id']}")

    # 2) cancel a running execution
    cancel_graph = {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "parameters": {}},
            {"id": "d", "type": "delay", "parameters": {"seconds": 5}},
            {"id": "after", "type": "set_variable", "parameters": {"assignments": {"done": "yes"}, "keep_input": False}},
        ],
        "edges": [{"id": "e1", "source": "t", "target": "d"}, {"id": "e2", "source": "d", "target": "after"}],
    }
    status, cwf = req("POST", "/workflows", {"name": "tmp v8 cancel", "graph": cancel_graph, "is_active": False})
    assert status == 201, cwf
    try:
        status, acc = req("POST", f"/workflows/{cwf['id']}/run", {})
        cexec = acc["execution_id"]
        time.sleep(0.5)  # let it enter the delay node
        status, cres = req("POST", f"/executions/{cexec}/cancel")
        assert status == 202, cres
        for _ in range(40):
            status, run = req("GET", f"/executions/{cexec}")
            if run["status"] != "running":
                break
            time.sleep(0.2)
        assert run["status"] == "cancelled", run
        assert "after" not in {n["node_id"] for n in run["node_runs"]}, "cancel did not stop before next node"
        status, _ = req("POST", f"/executions/{cexec}/cancel")
        assert status == 409, "cancel after finish should 409"
        print("cancel mid-run → cancelled, next node never ran; re-cancel → 409")
    finally:
        req("DELETE", f"/workflows/{cwf['id']}")

    # 3) error-workflow routing with structured payload
    handler_graph = {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "parameters": {}},
            {"id": "grab", "type": "set_variable",
             "parameters": {"assignments": {
                 "src": "{{ execution.trigger_payload.workflow_name }}",
                 "err": "{{ execution.trigger_payload.error | string | truncate(80) }}",
             }, "keep_input": False}},
        ],
        "edges": [{"id": "e1", "source": "t", "target": "grab"}],
    }
    status, hwf = req("POST", "/workflows", {"name": "tmp v8 handler", "graph": handler_graph, "is_active": False})
    assert status == 201, hwf
    fail_graph = {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "parameters": {}},
            {"id": "boom", "type": "code", "parameters": {"code": "result = 1/0"}},
        ],
        "edges": [{"id": "e1", "source": "t", "target": "boom"}],
    }
    status, fwf = req("POST", "/workflows", {"name": "tmp v8 failing", "graph": fail_graph,
                                             "is_active": False, "error_workflow_id": hwf["id"]})
    assert status == 201, fwf
    try:
        status, acc = req("POST", f"/workflows/{fwf['id']}/run", {})
        fexec = acc["execution_id"]
        for _ in range(40):
            status, run = req("GET", f"/executions/{fexec}")
            if run["status"] != "running":
                break
            time.sleep(0.2)
        assert run["status"] == "error", run
        time.sleep(1.0)  # let the dispatched handler run finish
        status, hrows = req("GET", f"/executions?workflow_id={hwf['id']}&limit=5")
        err_runs = [r for r in hrows if r["trigger_type"] == "error"]
        assert err_runs, "error workflow was not dispatched"
        status, hdetail = req("GET", f"/executions/{err_runs[0]['id']}")
        tp = hdetail["trigger_payload"]
        assert tp["workflow_name"] == "tmp v8 failing" and "ZeroDivision" in tp["error"], tp
        assert tp["execution_id"] == fexec and tp["failed_nodes"][0]["node_id"] == "boom", tp
        grab = next(n for n in hdetail["node_runs"] if n["node_id"] == "grab")
        assert grab["status"] == "success" and grab["output"]["src"] == "tmp v8 failing", grab
        print("error workflow dispatched + ran:", grab["output"])

        # binding guards: unknown handler and self-binding rejected
        status, _ = req("POST", "/workflows", {"name": "tmp v8 badbind", "graph": fail_graph,
                                               "error_workflow_id": "no-such-id"})
        assert status == 400
        status, _ = req("PUT", f"/workflows/{fwf['id']}", {"error_workflow_id": fwf["id"]})
        assert status == 400
        status, wl = req("GET", "/workflows")
        entry = next(w for w in wl if w["id"] == fwf["id"])
        assert entry["error_workflow_name"] == "tmp v8 handler", entry
        print("binding guards OK; dashboard shows on error → tmp v8 handler")
    finally:
        req("DELETE", f"/workflows/{fwf['id']}")
        req("DELETE", f"/workflows/{hwf['id']}")

    # ------------------------------------------------------------- v10 wave
    print("== v10 credentials vault: update -> detail -> usage -> test -> protected delete ==")
    status, cred = req("POST", "/credentials", {
        "name": "tmp v10 header cred", "type": "header_auth",
        "data": {"header_name": "X-Api-Key", "value": "smoke-secret-9876"},
    })
    assert status == 201, cred
    cid = cred["id"]
    assert cred["masked_hint"].endswith("9876"), cred
    try:
        # rename + rotate
        status, upd = req("PATCH", f"/credentials/{cid}", {"name": "tmp v10 renamed"})
        assert status == 200 and upd["name"] == "tmp v10 renamed", upd
        status, upd = req("PATCH", f"/credentials/{cid}",
                          {"data": {"header_name": "X-Api-Key", "value": "rotated-4321"}})
        assert status == 200 and upd["masked_hint"].endswith("4321"), upd

        # detail view: non-secret visible, secret blanked
        status, det = req("GET", f"/credentials/{cid}")
        assert status == 200 and det["data"]["header_name"] == "X-Api-Key" and det["data"]["value"] == "", det

        # __keep__ marker preserves the stored secret
        status, upd = req("PATCH", f"/credentials/{cid}",
                          {"data": {"header_name": "Y-Key", "value": "__keep__"}})
        assert status == 200 and upd["masked_hint"].endswith("4321"), upd

        # usage: workflow referencing the credential from an http_request node
        ugraph = {
            "nodes": [
                {"id": "t", "type": "manual_trigger", "parameters": {}},
                {"id": "h", "type": "http_request",
                 "parameters": {"method": "GET", "url": "https://example.com", "credential_id": cid}},
            ],
            "edges": [{"id": "e1", "source": "t", "target": "h"}],
        }
        status, uwf = req("POST", "/workflows", {"name": "tmp v10 usage", "graph": ugraph, "is_active": False})
        assert status == 201, uwf
        status, usage = req("GET", f"/credentials/{cid}/usage")
        assert status == 200 and usage["workflow_count"] == 1 and usage["workflows"][0]["nodes"] == ["h"], usage
        print("usage tracked:", usage["workflows"][0]["name"], "node", usage["workflows"][0]["nodes"])

        # live probe against example.com (header_auth default target)
        status, probe = req("POST", f"/credentials/{cid}/test", {})
        assert status == 200 and probe["ok"] is True, probe
        print("live probe ok:", probe["message"], f"({probe['latency_ms']}ms)")

        # delete protection: 409 while referenced, force works
        status, conflict = req("DELETE", f"/credentials/{cid}")
        assert status == 409 and "force" in conflict["detail"], conflict
        status, _ = req("DELETE", f"/credentials/{cid}?force=true")
        assert status == 204
        status, gone = req("GET", f"/credentials/{cid}")
        assert status == 404
        print("protected delete (409) + force delete — OK")
    finally:
        req("DELETE", f"/workflows/{uwf['id']}")
        req("DELETE", f"/credentials/{cid}?force=true")

    # ------------------------------------------------------------- v11 wave
    print("== v11 insights: scoped aggregation -> timeline -> window guards ==")
    ok_graph = {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "parameters": {}},
            {"id": "c", "type": "code", "parameters": {"language": "python", "code": "out = {'v': 11}"}},
        ],
        "edges": [{"id": "e1", "source": "t", "target": "c"}],
    }
    bad_graph = {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "parameters": {}},
            {"id": "c", "type": "code", "parameters": {"language": "python", "code": "raise RuntimeError('boom-v11-smoke')"}},
        ],
        "edges": [{"id": "e1", "source": "t", "target": "c"}],
    }
    status, okwf = req("POST", "/workflows", {"name": "tmp v11 ok", "graph": ok_graph, "is_active": False})
    assert status == 201, okwf
    status, badwf = req("POST", "/workflows", {"name": "tmp v11 bad", "graph": bad_graph, "is_active": False})
    assert status == 201, badwf
    try:
        for _ in range(2):
            status, acc = req("POST", f"/workflows/{okwf['id']}/run", {"payload": {}})
            assert status in (200, 202), acc
        status, acc = req("POST", f"/workflows/{badwf['id']}/run", {"payload": {}})
        assert status in (200, 202), acc
        for _ in range(40):
            time.sleep(0.25)
            status, runs = req("GET", "/executions?limit=10")
            recent = [r for r in runs if r["workflow_id"] in (okwf["id"], badwf["id"])]
            if len(recent) >= 3 and all(r["status"] != "running" for r in recent):
                break
        assert len(recent) == 3 and {r["status"] for r in recent} == {"success", "error"}, recent

        # scoped aggregation is exact
        status, scoped = req("GET", f"/insights?days=3&workflow_id={okwf['id']}")
        assert status == 200, scoped
        s = scoped["summary"]
        assert s["total"] == 2 and s["success"] == 2 and s["success_rate"] == 100.0, s
        assert s["node_runs_total"] >= 4  # trigger + code per run
        assert scoped["timeline"][-1]["total"] == 2
        types = {n["node_type"] for n in scoped["node_stats"]}
        assert {"manual_trigger", "code"} <= types
        assert scoped["top_workflows"][0]["workflow_name"] == "tmp v11 ok"
        print(f"scoped: total={s['total']} rate={s['success_rate']}% avg={s['avg_duration_ms']}ms nodes={s['node_runs_total']}")

        # error workflow scoped → 0% finished-success rate
        status, badscoped = req("GET", f"/insights?days=3&workflow_id={badwf['id']}")
        bs = badscoped["summary"]
        assert bs["total"] == 1 and bs["error"] == 1 and bs["success_rate"] == 0.0, bs
        print("error-scoped rate semantics OK (finished-only)")

        # global view includes the new runs and zero-fills the window
        status, glob = req("GET", "/insights?days=5")
        assert status == 200 and len(glob["timeline"]) == 5 and glob["summary"]["total"] >= 3, glob["summary"]
        # leaderboard: capped at 8, ordered by runs descending
        assert len(glob["top_workflows"]) <= 8
        runs_list = [w["runs"] for w in glob["top_workflows"]]
        assert runs_list == sorted(runs_list, reverse=True), runs_list
        print(f"global: {glob['summary']['total']} runs, rate={glob['summary']['success_rate']}%, "
              f"triggers={glob['trigger_breakdown']}, top={glob['top_workflows'][0]['workflow_name']}")

        # window validation
        status, _ = req("GET", "/insights?days=0")
        assert status == 422, status
        status, empty = req("GET", "/insights?days=5&workflow_id=does-not-exist")
        assert status == 200 and empty["summary"]["total"] == 0 and empty["node_stats"] == [], empty
        print("window guards (422 / honest zeros) OK")
    finally:
        req("DELETE", f"/workflows/{okwf['id']}")
        req("DELETE", f"/workflows/{badwf['id']}")

    # ------------------------------------------------------------- v12 wave
    print("== v12 tags + search: normalize -> filter -> summary -> duplicate ==")
    plain_graph = {"nodes": [{"id": "t", "type": "manual_trigger", "parameters": {}}], "edges": []}
    uniq = str(time.time())[-6:]  # keep tags unique across smoke runs
    status, twf = req("POST", "/workflows", {
        "name": f"tmp v12 tagged {uniq}",
        "description": "smoke tag fixture",
        "graph": plain_graph, "is_active": False,
        "tags": ["  Smoke ", "SMOKE", "prod", "", 42],
    })
    assert status == 201, twf
    assert twf["tags"] == ["smoke", "prod"], twf["tags"]
    try:
        # tri-state PUT: omitted tags untouched; [] clears; replace works
        status, upd = req("PUT", f"/workflows/{twf['id']}", {"description": "x"})
        assert status == 200 and upd["tags"] == ["smoke", "prod"], upd
        status, upd = req("PUT", f"/workflows/{twf['id']}", {"tags": ["pipeline", uniq]})
        assert status == 200 and upd["tags"] == ["pipeline", uniq], upd

        # tag filter (case-insensitive query) + search on name
        status, rows = req("GET", f"/workflows?tag=PIPELINE")
        assert status == 200 and any(r["id"] == twf["id"] for r in rows), rows
        status, rows = req("GET", f"/workflows?search={quote(f'v12 tagged {uniq}')}")
        assert status == 200 and [r["id"] for r in rows] == [twf["id"]], rows
        status, rows = req("GET", "/workflows?tag=no-such-tag")
        assert status == 200 and rows == []

        # vocabulary summary carries our tags
        status, vocab = req("GET", "/workflows/tags")
        assert status == 200, vocab
        vmap = {v["tag"]: v["count"] for v in vocab}
        assert vmap.get("pipeline") >= 1 and vmap.get(uniq) == 1, vmap.get(uniq)

        # duplicate carries the tags
        status, dup = req("POST", f"/workflows/{twf['id']}/duplicate")
        assert status == 201 and dup["tags"] == ["pipeline", uniq], dup
        req("DELETE", f"/workflows/{dup['id']}")

        # clear via []
        status, upd = req("PUT", f"/workflows/{twf['id']}", {"tags": []})
        assert status == 200 and upd["tags"] == [], upd
        print("tags normalize + tri-state + filter/search + summary + duplicate — OK")
    finally:
        req("DELETE", f"/workflows/{twf['id']}")

    # ------------------------------------------------------------- v13 wave
    print("== v13 versioning: snapshot-on-save -> restore appends -> cap 20 ==")
    hist_graph = {"nodes": [{"id": "t", "type": "manual_trigger", "parameters": {}}], "edges": []}
    status, vwf = req("POST", "/workflows", {"name": f"tmp v13 hist {uniq}", "graph": hist_graph, "is_active": False})
    assert status == 201, vwf
    try:
        # v1 auto-snapshot on create
        status, hist = req("GET", f"/workflows/{vwf['id']}/versions")
        assert status == 200 and hist["latest"] == 1 and hist["versions"][0]["is_current"], hist

        # content saves bump versions; org changes don't
        two_node = {"nodes": [
            {"id": "t", "type": "manual_trigger", "parameters": {}},
            {"id": "s", "type": "set_variable", "parameters": {}},
        ], "edges": [{"id": "e", "source": "t", "target": "s"}]}
        status, _ = req("PUT", f"/workflows/{vwf['id']}", {"graph": two_node, "description": "now 2 nodes"})
        assert status == 200
        status, _ = req("PUT", f"/workflows/{vwf['id']}", {"tags": ["hist"]})  # no snapshot
        assert status == 200
        hist = req("GET", f"/workflows/{vwf['id']}/versions")[1]
        assert hist["latest"] == 2, hist

        # restore v1: content back, restore lands as v3
        status, snap1 = req("GET", f"/workflows/{vwf['id']}/versions/1")
        assert status == 200 and snap1["node_count"] == 1, snap1
        status, restored = req("POST", f"/workflows/{vwf['id']}/versions/1/restore")
        assert status == 200 and len(restored["graph"]["nodes"]) == 1, restored
        hist = req("GET", f"/workflows/{vwf['id']}/versions")[1]
        assert hist["latest"] == 3 and hist["versions"][0]["is_current"], hist
        assert [v["version"] for v in hist["versions"]] == [3, 2, 1]
        row = req("GET", f"/workflows/{vwf['id']}")[1]
        assert row["tags"] == ["hist"], "tags must survive restore"

        # cap: push to 25 total versions → only newest 20 kept
        for i in range(22):
            status, _ = req("PUT", f"/workflows/{vwf['id']}", {"description": f"gen {i}"})
            assert status == 200
        hist = req("GET", f"/workflows/{vwf['id']}/versions")[1]
        assert len(hist["versions"]) == 20 and hist["latest"] == 25, (len(hist["versions"]), hist["latest"])
        assert [v["version"] for v in hist["versions"]] == list(range(25, 5, -1))
        print(f"lifecycle OK; cap pruned to {len(hist['versions'])} (latest v{hist['latest']})")

        # 404 guards
        assert req("GET", "/workflows/nope/versions")[0] == 404
        assert req("GET", f"/workflows/{vwf['id']}/versions/99")[0] == 404
        assert req("POST", f"/workflows/{vwf['id']}/versions/99/restore")[0] == 404
        print("404 guards OK")
    finally:
        req("DELETE", f"/workflows/{vwf['id']}")

    # ------------------------------------------------------------- v14 wave
    # v14 is the Ctrl+K command palette (frontend-only); backend contract:
    # health reports the new version and the palette's two data sources
    # (workflow list + tag vocabulary) keep responding.
    print("== v14 palette: backend contract for the command palette ==")
    status, health = req("GET", "/health")
    ver = tuple(int(x) for x in health.get("version", "0").split(".")[:2])
    assert status == 200 and ver >= (1, 14), health
    print(f"health OK (version {health['version']}, mode {health['execution_mode']})")

    status, wl = req("GET", "/workflows")
    assert status == 200 and isinstance(wl, list) and wl, "workflow list must be non-empty"
    sample = wl[0]
    for key in ("id", "name", "tags", "is_active", "node_count", "trigger_types"):
        assert key in sample, f"palette item field missing: {key}"
    print(f"workflow list OK ({len(wl)} rows; fields id/name/tags/is_active/node_count present)")

    status, vocab = req("GET", "/workflows/tags")
    assert status == 200 and isinstance(vocab, list), vocab
    print(f"tag vocabulary OK ({len(vocab)} tags)")
    print("v14 backend contract OK")

    # ------------------------------------------------------------- v15 wave
    # Environment variables: CRUD + secret masking + {{ env.KEY }} resolution.
    print("== v15 env vars: CRUD -> masking -> template resolution in a run ==")
    uniq15 = uuid.uuid4().hex[:8]
    status, plain = req("POST", "/env-vars", {"key": f"smoke_plain_{uniq15}", "value": "plain-15", "description": "smoke"})
    assert status == 201 and plain["key"] == f"smoke_plain_{uniq15}" and plain["value"] == "plain-15", plain
    status, secret = req("POST", "/env-vars", {"key": f"SMOKE_TOK_{uniq15}", "value": "tok-15-secret", "is_secret": True})
    assert status == 201 and secret["value"] is None and secret["is_secret"] is True, secret
    # case-insensitive duplicate
    assert req("POST", "/env-vars", {"key": f"SMOKE_PLAIN_{uniq15}", "value": "x"})[0] == 409
    print("create + masking + dup-409 OK")

    # run a workflow that reads both vars via templates (secret resolves server-side)
    wf15_graph = {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "parameters": {}},
            {"id": "c", "type": "code", "parameters": {
                "code": (
                    "result = {'plain': '{{ env.smoke_plain_" + uniq15 + " }}', "
                    "'tok': '{{ env.SMOKE_TOK_" + uniq15 + " }}'}\n"
                ),
            }},
        ],
        "edges": [{"id": "e", "source": "t", "target": "c", "sourceHandle": "main", "targetHandle": "main"}],
    }
    status, wf15 = req("POST", "/workflows", {"name": f"tmp v15 env {uniq15}", "graph": wf15_graph, "is_active": False})
    assert status == 201, wf15
    try:
        status, run = req("POST", f"/workflows/{wf15['id']}/run", {"payload": {"hello": "v15"}})
        assert status in (200, 202), run
        exec_id = run["execution_id"]
        deadline = time.time() + 15
        detail = {}
        while time.time() < deadline:
            status, detail = req("GET", f"/executions/{exec_id}")
            assert status == 200
            if detail["status"] != "running":
                break
            time.sleep(0.3)
        assert detail["status"] == "success", detail.get("error")
        out = next(r for r in detail["node_runs"] if r["node_id"] == "c")["output"]
        assert out["result"]["plain"] == "plain-15", out
        assert out["result"]["tok"] == "tok-15-secret", out  # secrets resolve in-engine
        assert "env" not in (detail.get("context_snapshot") or {}), "env must never dump to logs"
        print(f"run OK in {detail['duration_ms']}ms — plain + secret resolved via {{ env.* }}")

        # unmask flip reveals the kept value; then cleanup vars
        status, tok_id = None, secret["id"]
        status, updated = req("PUT", f"/env-vars/{tok_id}", {"is_secret": False})
        assert status == 200 and updated["value"] == "tok-15-secret", updated
        assert req("DELETE", f"/env-vars/{tok_id}")[0] == 204
        assert req("DELETE", f"/env-vars/{plain['id']}")[0] == 204
        print("unmask flip + deletes OK")
    finally:
        req("DELETE", f"/workflows/{wf15['id']}")
        req("DELETE", f"/env-vars/{secret['id']}")
        req("DELETE", f"/env-vars/{plain['id']}")
    print("v15 env vars OK")

    # ------------------------------------------------------------- v16 wave
    # Folders: hierarchical grouping — CRUD, workflow assignment, filters.
    print("== v16 folders: CRUD -> assignment -> filters -> cascade-to-root ==")
    uniq16 = uuid.uuid4().hex[:8]
    status, health = req("GET", "/health")
    ver = tuple(int(x) for x in health.get("version", "0").split(".")[:2])
    assert status == 200 and ver >= (1, 16), health

    status, root16 = req("POST", "/folders", {"name": f"smoke root {uniq16}"})
    assert status == 201 and root16["parent_id"] is None, root16
    status, child16 = req("POST", "/folders", {"name": f"smoke child {uniq16}", "parent_id": root16["id"]})
    assert status == 201 and child16["parent_id"] == root16["id"], child16
    assert req("POST", "/folders", {"name": "  "})[0] == 400
    assert req("POST", "/folders", {"name": "x", "parent_id": "nope"})[0] == 400
    print("create + validation OK")

    status, wf16 = req("POST", "/workflows", {"name": f"tmp v16 filed {uniq16}", "folder_id": child16["id"], "is_active": False})
    assert status == 201 and wf16["folder_id"] == child16["id"], wf16
    status, dup16 = req("POST", f"/workflows/{wf16['id']}/duplicate")
    assert status == 201 and dup16["folder_id"] == child16["id"], dup16
    try:
        status, rows = req("GET", f"/workflows?folder_id={child16['id']}")
        ids = {r["id"] for r in rows}
        assert status == 200 and wf16["id"] in ids and dup16["id"] in ids
        assert all(r["folder_name"] == f"smoke child {uniq16}" for r in rows if r["id"] in ids)
        status, unfiled = req("GET", "/workflows?folder_id=none")
        assert all(r["id"] != wf16["id"] for r in unfiled)
        print("list enrichment + folder filters OK")

        # folder counts: root total includes the child's workflows
        status, frows = req("GET", "/folders")
        by_id = {f["id"]: f for f in frows}
        assert by_id[child16["id"]]["workflow_count"] == 2
        assert by_id[root16["id"]]["total_count"] == 2 and by_id[root16["id"]]["workflow_count"] == 0
        print("recursive counts OK")

        # tri-state PUT: "" moves to root; folder delete cascades workflows to root
        status, moved = req("PUT", f"/workflows/{wf16['id']}", {"folder_id": ""})
        assert status == 200 and moved["folder_id"] is None, moved
        assert req("DELETE", f"/folders/{child16['id']}")[0] == 204
        status, after = req("GET", f"/workflows/{dup16['id']}")
        assert status == 200 and after["folder_id"] is None, after
        print("move-to-root + delete-cascade OK")

        # delete refusal while subfolders exist
        status, child16b = req("POST", "/folders", {"name": f"smoke child b {uniq16}", "parent_id": root16["id"]})
        assert status == 201, child16b
        assert req("DELETE", f"/folders/{root16['id']}")[0] == 409
        assert req("DELETE", f"/folders/{child16b['id']}")[0] == 204
        print("409-with-children guard OK")
    finally:
        req("DELETE", f"/workflows/{wf16['id']}")
        req("DELETE", f"/workflows/{dup16['id']}")
        req("DELETE", f"/folders/{root16['id']}")
    print("v16 folders OK")

    # ------------------------------------------------------------- v17 wave
    # Pinned output data + single-node test step (n8n-style building loop).
    print("== v17 pins: pin -> manual honors -> webhook ignores -> test step ==")
    uniq17 = uuid.uuid4().hex[:8]
    status, health = req("GET", "/health")
    ver = tuple(int(x) for x in health.get("version", "0").split(".")[:2])
    assert status == 200 and ver >= (1, 17), health

    wf17_graph = {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "name": "Trigger", "position": {"x": 0, "y": 0}, "parameters": {"payload": {}}},
            {"id": "w", "type": "webhook_trigger", "name": "Hook", "position": {"x": 0, "y": 160}, "parameters": {"response_mode": "immediately"}},
            {"id": "c", "type": "code", "name": "Doubler", "position": {"x": 220, "y": 0},
             "parameters": {"code": "src = input_data.get('payload') or input_data.get('body') or {}\nresult = {'doubled': src.get('n', 0) * 2}\n"}},
        ],
        "edges": [
            {"id": "e1", "source": "t", "target": "c", "sourceHandle": "main", "targetHandle": "main"},
            {"id": "e2", "source": "w", "target": "c", "sourceHandle": "main", "targetHandle": "main"},
        ],
    }
    status, wf17 = req("POST", "/workflows", {"name": f"tmp v17 pin {uniq17}", "graph": wf17_graph, "is_active": False})
    assert status == 201, wf17

    def _set_pin(pin_value):
        status, cur = req("GET", f"/workflows/{wf17['id']}")
        assert status == 200
        for n in cur["graph"]["nodes"]:
            if n["id"] == "c":
                n["pinned_data"] = pin_value
        status, saved = req("PUT", f"/workflows/{wf17['id']}", {"graph": cur["graph"]})
        assert status == 200, saved
        return next(n for n in saved["graph"]["nodes"] if n["id"] == "c")

    def _wait_exec(exec_id):
        deadline, detail = time.time() + 15, {}
        while time.time() < deadline:
            status, detail = req("GET", f"/executions/{exec_id}")
            assert status == 200
            if detail["status"] != "running":
                return detail
            time.sleep(0.3)
        raise AssertionError("execution did not finish in time")

    try:
        saved_node = _set_pin({"result": {"doubled": 100}})
        assert saved_node["pinned_data"] == {"result": {"doubled": 100}}, saved_node
        print("pin persists through save OK")

        # manual run honors the pin: zero-duration fake output, flagged
        status, run = req("POST", f"/workflows/{wf17['id']}/run", {"payload": {"n": 7}})
        assert status in (200, 202), run
        detail = _wait_exec(run["execution_id"])
        assert detail["status"] == "success", detail.get("error")
        run_c = next(r for r in detail["node_runs"] if r["node_id"] == "c")
        assert run_c["output"] == {"result": {"doubled": 100}} and run_c.get("pinned") is True, run_c
        assert run_c["duration_ms"] == 0, run_c
        print("manual run honors pin OK")

        # production path: webhook fire ignores the pin — real execution
        _set_pin({"result": {"doubled": 100}})
        assert req("POST", f"/workflows/{wf17['id']}/activate")[0] == 200
        status, hook17 = req("POST", f"/webhooks/{wf17['id']}", {"n": 7})
        assert status in (200, 202) and hook17.get("execution_id"), hook17
        detail = _wait_exec(hook17["execution_id"])
        assert detail["status"] == "success", detail.get("error")
        run_c = next(r for r in detail["node_runs"] if r["node_id"] == "c")
        assert run_c["output"]["result"]["doubled"] == 14 and "pinned" not in run_c, run_c
        assert req("POST", f"/workflows/{wf17['id']}/deactivate")[0] == 200
        print("webhook fire ignores pin (real execution) OK")

        # test step: pinned preview returns exactly what a manual run would
        status, body = req("POST", f"/workflows/{wf17['id']}/nodes/c/test", {"items": {"payload": {"n": 5}}})
        assert status == 200 and body["ok"] and body["pinned_used"] is True, body
        assert body["output"] == {"result": {"doubled": 100}}, body

        # guards: unknown node 404; executions count untouched by test steps
        assert req("POST", f"/workflows/{wf17['id']}/nodes/ghost/test", {})[0] == 404
        status, execs_now = req("GET", f"/executions?workflow_id={wf17['id']}&limit=50")
        assert status == 200 and len(execs_now) == 2, len(execs_now)  # manual + webhook only
        print("test step pinned preview + guards OK")

        # unpin → test step REALLY executes with the ad-hoc input
        _set_pin(None)
        status, body = req("POST", f"/workflows/{wf17['id']}/nodes/c/test", {"items": {"payload": {"n": 21}}})
        assert status == 200 and body["ok"] and body["pinned_used"] is False, body
        assert body["output"]["result"]["doubled"] == 42, body
        assert body["duration_ms"] >= 0
        print("unpinned test step executes for real OK")
    finally:
        req("DELETE", f"/workflows/{wf17['id']}")
    print("v17 pins + test step OK")

    # ------------------------------------------------------------------ v19
    print("== v19: AI Agent node (real bridge tool loop) + retention ==")
    status, defs = req("GET", "/node-definitions")
    types = [d["type"] for d in defs["definitions"]]
    assert status == 200 and "ai_agent" in types and "sticky_note" not in types, types
    agent_def = next(d for d in defs["definitions"] if d["type"] == "ai_agent")
    tools_schema = agent_def["parameters_schema"]["properties"]["tools"]
    assert "$ref" not in json.dumps(tools_schema), tools_schema  # v19: nested ToolSpec inlined
    assert "kind" in json.dumps(tools_schema), tools_schema
    print("node definitions: ai_agent present, sticky hidden, tools schema inlined OK")

    status, health = req("GET", "/health")
    ver = tuple(int(x) for x in health.get("version", "0").split(".")[:2])
    assert status == 200 and ver >= (1, 19), health

    wf19_graph = {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "name": "Trigger", "position": {"x": 0, "y": 0}, "parameters": {}},
            {
                "id": "agent",
                "type": "ai_agent",
                "name": "Agent",
                "position": {"x": 220, "y": 0},
                "parameters": {
                    "system_prompt": "You are a terse assistant in a workflow smoke test.",
                    "user_message": "Use the tier_table tool to look up code A1, then state the tier in one short sentence.",
                    "max_iterations": 4,
                    "tools": [
                        {
                            "kind": "knowledge",
                            "name": "tier_table",
                            "description": "Maps customer codes to loyalty tiers",
                            "content": "code A1 = GOLD tier; code B2 = SILVER tier",
                        }
                    ],
                },
            },
        ],
        "edges": [{"id": "e1", "source": "t", "target": "agent", "sourceHandle": "main", "targetHandle": "main"}],
    }
    status, wf19 = req("POST", "/workflows", {"name": f"tmp v19 agent {uuid.uuid4().hex[:6]}", "graph": wf19_graph})
    assert status == 201, wf19
    try:
        status, body = req("POST", f"/workflows/{wf19['id']}/run", {"payload": {}})
        assert status in (200, 202), body
        exec_id = body["execution_id"]
        detail = None
        for _ in range(120):
            status, detail = req("GET", f"/executions/{exec_id}")
            assert status == 200
            if detail["status"] != "running":
                break
            time.sleep(0.5)
        assert detail["status"] == "success", detail.get("error")
        agent_run = next(r for r in detail["node_runs"] if r["node_id"] == "agent")
        out = agent_run["output"]
        assert out["answer"], out
        assert out["tools_available"] == ["tier_table"], out
        assert isinstance(out["iterations"], int) and out["iterations"] >= 1
        print(f"agent answered via bridge (iterations={out['iterations']}, tool_calls={len(out['tool_calls'])}) OK")
    finally:
        req("DELETE", f"/workflows/{wf19['id']}")

    # retention policy API
    status, pol = req("GET", "/settings/retention")
    assert status == 200 and "retention_days" in pol, pol
    original = pol
    status, pol = req("PUT", "/settings/retention", {"retention_days": 30, "max_executions_per_workflow": 2})
    assert status == 200 and pol["retention_days"] == 30 and pol["max_executions_per_workflow"] == 2, pol
    status, purged = req("POST", "/settings/retention/purge", {})
    assert status == 200 and "total" in purged, purged
    status, pol = req("GET", "/settings/retention")
    assert status == 200 and pol["last_purge_deleted"] >= purged["total"], pol
    status, pol = req("PUT", "/settings/retention", {
        "retention_days": original["retention_days"],
        "max_executions_per_workflow": original["max_executions_per_workflow"],
    })
    assert status == 200, pol
    print(f"retention policy + purge OK (last purge removed {purged['total']} records)")
    print("v19 agent + retention OK")

    # ------------------------------------------------------------------ v20
    print("== v20: per-workflow retention override + settings surface ==")
    status, wf20 = req("POST", "/workflows", {"name": f"tmp v20 override {uuid.uuid4().hex[:6]}", "graph": {
        "nodes": [{"id": "t", "type": "manual_trigger", "name": "T", "position": {"x": 0, "y": 0}, "parameters": {}}],
        "edges": [],
    }})
    assert status == 201, wf20
    try:
        status, body = req("PUT", f"/workflows/{wf20['id']}", {"retention_days": 0, "description": "v20 settings modal"})
        assert status == 200 and body["retention_days"] == 0, body
        # omitted = untouched
        status, body = req("PUT", f"/workflows/{wf20['id']}", {"name": body["name"]})
        assert status == 200 and body["retention_days"] == 0, body
        # null = back to inherit
        status, body = req("PUT", f"/workflows/{wf20['id']}", {"retention_days": None})
        assert status == 200 and body["retention_days"] is None, body
        # negative rejected
        status, _ = req("PUT", f"/workflows/{wf20['id']}", {"retention_days": -2})
        assert status in (400, 422)
        # list items expose the field
        status, wl = req("GET", "/workflows")
        item = next(w for w in wl if w["id"] == wf20["id"])
        assert "retention_days" in item and item["retention_days"] is None, item
        print("retention override tri-state (0 / null / omitted / negative) OK")
    finally:
        req("DELETE", f"/workflows/{wf20['id']}")
    status, pol = req("GET", "/settings/retention")
    assert status == 200 and pol["retention_days"] == 30, pol
    print("v20 overrides OK")

    # ------------------------------------------------------------------ v21
    print("== v21: respond_to_webhook node (custom mid-flow HTTP response) ==")
    status, defs = req("GET", "/node-definitions")
    assert status == 200
    types21 = [d["type"] for d in defs["definitions"]]
    assert len(types21) == 30 and "respond_to_webhook" in types21, types21  # 30 after v25
    rdef = next(d for d in defs["definitions"] if d["type"] == "respond_to_webhook")
    assert rdef["category"] == "actions" and rdef["icon"] == "reply", rdef
    print("21 node types, respond_to_webhook exported OK")

    status, wf21 = req("POST", "/workflows", {"name": f"tmp v21 respond {uuid.uuid4().hex[:6]}", "graph": {
        "nodes": [
            {"id": "h", "type": "webhook_trigger", "name": "Hook", "position": {"x": 0, "y": 0},
             "parameters": {"response_mode": "respond_node", "allowed_methods": "POST"}},
            {"id": "e", "type": "code", "name": "Enricher", "position": {"x": 220, "y": 0},
             "parameters": {"code": "src = input_data.get('body') or {}\nresult = {'ticket': src.get('ticket'), 'level': (src.get('level') or 'normal')}\n"}},
            {"id": "r", "type": "respond_to_webhook", "name": "Answer caller", "position": {"x": 440, "y": 0},
             "parameters": {"status_code": 202,
                            "body": '{"ticket": "{{ nodes.e.output.result.ticket }}", "level": "{{ nodes.e.output.result.level }}", "accepted": true}',
                            "content_type": "application/json"}},
            {"id": "d", "type": "set_variable", "name": "After respond", "position": {"x": 660, "y": 0},
             "parameters": {"assignments": {"done": "{{ nodes.e.output.result.ticket }}"}, "keep_input": False}},
        ],
        "edges": [
            {"id": "e1", "source": "h", "target": "e", "sourceHandle": "main", "targetHandle": "main"},
            {"id": "e2", "source": "e", "target": "r", "sourceHandle": "main", "targetHandle": "main"},
            {"id": "e3", "source": "r", "target": "d", "sourceHandle": "main", "targetHandle": "main"},
        ],
    }, "is_active": False})
    assert status == 201, wf21
    hook21 = wf21
    try:
        status, act = req("POST", f"/workflows/{wf21['id']}/activate")
        assert status == 200 and act["is_active"] is True, act
        # REAL webhook call: custom 202 + resolved JSON body
        status, body = req("POST", f"/webhooks/{wf21['id']}", {"ticket": "T-777", "level": "urgent"})
        assert status == 202, (status, body)
        assert body == {"ticket": "T-777", "level": "urgent", "accepted": True}, body
        # flow kept running after the respond — downstream node executed
        status, execs = req("GET", f"/executions?workflow_id={wf21['id']}&limit=5")
        wh_execs = [e for e in execs if e["trigger_type"] == "webhook"]
        assert len(wh_execs) == 1, execs
        detail = None
        for _ in range(40):
            status, detail = req("GET", f"/executions/{wh_execs[0]['id']}")
            if detail["status"] != "running":
                break
            time.sleep(0.1)
        assert detail["status"] == "success", detail.get("error")
        runs21 = {r["node_id"]: r for r in detail["node_runs"]}
        assert runs21["r"]["status"] == "success" and runs21["d"]["status"] == "success", runs21
        assert runs21["d"]["output"]["done"] == "T-777"
        print("real webhook: 202 custom body + downstream ran after respond OK")
        # respond_node mode without a respond node -> 404
        status, wf21b = req("POST", "/workflows", {"name": f"tmp v21 noresp {uuid.uuid4().hex[:6]}", "graph": {
            "nodes": [
                {"id": "h", "type": "webhook_trigger", "name": "Hook", "position": {"x": 0, "y": 0},
                 "parameters": {"response_mode": "respond_node"}},
                {"id": "s", "type": "set_variable", "name": "Map", "position": {"x": 220, "y": 0},
                 "parameters": {"assignments": {"x": "1"}, "keep_input": False}},
            ],
            "edges": [{"id": "e1", "source": "h", "target": "s", "sourceHandle": "main", "targetHandle": "main"}],
        }, "is_active": False})
        assert status == 201, wf21b
        status, act = req("POST", f"/workflows/{wf21b['id']}/activate")
        assert status == 200, act
        status, body = req("POST", f"/webhooks/{wf21b['id']}", {"ping": 1})
        assert status == 404 and "without calling" in body["detail"], (status, body)
        print("respond_node mode without respond node -> 404 OK")
    finally:
        req("DELETE", f"/workflows/{wf21['id']}")
        req("DELETE", f"/workflows/{wf21b['id']}")
    print("v21 respond-to-webhook OK")

    # ---------------------------------------------------------------
    # v22: data ops (sort/limit/remove_duplicates), stop-and-error,
    # and the error-trigger handler end-to-end
    # ---------------------------------------------------------------
    print("\n== v22: data ops + stop-and-error + error trigger ==")
    for t in ("error_trigger", "stop_and_error", "sort", "limit", "remove_duplicates"):
        assert t in types, f"missing v22 node {t}"
    # error trigger def: source-only trigger node
    et_def = next(d for d in defs["definitions"] if d["type"] == "error_trigger")
    assert et_def["inputs"] == [] and et_def["category"] == "triggers", et_def
    print("29 node types incl. 5 v22 nodes; error trigger def OK")

    status, wf22 = req("POST", "/workflows", {"name": f"tmp v22 ops {uuid.uuid4().hex[:6]}", "graph": {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "name": "Trigger", "position": {"x": 0, "y": 0},
             "parameters": {"payload": {"items": [
                 {"name": "b", "price": 3}, {"name": "a", "price": 10},
                 {"name": "a", "price": 1}, {"name": "d", "price": 7},
             ]}}},
            {"id": "s", "type": "sort", "name": "Sort", "position": {"x": 200, "y": 0},
             "parameters": {"field": "price", "direction": "asc"}},
            {"id": "l", "type": "limit", "name": "Top2", "position": {"x": 400, "y": 0},
             "parameters": {"max_items": 2, "keep": "last"}},
            {"id": "d", "type": "remove_duplicates", "name": "Dedupe", "position": {"x": 600, "y": 0},
             "parameters": {"field": "name"}},
        ],
        "edges": [
            {"id": "e1", "source": "t", "target": "s", "sourceHandle": "main", "targetHandle": "main"},
            {"id": "e2", "source": "s", "target": "l", "sourceHandle": "main", "targetHandle": "main"},
            {"id": "e3", "source": "l", "target": "d", "sourceHandle": "main", "targetHandle": "main"},
        ],
    }})
    assert status == 201, wf22
    try:
        status, run = req("POST", f"/workflows/{wf22['id']}/run", {"payload": {}})
        assert status in (200, 202), run
        exec_id22 = run["execution_id"]
        detail22 = None
        for _ in range(40):
            status, detail22 = req("GET", f"/executions/{exec_id22}")
            if detail22["status"] != "running":
                break
            time.sleep(0.1)
        assert detail22["status"] == "success", detail22.get("error")
        runs22 = {r["node_id"]: r for r in detail22["node_runs"]}
        # sort asc: 1(a),3(b),7(d),10(a); limit last 2: 7(d),10(a); dedupe by name: both distinct, kept
        got = runs22["d"]["output"]["items"]
        assert [i["name"] for i in got] == ["d", "a"], got
        assert runs22["d"]["output"]["duplicates_removed"] == 0
        print("sort -> limit -> remove_duplicates chain OK:", got)
    finally:
        req("DELETE", f"/workflows/{wf22['id']}")

    # stop-and-error -> deliberate run failure with resolved message
    status, wf22b = req("POST", "/workflows", {"name": f"tmp v22 halt {uuid.uuid4().hex[:6]}", "graph": {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "name": "Trigger", "position": {"x": 0, "y": 0},
             "parameters": {"payload": {"order_id": "ORD-42"}}},
            {"id": "h", "type": "stop_and_error", "name": "Halt", "position": {"x": 200, "y": 0},
             "parameters": {"error_message": "Order {{ nodes.t.output.payload.order_id }} is invalid", "error_type": "ValidationError"}},
        ],
        "edges": [{"id": "e1", "source": "t", "target": "h", "sourceHandle": "main", "targetHandle": "main"}],
    }})
    assert status == 201, wf22b
    # handler workflow with the error trigger
    status, wf22h = req("POST", "/workflows", {"name": f"tmp v22 handler {uuid.uuid4().hex[:6]}", "graph": {
        "nodes": [
            {"id": "et", "type": "error_trigger", "name": "On Error", "position": {"x": 0, "y": 0}, "parameters": {}},
            {"id": "a", "type": "set_variable", "name": "Alert", "position": {"x": 200, "y": 0},
             "parameters": {"assignments": {"msg": "WF {{ nodes.et.output.workflow_name }}: {{ nodes.et.output.error }}"}, "keep_input": False}},
        ],
        "edges": [{"id": "e1", "source": "et", "target": "a", "sourceHandle": "main", "targetHandle": "main"}],
    }})
    assert status == 201, wf22h
    try:
        # bind handler to the failing workflow
        status, patched = req("PATCH" if False else "PUT", f"/workflows/{wf22b['id']}", {"error_workflow_id": wf22h["id"]})
        assert status == 200 and patched["error_workflow_id"] == wf22h["id"], patched
        status, run = req("POST", f"/workflows/{wf22b['id']}/run", {"payload": {}})
        assert status in (200, 202), run
        detail22b = None
        for _ in range(40):
            status, detail22b = req("GET", f"/executions/{run['execution_id']}")
            if detail22b["status"] != "running":
                break
            time.sleep(0.1)
        assert detail22b["status"] == "error", detail22b
        assert "Order ORD-42 is invalid" in (detail22b.get("error") or ""), detail22b.get("error")
        # handler ran with trigger_type=error and resolved the alert
        h_exec = None
        for _ in range(40):
            status, execs22 = req("GET", f"/executions?workflow_id={wf22h['id']}&limit=5")
            err_runs = [e for e in execs22 if e["trigger_type"] == "error"]
            if err_runs and err_runs[0]["status"] != "running":
                h_exec = err_runs[0]
                break
            time.sleep(0.1)
        assert h_exec and h_exec["status"] == "success", h_exec
        status, h_detail = req("GET", f"/executions/{h_exec['id']}")
        runs_h = {r["node_id"]: r for r in h_detail["node_runs"]}
        alert_out = str(runs_h["a"]["output"])
        assert "Order ORD-42 is invalid" in alert_out, alert_out
        print("stop-and-error -> error workflow -> error trigger handler OK")
    finally:
        req("DELETE", f"/workflows/{wf22b['id']}")
        req("DELETE", f"/workflows/{wf22h['id']}")
    print("v22 data ops + stop-and-error + error trigger OK")

    # ---------------------------------------------------------------
    # v23: agent session memory + webhook authentication
    # ---------------------------------------------------------------
    print("\n== v23: agent session memory + webhook auth ==")
    # webhook header auth: 401 without, 202 with
    status, wfh = req("POST", "/workflows", {"name": f"tmp v23 auth {uuid.uuid4().hex[:6]}", "graph": {
        "nodes": [
            {"id": "h", "type": "webhook_trigger", "name": "Hook", "position": {"x": 0, "y": 0},
             "parameters": {"response_mode": "immediately", "auth_mode": "header",
                            "auth_header_name": "X-Smoke-Token", "auth_header_value": "tok-123"}},
            {"id": "s", "type": "set_variable", "name": "Set", "position": {"x": 200, "y": 0},
             "parameters": {"assignments": {"ok": "1"}, "keep_input": False}},
        ],
        "edges": [{"id": "e1", "source": "h", "target": "s", "sourceHandle": "main", "targetHandle": "main"}],
    }})
    assert status == 201, wfh
    try:
        status, act = req("POST", f"/workflows/{wfh['id']}/activate")
        assert status == 200, act
        status, body = req("POST", f"/webhooks/{wfh['id']}", {"ping": 1})
        assert status == 401, (status, body)
        status, body = req("POST", f"/webhooks/{wfh['id']}", {"ping": 1}, headers={"X-Smoke-Token": "wrong"})
        assert status == 401, (status, body)
        status, body = req("POST", f"/webhooks/{wfh['id']}", {"ping": 1}, headers={"X-Smoke-Token": "tok-123"})
        assert status == 202, (status, body)
        print("webhook header auth: 401/401/202 + single execution OK")
    finally:
        req("DELETE", f"/workflows/{wfh['id']}")

    # agent memory with the REAL bridge: turn 2 must load turn 1 from the store
    mem_key = f"smoke-v23-{uuid.uuid4().hex[:6]}"
    status, wfm = req("POST", "/workflows", {"name": f"tmp v23 mem {uuid.uuid4().hex[:6]}", "graph": {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "name": "Trigger", "position": {"x": 0, "y": 0},
             "parameters": {"payload": {}}},
            {"id": "ag", "type": "ai_agent", "name": "Agent", "position": {"x": 200, "y": 0},
             "parameters": {"memory": "buffer", "session_key": mem_key, "max_history_turns": 3,
                            "user_message": "My favorite color is teal. Just acknowledge it briefly."}},
        ],
        "edges": [{"id": "e1", "source": "t", "target": "ag", "sourceHandle": "main", "targetHandle": "main"}],
    }})
    assert status == 201, wfm
    try:
        status, run1 = req("POST", f"/workflows/{wfm['id']}/run", {"payload": {}})
        assert status in (200, 202), run1
        d1 = None
        for _ in range(120):
            status, d1 = req("GET", f"/executions/{run1['execution_id']}")
            if d1["status"] != "running":
                break
            time.sleep(0.1)
        assert d1["status"] == "success", d1.get("error")
        out1 = next(r["output"] for r in d1["node_runs"] if r["node_id"] == "ag")
        assert out1["memory_turns_loaded"] == 0 and out1["memory_key"] == mem_key, out1
        print("memory run 1: stored, nothing loaded OK ->", str(out1["answer"])[:60])

        # run 2 on a NEW execution asks about the color — agent must recall via injected history
        status, r2res = req("POST", f"/workflows/{wfm['id']}/run", {"payload": {}})
        exec2 = r2res["execution_id"]
        d2 = None
        for _ in range(120):
            status, d2 = req("GET", f"/executions/{exec2}")
            if d2["status"] != "running":
                break
            time.sleep(0.1)
        assert d2["status"] == "success", d2.get("error")
        out2 = next(r["output"] for r in d2["node_runs"] if r["node_id"] == "ag")
        assert out2["memory_turns_loaded"] == 1, out2
        print("memory run 2: prior turn injected OK ->", str(out2["answer"])[:80])
    finally:
        req("DELETE", f"/workflows/{wfm['id']}")
        # drop the memory row
        import sqlite3
        db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "mini-services", "api-backend", "data", "py8n.db")
        con = sqlite3.connect(db_path)
        con.execute("DELETE FROM agent_memories WHERE session_key = ?", (mem_key,))
        con.commit()
        con.close()
    print("v23 agent memory + webhook auth OK")

    # ---------------------------------------------------------------
    # v24: compare datasets + summarize + csv
    # ---------------------------------------------------------------
    print("\n== v24: compare datasets + summarize + csv ==")
    # definitions: 29 types, compare_datasets exposes 2 inputs / 3 outputs
    status, defs24 = req("GET", "/node-definitions")
    assert status == 200
    types24 = [d["type"] for d in defs24["definitions"]]
    assert len(types24) == 30, f"expected 30 node types after v25, got {len(types24)}"
    cmp_def = next(d for d in defs24["definitions"] if d["type"] == "compare_datasets")
    assert [h["key"] for h in cmp_def["inputs"]] == ["main", "secondary"], cmp_def["inputs"]
    assert [h["key"] for h in cmp_def["outputs"]] == ["matched", "a_only", "b_only"]
    assert "summarize" in types24 and "csv" in types24
    print("29 node types incl. 3 v24 nodes; compare_datasets 2-in/3-out OK")

    # live reconciliation: two split_out sources -> compare -> 3 routed branches
    status, wf24 = req("POST", "/workflows", {"name": f"tmp v24 recon {uuid.uuid4().hex[:6]}", "graph": {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "name": "Trigger", "position": {"x": 0, "y": 0}, "parameters": {}},
            {"id": "sa", "type": "split_out", "name": "CRM", "position": {"x": 150, "y": -80}, "parameters": {"field": "a"}},
            {"id": "sb", "type": "split_out", "name": "Billing", "position": {"x": 150, "y": 80}, "parameters": {"field": "b"}},
            {"id": "cmp", "type": "compare_datasets", "name": "Reconcile", "position": {"x": 320, "y": 0},
             "parameters": {"field_a": "sku", "field_b": "sku"}},
            {"id": "mo", "type": "set_variable", "name": "Matched Out", "position": {"x": 500, "y": -120},
             "parameters": {"assignments": {"n": "{{ input | length }}"}, "keep_input": False}},
            {"id": "ao", "type": "set_variable", "name": "A Only Out", "position": {"x": 500, "y": 0},
             "parameters": {"assignments": {"n": "{{ input | length }}"}, "keep_input": False}},
            {"id": "bo", "type": "set_variable", "name": "B Only Out", "position": {"x": 500, "y": 120},
             "parameters": {"assignments": {"n": "{{ input | length }}"}, "keep_input": False}},
        ],
        "edges": [
            {"id": "e1", "source": "t", "target": "sa", "sourceHandle": "main", "targetHandle": "main"},
            {"id": "e2", "source": "t", "target": "sb", "sourceHandle": "main", "targetHandle": "main"},
            {"id": "e3", "source": "sa", "target": "cmp", "sourceHandle": "main", "targetHandle": "main"},
            {"id": "e4", "source": "sb", "target": "cmp", "sourceHandle": "main", "targetHandle": "secondary"},
            {"id": "e5", "source": "cmp", "target": "mo", "sourceHandle": "matched", "targetHandle": "main"},
            {"id": "e6", "source": "cmp", "target": "ao", "sourceHandle": "a_only", "targetHandle": "main"},
            {"id": "e7", "source": "cmp", "target": "bo", "sourceHandle": "b_only", "targetHandle": "main"},
        ],
    }})
    assert status == 201, wf24
    try:
        status, run = req("POST", f"/workflows/{wf24['id']}/run", {"payload": {
            "a": [{"sku": "S1"}, {"sku": "S2"}, {"sku": "S3"}],
            "b": [{"sku": "S2", "paid": True}, {"sku": "S9", "paid": False}],
        }})
        assert status in (200, 202), run
        d24 = None
        for _ in range(40):
            status, d24 = req("GET", f"/executions/{run['execution_id']}")
            if d24["status"] != "running":
                break
            time.sleep(0.1)
        assert d24["status"] == "success", d24.get("error")
        runs24 = {r["node_id"]: r for r in d24["node_runs"]}
        assert runs24["cmp"]["output"] == {"matched": 1, "a_only": 2, "b_only": 1, "b_duplicates_skipped": 0}, runs24["cmp"]["output"]
        assert runs24["mo"]["output"]["n"] == 1  # S2 pair
        assert runs24["ao"]["output"]["n"] == 2  # S1, S3
        assert runs24["bo"]["output"]["n"] == 1  # S9
        print("compare datasets live: 2-in routing -> matched/a_only/b_only branches OK")
    finally:
        req("DELETE", f"/workflows/{wf24['id']}")

    # csv parse -> summarize group-by chain
    status, wf24b = req("POST", "/workflows", {"name": f"tmp v24 csvsum {uuid.uuid4().hex[:6]}", "graph": {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "name": "Trigger", "position": {"x": 0, "y": 0}, "parameters": {}},
            {"id": "p", "type": "csv", "name": "Parse", "position": {"x": 150, "y": 0},
             "parameters": {"mode": "parse", "content": "{{ input.payload.sheet }}", "auto_convert": True}},
            {"id": "s", "type": "summarize", "name": "By Dept", "position": {"x": 320, "y": 0},
             "parameters": {"group_by": ["dept"], "aggregates": [{"field": "salary", "op": "sum"}, {"field": "salary", "op": "avg"}]}},
        ],
        "edges": [
            {"id": "e1", "source": "t", "target": "p", "sourceHandle": "main", "targetHandle": "main"},
            {"id": "e2", "source": "p", "target": "s", "sourceHandle": "main", "targetHandle": "main"},
        ],
    }})
    assert status == 201, wf24b
    try:
        sheet = "name,dept,salary\nAnn,eng,120\nBob,eng,80\nCid,ops,100\n"
        status, run = req("POST", f"/workflows/{wf24b['id']}/run", {"payload": {"sheet": sheet}})
        assert status in (200, 202), run
        d24b = None
        for _ in range(40):
            status, d24b = req("GET", f"/executions/{run['execution_id']}")
            if d24b["status"] != "running":
                break
            time.sleep(0.1)
        assert d24b["status"] == "success", d24b.get("error")
        runs24b = {r["node_id"]: r for r in d24b["node_runs"]}
        parsed_items = runs24b["p"]["output"]["items"]
        assert parsed_items == [{"name": "Ann", "dept": "eng", "salary": 120},
                                {"name": "Bob", "dept": "eng", "salary": 80},
                                {"name": "Cid", "dept": "ops", "salary": 100}], parsed_items
        groups = {g["dept"]: g for g in runs24b["s"]["output"]["items"]}
        assert groups["eng"]["salary_sum"] == 200 and groups["eng"]["salary_avg"] == 100.0
        assert groups["ops"]["salary_sum"] == 100 and runs24b["s"]["output"]["groups"] == 2
        print("csv parse -> summarize group-by chain OK:", groups)
    finally:
        req("DELETE", f"/workflows/{wf24b['id']}")
    print("v24 compare + summarize + csv OK")

    # ---------------------------------------------------------------
    # v25: chat trigger + /chat endpoint (last_node + respond_node)
    # ---------------------------------------------------------------
    print("\n== v25: chat trigger + chat endpoint ==")
    status, defs25 = req("GET", "/node-definitions")
    assert status == 200
    chat_def = next(d for d in defs25["definitions"] if d["type"] == "chat_trigger")
    assert chat_def["category"] == "triggers" and chat_def["inputs"] == []
    chat_params = chat_def["parameters_schema"]["properties"]
    assert chat_params["response_mode"]["options"] == ["last_node", "respond_node"]
    assert "welcome_message" in chat_params

    # live chat: last_node mode echoes the message back, session_id round-trips
    status, wf25 = req("POST", "/workflows", {"name": f"tmp v25 chat {uuid.uuid4().hex[:6]}", "graph": {
        "nodes": [
            {"id": "chat1", "type": "chat_trigger", "name": "Chat Trigger", "position": {"x": 0, "y": 0},
             "parameters": {"response_mode": "last_node"}},
            {"id": "rep", "type": "set_variable", "name": "Reply", "position": {"x": 200, "y": 0},
             "parameters": {"assignments": {"reply": "Echo: {{ nodes.chat1.output.message }} ({{ nodes.chat1.output.session_id }})"}}},
        ],
        "edges": [
            {"id": "e1", "source": "chat1", "target": "rep", "sourceHandle": "main", "targetHandle": "main"},
        ],
    }})
    assert status == 201, wf25
    try:
        status, act = req("POST", f"/workflows/{wf25['id']}/activate")
        assert status in (200, 201), act
        status, resp = req("POST", f"/chat/{wf25['id']}", {"message": "hello smoke", "session_id": "smoke-s1"})
        assert status == 200, resp
        assert resp["status"] == "success" and resp["session_id"] == "smoke-s1"
        assert resp["reply"] == "Echo: hello smoke (smoke-s1)", resp["reply"]
        # execution record attributed to the chat trigger
        status, ex = req("GET", f"/executions/{resp['execution_id']}")
        assert status == 200 and ex["trigger_type"] == "chat"
        print("chat last_node: echo reply + session round-trip + trigger_type=chat OK")

        # respond_node variant answers mid-flow with the custom body
        status, wf25b = req("POST", "/workflows", {"name": f"tmp v25 chat-r {uuid.uuid4().hex[:6]}", "graph": {
            "nodes": [
                {"id": "chat1", "type": "chat_trigger", "name": "Chat Trigger", "position": {"x": 0, "y": 0},
                 "parameters": {"response_mode": "respond_node"}},
                {"id": "rw", "type": "respond_to_webhook", "name": "Respond", "position": {"x": 200, "y": 0},
                 "parameters": {"status_code": 200, "content_type": "application/json",
                                "body": '{"reply": "custom: {{ nodes.chat1.output.message }}"}'}},
            ],
            "edges": [
                {"id": "e1", "source": "chat1", "target": "rw", "sourceHandle": "main", "targetHandle": "main"},
            ],
        }})
        assert status == 201, wf25b
        try:
            status, act = req("POST", f"/workflows/{wf25b['id']}/activate")
            assert status in (200, 201), act
            status, resp = req("POST", f"/chat/{wf25b['id']}", {"message": "mid-flow", "session_id": "smoke-s2"})
            assert status == 200, resp
            assert resp == {"reply": "custom: mid-flow"}, resp
            print("chat respond_node: custom mid-flow reply OK")
        finally:
            req("DELETE", f"/workflows/{wf25b['id']}")

        # guard: chat endpoint on a workflow without a chat trigger -> 409
        status, resp = req("POST", f"/chat/{hook['id']}", {"message": "x"})
        assert status == 409 and "no Chat Trigger" in resp.get("detail", ""), (status, resp)
        print("chat guard: no-chat-trigger workflow -> 409 OK")
    finally:
        req("DELETE", f"/workflows/{wf25['id']}")
    print("v25 chat trigger + endpoint OK")

    for wf in (pipe, child, parent, imported, dup, integ, hook, integ2):
        req("DELETE", f"/workflows/{wf['id']}")
    print("cleaned up temp workflows")

    print("\nALL SMOKE TESTS PASSED ✅")


if __name__ == "__main__":
    main()
