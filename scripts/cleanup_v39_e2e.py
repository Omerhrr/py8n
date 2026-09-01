"""One-off v39 E2E cleanup: imported pack copies + leaked test workflows."""
import json
import urllib.request
from collections import defaultdict

BASE = "http://localhost:8000/api/v1"


def req(method, path, payload=None):
    r = urllib.request.Request(
        f"{BASE}{path}",
        method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(r) as resp:
        return resp.status, (json.load(resp) if resp.status != 204 else {})


_, wfs = req("GET", "/workflows")
by_active = defaultdict(list)
for w in sorted(wfs, key=lambda w: w["created_at"]):
    if w["name"] in ("Hello Py8n - Quickstart", "AI Writer - free LLM demo"):
        by_active[w["name"]].append(w["id"])

kills = []
for name, ids in by_active.items():
    kills.extend(ids[1:])  # keep the original (oldest)
for w in wfs:
    if w["name"] in ("v39 pack wf a df128767", "v39 pack wf a 57390e51"):
        kills.append(w["id"])

for wid in kills:
    s, _ = req("DELETE", f"/workflows/{wid}")
    print("deleted wf", wid[:8], s)

_, dss = req("GET", "/datasets")
for d in dss:
    if d["name"] == "smoke27 live 436351 (2)":
        s, _ = req("DELETE", f"/datasets/{d['id']}")
        print("deleted ds", d["name"], s)

_, wfs2 = req("GET", "/workflows")
print("remaining workflows:", len(wfs2), "| duplicates left:", len(wfs2) - len(set(w["name"] for w in wfs2)))
