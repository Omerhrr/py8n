#!/usr/bin/env python3
"""Clean up v43 browser-E2E artifacts (run after E2E, before commit).

Removes, scoped to the e2e43-owner account (plus the anonymous credential):
  - the "E2E43 Gallery" pack registry
  - every workflow owned by the e2e43 user (the 17 synced gallery copies)
  - the e2e43 user's API keys (revoke = delete endpoint)
  - the "E2E43 API Gateway" credential

Keeps: the e2e43 user account itself (harmless, matches earlier waves).
"""

import json
import sys
import urllib.request

BASE = "http://localhost:8000/api/v1"
EMAIL = "e2e43-owner@py8n.test"
PASSWORD = "e2e43-password-1"


def req(method: str, path: str, body: dict | None = None, token: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(r) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw and resp.status != 204 else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else None
        except Exception:
            return e.code, raw.decode(errors="replace")


def main() -> None:
    status, login = req("POST", "/auth/login", {"email": EMAIL, "password": PASSWORD})
    if status != 200:
        print(f"e2e43 user not found or password changed ({status}); nothing to clean")
        sys.exit(0)
    tok = login["token"]
    user_id = login["user"]["id"]
    removed = {"registries": 0, "workflows": 0, "keys": 0, "credentials": 0}

    status, regs = req("GET", "/registries", token=tok)
    if status == 200:
        for r in regs:
            if r["name"].startswith("E2E43"):
                s, _ = req("DELETE", f"/registries/{r['id']}", token=tok)
                removed["registries"] += s == 204

    status, wfs = req("GET", "/workflows", token=tok)
    if status == 200:
        # NOTE: the LIST response omits owner_id (only the detail endpoint
        # carries it) - so confirm ownership via the detail GET before deleting.
        for w in wfs:
            s, det = req("GET", f"/workflows/{w['id']}", token=tok)
            if s == 200 and det.get("owner_id") == user_id:
                s2, _ = req("DELETE", f"/workflows/{w['id']}", token=tok)
                removed["workflows"] += s2 == 204

    status, keys = req("GET", "/keys", token=tok)
    if status == 200:
        for k in keys:
            if not k["revoked"]:
                s, _ = req("DELETE", f"/keys/{k['id']}", token=tok)
                removed["keys"] += s == 204

    status, creds = req("GET", "/credentials", token=tok)
    if status == 200:
        for c in creds:
            if c["name"].startswith("E2E43"):
                s, _ = req("DELETE", f"/credentials/{c['id']}?force=true", token=tok)
                removed["credentials"] += s == 204

    print(f"cleanup done: {removed}")
    status, rest_wfs = req("GET", "/workflows", token=tok)
    left = 0
    for w in rest_wfs:
        s, det = req("GET", f"/workflows/{w['id']}", token=tok)
        if s == 200 and det.get("owner_id") == user_id:
            left += 1
    print(f"workflows still owned by e2e43 user: {left}")
    assert not left, "leftover owned workflows"
    assert removed["registries"] == 1 and removed["credentials"] == 1, "expected registry + credential removed"


if __name__ == "__main__":
    main()
