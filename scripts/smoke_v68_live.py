"""V68 live smoke: boot the real server and verify the three fronts.

1. SERVING TOKENS + SSE: a deployed LM's webhook endpoint is open until a
   serving token is minted; then unauthenticated calls 401, the bearer
   token opens it (last_used stamped), and POST /deployments/{id}/stream
   emits meta -> token* -> done Server-Sent Events whose joined token
   text equals the done text. Revoking the token reopens the endpoint.
2. REDEPLOY / ROLLBACK: continued pretraining lands v2 of the model;
   redeploying flips the SAME URL to v2 (the answer reports the new
   version), rolling back to revision 1 serves v1 again, and the
   revision ledger shows deploy -> redeploy -> rollback with exactly one
   active revision.
3. INTERACTION LAYER: a handler workflow (code node echoing the inbound
   envelope) answers inbound messages; one conversation starts on voice
   and CONTINUES on WhatsApp via conversation_ref (the transcript shows
   both channels + agent replies), a human takeover is recorded, the
   conversation closes with an outcome, and the next inbound from the
   same sender starts a fresh conversation.

Usage: /home/z/.venv/bin/python scripts/smoke_v68_live.py
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid

import httpx

BACKEND = "/home/z/my-project/py8n/mini-services/api-backend"
API = "http://127.0.0.1:8199/api/v1"

CORPUS = [
    "the support agent resolved the ticket about the login issue",
    "the agent fixed the login bug and the customer left a review",
    "the customer asked about the refund policy for the order",
    "the agent shipped the order and closed the ticket today",
    "the ticket about the refund was escalated to the team",
    "the customer thanked the agent for the quick fix",
    "the login issue returned and the agent reprovisioned access",
    "the order arrived late so the agent applied a refund",
] * 6


def wait_health(client: httpx.Client, deadline: float = 30.0) -> None:
    end = time.time() + deadline
    while time.time() < end:
        try:
            res = client.get(f"{API}/health")
            if res.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.4)
    raise SystemExit("server never became healthy")


def _node_run(execution: dict, node_name: str) -> dict:
    for run in execution.get("node_runs") or []:
        if run.get("node_name") == node_name:
            return run
    raise AssertionError(f"node run {node_name!r} not found")


def _run_wf(c: httpx.Client, name: str, graph: dict, payload: dict | None = None) -> dict:
    res = c.post("/workflows", json={"name": name, "graph": graph})
    assert res.status_code in (200, 201), res.text
    wf_id = res.json()["id"]
    res = c.post(f"/workflows/{wf_id}/run", json={"payload": payload or {}})
    assert res.status_code in (200, 202), res.text
    ex = res.json()["execution_id"]
    for _ in range(4000):
        det = c.get(f"/executions/{ex}").json()
        if det["status"] not in ("running", "queued"):
            return det
        time.sleep(0.05)
    raise AssertionError("execution did not finish in time")


def _train_lm(c: httpx.Client, name: str, base: str | None = None) -> dict:
    params = {"text_column": "doc", "d_model": 16, "epochs": 4, "model_name": name}
    if base:
        params["base_model"] = base
    return _run_wf(c, f"train-{name}", {"nodes": [
        {"id": "t", "type": "manual_trigger", "name": "t", "position": {"x": 0, "y": 0}, "parameters": {}},
        {"id": "lm", "type": "lm_train", "name": "lm", "position": {"x": 1, "y": 0}, "parameters": params},
    ], "edges": [{"id": "e1", "source": "t", "target": "lm"}]},
        {"items": [{"doc": d} for d in CORPUS]})


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v68_{uuid.uuid4().hex[:8]}.sqlite3"
    env = dict(os.environ)
    env.update({
        "PY8N_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "PY8N_EXECUTION_MODE": "inline",
        "PY8N_REQUIRE_AUTH": "false",
    })
    proc = subprocess.Popen(
        ["/home/z/.venv/bin/python", "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", "8199", "--log-level", "warning"],
        cwd=BACKEND, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        with httpx.Client(base_url=API, timeout=600) as c:
            wait_health(c)
            tag = uuid.uuid4().hex[:6]

            # ================= 1) tokens + SSE streaming ====================
            run = _train_lm(c, f"smoke68-lm-{tag}")
            assert run["status"] == "success", str(run.get("error"))[:400]

            res = c.post("/deployments", json={"name": f"tok ep {tag}", "model": f"smoke68-lm-{tag}"})
            assert res.status_code == 201, res.text
            dep = res.json()
            wf_id = dep["workflow"]["id"]
            assert dep["auth_required"] is False

            res = c.post(f"/webhooks/{wf_id}", json={"prompt": "the agent"})
            assert res.status_code == 200, res.text  # open until a token exists

            res = c.post(f"/deployments/{dep['id']}/tokens", json={"name": "smoke caller"})
            assert res.status_code == 201, res.text
            tok = res.json()["token"]
            assert tok.startswith("py8nd_")

            res = c.post(f"/webhooks/{wf_id}", json={"prompt": "the agent"})
            assert res.status_code == 401, res.status_code  # gated now
            res = c.post(f"/webhooks/{wf_id}", json={"prompt": "the agent and the ticket"},
                         headers={"Authorization": f"Bearer {tok}"})
            assert res.status_code == 200, res.text
            assert res.json()["last_output"]["text"].strip()
            toks_list = c.get(f"/deployments/{dep['id']}/tokens").json()
            assert toks_list[0]["last_used_at"] is not None

            # SSE stream with the token
            frames: list[tuple[str, dict]] = []
            with c.stream("POST", f"/deployments/{dep['id']}/stream",
                          json={"prompt": "the agent and the ticket", "max_tokens": 10},
                          headers={"Authorization": f"Bearer {tok}"}) as sres:
                assert sres.status_code == 200, sres.status_code
                assert sres.headers["content-type"].startswith("text/event-stream")
                event, data = None, None
                for line in sres.iter_lines():
                    line = (line or "").rstrip("\r")
                    if line.startswith("event: "):
                        event = line[7:].strip()
                    elif line.startswith("data: ") and event:
                        frames.append((event, json.loads(line[6:])))
                        event = None
            kinds = [k for k, _ in frames]
            assert kinds[0] == "meta" and kinds[-1] == "done", kinds
            assert kinds.count("token") == 10, kinds
            token_pieces = [d["text"] for k, d in frames if k == "token"]
            done = frames[-1][1]
            assert done["text"] == "".join(token_pieces)
            assert done["tokens_generated"] == 10
            assert done["model"]["name"] == f"smoke68-lm-{tag}"

            # unauthenticated stream is refused too
            res = c.post(f"/deployments/{dep['id']}/stream", json={"prompt": "x"})
            assert res.status_code == 401, res.status_code

            # revoke -> the webhook reopens
            tok_id = toks_list[0]["id"]
            assert c.delete(f"/deployments/{dep['id']}/tokens/{tok_id}").status_code == 200
            res = c.post(f"/webhooks/{wf_id}", json={"prompt": "the agent"})
            assert res.status_code == 200, res.status_code
            print(f"[1] tokens+SSE ok: 401->200 with py8nd_ token, streamed 10 tokens "
                  f"('{done['text'][:24]}...'), revoke reopens")

            # ================= 2) redeploy / rollback =======================
            run = _train_lm(c, f"smoke68-lm-{tag}", base=f"smoke68-lm-{tag}")  # v2
            assert run["status"] == "success", str(run.get("error"))[:400]

            res = c.post(f"/deployments/{dep['id']}/redeploy", json={"model": f"smoke68-lm-{tag}"})
            assert res.status_code == 200, res.text
            v2_version = res.json()["deployment"]["model"]["version"]
            assert v2_version == 2, v2_version
            res = c.post(f"/webhooks/{wf_id}", json={"prompt": "the agent"})
            assert res.json()["last_output"]["model"]["version"] == 2  # same URL, new weights

            res = c.post(f"/deployments/{dep['id']}/rollback", json={"revision": 1})
            assert res.status_code == 200, res.text
            assert res.json()["deployment"]["model"]["version"] == 1

            res = c.post(f"/webhooks/{wf_id}", json={"prompt": "the agent"})
            assert res.json()["last_output"]["model"]["version"] == 1
            vers = c.get(f"/deployments/{dep['id']}/versions").json()
            assert [r["action"] for r in vers["revisions"]] == ["rollback", "redeploy", "deploy"]
            assert sum(1 for r in vers["revisions"] if r["active"]) == 1
            assert len(vers["available"]) == 2  # v1 + v2 of the family
            print(f"[2] redeploy/rollback ok: v1 -> v2 -> rollback on the SAME url; "
                  f"ledger deploy->redeploy->rollback, one active")

            # ================= 3) interaction layer =========================
            handler_code = (
                "env = input_data.get('payload', {})\n"
                "p = env.get('participant') or {}\n"
                "result = {'text': 'Hi ' + str(p.get('name', '')) + ', got: ' + str(env.get('text', ''))}\n"
            )
            res = c.post("/workflows", json={"name": f"ix-handler-{tag}", "graph": {
                "nodes": [
                    {"id": "t", "type": "manual_trigger", "name": "t", "position": {"x": 0, "y": 0}, "parameters": {}},
                    {"id": "reply", "type": "code", "name": "reply", "position": {"x": 1, "y": 0},
                     "parameters": {"code": handler_code}},
                ],
                "edges": [{"id": "e1", "source": "t", "target": "reply"}],
            }})
            assert res.status_code in (200, 201), res.text
            handler_id = res.json()["id"]

            chans = c.get("/interactions/channels").json()["channels"]
            assert {ch["id"] for ch in chans} == {"app", "web", "api", "voice",
                                                  "whatsapp", "telegram", "discord", "sms", "email"}

            # inbound on voice -> conversation + agent reply
            res = c.post("/interactions/inbound", json={
                "channel": "voice", "sender_id": "+234-801", "sender_name": "Ada",
                "text": "I want to order a laptop", "handler_workflow_id": handler_id})
            assert res.status_code == 200, res.text
            first = res.json()
            assert first["handler_bound"] and "Ada" in (first["reply"] or "")
            conv_id = first["conversation_id"]

            # channel hop: whatsapp continues the SAME conversation
            res = c.post("/interactions/inbound", json={
                "channel": "whatsapp", "sender_id": "wa-991", "text": "no answer, trying WhatsApp",
                "conversation_ref": conv_id, "handler_workflow_id": handler_id})
            assert res.json()["conversation_id"] == conv_id

            conv = c.get(f"/interactions/conversations/{conv_id}").json()
            assert set(conv["channels_used"]) == {"voice", "whatsapp"}
            assert conv["message_count"] == 5  # system + 2 user + 2 agent
            roles = [m["role"] for m in conv["messages"]]
            assert roles == ["system", "user", "agent", "user", "agent"], roles

            # human takeover + close with outcome
            res = c.post(f"/interactions/conversations/{conv_id}/messages",
                         json={"text": "Hi, this is Emeka from the team", "role": "human_agent"})
            assert res.status_code == 200 and res.json()["reply"] is None
            res = c.post(f"/interactions/conversations/{conv_id}/close",
                         json={"outcome": "order confirmed"})
            assert res.json()["state"] == "closed"

            # closed -> the next inbound from the same sender starts FRESH
            res = c.post("/interactions/inbound", json={
                "channel": "voice", "sender_id": "+234-801", "text": "another order",
                "handler_workflow_id": handler_id})
            assert res.json()["conversation_id"] != conv_id

            # unknown channel refused honestly
            res = c.post("/interactions/inbound", json={
                "channel": "carrier_pigeon", "sender_id": "x", "text": "coo"})
            assert res.status_code == 400 and "known channels" in res.json()["detail"]
            print(f"[3] interactions ok: voice->whatsapp ONE conversation (5 msgs, "
                  f"2 channels), human takeover, close+outcome, fresh after close")

            print(f"\nSMOKE V68 GREEN - 3/3 checks passed")
            return 0
    except AssertionError as exc:
        print(f"SMOKE V68 FAILED: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"SMOKE V68 ERROR: {exc}")
        raise
    finally:
        proc.terminate()
        proc.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
