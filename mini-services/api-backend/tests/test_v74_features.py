"""V74 feature tests: the brain routed through a REAL openai_compatible
credential (one routing layer for openai, claude, deepseek, kimi, qwen,
openrouter, groq, together, mistral, xai and the local runtimes), the
speech-loop VERIFIER (piper speaks, the ASR hears - the whisper-tiny-en +
whisper-cli proof), multi-party MEETINGS (legs + a merged, speaker-
attributed transcript derived from the leg timelines) and OUTBOUND
CAMPAIGNS (dial a list through an agent, honest skips without carrier
credentials, answered calls open agent-bound sessions).

Runs the FastAPI app in-process (httpx ASGITransport); the LLM routing
layer's transport is injected (httpx.MockTransport) so the wire format is
asserted byte-for-byte without network egress - the LIVE smoke proves the
same code path against a real local HTTP server.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import httpx
import pytest

from app.main import app
from app.services import executor as executor_mod
from app.services import llm_routing
from app.services import speech_engines as engines
from app.services import voice as voice_svc

API = "http://testserver/api/v1"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


async def _drain_background() -> None:
    tasks = [t for t in executor_mod._background_tasks if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _sync(coro):
    return asyncio.run(coro)


async def _wrap(coro):
    try:
        return await coro
    finally:
        await _drain_background()


async def _mk_user(client: httpx.AsyncClient, tag: str, n: int = 1) -> dict:
    res = await client.post("/auth/register", json={
        "email": f"v74-{tag}-u{n}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v74 u{n} {tag}",
    })
    assert res.status_code == 201, res.text
    body = res.json()
    return {"token": body["token"], "id": body["user"]["id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _mock_handler(handled: list[dict], status: int = 200, body: dict | None = None):
    """An httpx.MockTransport handler that records requests and answers
    with an OpenAI-shaped completion by default."""
    def handler(request: httpx.Request) -> httpx.Response:
        handled.append({
            "url": str(request.url),
            "headers": dict(request.headers),
            "body": json.loads(request.content.decode("utf-8") or "{}"),
        })
        return httpx.Response(status, json=body or {
            "choices": [{"message": {"content": "{\"answer\": \"routed\"}"},
                         "finish_reason": "stop"}],
            "model": (json.loads(request.content.decode() or "{}").get("model")
                      if request.content else None) or "stub-model",
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
        })
    return handler


# ---------------------------------------------------------------------------
# 1) the provider catalog + credential resolution
# ---------------------------------------------------------------------------

def test_v74_provider_catalog():
    names = set(llm_routing.PROVIDERS)
    for required in ("openai", "anthropic", "deepseek", "kimi", "qwen",
                     "openrouter", "groq", "together", "mistral", "xai", "ollama"):
        assert required in names, f"the catalog must carry {required}"
    assert llm_routing.PROVIDERS["anthropic"]["kind"] == "anthropic"
    assert llm_routing.PROVIDERS["openai"]["kind"] == "openai"
    assert llm_routing.PROVIDERS["deepseek"]["base_url"] == "https://api.deepseek.com/v1"
    assert "moonshot" in llm_routing.PROVIDERS["kimi"]["base_url"]
    assert "compatible-mode" in llm_routing.PROVIDERS["qwen"]["base_url"]
    assert llm_routing.PROVIDERS["ollama"].get("keyless") is True

    # presets fill the credential payload
    data = llm_routing.preset_credential_data("deepseek", "sk-x")
    assert data["base_url"] == "https://api.deepseek.com/v1"
    assert data["provider"] == "deepseek"
    assert data["suggested_model"] == "deepseek-chat"
    with pytest.raises(llm_routing.LLMRoutingError) as exc:
        llm_routing.preset_credential_data("not-a-provider")
    assert "known:" in str(exc.value)

    # resolution: missing base_url fails loud, keyless providers pass,
    # a legacy anthropic URL is recognized honestly
    with pytest.raises(llm_routing.LLMRoutingError) as exc:
        llm_routing.resolve_credential({"provider": "openai", "api_key": "k"})
    assert "base_url" in str(exc.value)
    with pytest.raises(llm_routing.LLMRoutingError):
        llm_routing.resolve_credential({"provider": "openai",
                                        "base_url": "https://api.openai.com/v1"})
    resolved = llm_routing.resolve_credential({
        "provider": "ollama", "base_url": "http://localhost:11434/v1"})
    assert resolved["kind"] == "openai" and resolved["api_key"] == ""
    legacy = llm_routing.resolve_credential({
        "base_url": "https://api.anthropic.com/v1", "api_key": "sk-ant-x"})
    assert legacy["kind"] == "anthropic"
    legacy_openai = llm_routing.resolve_credential({
        "base_url": "https://my-gateway.example/v1", "api_key": "k"})
    assert legacy_openai["kind"] == "openai"


def test_v74_routing_openai_wire():
    handled: list[dict] = []
    transport = httpx.MockTransport(_mock_handler(handled))
    try:
        llm_routing.set_transport(transport)
        result = asyncio.run(llm_routing.chat_completion(
            {"provider": "deepseek", "base_url": "https://api.deepseek.com/v1",
             "api_key": "sk-deepseek-test"},
            model="deepseek-chat",
            messages=[{"role": "system", "content": "be brief"},
                      {"role": "user", "content": "hi"}],
            temperature=0.2, max_tokens=128))
    finally:
        llm_routing.set_transport(None)
    assert len(handled) == 1
    req = handled[0]
    assert req["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert req["headers"].get("authorization") == "Bearer sk-deepseek-test"
    assert req["body"]["model"] == "deepseek-chat"
    assert req["body"]["max_tokens"] == 128
    assert [m["role"] for m in req["body"]["messages"]] == ["system", "user"]
    assert result["text"] == '{"answer": "routed"}'
    assert result["usage"]["total_tokens"] == 18
    assert result["stop_reason"] == "stop"
    assert result["provider"] == "deepseek"


def test_v74_routing_anthropic_wire():
    handled: list[dict] = []
    def _claude_handler(request: httpx.Request) -> httpx.Response:
        handled.append({
            "url": str(request.url),
            "headers": dict(request.headers),
            "body": json.loads(request.content.decode("utf-8") or "{}"),
        })
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": "hello "},
                        {"type": "text", "text": "from claude"}],
            "model": "claude-sonnet-4-5", "stop_reason": "end_turn",
            "usage": {"input_tokens": 9, "output_tokens": 4},
        })
    transport = httpx.MockTransport(_claude_handler)
    try:
        llm_routing.set_transport(transport)
        result = asyncio.run(llm_routing.chat_completion(
            {"provider": "anthropic", "base_url": "https://api.anthropic.com/v1",
             "api_key": "sk-ant-test"},
            model="claude-sonnet-4-5",
            messages=[{"role": "system", "content": "you are terse"},
                      {"role": "user", "content": "say hi"}],
            temperature=0.1, max_tokens=64))
    finally:
        llm_routing.set_transport(None)
    assert len(handled) == 1
    req = handled[0]
    # Claude's NATIVE wire: /messages, x-api-key + anthropic-version, system
    # TOP-LEVEL, max_tokens REQUIRED, content BLOCKS
    assert req["url"] == "https://api.anthropic.com/v1/messages"
    assert req["headers"].get("x-api-key") == "sk-ant-test"
    assert req["headers"].get("anthropic-version") == llm_routing.ANTHROPIC_VERSION
    assert "authorization" not in req["headers"]
    assert req["body"]["system"] == "you are terse"
    assert req["body"]["max_tokens"] == 64
    assert [m["role"] for m in req["body"]["messages"]] == ["user"]
    assert result["text"] == "hello from claude"
    assert result["usage"] == {"prompt_tokens": 9, "completion_tokens": 4,
                               "total_tokens": 13}
    assert result["stop_reason"] == "end_turn"


def test_v74_routing_errors_are_honest():
    # a provider HTTP error surfaces the status + a body preview, never the key
    handled: list[dict] = []
    transport = httpx.MockTransport(_mock_handler(handled, status=401,
                                                  body={"error": "bad key"}))
    try:
        llm_routing.set_transport(transport)
        with pytest.raises(llm_routing.LLMRoutingError) as exc:
            asyncio.run(llm_routing.chat_completion(
                {"provider": "openai", "base_url": "https://api.openai.com/v1",
                 "api_key": "sk-secret-never-logged"},
                model="gpt-4o-mini", messages=[{"role": "user", "content": "x"}]))
        assert "HTTP 401" in str(exc.value) and "bad key" in str(exc.value)
        # an empty completion is an honest error, not a silent empty answer
        transport = httpx.MockTransport(lambda request: httpx.Response(
            200, json={"choices": [{"message": {"content": ""}}]}))
        llm_routing.set_transport(transport)
        with pytest.raises(llm_routing.LLMRoutingError) as exc:
            asyncio.run(llm_routing.chat_completion(
                {"provider": "openai", "base_url": "https://api.openai.com/v1",
                 "api_key": "k"}, model="gpt-4o-mini",
                messages=[{"role": "user", "content": "x"}]))
        assert "empty completion" in str(exc.value)
    finally:
        llm_routing.set_transport(None)
    # no model and a preset without a default (lm_studio) -> exact remediation,
    # raised BEFORE any network byte is spent
    with pytest.raises(llm_routing.LLMRoutingError) as exc:
        asyncio.run(llm_routing.chat_completion(
            {"provider": "lm_studio", "base_url": "http://localhost:1234/v1",
             "api_key": "placeholder"},
            model="", messages=[{"role": "user", "content": "x"}]))
    assert "no model given" in str(exc.value)


# ---------------------------------------------------------------------------
# 2) the nodes route through the credential (llm_chat end-to-end)
# ---------------------------------------------------------------------------

def test_v74_llm_chat_node_real_routing():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "route", 1)
            h = _auth(user["token"])
            res = await client.post("/credentials", headers=h, json={
                "name": "stub openai", "type": "openai_compatible",
                "data": {"provider": "openai",
                         "base_url": "https://stub.example/v1",
                         "api_key": "sk-stub-123"}})
            assert res.status_code == 201, res.text
            cred_id = res.json()["id"]

            wf = {"nodes": [
                {"id": "t", "type": "manual_trigger", "name": "T",
                 "position": {"x": 0, "y": 0}, "parameters": {}},
                {"id": "chat", "type": "llm_chat", "name": "Chat",
                 "position": {"x": 200, "y": 0},
                 "parameters": {"provider": "openai_compatible",
                                "model": "gpt-4o-mini",
                                "user_prompt": "ping",
                                "credential_id": cred_id}},
            ], "edges": [{"id": "e", "source": "t", "target": "chat",
                          "sourceHandle": "main", "targetHandle": "main"}]}
            res = await client.post("/workflows", headers=h, json={
                "name": "v74 routed chat", "graph": wf})
            assert res.status_code == 201, res.text
            wid = res.json()["id"]
            return h, wid, cred_id

    async def _run():
        h, wid, _cred = await _go()
        handled: list[dict] = []
        transport = httpx.MockTransport(_mock_handler(handled))
        try:
            llm_routing.set_transport(transport)
            async with _client() as client:
                res = await client.post(f"/workflows/{wid}/run", headers=h)
                assert res.status_code == 200, res.text
                import asyncio as _a
                exec_id = res.json()["execution_id"]
                for _ in range(50):
                    poll = await client.get(f"/executions/{exec_id}", headers=h)
                    if poll.json().get("status") in ("success", "failed"):
                        return poll.json(), handled
                    await _a.sleep(0.05)
                raise AssertionError("execution never finished")
        finally:
            llm_routing.set_transport(None)

    execution, handled = _sync(_wrap(_run()))
    assert execution["status"] == "success", execution.get("error")
    assert len(handled) == 1
    assert handled[0]["url"] == "https://stub.example/v1/chat/completions"
    assert handled[0]["headers"].get("authorization") == "Bearer sk-stub-123"
    chat_node = next(r for r in execution["node_runs"] if r["node_id"] == "chat")
    assert chat_node["output"]["text"] == '{"answer": "routed"}'
    assert chat_node["output"]["provider"] == "openai"


# ---------------------------------------------------------------------------
# 3) the phone brain carries the credential (scaffold + validation)
# ---------------------------------------------------------------------------

async def _mk_llm_cred(client: httpx.AsyncClient, headers: dict, name: str,
                       cred_type: str = "openai_compatible") -> dict:
    res = await client.post("/credentials", headers=headers, json={
        "name": name, "type": cred_type,
        "data": ({"provider": "anthropic", "base_url": "https://api.anthropic.com/v1",
                  "api_key": "sk-ant-test"}
                 if cred_type == "anthropic" else
                 {"provider": "openai", "base_url": "https://stub.example/v1",
                  "api_key": "sk-stub-123"})})
    assert res.status_code == 201, res.text
    return res.json()


def test_v74_brain_credential_scaffold_rules():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "brain", 1)
            stranger = await _mk_user(client, "brain", 2)
            h = _auth(user["token"])
            sh = _auth(stranger["token"])
            cred = await _mk_llm_cred(client, h, "deepseek key")
            anth = await _mk_llm_cred(client, h, "claude key", "anthropic")

            # openai_compatible WITHOUT a credential fails loud, nothing created
            res = await client.post("/voice/agents", headers=h, json={
                "name": "no cred", "scaffold_handler": True,
                "brain": "ai_agent", "brain_provider": "openai_compatible"})
            assert res.status_code == 400, res.text
            assert "REAL LLM credential" in res.json()["detail"]

            # with a credential: the scaffolded ai_agent node CARRIES it
            res = await client.post("/voice/agents", headers=h, json={
                "name": "grounded brain", "scaffold_handler": True,
                "brain": "ai_agent", "brain_provider": "openai_compatible",
                "brain_model": "deepseek-chat",
                "llm_credential_id": cred["id"]})
            assert res.status_code == 201, res.text
            agent = res.json()
            assert agent["brain"]["credential_id"] == cred["id"]
            assert agent["brain"]["credential_name"] == "deepseek key"
            res = await client.get(f"/workflows/{agent['handler_workflow_id']}", headers=h)
            graph = res.json().get("graph") or res.json()
            node = next(n for n in graph["nodes"] if n["type"] == "ai_agent")
            assert node["parameters"]["credential_id"] == cred["id"]
            assert node["parameters"]["provider"] == "openai_compatible"
            assert node["parameters"]["model"] == "deepseek-chat"

            # an anthropic credential binds too (Claude's native wire)
            res = await client.post("/voice/agents", headers=h, json={
                "name": "claude brain", "scaffold_handler": True,
                "brain": "ai_agent", "brain_provider": "openai_compatible",
                "llm_credential_id": anth["id"]})
            assert res.status_code == 201, res.text

            # a foreign credential is a 404-grade refusal
            res = await client.post("/voice/agents", headers=sh, json={
                "name": "thief", "scaffold_handler": True,
                "brain": "ai_agent", "brain_provider": "openai_compatible",
                "llm_credential_id": cred["id"]})
            assert res.status_code in (400, 404), res.text

            # a NON-LLM credential type is refused with the exact remediation
            res = await client.post("/credentials", headers=h, json={
                "name": "plain header", "type": "header_auth",
                "data": {"header_name": "X-Key", "value": "v"}})
            plain = res.json()["id"]
            res = await client.post("/voice/agents", headers=h, json={
                "name": "wrong type", "scaffold_handler": True,
                "brain": "ai_agent", "brain_provider": "openai_compatible",
                "llm_credential_id": plain})
            assert res.status_code == 400, res.text
            assert "openai_compatible or anthropic" in res.json()["detail"]

            # '' on an openai_compatible brain REFUSES honestly - a credential-less
            # LLM brain would silently fall back to the toy bridge
            res = await client.put(f"/voice/agents/{agent['id']}", headers=h,
                                   json={"llm_credential_id": ""})
            assert res.status_code in (400, 404), res.text
            assert "REAL LLM credential" in res.json()["detail"]
            # ...unless the same update flips back to the sandbox bridge
            res = await client.put(f"/voice/agents/{agent['id']}", headers=h,
                                   json={"llm_credential_id": "",
                                         "brain_provider": "sandbox_bridge"})
            assert res.status_code == 200, res.text
            assert res.json()["brain"]["credential_id"] is None
            assert res.json()["brain"]["provider"] == "sandbox_bridge"
            res = await client.put(f"/voice/agents/{agent['id']}", headers=h,
                                   json={"llm_credential_id": anth["id"],
                                         "brain_provider": "openai_compatible"})
            flipped = res.json()
            assert flipped["brain"]["credential_id"] == anth["id"]
            res = await client.get(f"/workflows/{flipped['handler_workflow_id']}", headers=h)
            graph = res.json().get("graph") or res.json()
            node = next(n for n in graph["nodes"] if n["type"] == "ai_agent")
            assert node["parameters"]["credential_id"] == anth["id"]
            return agent

    _sync(_wrap(_go()))


def test_v74_phone_turn_routes_through_credential():
    """THE flagship: the phone's answer is composed by the REAL routed LLM -
    the request carries the Bearer key, the model, the caller's words and
    the knowledge matches; the LLM's reply is what the caller hears."""
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "turn", 1)
            h = _auth(user["token"])
            cred = await _mk_llm_cred(client, h, "routing key")
            res = await client.post("/datasets", headers=h, json={
                "name": f"v74 FAQ {uuid.uuid4().hex[:6]}",
                "rows": [
                    {"question": "What are your opening hours",
                     "answer": "We are open nine to six on weekdays."},
                    {"question": "Where are you located",
                     "answer": "Forty-two Market Street."},
                ]})
            assert res.status_code == 201, res.text
            ds = res.json()["id"]
            res = await client.post("/voice/agents", headers=h, json={
                "name": "routed phone brain", "scaffold_handler": True,
                "brain": "ai_agent", "brain_provider": "openai_compatible",
                "brain_model": "deepseek-chat",
                "llm_credential_id": cred["id"],
                "knowledge_dataset_id": ds,
                "knowledge_text_column": "question",
                "knowledge_answer_column": "answer",
                "system_prompt": "You are the front desk."})
            assert res.status_code == 201, res.text
            agent = res.json()
            res = await client.post("/voice/sessions", headers=h, json={
                "direction": "inbound", "provider": "telnyx",
                "call_ref": f"CA74{uuid.uuid4().hex[:6]}",
                "from_ref": "+15550001", "to_ref": "+15559999",
                "agent_id": agent["id"]})
            assert res.status_code == 201, res.text
            sess = res.json()
            for kind in ("call.ringing", "call.answered"):
                res = await client.post(f"/voice/sessions/{sess['id']}/events",
                                        headers=h, json={"kind": kind})
                assert res.status_code == 200, res.text
            return h, sess["id"]

    async def _turn():
        h, sid = await _go()
        handled: list[dict] = []
        transport = httpx.MockTransport(_mock_handler(handled, body={
            "choices": [{"message": {"content":
                "{\"answer\": \"We are open nine to six on weekdays.\"}"},
                "finish_reason": "stop"}],
            "model": "deepseek-chat"}))
        try:
            llm_routing.set_transport(transport)
            async with _client() as client:
                res = await client.post(f"/voice/sessions/{sid}/turn", headers=h,
                                        json={"transcript": "What are your opening hours",
                                              "confidence": 0.92})
                assert res.status_code == 200, res.text
                turn = res.json()
        finally:
            llm_routing.set_transport(None)
        assert turn["reply"] == "We are open nine to six on weekdays."
        assert len(handled) == 1, "exactly one LLM call routed through the credential"
        req = handled[0]
        assert req["url"] == "https://stub.example/v1/chat/completions"
        assert req["headers"].get("authorization") == "Bearer sk-stub-123"
        assert req["body"]["model"] == "deepseek-chat"
        user_msg = req["body"]["messages"][-1]["content"]
        assert "What are your opening hours" in user_msg
        assert "nine to six" in user_msg, "the knowledge match rode the prompt"
        system_msg = " ".join(m["content"] for m in req["body"]["messages"]
                              if m["role"] == "system")
        assert "front desk" in system_msg, "the persona rode the system prompt"

    _sync(_wrap(_turn()))


# ---------------------------------------------------------------------------
# 4) the speech-loop verifier
# ---------------------------------------------------------------------------

def test_v74_speech_verify_roundtrip():
    import io
    import wave

    def _wav(pcm: bytes, rate: int) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(pcm)
        return buf.getvalue()

    phrase = "What are your opening hours on saturday"
    spoken_wav = _wav(b"\x00\x01" * 8000, 22050)  # 22.05 kHz mono 16-bit

    from app.services import voice_transport

    engines.register_tts_engine("fake-tts",
                                lambda text, voice, fmt: spoken_wav)
    voice_transport.register_asr_engine("fake-asr-exact",
                                        lambda pcm, rate: {"transcript": phrase.upper(),
                                                           "confidence": 0.87})
    voice_transport.register_asr_engine("fake-asr-deaf",
                                        lambda pcm, rate: {"transcript": "completely different words here",
                                                           "confidence": 0.5})
    try:
        report = engines.verify_bridge(asr="fake-asr-exact", tts="fake-tts", phrase=phrase)
        assert report["ok"] is True and report["exact"] is True
        assert report["match_ratio"] == 1.0
        assert report["heard"].lower() == phrase.lower()
        assert report["tts"]["sample_rate"] == 22050
        assert report["tts"]["pcm_bytes"] == 16000
        assert report["asr"]["engine"] == "fake-asr-exact"

        report = engines.verify_bridge(asr="fake-asr-deaf", tts="fake-tts", phrase=phrase)
        assert report["ok"] is False and report["exact"] is False
        assert report["match_ratio"] < 0.5

        # honest unavailability: the whisper.cpp backend key resolves from the
        # machine probe - without a binary the error says exactly that
        with pytest.raises(voice_svc.VoiceError) as exc:
            engines.verify_bridge(asr="whisper.cpp", tts="fake-tts")
        assert "whisper.cpp bridge cannot run" in str(exc.value)
    finally:
        engines.unregister_tts_engine("fake-tts")
        voice_transport.unregister_asr_engine("fake-asr-exact")
        voice_transport.unregister_asr_engine("fake-asr-deaf")


def test_v74_verify_endpoint_and_probe(monkeypatch):
    # a BARE machine: the piper probe reports unavailable regardless of what
    # earlier tests left in the process env (the v74 smoke proves the real one)
    from app.services import speech_engines as speech_engines_mod

    monkeypatch.setattr(speech_engines_mod, "probe_piper",
                        lambda: {"available": False,
                                 "note": "no piper binary found (test bare machine)"})
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "verify", 1)
            h = _auth(user["token"])
            # the endpoint surfaces the honest 409 when the loop cannot run
            res = await client.post("/voice/speech/verify", headers=h, json={})
            assert res.status_code == 409, res.text
            assert "piper" in res.json()["detail"].lower()
            # an anthropic credential's secret is blanked in the edit view
            res = await client.post("/credentials", headers=h, json={
                "name": "claude", "type": "anthropic",
                "data": {"provider": "anthropic",
                         "base_url": "https://api.anthropic.com/v1",
                         "api_key": "sk-ant-verysecret"}})
            assert res.status_code == 201, res.text
            cred_id = res.json()["id"]
            res = await client.get(f"/credentials/{cred_id}", headers=h)
            assert res.json()["data"]["api_key"] == "", "secrets are never echoed"
            # the providers catalog rides the credentials API
            res = await client.get("/credentials/providers", headers=h)
            assert res.status_code == 200, res.text
            names = {p["provider"] for p in res.json()["providers"]}
            assert {"openai", "anthropic", "deepseek", "kimi", "qwen",
                    "openrouter"} <= names
            return

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 5) multi-party meetings - legs, one persona, the merged transcript
# ---------------------------------------------------------------------------

async def _mk_agent(client: httpx.AsyncClient, headers: dict, name: str) -> dict:
    res = await client.post("/voice/agents", headers=headers, json={
        "name": name, "scaffold_handler": True,
        "greeting_text": "Welcome to the room."})
    assert res.status_code == 201, res.text
    return res.json()


def test_v74_meetings_web_legs_and_transcript():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "mtg", 1)
            stranger = await _mk_user(client, "mtg", 2)
            h = _auth(user["token"])
            agent = await _mk_agent(client, h, "meeting persona")

            res = await client.post("/voice/meetings", headers=h, json={
                "title": "support huddle", "agent_id": agent["id"]})
            assert res.status_code == 201, res.text
            meeting = res.json()
            assert meeting["state"] == "active"
            assert meeting["agent_name"] == "meeting persona"

            # two WEB legs: real sessions, bound to the SAME agent, answered
            for label in ("alice", "bob"):
                res = await client.post(f"/voice/meetings/{meeting['id']}/join",
                                        headers=h, json={"label": label, "channel": "web"})
                assert res.status_code == 200, res.text
                leg = res.json()["participant"]
                assert leg["state"] == "joined" and leg["session_id"]
                assert leg["media_stream"], "web legs point at the media websocket"
                res = await client.get(f"/voice/sessions/{leg['session_id']}", headers=h)
                body = res.json()
                assert body["state"] == "in_progress"
                assert body["agent"]["voice_agent_id"] == agent["id"]

            # the greeting rode each leg (the agent's own TTS config)
            detail = (await client.get(f"/voice/meetings/{meeting['id']}", headers=h)).json()
            agent_lines = [l for l in detail["transcript"] if l["side"] == "agent"]
            assert len(agent_lines) == 2, "each leg heard the greeting"
            assert all("Welcome to the room" in l["text"] for l in agent_lines)

            # one turn per leg - the merged transcript attributes each speaker
            legs = detail["participants"]
            for leg, words in zip(legs, ("alice asks about the bill", "bob asks about shipping")):
                res = await client.post(f"/voice/sessions/{leg['session_id']}/turn",
                                        headers=h, json={"transcript": words,
                                                         "confidence": 0.9})
                assert res.status_code == 200, res.text
            detail = (await client.get(f"/voice/meetings/{meeting['id']}", headers=h)).json()
            lines = detail["transcript"]
            participant_lines = [l for l in lines if l["side"] == "participant"]
            assert [l["speaker"] for l in participant_lines] == ["alice", "bob"]
            assert "bill" in participant_lines[0]["text"]
            assert "shipping" in participant_lines[1]["text"]
            assert detail["counts"]["participants"] == 2

            # a stranger cannot even see the room
            sh = _auth(stranger["token"])
            res = await client.get(f"/voice/meetings/{meeting['id']}", headers=sh)
            assert res.status_code == 404, res.text

            # END: every live leg hangs up, the room closes
            res = await client.post(f"/voice/meetings/{meeting['id']}/end", headers=h)
            assert res.status_code == 200, res.text
            ended = res.json()
            assert ended["state"] == "ended"
            assert all(p["state"] == "left" for p in ended["participants"])
            res = await client.get(f"/voice/sessions/{legs[0]['session_id']}", headers=h)
            assert res.json()["state"] == "ended"
            # joining an ended room refuses
            res = await client.post(f"/voice/meetings/{meeting['id']}/join",
                                    headers=h, json={"label": "late", "channel": "web"})
            assert res.status_code == 400, res.text
            return

    _sync(_wrap(_go()))


def test_v74_meetings_phone_legs_are_honest():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "mtg2", 1)
            h = _auth(user["token"])
            agent = await _mk_agent(client, h, "dial persona")
            res = await client.post("/voice/meetings", headers=h, json={
                "title": "dial room", "agent_id": agent["id"]})
            meeting = res.json()

            # telnyx leg without an endpoint -> honest skip
            res = await client.post(f"/voice/meetings/{meeting['id']}/join",
                                    headers=h, json={"label": "phone guy",
                                                     "channel": "telnyx",
                                                     "address": "+15550003"})
            assert res.status_code == 200, res.text
            leg = res.json()["participant"]
            assert leg["state"] == "skipped"
            assert "endpoint" in leg["last_error"].lower()

            # an endpoint whose config carries no connection_id -> failed with remediation
            res = await client.post("/channels/endpoints", headers=h, json={
                "name": "bare telnyx", "provider": "telnyx_call_control",
                "config": {"api_key": "telnyx-key-abc", "public_key": "-----BEGIN PUBLIC KEY-----\nMFow\n-----END PUBLIC KEY-----"}})
            assert res.status_code == 201, res.text
            ep = res.json()
            res = await client.post(f"/voice/meetings/{meeting['id']}/join",
                                    headers=h, json={"label": "phone guy",
                                                     "channel": "telnyx",
                                                     "address": "+15550003",
                                                     "endpoint_id": ep["id"]})
            assert res.status_code == 200, res.text
            leg = res.json()["participant"]
            assert leg["state"] == "failed"
            assert "webhook_url" in leg["last_error"] or "connection_id" in leg["last_error"]
            return meeting, ep, h

    _sync(_wrap(_go()))


def test_v74_meeting_dial_and_link():
    """A full phone leg: the dial is built (client_state binds the row), the
    carrier answers, the webhook-side link joins the leg and binds the agent."""
    import app.models as models
    from app.db import AsyncSessionLocal
    from app.services import voice_meetings as meetings_svc

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "mtg3", 1)
            h = _auth(user["token"])
            agent = await _mk_agent(client, h, "linked persona")
            res = await client.post("/channels/endpoints", headers=h, json={
                "name": "full telnyx", "provider": "telnyx_call_control",
                "config": {"api_key": "telnyx-key-abc",
                           "public_key": "-----BEGIN PUBLIC KEY-----\nMFow\n-----END PUBLIC KEY-----",
                           "connection_id": "conn-123",
                           "webhook_url": "https://py8n.example/api/v1/channels/telnyx/x/webhook",
                           "from_number": "+15559999"}})
            ep = res.json()
            res = await client.post("/voice/meetings", headers=h, json={
                "title": "conference", "agent_id": agent["id"]})
            meeting = res.json()

            dials: list[dict] = []
            res = await client.post(f"/voice/meetings/{meeting['id']}/join", headers=h,
                                    json={"label": "phone leg", "channel": "telnyx",
                                          "address": "+15550004",
                                          "endpoint_id": ep["id"]})
            assert res.status_code == 200, res.text
            leg = res.json()["participant"]
            # the API path has NO egress in tests: the default sender would hit
            # the real carrier, so the honest outcome here is what py8n reports
            # when the carrier is unreachable - a failed leg with the cause,
            # never a claimed "dialing" state
            assert leg["state"] in ("failed", "skipped"), leg
            assert leg["last_error"], "the failure carries its exact cause"
            return meeting, agent, h, ep

    meeting, agent, h, ep = _sync(_wrap(_go()))

    # sender-injected service path: the REAL dial request is built + delivered
    dials: list[dict] = []

    async def _sender(config, request):
        dials.append(request)
        return {"status_code": 200,
                "json": {"data": {"call_control_id": "CC-meeting-2"}}}

    async def _join():
        from app.db import AsyncSessionLocal as _L

        async with _L() as db:
            out = await meetings_svc.join_participant(
                db, None, meeting["id"], label="phone leg two", channel="telnyx",
                address="+15550005", endpoint_id=ep["id"], sender=_sender)
            await db.commit()
        return out

    out = _sync(_wrap(_join()))
    leg = out["participant"]
    assert leg["state"] == "dialing"
    assert leg["call_control_id"] == "CC-meeting-2"
    dial = dials[0]
    assert dial["url"].endswith("/v2/calls")
    assert dial["headers"]["Authorization"] == "Bearer telnyx-key-abc"
    assert dial["json"]["connection_id"] == "conn-123"
    assert dial["json"]["to"] == "+15550005"
    state = meetings_svc.decode_client_state(dial["json"]["client_state"])
    assert state["mtg"] == meeting["id"]
    assert state["prt"] == leg["id"]

    # the carrier answers -> the webhook-side link joins the leg and binds
    # the meeting's agent on the carrier-created session
    async def _link():
        async with _client() as client:
            res = await client.post("/voice/sessions", headers=h, json={
                "direction": "inbound", "provider": "telnyx",
                "call_ref": "CC-meeting-2", "from_ref": "+15550005",
                "to_ref": "+15559999"})
            assert res.status_code == 201, res.text
            sid = res.json()["id"]
        async with AsyncSessionLocal() as db:
            carrier = await db.get(models.VoiceSession, sid)
            linked = await meetings_svc.link_call(
                db, call_control_id="CC-meeting-2",
                client_state=dial["json"]["client_state"], session=carrier)
            await db.commit()
        return linked, sid

    linked, sid = _sync(_wrap(_link()))
    assert linked and linked["meeting_id"] == meeting["id"]
    assert linked["agent_bound"] == agent["id"]


# ---------------------------------------------------------------------------
# 6) outbound campaigns
# ---------------------------------------------------------------------------

def test_v74_campaign_lifecycle():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "cmp", 1)
            h = _auth(user["token"])
            agent = await _mk_agent(client, h, "campaign persona")

            # validation: empty targets / foreign agent / unknown endpoint
            res = await client.post("/voice/campaigns", headers=h, json={
                "name": "no targets", "agent_id": agent["id"], "targets": []})
            assert res.status_code in (400, 422), res.text
            res = await client.post("/voice/campaigns", headers=h, json={
                "name": "bad addr", "agent_id": agent["id"],
                "targets": [{"address": " "}]}
            )
            assert res.status_code == 400, res.text

            targets = [{"address": "+15551110001", "name": "alice"},
                       {"address": "+15551110002", "name": "bob"},
                       {"address": "sip:desk@pbx.example.com"}]
            # no endpoint bound: start marks every target skipped HONESTLY
            res = await client.post("/voice/campaigns", headers=h, json={
                "name": "renewals", "agent_id": agent["id"], "targets": targets})
            assert res.status_code == 201, res.text
            bare = res.json()
            assert bare["status"] == "draft"
            res = await client.post(f"/voice/campaigns/{bare['id']}/start",
                                    headers=h, json={})
            assert res.status_code == 200, res.text
            body = res.json()
            assert "nothing was dialed" in body["start_note"]
            assert all(t["status"] == "skipped" and "endpoint_id" in (t["last_error"] or "")
                       for t in body["targets"])
            assert body["status"] == "draft", "a skipped campaign never claims running"

            # with an endpoint: the dial is real-shaped; delivery is injected
            res = await client.post("/channels/endpoints", headers=h, json={
                "name": "camp telnyx", "provider": "telnyx_call_control",
                "config": {"api_key": "telnyx-key-xyz",
                           "public_key": "-----BEGIN PUBLIC KEY-----\nMFow\n-----END PUBLIC KEY-----",
                           "connection_id": "conn-camp",
                           "webhook_url": "https://py8n.example/api/v1/channels/telnyx/x/webhook",
                           "from_number": "+15559998"}})
            ep = res.json()
            res = await client.post("/voice/campaigns", headers=h, json={
                "name": "renewals real", "agent_id": agent["id"],
                "endpoint_id": ep["id"], "targets": targets})
            camp = res.json()

            dials: list[dict] = []
            import app.services.voice_campaigns as camp_svc

            async def _sender(config, request):
                dials.append(request)
                return {"status_code": 200,
                        "json": {"data": {"call_control_id": f"CC-camp-{len(dials)}"}}}

            from app.db import AsyncSessionLocal

            async with AsyncSessionLocal() as db:
                out = await camp_svc.start_campaign(db, user["id"], camp["id"], sender=_sender)
                await db.commit()
            assert out["status"] == "running"
            assert out["progress"]["placed"] == 3
            assert all(t["status"] == "dialing" for t in out["targets"])
            assert len(dials) == 3
            first = dials[0]
            assert first["url"].endswith("/v2/calls")
            assert first["headers"]["Authorization"] == "Bearer telnyx-key-xyz"
            assert first["json"]["to"] == "+15551110001"
            assert first["json"]["connection_id"] == "conn-camp"
            state = camp_svc.decode_client_state(first["json"]["client_state"])
            assert state["cmp"] == camp["id"]

            # simulate the answer path for one target (honest: named simulate)
            tid = out["targets"][0]["id"]
            res = await client.post(
                f"/voice/campaigns/{camp['id']}/targets/{tid}/simulate-answer",
                headers=h, json={})
            assert res.status_code == 200, res.text
            sim = res.json()
            assert sim["simulated"] is True
            res = await client.get(f"/voice/sessions/{sim['session_id']}", headers=h)
            sess = res.json()
            assert sess["state"] == "in_progress"
            assert sess["agent"]["voice_agent_id"] == agent["id"]
            assert sess["direction"] == "outbound"
            # the session's context records the campaign + the simulation
            sim_event = [e for e in sess["events"] if e["kind"] == "call.answered"]
            assert sim_event and sim_event[0]["payload"]["source"] == "campaign_simulate"
            # a second simulate on the same target refuses loudly
            res = await client.post(
                f"/voice/campaigns/{camp['id']}/targets/{tid}/simulate-answer",
                headers=h, json={})
            assert res.status_code == 400, res.text

            # progress is derived from the target rows
            res = await client.get(f"/voice/campaigns/{camp['id']}", headers=h)
            body = res.json()
            assert body["progress"]["counts"]["answered"] == 1
            assert body["progress"]["counts"]["dialing"] == 2

            # stop: no further dials
            res = await client.post(f"/voice/campaigns/{camp['id']}/stop", headers=h)
            assert res.status_code == 200, res.text
            res = await client.post(f"/voice/campaigns/{camp['id']}/start", headers=h, json={})
            assert res.status_code == 400, res.text
            return

    _sync(_wrap(_go()))


def test_v74_campaign_webhook_link_and_outcomes():
    """The receiver-side linkage: an answered carrier call binds its target
    to the campaign agent; a dial that never connects maps to an honest
    no_answer; calls outside any campaign bind nothing."""
    import app.models as models
    from app.db import AsyncSessionLocal
    import app.services.voice_campaigns as camp_svc

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "cmp2", 1)
            h = _auth(user["token"])
            agent = await _mk_agent(client, h, "linked campaign persona")
            res = await client.post("/voice/campaigns", headers=h, json={
                "name": "link camp", "agent_id": agent["id"],
                "targets": [{"address": "+15552220001"},
                            {"address": "+15552220002"}]})
            camp = res.json()
            res = await client.get(f"/voice/campaigns/{camp['id']}", headers=h)
            targets = res.json()["targets"]
            return camp, targets, agent

    camp, targets, agent = _sync(_wrap(_go()))
    t1, t2 = targets[0], targets[1]

    # t1 ANSWERED: py8n opens the session through the same path a real
    # answer takes and binds the campaign agent
    async def _answer():
        state = camp_svc.client_state_for(camp["id"], t1["id"])
        async with AsyncSessionLocal() as db:
            link = await camp_svc.on_call_event(
                db, call_control_id="CC-link-1", client_state=state,
                event_kind="call.answered", session=None)
            await db.commit()
        return link

    link = _sync(_wrap(_answer()))
    assert link and link["campaign_id"] == camp["id"]
    assert link["status"] == "answered" and link["session_id"]

    async def _check_session():
        async with AsyncSessionLocal() as db:
            sess = await db.get(models.VoiceSession, link["session_id"])
            agent_block = (sess.context or {}).get("voice_agent") or {}
            tgt = await db.get(models.VoiceCampaignTarget, link["target_id"])
            return sess.state, agent_block.get("voice_agent_id"), tgt.session_id

    sess_state, bound_agent, tgt_session = _sync(_wrap(_check_session()))
    assert sess_state == "in_progress"
    assert bound_agent == agent["id"]
    assert tgt_session == link["session_id"]

    # t2 was DIALED (what start_campaign does) but the call never connected:
    # the hangup-family event maps to an honest no_answer
    async def _no_answer():
        async with AsyncSessionLocal() as db:
            row = await db.get(models.VoiceCampaignTarget, t2["id"])
            row.status = "dialing"
            row.call_control_id = "CC-link-2"
            db.add(row)
            await db.commit()
        async with AsyncSessionLocal() as db:
            link2 = await camp_svc.on_call_event(
                db, call_control_id="CC-link-2", event_kind="no_answer")
            await db.commit()
            row = await db.get(models.VoiceCampaignTarget, t2["id"])
            return link2, row.status

    link2, status2 = _sync(_wrap(_no_answer()))
    assert link2 and link2["target_id"] == t2["id"]
    assert status2 == "no_answer"

    # a call that belongs to NO campaign binds nothing
    async def _stranger():
        async with AsyncSessionLocal() as db:
            return await camp_svc.on_call_event(
                db, call_control_id="CC-stranger", event_kind="call.answered")

    assert _sync(_wrap(_stranger())) is None


def test_v74_dial_builder_validation():
    from app.services.channel_adapters import telnyx_build_dial

    cfg = {"api_key": "k"}
    req = telnyx_build_dial(cfg, to="+15550005", from_ref="+15559999",
                            connection_id="conn-1",
                            webhook_url="https://x.example/hook",
                            client_state="abc")
    assert req["method"] == "POST" and req["url"].endswith("/v2/calls")
    assert req["json"]["connection_id"] == "conn-1"
    assert req["json"]["client_state"] == "abc"
    for kwargs in (
        {"from_ref": "+15559999", "connection_id": "c", "webhook_url": "https://x/h"},
        {"to": "+15550005", "connection_id": "c", "webhook_url": "https://x/h"},
        {"to": "+15550005", "from_ref": "+15559999", "webhook_url": "https://x/h"},
        {"to": "+15550005", "from_ref": "+15559999", "connection_id": "c"},
    ):
        with pytest.raises((ValueError, TypeError)):
            telnyx_build_dial(cfg, **kwargs)
    with pytest.raises(ValueError) as exc:
        telnyx_build_dial(cfg, to="+15550005", from_ref="+15559999",
                          connection_id="c", webhook_url="not-absolute")
    assert "webhook_url" in str(exc.value)
