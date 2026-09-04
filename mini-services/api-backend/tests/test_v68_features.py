"""V68 feature tests: authenticated serving tokens, redeploy/rollback
flows between versions, SSE streaming generation for deployed LMs, and
the interaction layer that makes channels interchangeable adapters.

- serving tokens: a deployment with >=1 active token demands it on every
  webhook call (Bearer or X-Deployment-Token, timing-safe against the
  stored sha256); zero tokens keeps the v67 open behavior; revocation
  re-closes the endpoint; last_used_at is stamped on success; tokens are
  mint-once (raw shown exactly once) and scoped to ONE deployment.
- redeploy/rollback: the revision ledger starts at the initial deploy,
  redeploying patches the serving workflow's model parameter IN PLACE
  (same URL, new weights - the answer reports the new version), rollback
  re-activates an older revision, cross-family redeploys are refused
  (the request contract would change), and /versions lists the ledger
  plus the other registry versions available as targets.
- SSE stream: POST /deployments/{id}/stream emits meta -> token* -> done
  frames (text/event-stream), the done text equals the joined token
  pieces, predict-mode deployments refuse honestly, and the serving
  token gates the stream like the webhook.
- interactions: the 9-channel adapter catalog (3 builtin, 6 external
  with providers), the universal ingress find-or-creates conversations
  and runs the bound handler workflow (last node output = the reply),
  conversation_ref keeps ONE conversation across a channel hop (the
  transcript shows both channels), human takeover and close-with-outcome
  behave, unknown channels/foreign conversations are refused.

Runs the FastAPI app in-process via httpx ASGITransport (same harness as v4-v67).
"""

from __future__ import annotations

import asyncio
import json
import uuid

import httpx

from app.main import app
from app.services import executor as executor_mod

API = "http://testserver/api/v1"

_CORPUS_SENTENCE = "the support agent resolved the ticket and the customer left a happy review"


def _corpus_rows(n: int = 14) -> list[dict]:
    return [{"doc": _CORPUS_SENTENCE + f" number {i} about the ticket and the agent"} for i in range(n)]


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


async def _drain_background() -> None:
    tasks = [t for t in executor_mod._background_tasks if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _mk_user(client: httpx.AsyncClient, tag: str, n: int) -> dict:
    res = await client.post("/auth/register", json={
        "email": f"v68-{tag}-u{n}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v68 u{n} {tag}",
    })
    assert res.status_code == 201, res.text
    body = res.json()
    return {"token": body["token"], "id": body["user"]["id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _node(nid: str, ntype: str, params: dict | None = None) -> dict:
    return {"id": nid, "type": ntype, "name": nid, "position": {"x": 0, "y": 0}, "parameters": params or {}}


def _edge(eid: str, source: str, target: str) -> dict:
    return {"id": eid, "source": source, "target": target, "sourceHandle": "main", "targetHandle": "main"}


async def _run_and_wait(client: httpx.AsyncClient, wf_id: str, headers: dict, payload: dict | None = None) -> dict:
    res = await client.post(f"/workflows/{wf_id}/run", headers=headers, json={"payload": payload or {}})
    assert res.status_code in (200, 202), res.text
    exec_id = res.json()["execution_id"]
    for _ in range(400):
        res = await client.get(f"/executions/{exec_id}", headers=headers)
        assert res.status_code == 200, res.text
        if res.json()["status"] not in ("running", "queued"):
            return res.json()
        await asyncio.sleep(0.05)
    raise AssertionError("execution did not finish in time")


async def _train_lm(client: httpx.AsyncClient, tag: str, headers: dict, name: str,
                    base_model: str | None = None) -> None:
    params = {"text_column": "doc", "d_model": 16, "epochs": 3, "model_name": name}
    if base_model:
        params["base_model"] = base_model
    res = await client.post("/workflows", headers=headers, json={"name": f"lm-{name}", "graph": {
        "nodes": [_node("t", "manual_trigger"), _node("lm", "lm_train", params)],
        "edges": [_edge("e1", "t", "lm")],
    }})
    wf = res.json()
    run = await _run_and_wait(client, wf["id"], headers, {"items": _corpus_rows()})
    assert run["status"] == "success", str(run.get("error"))[:400]


async def _deploy(client: httpx.AsyncClient, headers: dict, name: str, model: str,
                  env: str = "dev") -> dict:
    res = await client.post("/deployments", headers=headers, json={
        "name": name, "model": model, "environment": env})
    assert res.status_code == 201, res.text
    return res.json()


def _parse_sse(raw: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for frame in raw.split("\n\n"):
        frame = frame.strip()
        if not frame:
            continue
        event, data = "message", None
        for line in frame.split("\n"):
            if line.startswith("event: "):
                event = line[7:].strip()
            elif line.startswith("data: "):
                data = line[6:]
        if data is not None:
            events.append((event, json.loads(data)))
    return events


# ---------------------------------------------------------------------------
# 1) serving tokens: mint-once credentials that gate the serving webhook
# ---------------------------------------------------------------------------
def test_v68_serving_tokens():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"tok-{tag}", 1)
            h = _auth(user["token"])

            await _train_lm(client, tag, h, f"tok-model-{tag}")
            dep = await _deploy(client, h, f"tok endpoint {tag}", f"tok-model-{tag}")
            wf_id = dep["workflow"]["id"]
            assert dep["auth_required"] is False  # open endpoint until a token exists

            res = await client.post(f"/webhooks/{wf_id}", json={"prompt": "the agent"})
            assert res.status_code == 200  # zero tokens = v67 behavior

            # ---- mint: raw shown once, list masked -------------------------
            res = await client.post(f"/deployments/{dep['id']}/tokens", headers=h,
                                    json={"name": "checkout service"})
            assert res.status_code == 201, res.text
            tok = res.json()
            assert tok["token"].startswith("py8nd_")
            assert tok["prefix"].startswith("py8nd_")
            assert "token" not in json.dumps(await _list_tokens(client, h, dep["id"]))

            res = await client.get(f"/deployments/{dep['id']}", headers=h)
            assert res.json()["auth_required"] is True
            assert res.json()["active_tokens"] == 1

            # ---- the endpoint now demands the token ------------------------
            res = await client.post(f"/webhooks/{wf_id}", json={"prompt": "the agent"})
            assert res.status_code == 401
            assert "serving token" in res.json()["detail"]

            res = await client.post(f"/webhooks/{wf_id}", json={"prompt": "the agent"},
                                    headers={"Authorization": "Bearer py8nd_wrong"})
            assert res.status_code == 401

            res = await client.post(f"/webhooks/{wf_id}", json={"prompt": "the agent and the ticket"},
                                    headers={"Authorization": f"Bearer {tok['token']}"})
            assert res.status_code == 200, res.text
            assert res.json()["last_output"]["text"].strip()

            # the X-Deployment-Token header works too
            res = await client.post(f"/webhooks/{wf_id}", json={"prompt": "the agent"},
                                    headers={"X-Deployment-Token": tok["token"]})
            assert res.status_code == 200

            # last_used_at stamped by the successful calls
            toks = await _list_tokens(client, h, dep["id"])
            assert toks[0]["last_used_at"] is not None

            # ---- tokens are scoped: another gated deployment refuses them --
            dep2 = await _deploy(client, h, f"tok2 endpoint {tag}", f"tok-model-{tag}")
            res = await client.post(f"/deployments/{dep2['id']}/tokens", headers=h,
                                    json={"name": "dep2 gate"})
            tok2 = res.json()
            res = await client.post(f"/webhooks/{dep2['workflow']['id']}",
                                    json={"prompt": "the agent"},
                                    headers={"Authorization": f"Bearer {tok['token']}"})
            assert res.status_code == 401  # dep's token does NOT open dep2
            res = await client.post(f"/webhooks/{dep2['workflow']['id']}",
                                    json={"prompt": "the agent"},
                                    headers={"Authorization": f"Bearer {tok2['token']}"})
            assert res.status_code == 200  # its own token does

            # ---- revoke: the endpoint reopens (all tokens gone) -----------
            res = await client.delete(f"/deployments/{dep['id']}/tokens/{tok['id']}", headers=h)
            assert res.status_code == 200
            assert res.json()["revoked"] is True
            res = await client.post(f"/webhooks/{wf_id}", json={"prompt": "the agent"})
            assert res.status_code == 200  # zero ACTIVE tokens = open again

            # ownership: user B can neither see nor mint tokens on dep2
            user_b = await _mk_user(client, f"tok-{tag}", 2)
            res = await client.post(f"/deployments/{dep2['id']}/tokens",
                                    headers=_auth(user_b["token"]), json={"name": "stealth"})
            assert res.status_code == 404
            res = await client.get(f"/deployments/{dep2['id']}/tokens", headers=_auth(user_b["token"]))
            assert res.status_code == 404

    asyncio.run(_go())
    asyncio.run(_drain_background())


async def _list_tokens(client: httpx.AsyncClient, headers: dict, dep_id: str) -> list[dict]:
    res = await client.get(f"/deployments/{dep_id}/tokens", headers=headers)
    assert res.status_code == 200, res.text
    return res.json()


# ---------------------------------------------------------------------------
# 2) redeploy / rollback: same URL, new weights, revision ledger intact
# ---------------------------------------------------------------------------
def test_v68_redeploy_rollback_versions():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"rd-{tag}", 1)
            h = _auth(user["token"])

            # v1 first, deploy against it, THEN continue-pretrain to v2 (the
            # registry's version chain: same name, next version auto-activates)
            await _train_lm(client, tag, h, f"rd-model-{tag}")

            dep = await _deploy(client, h, f"rd endpoint {tag}", f"rd-model-{tag}")
            v1_version = dep["model"]["version"]
            assert dep["model"]["name"] == f"rd-model-{tag}"
            wf_id = dep["workflow"]["id"]

            await _train_lm(client, tag, h, f"rd-model-{tag}",
                            base_model=f"rd-model-{tag}")

            # revision ledger starts at the initial deploy
            res = await client.get(f"/deployments/{dep['id']}/versions", headers=h)
            vers = res.json()
            assert len(vers["revisions"]) == 1
            assert vers["revisions"][0]["action"] == "deploy"
            assert vers["revisions"][0]["active"] is True

            # ---- answer reports v1 -----------------------------------------
            res = await client.post(f"/webhooks/{wf_id}", json={"prompt": "the agent"})
            assert res.json()["last_output"]["model"]["version"] == v1_version

            # ---- redeploy to v2 (now the registry's ACTIVE version) -------
            res = await client.post(f"/deployments/{dep['id']}/redeploy", headers=h,
                                    json={"model": f"rd-model-{tag}", "note": "continued on new corpus"})
            assert res.status_code == 200, res.text
            out = res.json()
            assert out["revision"]["action"] == "redeploy"
            assert out["revision"]["revision"] == 2
            assert out["deployment"]["model"]["version"] > v1_version

            res = await client.post(f"/webhooks/{wf_id}", json={"prompt": "the agent"})
            last = res.json()["last_output"]
            assert last["model"]["name"] == f"rd-model-{tag}"  # same URL, new weights
            assert last["model"]["version"] > v1_version

            # ---- redeploy refusal matrix -----------------------------------
            res = await client.post(f"/deployments/{dep['id']}/redeploy", headers=h,
                                    json={"model": f"rd-model-{tag}"})
            assert res.status_code == 400  # already serving it
            res = await client.post(f"/deployments/{dep['id']}/redeploy", headers=h,
                                    json={"model": f"ghost-{tag}"})
            assert res.status_code == 400

            # cross-family refusal: a tabular model cannot take over an LM endpoint
            rows = [{"a": float(i), "label": "yes" if i % 3 == 0 else "no"} for i in range(32)]
            res = await client.post("/datasets", headers=h, json={"name": f"rd-rows-{tag}", "rows": rows})
            ds_name = res.json()["name"]
            res = await client.post("/workflows", headers=h, json={"name": f"rd-nn-{tag}", "graph": {
                "nodes": [_node("t", "manual_trigger"),
                          _node("r", "dataset_read", {"dataset": ds_name}),
                          _node("nn", "neural_train", {"task": "classification", "target": "label",
                                                       "features": "a", "hidden_layers": "8",
                                                       "epochs": 40, "learning_rate": 0.05,
                                                       "model_name": f"rd-nn-{tag}"})],
                "edges": [_edge("e1", "t", "r"), _edge("e2", "r", "nn")]}})
            run = await _run_and_wait(client, res.json()["id"], h, {})
            assert run["status"] == "success", str(run.get("error"))[:300]
            res = await client.post(f"/deployments/{dep['id']}/redeploy", headers=h,
                                    json={"model": f"rd-nn-{tag}"})
            assert res.status_code == 400
            assert "contract" in res.json()["detail"]

            # ---- rollback to revision 1 (the original v1 row) --------------
            res = await client.post(f"/deployments/{dep['id']}/rollback", headers=h,
                                    json={"revision": 1})
            assert res.status_code == 200, res.text
            out = res.json()
            assert out["revision"]["action"] == "rollback"
            assert out["revision"]["revision"] == 3
            assert out["deployment"]["model"]["version"] == v1_version
            res = await client.post(f"/webhooks/{wf_id}", json={"prompt": "the agent"})
            assert res.json()["last_output"]["model"]["version"] == v1_version

            # ledger: 3 revisions, exactly one active (the newest)
            res = await client.get(f"/deployments/{dep['id']}/versions", headers=h)
            vers = res.json()
            assert len(vers["revisions"]) == 3
            assert sum(1 for r in vers["revisions"] if r["active"]) == 1
            assert vers["revisions"][0]["revision"] == 3
            # available targets list both registry rows of the family
            assert len(vers["available"]) >= 2

            # rollback by registry version + error matrix
            res = await client.post(f"/deployments/{dep['id']}/rollback", headers=h,
                                    json={"version": 999})
            assert res.status_code == 400
            res = await client.post(f"/deployments/{dep['id']}/rollback", headers=h,
                                    json={"revision": 99})
            assert res.status_code == 400
            res = await client.post(f"/deployments/{dep['id']}/rollback", headers=h, json={})
            assert res.status_code == 400

            # ownership: user B cannot redeploy my deployment
            user_b = await _mk_user(client, f"rd-{tag}", 2)
            res = await client.post(f"/deployments/{dep['id']}/redeploy",
                                    headers=_auth(user_b["token"]),
                                    json={"model": f"rd-model-{tag}-c"})
            assert res.status_code == 404

    asyncio.run(_go())
    asyncio.run(_drain_background())


# ---------------------------------------------------------------------------
# 3) SSE streaming: meta -> token* -> done, gated by serving tokens
# ---------------------------------------------------------------------------
def test_v68_sse_streaming():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"sse-{tag}", 1)
            h = _auth(user["token"])

            await _train_lm(client, tag, h, f"sse-model-{tag}")
            dep = await _deploy(client, h, f"sse endpoint {tag}", f"sse-model-{tag}")

            # ---- stream: event frames with real tokens ---------------------
            res = await client.post(f"/deployments/{dep['id']}/stream",
                                    json={"prompt": "the agent and the ticket", "max_tokens": 8})
            assert res.status_code == 200, res.text
            assert res.headers["content-type"].startswith("text/event-stream")
            events = _parse_sse(res.text)
            kinds = [e for e, _ in events]
            assert kinds[0] == "meta"
            assert kinds[-1] == "done"
            assert kinds.count("token") == 8
            meta = events[0][1]
            assert meta["model"]["name"] == f"sse-model-{tag}"
            assert meta["tokenizer"] in ("word", "bpe")
            tokens = [d for e, d in events if e == "token"]
            assert [t["index"] for t in tokens] == list(range(8))
            done = events[-1][1]
            assert done["tokens_generated"] == 8
            assert done["text"] == "".join(t["text"] for t in tokens)
            assert done["text"].strip()

            # determinism: same seed -> identical done text
            res2 = await client.post(f"/deployments/{dep['id']}/stream",
                                     json={"prompt": "the agent and the ticket", "max_tokens": 8})
            assert _parse_sse(res2.text)[-1][1]["text"] == done["text"]

            # ---- predict-mode deployments refuse honestly ------------------
            rows = [{"a": float(i), "label": "yes" if i % 3 == 0 else "no"} for i in range(32)]
            res = await client.post("/datasets", headers=h, json={"name": f"sse-rows-{tag}", "rows": rows})
            ds_name = res.json()["name"]
            res = await client.post("/workflows", headers=h, json={"name": f"sse-nn-{tag}", "graph": {
                "nodes": [_node("t", "manual_trigger"),
                          _node("r", "dataset_read", {"dataset": ds_name}),
                          _node("nn", "neural_train", {"task": "classification", "target": "label",
                                                       "features": "a", "hidden_layers": "8",
                                                       "epochs": 40, "learning_rate": 0.05,
                                                       "model_name": f"sse-nn-{tag}"})],
                "edges": [_edge("e1", "t", "r"), _edge("e2", "r", "nn")]}})
            run = await _run_and_wait(client, res.json()["id"], h, {})
            assert run["status"] == "success"
            dep_nn = await _deploy(client, h, f"sse nn endpoint {tag}", f"sse-nn-{tag}")
            res = await client.post(f"/deployments/{dep_nn['id']}/stream", json={"prompt": "x"})
            assert res.status_code == 200  # SSE contract: the error ARRIVES as an event
            ev = _parse_sse(res.text)
            assert ev[-1][0] == "error"
            assert "nothing to stream" in ev[-1][1]["error"]

            # ---- serving tokens gate the stream ----------------------------
            res = await client.post(f"/deployments/{dep['id']}/tokens", headers=h,
                                    json={"name": "stream client"})
            tok = res.json()
            res = await client.post(f"/deployments/{dep['id']}/stream", json={"prompt": "the agent"})
            assert res.status_code == 401
            res = await client.post(f"/deployments/{dep['id']}/stream",
                                    json={"prompt": "the agent and the ticket", "max_tokens": 4},
                                    headers={"Authorization": f"Bearer {tok['token']}"})
            assert res.status_code == 200
            ev = _parse_sse(res.text)
            assert ev[-1][0] == "done"
            assert len(ev) == 4 + 2  # meta + 4 tokens + done

            # ownership: user B cannot stream my deployment
            user_b = await _mk_user(client, f"sse-{tag}", 2)
            res = await client.post(f"/deployments/{dep['id']}/stream",
                                    headers=_auth(user_b["token"]), json={"prompt": "x"})
            assert res.status_code == 404

    asyncio.run(_go())
    asyncio.run(_drain_background())


# ---------------------------------------------------------------------------
# 4) the interaction layer: channels are adapters, the conversation is the product
# ---------------------------------------------------------------------------
def test_v68_interaction_layer():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"ix-{tag}", 1)
            h = _auth(user["token"])

            # ---- the adapter catalog ---------------------------------------
            res = await client.get("/interactions/channels", headers=h)
            assert res.status_code == 200
            chans = {c["id"]: c for c in res.json()["channels"]}
            assert set(chans) == {"app", "web", "api", "voice", "whatsapp",
                                  "telegram", "discord", "sms", "email"}
            assert chans["app"]["builtin"] is True
            assert chans["voice"]["builtin"] is False
            assert "twilio" in chans["voice"]["providers"]
            assert "meta_cloud_api" in chans["whatsapp"]["providers"]
            assert chans["voice"]["adapter"]["outbound"]  # what an adapter must supply

            # ---- an echo handler: the LAST node's output is the reply ------
            handler_code = (
                "env = input_data.get('payload', {})\n"
                "p = env.get('participant') or {}\n"
                "result = {'text': 'Hi ' + str(p.get('name', '')) + ', got: ' + str(env.get('text', ''))}\n"
            )
            res = await client.post("/workflows", headers=h, json={"name": f"ix-handler-{tag}", "graph": {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("reply", "code", {"code": handler_code}),
                ],
                "edges": [_edge("e1", "t", "reply")],
            }})
            assert res.status_code in (200, 201), res.text
            handler = res.json()

            # ---- inbound creates the conversation and answers --------------
            res = await client.post("/interactions/inbound", headers=h, json={
                "channel": "voice", "sender_id": "+234-801", "sender_name": "Ada",
                "text": "I want to order a laptop", "handler_workflow_id": handler["id"]})
            assert res.status_code == 200, res.text
            first = res.json()
            assert first["handler_bound"] is True
            assert "Ada" in first["reply"] and "order a laptop" in first["reply"]
            conv_id = first["conversation_id"]

            # the transcript records both sides + the system opening
            res = await client.get(f"/interactions/conversations/{conv_id}", headers=h)
            conv = res.json()
            assert conv["channel"] == "voice"
            assert conv["participant"]["name"] == "Ada"
            roles = [m["role"] for m in conv["messages"]]
            assert roles == ["system", "user", "agent"]
            assert conv["messages"][1]["channel"] == "voice"

            # ---- channel hop: same conversation_ref, different channel -----
            res = await client.post("/interactions/inbound", headers=h, json={
                "channel": "whatsapp", "sender_id": "wa-991",
                "text": "no answer on the phone, trying WhatsApp",
                "conversation_ref": conv_id, "handler_workflow_id": handler["id"]})
            assert res.status_code == 200
            assert res.json()["conversation_id"] == conv_id  # ONE conversation

            res = await client.get(f"/interactions/conversations/{conv_id}", headers=h)
            conv = res.json()
            assert conv["message_count"] == 5  # system + 2 user + 2 agent
            assert set(conv["channels_used"]) == {"voice", "whatsapp"}  # the hop is visible
            assert conv["messages"][3]["channel"] == "whatsapp"

            # find-or-create by participant (no ref): same channel+sender reuses
            res = await client.post("/interactions/inbound", headers=h, json={
                "channel": "voice", "sender_id": "+234-801", "text": "checking my order"})
            assert res.json()["conversation_id"] == conv_id

            # ---- human takeover + close with outcome ------------------------
            res = await client.post(f"/interactions/conversations/{conv_id}/messages",
                                    headers=h, json={"text": "Hi, this is Emeka from the team",
                                                     "role": "human_agent"})
            assert res.status_code == 200
            assert res.json()["reply"] is None  # humans don't trigger the handler

            res = await client.post(f"/interactions/conversations/{conv_id}/close",
                                    headers=h, json={"outcome": "order confirmed"})
            assert res.json()["state"] == "closed"

            # closed conversation: find-or-create starts a FRESH one
            res = await client.post("/interactions/inbound", headers=h, json={
                "channel": "voice", "sender_id": "+234-801", "text": "another order"})
            assert res.status_code == 200
            assert res.json()["conversation_id"] != conv_id

            # ---- error honesty ----------------------------------------------
            res = await client.post("/interactions/inbound", headers=h, json={
                "channel": "smoke_signals", "sender_id": "x", "text": "hello"})
            assert res.status_code == 400
            assert "known channels" in res.json()["detail"]

            res = await client.post("/interactions/inbound", headers=h, json={
                "channel": "app", "sender_id": "x", "text": ""})
            assert res.status_code in (400, 422)

            res = await client.post("/interactions/conversations", headers=h,
                                    json={"channel": "web", "participant_id": "w1",
                                          "handler_workflow_id": "no-such-wf"})
            assert res.status_code == 400

            # no handler bound: honest reply=None
            res = await client.post("/interactions/conversations", headers=h,
                                    json={"channel": "web", "participant_id": "w2"})
            assert res.status_code == 201
            conv2 = res.json()
            res = await client.post(f"/interactions/conversations/{conv2['id']}/messages",
                                    headers=h, json={"text": "anyone there?"})
            assert res.json()["reply"] is None

            # bind/unbind explicitly
            res = await client.post(f"/interactions/conversations/{conv2['id']}/handler",
                                    headers=h, json={"workflow_id": handler["id"]})
            assert res.json()["handler_workflow_name"] == f"ix-handler-{tag}"

            # ownership: user B's view of my conversations is empty
            user_b = await _mk_user(client, f"ix-{tag}", 2)
            res = await client.get(f"/interactions/conversations/{conv_id}",
                                   headers=_auth(user_b["token"]))
            assert res.status_code == 404

            # channel filter on the list
            res = await client.get("/interactions/conversations", headers=h,
                                   params={"channel": "voice"})
            assert all(c["channel"] == "voice" for c in res.json()["conversations"])

    asyncio.run(_go())
    asyncio.run(_drain_background())
