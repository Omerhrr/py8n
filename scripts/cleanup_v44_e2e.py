#!/usr/bin/env python3
"""Clean up v44 browser-E2E artifacts.

Removes:
  - the "e2e44 versions demo" dataset (+ its snapshots die with it)
  - the "E2E44 hook catcher" workflow
  - every notification rule named "E2E44 ..." or "e2e44 ..."
  - the leftover e2e tags ("showcase", "e2e44", "v44-verified")

Leaked "tmp ..." workflows from today's aborted smoke runs are NOT touched
here - the smoke cleans its own when a full pass completes; the pre-existing
estate rows were already documented in earlier waves.
"""

import json
import sys
import urllib.request

BASE = "http://localhost:8000/api/v1"


def req(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw and resp.status != 204 else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else None
        except Exception:
            return e.code, None


def main() -> None:
    removed = {"datasets": 0, "workflows": 0, "rules": 0, "tags": 0}

    status, datasets = req("GET", "/datasets")
    if status == 200:
        for d in datasets:
            if d["name"].startswith("e2e44"):
                s, _ = req("DELETE", f"/datasets/{d['id']}")
                removed["datasets"] += s == 204

    status, wfs = req("GET", "/workflows")
    if status == 200:
        for w in wfs:
            if w["name"].startswith("E2E44"):
                s, _ = req("DELETE", f"/workflows/{w['id']}")
                removed["workflows"] += s == 204

    status, rules = req("GET", "/notifications")
    if status == 200:
        for r in rules:
            if r["name"].lower().startswith("e2e44"):
                s, _ = req("DELETE", f"/notifications/{r['id']}")
                removed["rules"] += s == 204

    for tag in ("showcase", "e2e44", "v44-verified"):
        status, inv = req("GET", "/tags")
        if status == 200 and any(e["name"].lower() == tag for e in inv):
            s, _ = req("DELETE", f"/tags/{tag}")
            removed["tags"] += s == 200

    print(f"cleanup done: {removed}")
    assert removed["datasets"] == 1 and removed["workflows"] == 1 and removed["rules"] == 1, "expected e2e44 dataset + catcher wf + rule removed"


if __name__ == "__main__":
    main()
