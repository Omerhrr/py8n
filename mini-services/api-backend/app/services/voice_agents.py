"""Voice agents (v71) - the builder object that composes the voice stack.

v69 built the voice PRIMITIVES (call state machine, barge-in, ASR/TTS
contracts); v70 built the TRANSPORT (websocket media streams, streaming
ASR, utterance segmentation). A VoiceAgent composes them into one
deployable persona - the object a user actually WANTS:

* a greeting, spoken the moment the call is answered (an interruptible
  tts.started utterance - the caller barge-ins over it exactly like any
  other);
* a speech configuration (ASR engine, TTS provider/voice/format,
  language, barge-in) that every session of the agent inherits - the
  media stream resolves its ASR engine and the turn loop resolves its
  TTS request from the agent config, with explicit per-call parameters
  still winning;
* a system prompt, injected into the handler envelope's metadata so AI
  handlers (ai_agent nodes) speak with the agent's voice;
* a handler workflow: the one bound at build time, or a SCAFFOLDED one
  (trigger -> voice-agent code template) so a fresh agent is runnable
  immediately - swap the code node for ai_agent + knowledge when the
  real brain exists.

An agent is CONFIGURATION, not state: sessions copy the relevant fields
into their context at creation, so editing an agent never rewrites
history on live calls (the honest separation the rest of py8n keeps).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import VoiceAgent, Workflow
from . import voice as voice_svc
from .interactions import _handler_name as _wf_name


class VoiceAgentError(ValueError):
    """Honest 4xx-grade agent failures."""


# The scaffolded handler template: a REAL runnable workflow (manual
# trigger -> code node) that answers with the envelope's text through the
# agent's persona line. The comment IS the builder instruction: swap the
# code node for ai_agent (+ knowledge tools) when the real brain exists.
_SCAFFOLD_CODE = (
    "# Voice agent handler (scaffolded by the py8n voice agent builder)\n"
    "# The envelope: input_data['payload'] = {text, conversation_id, channel,\n"
    "#   participant, history, metadata: {system_prompt, voice_agent_id, ...}}\n"
    "# Swap this code node for ai_agent (+ knowledge) when the real brain exists.\n"
    "env = input_data.get('payload', {})\n"
    "meta = env.get('metadata') or {}\n"
    "text = str(env.get('text', ''))\n"
    "system_prompt = str(meta.get('system_prompt', ''))\n"
    "reply = 'You said: ' + text\n"
    "if system_prompt:\n"
    "    reply = '[' + system_prompt.split('.')[0] + '] ' + reply\n"
    "result = {'text': reply}\n"
)


def _agent_ctx(agent: VoiceAgent) -> dict:
    """The config block copied into a session's context at creation."""
    return {
        "voice_agent_id": agent.id,
        "voice_agent_name": agent.name,
        "greeting_text": agent.greeting_text,
        "asr_provider": agent.asr_provider,
        "tts_provider": agent.tts_provider,
        "tts_voice": agent.tts_voice,
        "tts_format": agent.tts_format,
        "language": agent.language,
        "barge_in": bool(agent.barge_in),
        "system_prompt": agent.system_prompt,
    }


def agent_out(row: VoiceAgent, handler_name: str | None = None) -> dict:
    """The API shape: config + derived wiring guidance (nothing stored)."""
    from . import voice_transport as transport
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "greeting_text": row.greeting_text,
        "speech": {
            "asr_provider": row.asr_provider,
            "asr_engine_registered": row.asr_provider in transport.registered_asr_engines(),
            "tts_provider": row.tts_provider,
            "tts_voice": row.tts_voice,
            "tts_format": row.tts_format,
            "language": row.language,
            "barge_in": bool(row.barge_in),
        },
        "system_prompt": row.system_prompt,
        "handler_workflow_id": row.handler_workflow_id,
        "handler_workflow_name": handler_name,
        "handler_is_scaffold": bool((row.context or {}).get("scaffolded_handler")),
        "wiring": {
            "inbound_webhook": "point the provider's call-control webhook at "
                               "/api/v1/channels/telnyx/{endpoint_id}/webhook (or the twilio "
                               "status callback at /api/v1/voice/webhooks/twilio/{session_id})",
            "media_stream": "point the provider's audio fork/stream at "
                            "ws://<host>/api/v1/voice/sessions/{session_id}/media",
            "asr_note": (f"no ASR engine is registered for {row.asr_provider!r} in this process - "
                         "the transport will honestly report asr.unavailable until one binds"
                         if row.asr_provider not in transport.registered_asr_engines()
                         else f"engine {row.asr_provider!r} is live in this process"),
        },
        "context": row.context or {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def _load(db: AsyncSession, agent_id: str, owner_id: str | None) -> VoiceAgent:
    row = await db.get(VoiceAgent, agent_id)
    if row is None:
        raise VoiceAgentError(f"voice agent {agent_id!r} not found")
    if owner_id is not None and row.owner_id is not None and row.owner_id != owner_id:
        raise VoiceAgentError(f"voice agent {agent_id!r} not found")
    return row


def _validate_speech(*, asr_provider: str, tts_provider: str, tts_format: str,
                     language: str) -> None:
    if asr_provider not in voice_svc.ASR_PROVIDERS:
        raise VoiceAgentError(f"unknown asr provider {asr_provider!r} - known: "
                              f"{', '.join(sorted(voice_svc.ASR_PROVIDERS))}")
    if tts_provider not in voice_svc.TTS_PROVIDERS:
        raise VoiceAgentError(f"unknown tts provider {tts_provider!r} - known: "
                              f"{', '.join(sorted(voice_svc.TTS_PROVIDERS))}")
    if tts_format not in ("wav", "mp3", "opus", "mulaw"):
        raise VoiceAgentError(f"tts format must be wav|mp3|opus|mulaw, got {tts_format!r}")
    if len(language) > 20:
        raise VoiceAgentError("language must be a bcp-47 tag (max 20 chars)")


async def _scaffold_handler(db: AsyncSession, owner_id: str | None,
                            agent_name: str) -> Workflow:
    """A REAL, runnable handler workflow: trigger -> voice-agent code node."""
    wf = Workflow(
        name=f"{agent_name} - voice handler",
        description=f"Scaffolded by the voice agent builder for {agent_name!r} - "
                    "swap the code node for ai_agent + knowledge when ready",
        graph={
            "nodes": [
                {"id": "t", "type": "manual_trigger", "name": "Trigger",
                 "position": {"x": 0, "y": 0}, "parameters": {}},
                {"id": "reply", "type": "code", "name": "Voice reply",
                 "position": {"x": 200, "y": 0}, "parameters": {"code": _SCAFFOLD_CODE}},
            ],
            "edges": [{"id": "e1", "source": "t", "target": "reply",
                       "sourceHandle": "main", "targetHandle": "main"}],
        },
        is_active=False,  # handlers run through execute_workflow directly, like every channel
        tags=["voice-agent", "scaffold"],
    )
    wf.owner_id = owner_id
    db.add(wf)
    await db.flush()
    return wf


async def create_agent(db: AsyncSession, *, owner_id: str | None, name: str,
                       description: str = "", greeting_text: str = "",
                       asr_provider: str = "py8n_local", tts_provider: str = "openai_tts",
                       tts_voice: str = "alloy", tts_format: str = "wav",
                       language: str = "en-US", barge_in: bool = True,
                       system_prompt: str = "",
                       handler_workflow_id: str | None = None,
                       scaffold_handler: bool = False) -> dict:
    """Create an agent; scaffold a runnable handler when none is bound."""
    if not name or not name.strip():
        raise VoiceAgentError("an agent name is required")
    _validate_speech(asr_provider=asr_provider, tts_provider=tts_provider,
                     tts_format=tts_format, language=language)
    if handler_workflow_id:
        wf = await db.get(Workflow, handler_workflow_id)
        if wf is None or (owner_id is not None and wf.owner_id is not None
                          and wf.owner_id != owner_id):
            raise VoiceAgentError(f"handler workflow {handler_workflow_id!r} not found")
        scaffolded = False
    elif scaffold_handler:
        wf = await _scaffold_handler(db, owner_id, name.strip()[:100])
        handler_workflow_id = wf.id
        scaffolded = True
    else:
        scaffolded = False

    row = VoiceAgent(
        name=name.strip()[:140], description=description,
        greeting_text=greeting_text, asr_provider=asr_provider,
        tts_provider=tts_provider, tts_voice=tts_voice, tts_format=tts_format,
        language=language, barge_in=bool(barge_in), system_prompt=system_prompt,
        handler_workflow_id=handler_workflow_id,
        context={"scaffolded_handler": scaffolded},
    )
    row.owner_id = owner_id
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return agent_out(row, await _wf_name(db, row.handler_workflow_id))


async def list_agents(db: AsyncSession, owner_id: str | None) -> list[dict]:
    q = select(VoiceAgent).order_by(VoiceAgent.created_at.desc())
    rows = (await db.execute(q)).scalars().all()
    if owner_id is not None:
        rows = [r for r in rows if r.owner_id is None or r.owner_id == owner_id]
    return [agent_out(r, await _wf_name(db, r.handler_workflow_id)) for r in rows]


async def get_agent(db: AsyncSession, agent_id: str, owner_id: str | None) -> dict:
    row = await _load(db, agent_id, owner_id)
    return agent_out(row, await _wf_name(db, row.handler_workflow_id))


async def update_agent(db: AsyncSession, agent_id: str, owner_id: str | None, **fields) -> dict:
    row = await _load(db, agent_id, owner_id)
    speech_keys = ("asr_provider", "tts_provider", "tts_format")
    if any(fields.get(k) is not None for k in speech_keys):
        _validate_speech(
            asr_provider=fields.get("asr_provider") or row.asr_provider,
            tts_provider=fields.get("tts_provider") or row.tts_provider,
            tts_format=fields.get("tts_format") or row.tts_format,
            language=fields.get("language") or row.language)
    for key in ("name", "description", "greeting_text", "asr_provider", "tts_provider",
                "tts_voice", "tts_format", "language", "system_prompt"):
        if fields.get(key) is not None:
            setattr(row, key, fields[key])
    if fields.get("barge_in") is not None:
        row.barge_in = bool(fields["barge_in"])
    if "handler_workflow_id" in fields:
        hwid = fields["handler_workflow_id"]
        if hwid:
            wf = await db.get(Workflow, hwid)
            if wf is None or (owner_id is not None and wf.owner_id is not None
                              and wf.owner_id != owner_id):
                raise VoiceAgentError(f"handler workflow {hwid!r} not found")
        row.handler_workflow_id = hwid or None
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return agent_out(row, await _wf_name(db, row.handler_workflow_id))


async def delete_agent(db: AsyncSession, agent_id: str, owner_id: str | None) -> dict:
    row = await _load(db, agent_id, owner_id)
    payload = {"id": row.id, "name": row.name,
               "note": "voice agent removed - sessions keep the config they copied at creation"}
    await db.delete(row)
    await db.flush()
    return payload


# ---------------------------------------------------------------------------
# Session integration - the agent's config flows into every call
# ---------------------------------------------------------------------------


async def bind_to_session(db: AsyncSession, session: "object", agent_id: str,
                          owner_id: str | None) -> dict | None:
    """Copy the agent's config into a new session's context (create-time).

    Returns the agent's handler workflow id when the session has none -
    voice.create_session uses it as the effective handler. A missing or
    foreign agent is an honest 404-grade error.
    """
    row = await _load(db, agent_id, owner_id)
    ctx = dict(session.context or {})
    ctx["voice_agent"] = _agent_ctx(row)
    session.context = ctx
    db.add(session)
    return row.handler_workflow_id


def session_agent(session: "object") -> dict | None:
    """The agent config block a session carries (or None)."""
    return (session.context or {}).get("voice_agent") or None


async def on_answered(db: AsyncSession, session: "object") -> dict | None:
    """The greeting: spoken the moment the call is answered.

    Builds the TTS request from the agent's OWN configuration (provider,
    voice, format, barge-in), records the interruptible tts.started
    utterance and points active_tts at it - the caller barge-ins over
    the greeting exactly like over any turn reply. No agent, no
    greeting, or an already-playing utterance -> None (honest nothing).
    """
    agent = session_agent(session)
    if not agent or not str(agent.get("greeting_text") or "").strip():
        return None
    if session.state == "ended" or (session.context or {}).get("active_tts"):
        return None
    tts_request = voice_svc.build_tts_request(
        agent["greeting_text"], provider=agent.get("tts_provider") or "openai_tts",
        voice=agent.get("tts_voice") or "alloy", fmt=agent.get("tts_format") or "wav",
        barge_in_ok=bool(agent.get("barge_in", True)))
    tts_event = await voice_svc._add_event(db, session, "tts.started", {
        "text": agent["greeting_text"][:500], "provider": tts_request["provider"],
        "voice": tts_request["voice"], "barge_in_ok": tts_request["barge_in_ok"],
        "source": "greeting",
    })
    ctx = dict(session.context or {})
    ctx["active_tts"] = tts_event.id
    session.context = ctx
    db.add(session)
    await db.flush()
    tts_request["tts_id"] = tts_event.id
    return tts_request


def resolve_turn_tts(session: "object", tts_provider: str | None = None,
                     voice: str | None = None, tts_format: str | None = None) -> tuple[str, str, str]:
    """The turn's TTS config: explicit parameter -> agent config -> default."""
    agent = session_agent(session) or {}
    return (
        tts_provider or agent.get("tts_provider") or "openai_tts",
        voice or agent.get("tts_voice") or "alloy",
        tts_format or agent.get("tts_format") or "wav",
    )


def resolve_asr_engine_name(session: "object", custom_parameters: dict | None = None) -> str:
    """Which ASR engine the media stream consults, in priority order:
    the stream's own customParameters.asr_engine -> the agent's
    asr_provider -> py8n_local."""
    explicit = str((custom_parameters or {}).get("asr_engine") or "")
    if explicit:
        return explicit
    agent = session_agent(session) or {}
    return str(agent.get("asr_provider") or "py8n_local")
