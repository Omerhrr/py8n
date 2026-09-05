"""V73 feature tests: the ai_agent-over-knowledge handler variant (an LLM
brain on the SAME binding), voice session analytics (per-turn ASR
confidence trends), and real vosk/whisper model installs for a fully
offline phone.

- the LLM brain: a voice agent built with brain="ai_agent" scaffolds a
  trigger -> ai_agent handler whose prompt is a LIVE TEMPLATE over the
  handler envelope - the caller's words AND the knowledge matches from
  the SAME binding the deterministic handler reads ride into the model's
  user message (asserted by capturing the transport), the persona rides
  the system prompt, the {"answer": ...} reply is extracted by the
  handler convention, and brain flips re-scaffold honestly (never
  replacing a custom handler).
- analytics: per-turn confidence series derived from asr.final events
  (never stored), least-squares trends (degrading / stable / unknown),
  weak turns below the 0.6 gate, honest "unreported" accounting for
  engines that emit 0.0, and a per-agent pooled view. The vosk bridge
  now reports REAL per-turn confidences (mean of word confidences).
- model installs: the catalog downloads (fetcher-injected) REAL model
  layouts - the vosk Kaldi zip (CRC + am/conf verified), the whisper.cpp
  ggml magic, the piper onnx + json pair, the piper release tarball
  (extracted whole, binary linked under bin/) - with atomic .part
  writes, slip-guarded extraction, and fail-loud corruption checks.

Runs the FastAPI app in-process (httpx ASGITransport).
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import stat
import sys
import tarfile
import types
import uuid
import zipfile
from pathlib import Path

import httpx
import pytest

from app.main import app
from app.config import settings
from app.services import executor as executor_mod
from app.services import speech_models as models_svc
from app.services.interactions import _REPLY_KEYS

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


async def _mk_user(client: httpx.AsyncClient, tag: str, n: int) -> dict:
    res = await client.post("/auth/register", json={
        "email": f"v73-{tag}-u{n}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v73 u{n} {tag}",
    })
    assert res.status_code == 201, res.text
    body = res.json()
    return {"token": body["token"], "id": body["user"]["id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _mk_faq(client: httpx.AsyncClient, headers: dict, name: str) -> str:
    res = await client.post("/datasets", headers=headers, json={
        "name": name, "description": "v73 FAQ",
        "rows": [
            {"question": "What are your opening hours",
             "answer": "We are open Monday to Friday from nine AM to six PM."},
            {"question": "Where are you located",
             "answer": "Our office is at forty-two Market Street, downtown."},
            {"question": "How do I return a product",
             "answer": "You can return any product within thirty days with the receipt."},
        ]})
    assert res.status_code == 201, res.text
    return res.json()["id"]


async def _mk_session(client: httpx.AsyncClient, headers: dict, agent_id: str,
                      tag: str, n: int) -> dict:
    res = await client.post("/voice/sessions", headers=headers, json={
        "direction": "inbound", "provider": "telnyx", "call_ref": f"CA{tag}{n}",
        "from_ref": f"+1555000{n}", "to_ref": "+15559999",
        "agent_id": agent_id})
    assert res.status_code == 201, res.text
    sess = res.json()
    res = await client.post(f"/voice/sessions/{sess['id']}/events", headers=headers,
                            json={"kind": "call.ringing"})
    assert res.status_code == 200, res.text
    res = await client.post(f"/voice/sessions/{sess['id']}/events", headers=headers,
                            json={"kind": "call.answered"})
    assert res.status_code == 200, res.text
    return sess


async def _turn(client: httpx.AsyncClient, headers: dict, session_id: str,
                transcript: str, confidence: float) -> dict:
    res = await client.post(f"/voice/sessions/{session_id}/turn", headers=headers,
                            json={"transcript": transcript, "confidence": confidence})
    assert res.status_code == 200, res.text
    return res.json()


class _ScriptedChat:
    """Replaces AgentNode._chat with a scripted reply sequence + capture."""

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.calls: list[list[dict]] = []
        self._original = None

    def install(self):
        from app.engine.nodes.agent import AgentNode

        calls, replies = self.calls, self.replies

        async def _fake_chat(agent_self, messages, temperature):
            calls.append(json.loads(json.dumps(messages)))
            return replies.pop(0) if replies else '{"answer": "script exhausted"}'

        self._original = AgentNode._chat
        AgentNode._chat = _fake_chat  # type: ignore[method-assign]

    def restore(self):
        from app.engine.nodes.agent import AgentNode

        if self._original is not None:
            AgentNode._chat = self._original  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# 1) pins
# ---------------------------------------------------------------------------

def test_v73_pins():
    assert settings.version >= "1.73.0"  # v74+ carries the batch forward
    from app.services.voice_agents import BRAINS, BRAIN_PROVIDERS
    assert BRAINS == ("scaffold", "ai_agent")
    assert BRAIN_PROVIDERS == ("sandbox_bridge", "openai_compatible")
    # the ai_agent node's {"answer": ...} is now a first-class reply key
    assert "answer" in _REPLY_KEYS


# ---------------------------------------------------------------------------
# 2) THE FLAGSHIP: the LLM brain over the SAME knowledge binding
# ---------------------------------------------------------------------------

def test_v73_ai_brain_grounded_turn():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"{tag}a", 1)
            h = _auth(user["token"])
            ds_id = await _mk_faq(client, h, f"faq-{tag}a")

            # ---- create with the LLM brain: the scaffold IS an ai_agent node
            res = await client.post("/voice/agents", headers=h, json={
                "name": f"Brainy agent {tag}",
                "greeting_text": "Hello, how can I help?",
                "scaffold_handler": True, "brain": "ai_agent",
                "system_prompt": "You are a courteous phone support agent. Answer ONLY from the knowledge matches in metadata.knowledge; when nothing matches, say you will take a message.",
                "knowledge_dataset_id": ds_id,
                "knowledge_text_column": "question", "knowledge_answer_column": "answer"})
            assert res.status_code == 201, res.text
            agent = res.json()
            assert agent["brain"]["kind"] == "ai_agent"
            assert agent["brain"]["provider"] == "sandbox_bridge"
            assert agent["handler_is_scaffold"] is True
            assert "brain_note" in agent["wiring"]
            assert agent["knowledge"]["dataset_id"] == ds_id

            # the scaffolded workflow carries the ai_agent node with the
            # envelope-templated prompt
            res = await client.get(f"/workflows/{agent['handler_workflow_id']}",
                                   headers=h)
            assert res.status_code == 200, res.text
            graph = res.json().get("graph") or res.json()
            nodes = {n["id"]: n for n in graph["nodes"]}
            assert "ai_agent" in {n["type"] for n in graph["nodes"]}
            brain_node = next(n for n in graph["nodes"] if n["type"] == "ai_agent")
            params = brain_node["parameters"]
            assert "input.payload.metadata.get('knowledge')" in params["user_message"]
            assert "input.payload.text" in params["user_message"]
            assert "system_prompt" in params["system_prompt"]
            return agent, h, ds_id

    agent, h, ds_id = _sync(_wrap(_go()))

    # ---- a turn through the REAL engine: the LLM sees transcript + matches
    chat = _ScriptedChat(['{"answer": "We are open Monday to Friday from nine AM to six PM."}',
                          '{"answer": "I will take a message and call you back."}'])
    chat.install()
    try:
        async def _go2():
            async with _client() as client:
                sess = await _mk_session(client, h, agent["id"], f"{tag}a", 11)
                turn = await _turn(client, h, sess["id"],
                                   "What are your opening hours?", 0.87)
                assert turn["reply"] == "We are open Monday to Friday from nine AM to six PM."
                assert turn["knowledge"], "the turn is still grounded by the binding"
                await client.post(f"/voice/sessions/{sess['id']}/tts/complete", headers=h)
                turn2 = await _turn(client, h, sess["id"],
                                    "what is the moon made of", 0.9)
                assert turn2["reply"] == "I will take a message and call you back."
                return sess
        sess = _sync(_wrap(_go2()))
    finally:
        chat.restore()

    assert len(chat.calls) == 2
    sys_msg = chat.calls[0][0]["content"]
    user_msg = chat.calls[0][1]["content"]
    # the persona rode the envelope's metadata into the system prompt
    assert sys_msg.startswith("You are a courteous phone support agent.")
    # the caller's words AND the dataset's answer BOTH reached the model:
    # one binding, feeding the LLM brain exactly like the code node
    assert "What are your opening hours" in user_msg
    assert "nine AM to six PM" in user_msg
    # the unmatched turn saw an EMPTY match list (honest, never invented)
    user2 = chat.calls[1][1]["content"]
    assert "what is the moon made of" in user2
    assert "[]" in user2
    assert "ok" in user2  # the knowledge service status line


def test_v73_brain_flip_rules():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"{tag}b", 1)
            h = _auth(user["token"])

            # ---- scaffold agent: flipping the brain RE-SCAFFOLDS ----------
            res = await client.post("/voice/agents", headers=h, json={
                "name": f"Flip {tag}", "scaffold_handler": True})
            assert res.status_code == 201, res.text
            agent = res.json()
            old_handler = agent["handler_workflow_id"]
            assert agent["brain"]["kind"] == "scaffold"

            res = await client.put(f"/voice/agents/{agent['id']}", headers=h,
                                   json={"brain": "ai_agent",
                                         "brain_model": "qwen2.5-7b"})
            assert res.status_code == 200, res.text
            flipped = res.json()
            # v74: the brain dict grew credential_id/credential_name (None here);
            # the pin checks the v73 contract subset
            assert {k: flipped["brain"][k] for k in ("kind", "provider", "model")} == {
                "kind": "ai_agent", "provider": "sandbox_bridge", "model": "qwen2.5-7b"}
            assert flipped["handler_workflow_id"] != old_handler
            res = await client.get(f"/workflows/{old_handler}", headers=h)
            assert res.status_code == 200, "the old scaffold survives in the estate"
            res = await client.get(f"/workflows/{flipped['handler_workflow_id']}",
                                   headers=h)
            graph = res.json().get("graph") or res.json()
            assert any(n["type"] == "ai_agent" for n in graph["nodes"])

            # flip back to the echo brain
            res = await client.put(f"/voice/agents/{agent['id']}", headers=h,
                                   json={"brain": "scaffold"})
            assert res.json()["brain"]["kind"] == "scaffold"

            # ---- a CUSTOM handler is never silently replaced --------------
            handler = await client.post("/workflows", headers=h, json={
                "name": f"custom-{tag}", "graph": {
                    "nodes": [{"id": "t", "type": "manual_trigger", "name": "T",
                               "position": {"x": 0, "y": 0}, "parameters": {}}],
                    "edges": []}})
            custom_id = handler.json()["id"]
            res = await client.post("/voice/agents", headers=h, json={
                "name": f"Custom {tag}", "handler_workflow_id": custom_id})
            assert res.status_code == 201, res.text
            custom = res.json()
            res = await client.put(f"/voice/agents/{custom['id']}", headers=h,
                                   json={"brain": "ai_agent"})
            assert res.status_code == 404, res.text  # VoiceAgentError -> 404-grade
            assert "custom handler" in res.json()["detail"]
            res = await client.get(f"/voice/agents/{custom['id']}", headers=h)
            assert res.json()["brain"]["kind"] == "scaffold"  # untouched

            # ---- invalid brains/providers fail loud -----------------------
            res = await client.post("/voice/agents", headers=h,
                                    json={"name": "x", "brain": "sparks"})
            assert res.status_code == 400 and "brain must be" in res.json()["detail"]
            res = await client.post("/voice/agents", headers=h,
                                    json={"name": "x", "brain_provider": "hamsters"})
            assert res.status_code == 400 and "brain_provider" in res.json()["detail"]
            return flipped, h

    _sync(_wrap(_go()))


def test_v73_solution_install_brain_variants():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"{tag}c", 1)
            h = _auth(user["token"])

            # one-click LLM-brain phone agent over the SAME installed FAQ
            res = await client.post("/solutions/voice-agent-system/install",
                                    headers=h,
                                    json={"as_voice_agent": True, "brain": "ai_agent"})
            assert res.status_code == 200, res.text
            ref = res.json()["voice_agent"]
            assert ref["wiring"].get("brain_note")
            res = await client.get(f"/voice/agents/{ref['id']}", headers=h)
            agent = res.json()
            assert agent["brain"]["kind"] == "ai_agent"
            assert agent["handler_is_scaffold"] is True
            assert agent["knowledge"]["dataset_id"] == ref["knowledge"]["dataset_id"]
            res = await client.get(f"/workflows/{agent['handler_workflow_id']}",
                                   headers=h)
            graph = res.json().get("graph") or res.json()
            assert any(n["type"] == "ai_agent" for n in graph["nodes"])

            # the deterministic flavor still installs (default)
            res = await client.post("/solutions/voice-agent-system/install",
                                    headers=h,
                                    json={"as_voice_agent": True, "brain": "scaffold"})
            assert res.status_code == 200, res.text
            ref2 = res.json()["voice_agent"]
            res = await client.get(f"/voice/agents/{ref2['id']}", headers=h)
            assert res.json()["brain"]["kind"] == "scaffold"
            assert not res.json()["handler_is_scaffold"]  # the PACK handler

            # an unknown brain refuses before anything is created
            res = await client.post("/solutions/voice-agent-system/install",
                                    headers=h,
                                    json={"as_voice_agent": True, "brain": "cerebro"})
            assert res.status_code == 400 and "brain must be" in res.json()["detail"]

            # a solution without a voice agent pack refuses as before
            res = await client.post("/solutions/customer-support-automation/install",
                                    headers=h,
                                    json={"as_voice_agent": True})
            assert res.status_code in (400, 404)
            return agent

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 3) voice session analytics - per-turn ASR confidence trends
# ---------------------------------------------------------------------------

def test_v73_session_and_agent_analytics():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"{tag}d", 1)
            stranger = await _mk_user(client, f"{tag}d", 2)
            h = _auth(user["token"])
            res = await client.post("/voice/agents", headers=h, json={
                "name": f"Analyst {tag}", "scaffold_handler": True})
            agent = res.json()

            # ---- degrading series: 0.9, 0.8, 0.5, 0.2 ----------------------
            sess1 = await _mk_session(client, h, agent["id"], f"{tag}d", 21)
            for i, (text, conf) in enumerate([
                    ("hello there", 0.9), ("what are your hours", 0.8),
                    ("can I book a slot", 0.5), ("thanks bye", 0.2)]):
                await _turn(client, h, sess1["id"], text, conf)
                await client.post(f"/voice/sessions/{sess1['id']}/tts/complete",
                                  headers=h)
            res = await client.get(f"/voice/sessions/{sess1['id']}/analytics",
                                   headers=h)
            assert res.status_code == 200, res.text
            a1 = res.json()
            assert a1["session_id"] == sess1["id"]
            assert a1["confidence"]["turns"] == 4
            assert a1["confidence"]["turns_reported"] == 4
            assert a1["confidence"]["weak_turns"] == 2
            assert a1["trend"]["direction"] == "degrading"
            assert a1["trend"]["slope"] < 0
            assert a1["trend"]["first_half_mean"] > a1["trend"]["second_half_mean"]
            assert [s["turn_index"] for s in a1["series"]] == [1, 2, 3, 4]
            assert a1["series"][0]["transcript"] == "hello there"
            assert [w["confidence"] for w in a1["weak_turns"]] == [0.5, 0.2]
            assert a1["agent_id"] == agent["id"]

            # ---- stable series ---------------------------------------------
            sess2 = await _mk_session(client, h, agent["id"], f"{tag}d", 22)
            for text, conf in [("one", 0.9), ("two", 0.9), ("three", 0.9)]:
                await _turn(client, h, sess2["id"], text, conf)
                await client.post(f"/voice/sessions/{sess2['id']}/tts/complete",
                                  headers=h)
            a2 = (await client.get(f"/voice/sessions/{sess2['id']}/analytics",
                                   headers=h)).json()
            assert a2["trend"]["direction"] == "stable"
            assert abs(a2["trend"]["slope"]) <= 0.02

            # ---- unreported confidences (0.0) are NOT a trend ---------------
            sess3 = await _mk_session(client, h, agent["id"], f"{tag}d", 23)
            for text in ["alpha", "beta", "gamma", "delta"]:
                await _turn(client, h, sess3["id"], text, 0.0)
                await client.post(f"/voice/sessions/{sess3['id']}/tts/complete",
                                  headers=h)
            a3 = (await client.get(f"/voice/sessions/{sess3['id']}/analytics",
                                   headers=h)).json()
            assert a3["trend"]["direction"] == "unknown"
            assert a3["trend"]["turns_unreported"] == 4
            assert a3["confidence"]["weak_turns"] == 0, "0.0 is unreported, not weak"
            assert "does not emit" in a3["trend"]["note"]

            # ---- the pooled agent view ---------------------------------------
            res = await client.get(f"/voice/agents/{agent['id']}/analytics", headers=h)
            assert res.status_code == 200, res.text
            pool = res.json()
            assert pool["agent_name"] == f"Analyst {tag}"
            assert pool["sessions_with_turns"] == 3
            assert pool["turns_total"] == 11
            assert pool["confidence"]["reported_turns"] == 7
            assert pool["confidence"]["weak_turns"] == 2
            assert pool["confidence"]["weak_turn_rate"] == round(2 / 7, 4)
            assert pool["directions"]["degrading"] == 1
            assert pool["directions"]["stable"] == 1
            assert pool["directions"]["unknown"] == 1
            assert pool["brain"] == "scaffold"
            assert pool["knowledge_bound"] is False
            assert len(pool["per_session"]) == 3

            # ---- ownership: strangers see nothing ----------------------------
            s_h = _auth(stranger["token"])
            for path in (f"/voice/sessions/{sess1['id']}/analytics",
                         f"/voice/agents/{agent['id']}/analytics"):
                res = await client.get(path, headers=s_h)
                assert res.status_code == 404, (path, res.status_code)
            res = await client.get("/voice/agents/nope/analytics", headers=h)
            assert res.status_code == 404
            return agent

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 4) the vosk bridge reports REAL word confidences
# ---------------------------------------------------------------------------

def _install_fake_vosk(payload: dict, seen: dict) -> None:
    fake = types.ModuleType("vosk")

    class Model:
        def __init__(self, path: str):
            seen["model_path"] = path

    class KaldiRecognizer:
        def __init__(self, model, sample_rate: int):
            self.rate = sample_rate
            self.words_on = False

        def SetWords(self, flag: bool):
            self.words_on = flag
            seen["set_words"] = flag

        def AcceptWaveform(self, data: bytes):
            seen["bytes"] = len(data)

        def FinalResult(self):
            return json.dumps(payload)

    fake.Model = Model
    fake.KaldiRecognizer = KaldiRecognizer
    sys.modules["vosk"] = fake


def test_v73_vosk_word_confidence():
    seen: dict = {}
    saved = sys.modules.get("vosk")
    pcm = b"\x01\x02" * 160  # 320 bytes of "audio"
    try:
        # a) words present -> the MEAN of the word confidences
        _install_fake_vosk({"text": "hello there",
                            "result": [{"conf": 0.9, "word": "hello"},
                                       {"conf": 0.8, "word": "there"}]}, seen)
        eng = __import__("app.services.speech_engines", fromlist=["x"]).make_vosk_engine("/tmp/fake-model")
        result = eng(pcm, 16000)
        assert result["transcript"] == "hello there"
        assert result["confidence"] == pytest.approx(0.85)
        assert seen["set_words"] is True
        # resampling happens for a non-16k stream (8000 -> 16000 doubles it)
        assert eng(pcm, 8000)["confidence"] == pytest.approx(0.85)
        assert seen["bytes"] == len(pcm) * 2

        # b) no words but a top-level confidence -> use it
        _install_fake_vosk({"text": "doc", "confidence": 0.7}, seen)
        eng2 = __import__("app.services.speech_engines", fromlist=["x"]).make_vosk_engine("/tmp/fake-model")
        assert eng2(pcm, 16000)["confidence"] == pytest.approx(0.7)

        # c) nothing reported -> the honest 0.0
        _install_fake_vosk({"text": "doc"}, seen)
        eng3 = __import__("app.services.speech_engines", fromlist=["x"]).make_vosk_engine("/tmp/fake-model")
        assert eng3(pcm, 16000)["confidence"] == 0.0

        # d) silence is not words: an empty transcript fails the contract
        _install_fake_vosk({"text": ""}, seen)
        eng4 = __import__("app.services.speech_engines", fromlist=["x"]).make_vosk_engine("/tmp/fake-model")
        from app.services.voice import VoiceError
        with pytest.raises(VoiceError):
            eng4(pcm, 16000)
    finally:
        if saved is not None:
            sys.modules["vosk"] = saved
        else:
            sys.modules.pop("vosk", None)


# ---------------------------------------------------------------------------
# 5) real model installs - the fully offline phone
# ---------------------------------------------------------------------------

VOSK_URL = "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"
WHISPER_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-tiny.en.bin"
VOICE_ONNX_URL = ("https://huggingface.co/rhasspy/piper-voices/resolve/main/"
                  "en/en_US/lessac/medium/en_US-lessac-medium.onnx")
VOICE_JSON_URL = VOICE_ONNX_URL + ".json"
PIPER_BIN_URL = ("https://github.com/rhasspy/piper/releases/download/"
                 "2023.11.14-2/piper_linux_x86_64.tar.gz")


def _fetcher(files: dict[str, bytes], seen: list):
    """The injectable fetcher: simulates a SUCCESSFUL download of the fixture
    bytes (the real _default_fetch owns min-size enforcement - the truncated
    case gets its own fetcher below)."""
    def _fetch(url, dest, *, min_bytes=0):
        seen.append(url)
        if url not in files:
            raise models_svc.SpeechModelError(f"fixture has no bytes for {url}")
        Path(dest).write_bytes(files[url])
        return len(files[url])
    return _fetch


def _vosk_zip_bytes() -> bytes:
    """The layout the OFFICIAL vosk zip ships: one top-level model folder."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("vosk-model-small-en-us-0.15/conf/model.conf", "sample-rate=16000\n")
        zf.writestr("vosk-model-small-en-us-0.15/conf/phones.conf", "kaldi\n")
        zf.writestr("vosk-model-small-en-us-0.15/am/final.mdl", b"kaldi-acoustic-model")
    return buf.getvalue()


def _piper_tarball_bytes() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        def add(name: str, data: bytes, mode: int):
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            ti.mode = mode
            tf.addfile(ti, io.BytesIO(data))
        add("piper/libonnxruntime.so", b"elf-lib", 0o644)
        add("piper/espeak-ng-data/dict", b"dict-bytes", 0o644)
        add("piper/piper", b"#!/bin/sh\n# stand-in elf\n", 0o755)
    return buf.getvalue()


def _fixtures() -> dict[str, bytes]:
    return {
        VOSK_URL: _vosk_zip_bytes(),
        # the REAL whisper.cpp container magic: uint32 0x67676d6c little-endian
        WHISPER_URL: b"lmgg" + b"\x00" * 64,
        VOICE_ONNX_URL: b"x" * 2048,
        VOICE_JSON_URL: json.dumps({"num_symbols": 102,
                                    "phoneme_id_map": {"_": [0]}}).encode(),
        PIPER_BIN_URL: _piper_tarball_bytes(),
    }


def test_v73_model_installer_installs_real_layouts(tmp_path):
    seen: list = []
    fetch = _fetcher(_fixtures(), seen)
    root = tmp_path / "models"

    r1 = models_svc.install_model("vosk-small-en-us", fetch=fetch, root=root)
    assert r1["engine"] == "vosk" and r1["kind"] == "zip"
    dest = root / "vosk-model-small-en-us-0.15"
    # the nested model folder is STRIPPED: am/ + conf/ land at the dest root
    assert (dest / "conf" / "model.conf").exists()
    assert (dest / "am" / "final.mdl").exists()
    assert not (dest / "vosk-model-small-en-us-0.15").exists()
    assert models_svc.slug_installed("vosk-small-en-us", root)

    r2 = models_svc.install_model("whisper-tiny-en", fetch=fetch, root=root)
    assert (root / "ggml-tiny.en.bin").read_bytes()[:4] == b"lmgg"
    assert models_svc.slug_installed("whisper-tiny-en", root)

    r3 = models_svc.install_model("piper-lessac-medium", fetch=fetch, root=root)
    assert (root / "en_US-lessac-medium.onnx").exists()
    assert (root / "en_US-lessac-medium.onnx.json").exists()

    r4 = models_svc.install_model("piper-binary-linux", fetch=fetch, root=root)
    link = root / "bin" / "piper"
    assert link.exists() and os.path.islink(link)
    real = Path(os.path.realpath(link))
    assert real.name == "piper" and real.exists()
    # the release layout survives WHOLE: libs + espeak-ng-data stay beside the binary
    assert (real.parent / "libonnxruntime.so").exists()
    assert (real.parent / "espeak-ng-data" / "dict").exists()
    assert models_svc.slug_installed("piper-binary-linux", root)

    # every artifact came from the catalog's EXACT url, no invented mirrors
    assert set(seen) == set(_fixtures().keys())

    # the catalog reflects the disk honestly
    cat = models_svc.catalog_out(root)
    installed = {m["slug"]: m["installed"] for m in cat["models"]}
    assert installed == {"piper-binary-linux": True, "piper-lessac-medium": True,
                         "vosk-small-en-us": True, "whisper-tiny-en": True}
    assert cat["models_root"] == str(root)


def test_v73_model_installer_fails_loud(tmp_path):
    root = tmp_path / "m"

    # not a zip at all
    fetch = _fetcher({VOSK_URL: b"this is not a zip"}, [])
    with pytest.raises(models_svc.SpeechModelError, match="not a valid zip"):
        models_svc.install_model("vosk-small-en-us", fetch=fetch, root=root)

    # a zip, but not a Kaldi model
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "hello")
    fetch = _fetcher({VOSK_URL: buf.getvalue()}, [])
    with pytest.raises(models_svc.SpeechModelError, match="Kaldi layout"):
        models_svc.install_model("vosk-small-en-us", fetch=fetch, root=root)

    # a zip escape attempt is refused
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../evil.txt", "x")
    fetch = _fetcher({VOSK_URL: buf.getvalue()}, [])
    with pytest.raises(models_svc.SpeechModelError, match="refusing unsafe"):
        models_svc.install_model("vosk-small-en-us", fetch=fetch, root=root)

    # truncated downloads are refused before anything lands
    fetch = _fetcher({VOSK_URL: b"tiny"}, [])
    fetch_honoring_min = models_svc.SpeechModelError
    def _trunc(url, dest, *, min_bytes=0):
        Path(dest).write_bytes(b"tiny")
        if min_bytes and len(b"tiny") < min_bytes:
            raise fetch_honoring_min("too small")
        return 4
    with pytest.raises(fetch_honoring_min):
        models_svc.install_model("vosk-small-en-us", fetch=_trunc, root=root)

    # wrong ggml magic
    fetch = _fetcher({WHISPER_URL: b"XXXX-not-a-model"}, [])
    with pytest.raises(models_svc.SpeechModelError, match="ggml magic"):
        models_svc.install_model("whisper-tiny-en", fetch=fetch, root=root)

    # a piper voice whose config does not parse
    fetch = _fetcher({VOICE_ONNX_URL: b"x" * 2048, VOICE_JSON_URL: b"{{{"}, [])
    with pytest.raises(models_svc.SpeechModelError, match="config"):
        models_svc.install_model("piper-lessac-medium", fetch=fetch, root=root)

    # a piper voice config without piper's phoneme fields
    fetch = _fetcher({VOICE_ONNX_URL: b"x" * 2048,
                      VOICE_JSON_URL: json.dumps({"hello": 1}).encode()}, [])
    with pytest.raises(models_svc.SpeechModelError, match="phoneme"):
        models_svc.install_model("piper-lessac-medium", fetch=fetch, root=root)

    # a tarball with no executable piper inside
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        ti = tarfile.TarInfo("piper/readme.txt")
        ti.size = 5
        tf.addfile(ti, io.BytesIO(b"hello"))
    fetch = _fetcher({PIPER_BIN_URL: buf.getvalue()}, [])
    with pytest.raises(models_svc.SpeechModelError, match="no executable 'piper'"):
        models_svc.install_model("piper-binary-linux", fetch=fetch, root=root)

    # unknown slug
    with pytest.raises(models_svc.SpeechModelError, match="catalog carries"):
        models_svc.install_model("gpt-5-voice", fetch=_fetcher({}, []), root=root)

    # a corrupted run left NOTHING behind (atomic .part discipline)
    assert not (root / "vosk-model-small-en-us-0.15").exists()
    assert not (root / "ggml-tiny.en.bin").exists()


def test_v73_models_endpoints_and_rebind(tmp_path, monkeypatch):
    # the API endpoint uses the DEFAULT root + fetcher - patch both to fixtures
    monkeypatch.setattr(models_svc, "models_root", lambda: tmp_path / "models")
    monkeypatch.setattr(models_svc, "_default_fetch", _fetcher(_fixtures(), []))

    async def _go():
        async with _client() as client:
            # the catalog surface + the offline-phone readiness block
            res = await client.get("/voice/speech/models")
            assert res.status_code == 200, res.text
            surface = res.json()
            assert [m["slug"] for m in surface["models"]] == [
                "piper-binary-linux", "piper-lessac-medium",
                "vosk-small-en-us", "whisper-tiny-en"]
            assert surface["inventory"]["asr"]["vosk"]["available"] in (True, False)
            assert surface["offline_phone"]["ready"] in (True, False)
            assert "note" in surface["offline_phone"]

            # an install through the REAL endpoint: downloads, verifies, rebinds
            res = await client.post("/voice/speech/models/install",
                                    json={"slug": "piper-lessac-medium"})
            assert res.status_code == 200, res.text
            out = res.json()
            assert out["install"]["slug"] == "piper-lessac-medium"
            assert len(out["install"]["installed_paths"]) == 2
            assert "bound" in out and "inventory" in out

            # the catalog now reports it installed (derived from disk)
            res = await client.get("/voice/speech/models")
            m = {x["slug"]: x for x in res.json()["models"]}
            assert m["piper-lessac-medium"]["installed"] is True
            assert m["vosk-small-en-us"]["installed"] is False

            # unknown slug -> honest 400
            res = await client.post("/voice/speech/models/install",
                                    json={"slug": "alexa"})
            assert res.status_code == 400 and "catalog carries" in res.json()["detail"]

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 6) version + contracts surface
# ---------------------------------------------------------------------------

def test_v73_contracts_surface():
    async def _go():
        async with _client() as client:
            res = await client.get("/voice/contracts")
            assert res.status_code == 200
            contracts = res.json()
            # the contracts surface still describes the v69 wire truth
            assert "asr" in contracts and "tts" in contracts
    _sync(_wrap(_go()))
