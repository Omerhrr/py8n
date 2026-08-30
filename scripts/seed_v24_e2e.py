"""Seed the v24 E2E demo workflow ('v24 Ledger Audit')."""
import json
import urllib.request

API = "http://localhost:8000/api/v1"


def api(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        API + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


GRAPH = {
    "nodes": [
        {"id": "t", "type": "manual_trigger", "name": "Trigger", "position": {"x": -350, "y": 120}, "parameters": {}},
        {"id": "crm", "type": "split_out", "name": "CRM Contacts", "position": {"x": -130, "y": -10},
         "parameters": {"field": "crm"}},
        {"id": "bill", "type": "split_out", "name": "Billing Accounts", "position": {"x": -130, "y": 250},
         "parameters": {"field": "billing"}},
        {"id": "cmp", "type": "compare_datasets", "name": "Reconcile", "position": {"x": 170, "y": 120},
         "parameters": {"field_a": "email", "field_b": "email"}},
        {"id": "mo", "type": "set_variable", "name": "Synced", "position": {"x": 470, "y": -60},
         "parameters": {"assignments": {"matched": "{{ input | length }}"}, "keep_input": False}},
        {"id": "ao", "type": "set_variable", "name": "Missing In Billing", "position": {"x": 470, "y": 120},
         "parameters": {"assignments": {"orphans": "{{ input | length }}"}, "keep_input": False}},
        {"id": "bo", "type": "set_variable", "name": "Missing In CRM", "position": {"x": 470, "y": 300},
         "parameters": {"assignments": {"ghosts": "{{ input | length }}"}, "keep_input": False}},
    ],
    "edges": [
        {"id": "e1", "source": "t", "target": "crm", "sourceHandle": "main", "targetHandle": "main"},
        {"id": "e2", "source": "t", "target": "bill", "sourceHandle": "main", "targetHandle": "main"},
        {"id": "e3", "source": "crm", "target": "cmp", "sourceHandle": "main", "targetHandle": "main"},
        {"id": "e4", "source": "bill", "target": "cmp", "sourceHandle": "main", "targetHandle": "secondary"},
        {"id": "e5", "source": "cmp", "target": "mo", "sourceHandle": "matched", "targetHandle": "main"},
        {"id": "e6", "source": "cmp", "target": "ao", "sourceHandle": "a_only", "targetHandle": "main"},
        {"id": "e7", "source": "cmp", "target": "bo", "sourceHandle": "b_only", "targetHandle": "main"},
    ],
}

status, wf = api("POST", "/workflows", {"name": "v24 Ledger Audit", "graph": GRAPH})
assert status == 201, (status, wf)
print("seeded:", wf["id"])

# sanity: run it once via API so there is history
status, run = api("POST", f"/workflows/{wf['id']}/run", {"payload": {
    "crm": [
        {"email": "ada@corp.io", "name": "Ada"},
        {"email": "bob@corp.io", "name": "Bob"},
        {"email": "cyn@corp.io", "name": "Cyn"},
    ],
    "billing": [
        {"email": "ada@corp.io", "plan": "pro", "mrr": 200},
        {"email": "dan@corp.io", "plan": "team", "mrr": 500},
    ],
}})
assert status in (200, 202), run
print("ran:", run["execution_id"])
import time
for _ in range(40):
    status, d = api("GET", f"/executions/{run['execution_id']}")
    if d["status"] != "running":
        break
    time.sleep(0.1)
print("status:", d["status"])
runs = {r["node_id"]: r for r in d["node_runs"]}
print("cmp:", runs["cmp"]["output"])
print("Synced n =", runs["mo"]["output"])
print("MissingInBilling n =", runs["ao"]["output"])
print("MissingInCRM n =", runs["bo"]["output"])
