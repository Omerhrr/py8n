#!/usr/bin/env python3
"""v15 E2E helper: run a workflow that resolves UI-created env vars, then clean up."""
import json
import urllib.request

BASE = "http://127.0.0.1:8000/api/v1"


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def main():
    code = (
        "result = {'base': '{{ env.e2e_api_url }}', "
        "'tok_len': len('{{ env.e2e_token_secret }}'), 'flag': '{{ env.e2e_flag }}'}\n"
    )
    graph = {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "parameters": {}},
            {"id": "c", "type": "code", "parameters": {"code": code}},
        ],
        "edges": [{"id": "e", "source": "t", "target": "c",
                   "sourceHandle": "main", "targetHandle": "main"}],
    }
    status, wf = req("POST", "/workflows",
                     {"name": "tmp v15 e2e env check 2", "graph": graph, "is_active": False})
    assert status == 201, wf
    try:
        # also create a third var via API to prove the flag path
        status, flag = req("POST", "/env-vars",
                           {"key": "e2e_flag", "value": "on", "description": "temp"})
        assert status == 201, flag
        status, run = req("POST", f"/workflows/{wf['id']}/run", {"payload": {}})
        assert status in (200, 202), run
        import time
        deadline = time.time() + 15
        while True:
            status, detail = req("GET", f"/executions/{run['execution_id']}")
            assert status == 200
            if detail["status"] != "running" or time.time() > deadline:
                break
            time.sleep(0.3)
        assert detail["status"] == "success", detail.get("error")
        out = next(r for r in detail["node_runs"] if r["node_id"] == "c")["output"]["result"]
        assert out["base"] == "https://api.e2e-demo.dev/v1", out
        assert out["flag"] == "on", out
        assert out["tok_len"] == len("s3cr3t-e2e-42"), out
        print(f"E2E env resolution OK - base={out['base']!r}, flag={out['flag']!r}, "
              f"secret referenced without ever being exposed (length {out['tok_len']})")
        req("DELETE", f"/env-vars/{flag['id']}")
    finally:
        req("DELETE", f"/workflows/{wf['id']}")


if __name__ == "__main__":
    main()
