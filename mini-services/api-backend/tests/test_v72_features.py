"""V72 feature tests: live speech engine bridges (whisper.cpp / vosk /
piper in-process), knowledge binding on voice agents (dataset-backed
answers over the phone), and the Voice Agent marketplace solution.

- speech engines: the linear16 resampler is exact, the whisper.cpp and
  piper BRIDGE MECHANICS are exercised end-to-end with stand-in binaries
  (real temp-wav + subprocess + stdout/wav parsing through the v69
  contracts - fail-loud on exit codes, empty transcripts, wrong formats),
  the TTS registry mirrors the v70 ASR registry, and the machine
  inventory is HONEST (absent bridges report remediation, never fake
  availability).
- knowledge binding: a voice agent binds a dataset; every turn is scored
  against it (deterministic idf-weighted overlap) and the matches ride
  the handler envelope's metadata.knowledge - the phone answers from
  YOUR data. Broken bindings degrade honestly (knowledge_error recorded,
  the call survives), unknown datasets/columns fail loud, foreign
  datasets are not found.
- the marketplace solution: installing voice-agent-system WITH
  as_voice_agent creates the FAQ dataset + knowledge-grounded handler +
  a Voice Agent bound to both - a session rings, answers, and a turn
  quotes the dataset back over the phone, fully offline.

Runs the FastAPI app in-process (httpx ASGITransport).
"""

from __future__ import annotations

import asyncio
import base64
import os
import stat
import struct
import textwrap
import uuid

import httpx
import pytest

from app.main import app
from app.services import executor as executor_mod
from app.services import speech_engines as engines
from app.services import voice as voice_svc
from app.services import voice_transport as transport
from app.services.knowledge import score_match, tokenize

API = "http://testserver/api/v1"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


async def _drain_background() -> None:
    tasks = [t for t in executor_mod._background_tasks if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _sync(coro):
    return asyncio.run(coro)


async def _mk_user(client: httpx.AsyncClient, tag: str, n: int) -> dict:
    res = await client.post("/auth/register", json={
        "email": f"v72-{tag}-u{n}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v72 u{n} {tag}",
    })
    assert res.status_code == 201, res.text
    body = res.json()
    return {"token": body["token"], "id": body["user"]["id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _mk_faq(client: httpx.AsyncClient, headers: dict, name: str) -> str:
    res = await client.post("/datasets", headers=headers, json={
        "name": name, "description": "v72 FAQ",
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


_KNOWLEDGE_HANDLER_CODE = (
    "env = input_data.get('payload', {})\n"
    "meta = env.get('metadata') or {}\n"
    "matches = meta.get('knowledge') or []\n"
    "if matches:\n"
    "    reply = str(matches[0].get('answer') or '')\n"
    "else:\n"
    "    reply = 'no knowledge matched'\n"
    "result = {'text': reply}\n"
)


async def _mk_knowledge_handler(client: httpx.AsyncClient, headers: dict, name: str) -> str:
    res = await client.post("/workflows", headers=headers, json={
        "name": name, "graph": {
            "nodes": [
                {"id": "t", "type": "manual_trigger", "name": "Trigger",
                 "position": {"x": 0, "y": 0}, "parameters": {}},
                {"id": "reply", "type": "code", "name": "Answer",
                 "position": {"x": 200, "y": 0}, "parameters": {"code": _KNOWLEDGE_HANDLER_CODE}},
            ],
            "edges": [{"id": "e1", "source": "t", "target": "reply",
                       "sourceHandle": "main", "targetHandle": "main"}],
        }})
    assert res.status_code == 201, res.text
    return res.json()["id"]


# ---------------------------------------------------------------------------
# 1) speech engines: resampler + bridges + TTS registry + honest inventory
# ---------------------------------------------------------------------------

def test_v72_resampler_and_wav():
    # same rate = exact passthrough
    pcm = struct.pack("<4h", 100, -200, 300, -400)
    assert engines.resample_linear16(pcm, 8000, 8000) == pcm
    # doubling the rate doubles the sample count (endpoints preserved)
    up = engines.resample_linear16(pcm, 8000, 16000)
    assert len(up) == 2 * len(pcm)
    ups = struct.unpack(f"<{len(up)//2}h", up)
    assert ups[0] == 100 and ups[-1] == -400
    # halving keeps the first sample
    down = engines.resample_linear16(pcm, 16000, 8000)
    downs = struct.unpack(f"<{len(down)//2}h", down)
    assert downs[0] == 100
    # constant input stays constant at any rate
    const = struct.pack("<8h", *([500] * 8))
    assert struct.unpack("<16h", engines.resample_linear16(const, 8000, 16000)) == tuple([500] * 16)
    # empty + invalid inputs behave
    assert engines.resample_linear16(b"", 8000, 16000) == b""
    with pytest.raises(ValueError):
        engines.resample_linear16(pcm, 0, 16000)
    # wav wrapper produces a parseable RIFF container
    wav = engines.wav_bytes(pcm, 8000)
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
    assert engines.wav_duration_ms(wav) == 0  # 4 samples @ 8kHz = 0.5ms (int-rounded)
    assert engines.wav_duration_ms(engines.wav_bytes(b"\x00\x01" * 8000, 8000)) == 1000
    assert engines.wav_duration_ms(b"not a wav") == 0


def _write_script(path, body: str) -> str:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def test_v72_whispercpp_bridge_mechanics(tmp_path):
    """The REAL bridge path - temp wav in, subprocess call, stdout parsed,
    validated through the v69 contract - with a stand-in whisper-cli."""
    fake = _write_script(tmp_path / "whisper-cli",
                         "#!/bin/sh\n# consumes args, prints a transcript\necho 'hello from whisper'\n")
    engine = engines.make_whispercpp_engine(fake, str(tmp_path / "ggml-model.bin"))
    pcm = struct.pack("<16h", *([120] * 16))
    out = engine(pcm, 16000)
    assert out["transcript"] == "hello from whisper"
    assert out["is_final"] is True and out["confidence"] == 0.0  # honest default

    # 8 kHz input is resampled to the 16 kHz whisper.cpp expects (via the
    # wav the bridge writes - the stand-in can't verify the rate, but the
    # call must still succeed end to end)
    out8 = engine(pcm, 8000)
    assert out8["transcript"] == "hello from whisper"

    # a failing binary surfaces stderr through the contract (fail loud)
    bad = _write_script(tmp_path / "whisper-bad",
                        '#!/bin/sh\necho "model missing" >&2\nexit 3\n')
    with pytest.raises(voice_svc.VoiceError) as exc:
        engines.make_whispercpp_engine(bad, "m")(pcm, 16000)
    assert "exit 3" in str(exc.value) and "model missing" in str(exc.value)

    # an empty transcript is NOT words - the contract refuses it
    silent = _write_script(tmp_path / "whisper-silent", "#!/bin/sh\necho ''\n")
    with pytest.raises(voice_svc.VoiceError):
        engines.make_whispercpp_engine(silent, "m")(pcm, 16000)

    # no temp wav files leak
    leftovers = [p for p in os.listdir("/tmp") if p.startswith("py8n-asr-")]
    assert leftovers == []


def test_v72_piper_bridge_and_tts_registry(tmp_path):
    """piper: text in -> wav bytes out (stand-in binary writes a real wav);
    the TTS registry mirrors the ASR registry; synthesize() returns the
    contract result."""
    # a stand-in piper: writes a minimal valid wav to --output_file's value
    fake = _write_script(tmp_path / "piper", textwrap.dedent("""
        #!/usr/bin/env python3
        import sys, wave
        args = sys.argv[1:]
        out = args[args.index("--output_file") + 1]
        stdin = sys.stdin.buffer.read()
        with wave.open(out, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050)
            w.writeframes(b"\\x00\\x01" * 22050)  # 1 second
    """).strip())
    voice_path = str(tmp_path / "en_US-amy-medium.onnx")
    open(voice_path, "w").close()
    engine = engines.make_piper_engine(fake, {"en_US-amy-medium": voice_path},
                                       default_voice="en_US-amy-medium")

    audio = engine("hello there", "", "wav")
    assert audio[:4] == b"RIFF" and len(audio) > 100
    # piper is wav-only - anything else fails loud instead of lying
    with pytest.raises(voice_svc.VoiceError) as exc:
        engine("hello", "", "mp3")
    assert "wav" in str(exc.value)
    with pytest.raises(voice_svc.VoiceError):
        engine("   ", "", "wav")
    with pytest.raises(voice_svc.VoiceError) as exc:
        engine("hello", "no-such-voice", "wav")
    assert "not on disk" in str(exc.value)

    # the registry: register / duplicate replace / unregister
    assert "piper_local" not in engines.registered_tts_engines()
    engines.register_tts_engine("piper_local", engine)
    engines.register_tts_engine("piper_local", engine)  # rebind is allowed
    assert engines.registered_tts_engines() == ["piper_local"]
    with pytest.raises(ValueError):
        engines.register_tts_engine("bogus", "not-callable")
    try:
        result = engines.synthesize("piper_local", "hello there")
        assert result["format"] == "wav" and result["audio_ref"] == ""
        decoded = base64.b64decode(result["audio_b64"])
        assert decoded == audio
        assert result["duration_estimate_ms"] == pytest.approx(1000, abs=50)
        with pytest.raises(voice_svc.VoiceError) as exc:
            engines.synthesize("nope", "hello")
        assert "no TTS engine is registered for 'nope'" in str(exc.value)
    finally:
        assert engines.unregister_tts_engine("piper_local") is True
        assert engines.unregister_tts_engine("piper_local") is False


def test_v72_speech_inventory_is_honest(monkeypatch, tmp_path):
    """Absent bridges report remediation, never fake availability; binding
    binds nothing when nothing can run (the devices.py pattern)."""
    monkeypatch.delenv("PY8N_VOSK_MODEL", raising=False)
    monkeypatch.delenv("PY8N_WHISPER_CPP_BIN", raising=False)
    monkeypatch.delenv("PY8N_PIPER_BIN", raising=False)
    monkeypatch.delenv("PY8N_PIPER_VOICE", raising=False)
    monkeypatch.setattr(engines, "models_root", lambda: tmp_path)
    inv = engines.speech_inventory()
    # in this sandbox vosk is not installed - the note must say how to fix it
    if not inv["asr"]["vosk"]["available"]:
        assert "pip install vosk" in inv["asr"]["vosk"]["note"]
    if not inv["asr"]["whisper.cpp"]["available"]:
        assert "whisper-cli" in inv["asr"]["whisper.cpp"]["note"]
    if not inv["tts"]["piper"]["available"]:
        assert "piper" in inv["tts"]["piper"]["note"]
    # binding is best-effort and honest about what happened
    bound = engines.bind_local_engines()
    assert bound["asr"] is None or bound["asr"]["name"] == "py8n_local"
    assert bound["tts"] is None or bound["tts"]["name"] == "piper_local"


def test_v72_speech_engines_endpoint():
    async def _go():
        async with _client() as client:
            res = await client.get("/voice/speech/engines")
            assert res.status_code == 200, res.text
            inv = res.json()
            assert set(inv["asr"]) >= {"vosk", "whisper.cpp", "preferred_backend",
                                       "local_engine_registered"}
            assert "piper" in inv["tts"]
            assert isinstance(inv["registered"]["asr"], list)
            assert "nothing is faked" in inv["note"]
            # the synthesize endpoint refuses unregistered engines with 409
            res = await client.post("/voice/tts/synthesize", json={
                "text": "hello", "provider": "piper_local"})
            assert res.status_code == 409, res.text
            assert "no TTS engine is registered" in res.json()["detail"]

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 2) knowledge: deterministic retrieval + agent binding + search preview
# ---------------------------------------------------------------------------

def test_v72_knowledge_scoring():
    # question words are stopworded: only the CONTENT terms survive
    assert tokenize("What are YOUR hours?") == ["hours"] == tokenize("what are hours")
    # stopword-free overlap scores; nothing overlapping scores zero
    q = tokenize("opening hours")
    assert score_match(q, tokenize("What are your opening hours")) > 0
    assert score_match(q, tokenize("Where are you located")) == 0.0
    assert score_match([], tokenize("anything")) == 0.0


def test_v72_knowledge_retrieval_and_binding():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"{tag}b", 1)
            stranger = await _mk_user(client, f"{tag}b", 2)
            h = _auth(user["token"])
            ds_id = await _mk_faq(client, h, f"faq-{tag}b")
            handler_id = await _mk_knowledge_handler(client, h, f"kb-handler-{tag}b")

            # ---- bind knowledge at create: validation + resolution ----------
            res = await client.post("/voice/agents", headers=h, json={
                "name": f"FAQ agent {tag}", "greeting_text": "Hi, ask me anything.",
                "handler_workflow_id": handler_id,
                "knowledge_dataset_id": ds_id,
                "knowledge_text_column": "question", "knowledge_answer_column": "answer",
                "knowledge_top_k": 2})
            assert res.status_code == 201, res.text
            agent = res.json()
            assert agent["knowledge"]["dataset_id"] == ds_id
            assert agent["knowledge"]["dataset_name"] == f"faq-{tag}b"
            assert agent["knowledge"]["top_k"] == 2
            assert "knowledge_note" in agent["wiring"]

            # unknown dataset / column / foreign dataset fail loud
            res = await client.post("/voice/agents", headers=h, json={
                "name": "x", "knowledge_dataset_id": "nope"})
            assert res.status_code == 400 and "not found" in res.json()["detail"]
            res = await client.post("/voice/agents", headers=h, json={
                "name": "x", "knowledge_dataset_id": ds_id,
                "knowledge_text_column": "nope"})
            assert res.status_code == 400 and "no column" in res.json()["detail"]
            s_h = _auth(stranger["token"])
            res = await client.post("/voice/agents", headers=s_h, json={
                "name": "x", "knowledge_dataset_id": ds_id})
            assert res.status_code == 400 and "not found" in res.json()["detail"]
            # top_k bounds ride the schema
            res = await client.post("/voice/agents", headers=h, json={
                "name": "x", "knowledge_dataset_id": ds_id, "knowledge_top_k": 9})
            assert res.status_code == 422

            # ---- the search preview: what a turn would be grounded on -------
            res = await client.post(f"/voice/agents/{agent['id']}/knowledge/search",
                                    headers=h, json={"query": "tell me the opening hours"})
            assert res.status_code == 200, res.text
            out = res.json()
            assert out["dataset"] == f"faq-{tag}b" and out["searched"] == 3
            assert out["matches"][0]["question"] == "What are your opening hours"
            assert "nine AM" in out["matches"][0]["answer"] and out["matches"][0]["score"] > 0
            # two matches with top_k=2, ranked
            res = await client.post(f"/voice/agents/{agent['id']}/knowledge/search",
                                    headers=h, json={"query": "return a product", "top_k": 2})
            ranks = [m["question"] for m in res.json()["matches"]]
            assert ranks[0] == "How do I return a product"
            # nothing matches -> honest empty
            res = await client.post(f"/voice/agents/{agent['id']}/knowledge/search",
                                    headers=h, json={"query": "what is the moon made of"})
            assert res.json()["matches"] == []
            # blank (whitespace-only) query refused loudly by the service
            res = await client.post(f"/voice/agents/{agent['id']}/knowledge/search",
                                    headers=h, json={"query": "  "})
            assert res.status_code == 400 and "query is required" in res.json()["detail"]
            # agent WITHOUT a binding: 409 with remediation
            res = await client.post("/voice/agents", headers=h, json={"name": "plain"})
            plain = res.json()
            res = await client.post(f"/voice/agents/{plain['id']}/knowledge/search",
                                    headers=h, json={"query": "hours"})
            assert res.status_code == 409 and "no knowledge dataset bound" in res.json()["detail"]
            # unknown agent 404
            res = await client.post("/voice/agents/nope/knowledge/search",
                                    headers=h, json={"query": "hours"})
            assert res.status_code == 404

            # ---- sessions inherit the binding (config copied at creation) ---
            res = await client.post("/voice/sessions", headers=h, json={
                "direction": "inbound", "provider": "telnyx", "call_ref": f"CA{tag}b",
                "from_ref": "+15550001", "to_ref": "+15550002",
                "agent_id": agent["id"]})
            assert res.status_code == 201, res.text
            sess = res.json()
            assert sess["agent"]["knowledge"]["dataset_id"] == ds_id
            assert sess["agent"]["knowledge"]["text_column"] == "question"

            # ---- rewire + clear through PUT ---------------------------------
            ds2 = await _mk_faq(client, h, f"faq2-{tag}b")
            res = await client.put(f"/voice/agents/{agent['id']}", headers=h,
                                   json={"knowledge_dataset_id": ds2,
                                         "knowledge_text_column": "question"})
            assert res.json()["knowledge"]["dataset_id"] == ds2
            res = await client.put(f"/voice/agents/{agent['id']}", headers=h,
                                   json={"knowledge_dataset_id": ""})
            assert res.json()["knowledge"] is None
            # re-bind for the turn test below
            res = await client.put(f"/voice/agents/{agent['id']}", headers=h,
                                   json={"knowledge_dataset_id": ds_id,
                                         "knowledge_text_column": "question",
                                         "knowledge_answer_column": "answer"})
            assert res.json()["knowledge"]["dataset_id"] == ds_id
            return agent, sess, h, ds_id

    agent, sess, h, ds_id = _sync(_wrap(_go()))

    # ---- 3) THE FLAGSHIP: a turn grounded on the dataset ----------------
    async def _go3():
        async with _client() as client:
            await client.post(f"/voice/sessions/{sess['id']}/events", headers=h,
                              json={"kind": "call.ringing"})
            res = await client.post(f"/voice/sessions/{sess['id']}/events", headers=h,
                                    json={"kind": "call.answered"})
            assert res.status_code == 200, res.text
            res = await client.post(f"/voice/sessions/{sess['id']}/turn", headers=h,
                                    json={"transcript": "What are your opening hours?"})
            assert res.status_code == 200, res.text
            turn = res.json()
            assert turn["knowledge"], "the turn must carry its grounding"
            assert turn["knowledge"][0]["question"] == "What are your opening hours"
            assert turn["reply"] == ("We are open Monday to Friday from nine AM to six PM.")
            assert turn["knowledge_error"] is None
            # an unmatched question gets the handler's honest fallback
            await client.post(f"/voice/sessions/{sess['id']}/tts/complete", headers=h)
            res = await client.post(f"/voice/sessions/{sess['id']}/turn", headers=h,
                                    json={"transcript": "what is the moon made of"})
            t2 = res.json()
            assert t2["knowledge"] is None  # empty list -> None in the response
            assert t2["reply"] == "no knowledge matched"
            assert t2["knowledge_error"] is None

    _sync(_wrap(_go3()))

    # ---- 4) a broken binding degrades HONESTLY (the call survives) ------
    async def _go4():
        async with _client() as client:
            res = await client.delete(f"/datasets/{ds_id}", headers=h)
            assert res.status_code in (200, 204), res.status_code
            res = await client.post(f"/voice/sessions/{sess['id']}/tts/complete", headers=h)
            assert res.status_code in (200, 400)
            res = await client.post(f"/voice/sessions/{sess['id']}/turn", headers=h,
                                    json={"transcript": "What are your hours"})
            assert res.status_code == 200, res.text
            turn = res.json()
            assert turn["knowledge"] is None
            assert turn["knowledge_error"] and "not found" in turn["knowledge_error"]
            assert turn["reply"] == "no knowledge matched"  # handler ran ungrounded

    _sync(_wrap(_go4()))


# ---------------------------------------------------------------------------
# 5) the Voice Agent marketplace solution: one click -> a full phone agent
# ---------------------------------------------------------------------------

def test_v72_voice_agent_solution():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, tag, 1)
            h = _auth(user["token"])

            # the shelf: voice-agent-system with the voice_agent_ready badge
            res = await client.get("/solutions")
            assert res.status_code == 200, res.text
            slugs = {s["slug"]: s for s in res.json()["solutions"]}
            assert "voice-agent-system" in slugs
            shelf = slugs["voice-agent-system"]
            assert shelf["voice_agent_ready"] is True and shelf["category"] == "Voice"
            assert slugs["invoice-processing"]["voice_agent_ready"] is False
            res = await client.get("/solutions/voice-agent-system")
            detail = res.json()
            assert detail["pack"]["datasets"][0]["rows"] == 10
            assert "voice agent" in detail["docs"].lower()

            # install WITHOUT the flag: pack objects only, no voice agent
            res = await client.post("/solutions/invoice-processing/install",
                                    headers=h, json={"as_voice_agent": True})
            assert res.status_code == 400, res.text
            assert "does not declare a voice agent" in res.json()["detail"]

            res = await client.post("/solutions/voice-agent-system/install",
                                    headers=h, json={})
            assert res.status_code == 200, res.text
            assert res.json()["voice_agent"] is None

            # install AS A VOICE AGENT: dataset + handler + agent, wired
            res = await client.post("/solutions/voice-agent-system/install",
                                    headers=h, json={"as_voice_agent": True})
            assert res.status_code == 200, res.text
            body = res.json()
            va = body["voice_agent"]
            assert va["name"] == "Voice Agent System phone agent"
            assert va["knowledge"]["dataset_name"].startswith("voice_agent_faq")
            assert va["knowledge"]["text_column"] == "question"
            assert va["knowledge"]["answer_column"] == "answer"
            assert body["created_workflows"][0]["name"] == "Voice Agent Handler"

            # the installed agent detail resolves the dataset name + greeting
            res = await client.get(f"/voice/agents/{va['id']}", headers=h)
            assert res.status_code == 200, res.text
            agent = res.json()
            assert agent["greeting_text"].startswith("Hello, and thanks for calling")
            assert agent["speech"]["asr_provider"] == "py8n_local"
            assert agent["handler_is_scaffold"] is False

            # the knowledge preview works straight after install
            res = await client.post(f"/voice/agents/{va['id']}/knowledge/search",
                                    headers=h, json={"query": "where are you located"})
            assert res.status_code == 200
            assert "Market Street" in res.json()["matches"][0]["answer"]

            # THE PHONE EXPERIENCE: ring, answer, ask, get the dataset answer
            res = await client.post("/voice/sessions", headers=h, json={
                "direction": "inbound", "provider": "telnyx", "call_ref": f"CA-sol-{tag}",
                "from_ref": "+15550007", "to_ref": "+15550008", "agent_id": va["id"]})
            assert res.status_code == 201, res.text
            sess = res.json()
            assert sess["agent"]["knowledge"]["dataset_id"]
            await client.post(f"/voice/sessions/{sess['id']}/events", headers=h,
                              json={"kind": "call.ringing"})
            res = await client.post(f"/voice/sessions/{sess['id']}/events", headers=h,
                                    json={"kind": "call.answered"})
            g = res.json()["greeting_tts"]
            assert g["text"].startswith("Hello, and thanks for calling")
            res = await client.post(f"/voice/sessions/{sess['id']}/barge-in", headers=h)
            assert res.status_code == 200
            res = await client.post(f"/voice/sessions/{sess['id']}/turn", headers=h,
                                    json={"transcript": "How long does shipping take?"})
            assert res.status_code == 200, res.text
            turn = res.json()
            assert turn["reply"] == ("Standard shipping takes three to five business days. "
                                     "Express shipping arrives the next business day.")
            assert turn["knowledge"][0]["question"] == "How long does shipping take"
            assert turn["tts"]["provider"] == "openai_tts"

            # the shipped handler also RUNS standalone offline
            wf_id = body["created_workflows"][0]["id"]
            res = await client.post(f"/workflows/{wf_id}/run", headers=h, json={"payload": {
                "text": "what are your hours",
                "metadata": {"knowledge": [{"question": "What are your opening hours",
                                            "answer": "Nine to six, Monday to Friday.",
                                            "score": 0.9}]}}})
            assert res.status_code == 200, res.text
            ex = res.json()["execution_id"]
            for _ in range(200):
                det = (await client.get(f"/executions/{ex}", headers=h)).json()
                if det["status"] not in ("running", "queued"):
                    break
                await asyncio.sleep(0.05)
            assert det["status"] == "success", det.get("error")
            reply = det["node_runs"][-1]["output"]
            reply_text = reply.get("result", reply)["text"] if isinstance(reply, dict) else str(reply)
            assert reply_text == "Nine to six, Monday to Friday."
            return None

    _sync(_wrap(_go()))


async def _wrap(coro):
    try:
        return await coro
    finally:
        await _drain_background()


# ---------------------------------------------------------------------------
# 6) version + contracts surface
# ---------------------------------------------------------------------------

def test_v72_version_and_contracts():
    async def _go():
        async with _client() as client:
            res = await client.get("/health")
            assert res.json()["version"] == "1.72.0"
            res = await client.get("/voice/contracts")
            assert res.status_code == 200
            contracts = res.json()
            # the contract text already named the bridges - v72 makes them real
            assert "whisper.cpp" in contracts["asr"]["providers"]["py8n_local"]["request"]

    _sync(_wrap(_go()))
