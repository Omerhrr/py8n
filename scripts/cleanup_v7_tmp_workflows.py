#!/usr/bin/env python3
"""One-off cleanup: delete leftover tmp* smoke-test workflows (v7 session).

Runs against the live API on :8000. Keeps orphaned executions from piling up
by relying on the workflow delete cascade.
"""

import urllib.request

API = "http://localhost:8000/api/v1"


def req(method: str, path: str):
    r = urllib.request.Request(f"{API}{path}", method=method)
    with urllib.request.urlopen(r) as resp:
        body = resp.read().decode()
        return resp.status, body


status, body = req("GET", "/workflows")
rows = __import__("json").loads(body)
tmps = [w for w in rows if w["name"].lower().startswith("tmp")]
for w in tmps:
    s, _ = req("DELETE", f"/workflows/{w['id']}")
    print(f"deleted {w['name']} ({w['id'][:8]}) -> {s}")

status, body = req("GET", "/workflows")
rows = __import__("json").loads(body)
print(f"\nremaining workflows: {len(rows)}")
for w in rows:
    print(f" - {w['name']} [{'active' if w['is_active'] else 'paused'}] triggers={w['trigger_types']}")
