#!/usr/bin/env python3
"""One-off: remove leaked smoke temp workflows from aborted smoke runs.

Every workflow the smoke creates on the fly is named with a "tmp " prefix
(v2/v3/v16/v17/v25/v26/v33-era shapes) - today's aborted runs left ~100
copies behind. Real seeded/demo workflows (Hello Py8n, AI Writer, ...) and
everything else are untouched. "v41 keyed wf" leftovers are smoke artifacts
too and go as well.
"""

import json
import urllib.request

BASE = "http://localhost:8000/api/v1"


def req(method: str, path: str):
    r = urllib.request.Request(f"{BASE}{path}", method=method)
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def main() -> None:
    status, raw = req("GET", "/workflows")
    assert status == 200, status
    wfs = json.loads(raw)
    doomed = [w for w in wfs if w["name"].startswith("tmp ") or w["name"].startswith("v41 keyed wf")]
    print(f"estate: {len(wfs)} workflows, deleting {len(doomed)} leaked temp workflows")
    ok = 0
    for w in doomed:
        s, _ = req("DELETE", f"/workflows/{w['id']}")
        ok += s == 204
    print(f"deleted {ok}/{len(doomed)}")
    status, raw = req("GET", "/workflows")
    rest = json.loads(raw)
    print(f"estate now: {len(rest)} workflows:", [w["name"] for w in rest][:20])


if __name__ == "__main__":
    main()
