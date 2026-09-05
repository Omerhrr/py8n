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


BRAINS = ("scaffold", "ai_agent")
BRAIN_PROVIDERS = ("sandbox_bridge", "openai_compatible")

# v73: the LLM-brain scaffold. The ai_agent node's params are JINJA
# TEMPLATES resolved against the live execution context at every turn -
# the trigger node's output IS the template context (input.payload = the
# handler envelope), so the caller's words AND the knowledge matches
# retrieved from the SAME binding the deterministic handler reads ride
# straight into the model's user message. One binding, two brains.
_AI_BRAIN_USER_TMPL = (
    "Phone call turn. The caller said: {{ input.payload.text }}\n\n"
    "Grounded knowledge matches from the company dataset (a JSON list; "
    "answer ONLY from these when one fits):\n"
    "{{ (input.payload.metadata.get('knowledge') or []) | tojson }}\n\n"
    "Knowledge service status this turn: "
    "{{ input.payload.metadata.get('knowledge_error') or 'ok' }}.\n"
    "Speak the answer aloud: plain short sentences, no markdown, no lists."
)
_AI_BRAIN_SYSTEM_TMPL = (
    "{{ input.payload.metadata.get('system_prompt') or 'You are a courteous "
    "phone agent. Answer ONLY from the grounded knowledge matches provided; "
    "when nothing fits, say you will take a message.' }}"
)


def _validate_brain(*, brain: str, brain_provider: str) -> None:
    if brain not in BRAINS:
        raise VoiceAgentError(f"brain must be {'|'.join(BRAINS)}, got {brain!r}")
    if brain_provider not in BRAIN_PROVIDERS:
        raise VoiceAgentError(f"brain_provider must be {'|'.join(BRAIN_PROVIDERS)}, "
                              f"got {brain_provider!r}")


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


def _knowledge_ctx(agent: VoiceAgent, dataset_name: str | None) -> dict | None:
    """The knowledge binding copied into a session's context (or None)."""
    if not agent.knowledge_dataset_id:
        return None
    return {
        "dataset_id": agent.knowledge_dataset_id,
        "dataset_name": dataset_name,
        "text_column": agent.knowledge_text_column or "",
        "answer_column": agent.knowledge_answer_column or agent.knowledge_text_column or "",
        "top_k": max(1, min(int(agent.knowledge_top_k or 1), 5)),
    }


def _agent_ctx(agent: VoiceAgent, dataset_name: str | None = None) -> dict:
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
        "knowledge": _knowledge_ctx(agent, dataset_name),  # v72: dataset-backed answers
        "brain": {"kind": agent.brain, "provider": agent.brain_provider,  # v73
                  "model": agent.brain_model or None},
    }


def agent_out(row: VoiceAgent, handler_name: str | None = None,
              dataset_name: str | None = None) -> dict:
    """The API shape: config + derived wiring guidance (nothing stored)."""
    from . import voice_transport as transport
    wiring = {
            "inbound_webhook": "point the provider's call-control webhook at "
                               "/api/v1/channels/telnyx/{endpoint_id}/webhook (or the twilio "
                               "status callback at /api/v1/voice/webhooks/twilio/{session_id})",
            "media_stream": "point the provider's audio fork/stream at "
                            "ws://<host>/api/v1/voice/sessions/{session_id}/media",
            "asr_note": (f"no ASR engine is registered for {row.asr_provider!r} in this process - "
                         "the transport will honestly report asr.unavailable until one binds"
                         if row.asr_provider not in transport.registered_asr_engines()
                         else f"engine {row.asr_provider!r} is live in this process"),
        }
    knowledge = _knowledge_ctx(row, dataset_name)
    if knowledge:
        wiring["knowledge_note"] = (
            f"every turn is grounded on dataset {knowledge['dataset_name'] or knowledge['dataset_id']!r} "
            f"(matches ride the handler envelope's metadata.knowledge); "
            "preview with POST /voice/agents/{id}/knowledge/search")
    brain = {"kind": row.brain, "provider": row.brain_provider,
             "model": row.brain_model or None}
    if row.brain == "ai_agent":
        wiring["brain_note"] = (
            "the scaffolded handler runs an ai_agent (LLM) node grounded on the SAME "
            "knowledge binding - its user message is a template over the envelope "
            "(caller words + metadata.knowledge); the sandbox_bridge provider uses "
            "settings.llm_bridge_url, or edit the workflow for openai_compatible + "
            "a credential")
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
        "brain": brain,
        "knowledge": knowledge,
        "wiring": wiring,
        "context": row.context or {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def _dataset_name(db: AsyncSession, dataset_id: str | None) -> str | None:
    if not dataset_id:
        return None
    from ..models import Dataset

    row = await db.get(Dataset, dataset_id)
    return row.name if row is not None else None


async def _validate_knowledge(db: AsyncSession, owner_id: str | None, *,
                              dataset_id: str | None, text_column: str | None,
                              answer_column: str | None, top_k) -> tuple[str | None, str | None, str | None, int]:
    """A knowledge binding must point at a REAL dataset the owner can read."""
    if not dataset_id:
        return None, None, None, 1
    from . import knowledge as knowledge_svc

    try:
        ds = await knowledge_svc.load_knowledge_dataset(db, dataset_id, owner_id)
    except knowledge_svc.KnowledgeError as exc:
        raise VoiceAgentError(str(exc)) from exc
    cols = [str((c or {}).get("name") or "") for c in (ds.schema_json or [])
            if str((c or {}).get("name") or "")]
    if not cols:
        raise VoiceAgentError(f"knowledge dataset {ds.name!r} has no schema columns")
    text_col = text_column or cols[0]  # honest default: the first column
    if text_col not in cols:
        raise VoiceAgentError(f"knowledge dataset {ds.name!r} has no column "
                              f"{text_col!r} - columns: {', '.join(cols)}")
    answer_col = answer_column or text_col
    if answer_col not in cols:
        raise VoiceAgentError(f"knowledge dataset {ds.name!r} has no answer column "
                              f"{answer_col!r} - columns: {', '.join(cols)}")
    try:
        k = int(top_k or 1)
    except (TypeError, ValueError):
        raise VoiceAgentError("knowledge_top_k must be an integer 1..5") from None
    if not 1 <= k <= 5:
        raise VoiceAgentError("knowledge_top_k must be an integer 1..5")
    return dataset_id, text_col, answer_col, k


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
                            agent_name: str, *, brain: str = "scaffold",
                            brain_provider: str = "sandbox_bridge",
                            brain_model: str = "") -> Workflow:
    """A REAL, runnable handler workflow, in either brain flavor.

    * ``scaffold`` - trigger -> code node (the v71 offline echo).
    * ``ai_agent`` - trigger -> ai_agent node whose prompt is grounded on
      metadata.knowledge (v73): the LLM answers from the SAME binding the
      deterministic knowledge handler reads.
    """
    if brain == "ai_agent":
        nodes = [
            {"id": "t", "type": "manual_trigger", "name": "Trigger",
             "position": {"x": 0, "y": 0}, "parameters": {}},
            {"id": "brain", "type": "ai_agent", "name": "LLM brain (grounded)",
             "position": {"x": 200, "y": 0},
             "parameters": {
                 "provider": brain_provider or "sandbox_bridge",
                 "model": brain_model or "",
                 "system_prompt": _AI_BRAIN_SYSTEM_TMPL,
                 "user_message": _AI_BRAIN_USER_TMPL,
                 "max_iterations": 3, "temperature": 0.3,
                 "memory": "none", "tools": [],
             }},
        ]
        desc = (f"Scaffolded by the voice agent builder for {agent_name!r} - "
                "an ai_agent brain grounded on the agent's knowledge binding "
                "(metadata.knowledge); edit the node to point the LLM at your "
                "own provider + credential")
        tags = ["voice-agent", "scaffold", "ai-brain"]
    else:
        nodes = [
            {"id": "t", "type": "manual_trigger", "name": "Trigger",
             "position": {"x": 0, "y": 0}, "parameters": {}},
            {"id": "reply", "type": "code", "name": "Voice reply",
             "position": {"x": 200, "y": 0}, "parameters": {"code": _SCAFFOLD_CODE}},
        ]
        desc = (f"Scaffolded by the voice agent builder for {agent_name!r} - "
                "swap the code node for ai_agent + knowledge when ready")
        tags = ["voice-agent", "scaffold"]
    wf = Workflow(
        name=f"{agent_name} - voice handler",
        description=desc,
        graph={"nodes": nodes,
               "edges": [{"id": "e1", "source": "t",
                          "target": "brain" if brain == "ai_agent" else "reply",
                          "sourceHandle": "main", "targetHandle": "main"}]},
        is_active=False,  # handlers run through execute_workflow directly, like every channel
        tags=tags,
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
                       scaffold_handler: bool = False,
                       knowledge_dataset_id: str | None = None,
                       knowledge_text_column: str | None = None,
                       knowledge_answer_column: str | None = None,
                       knowledge_top_k: int = 1,
                       brain: str = "scaffold",
                       brain_provider: str = "sandbox_bridge",
                       brain_model: str = "") -> dict:
    """Create an agent; scaffold a runnable handler when none is bound."""
    if not name or not name.strip():
        raise VoiceAgentError("an agent name is required")
    _validate_speech(asr_provider=asr_provider, tts_provider=tts_provider,
                     tts_format=tts_format, language=language)
    _validate_brain(brain=brain, brain_provider=brain_provider)
    kb_dataset, kb_text, kb_answer, kb_topk = await _validate_knowledge(
        db, owner_id, dataset_id=knowledge_dataset_id or None,
        text_column=knowledge_text_column, answer_column=knowledge_answer_column,
        top_k=knowledge_top_k)
    if handler_workflow_id:
        wf = await db.get(Workflow, handler_workflow_id)
        if wf is None or (owner_id is not None and wf.owner_id is not None
                          and wf.owner_id != owner_id):
            raise VoiceAgentError(f"handler workflow {handler_workflow_id!r} not found")
        scaffolded = False
    elif scaffold_handler:
        wf = await _scaffold_handler(db, owner_id, name.strip()[:100], brain=brain,
                                     brain_provider=brain_provider,
                                     brain_model=brain_model.strip()[:120])
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
        knowledge_dataset_id=kb_dataset, knowledge_text_column=kb_text,
        knowledge_answer_column=kb_answer, knowledge_top_k=kb_topk,
        brain=brain, brain_provider=brain_provider,
        brain_model=brain_model.strip()[:120],
        context={"scaffolded_handler": scaffolded},
    )
    row.owner_id = owner_id
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return agent_out(row, await _wf_name(db, row.handler_workflow_id),
                     await _dataset_name(db, row.knowledge_dataset_id))


async def list_agents(db: AsyncSession, owner_id: str | None) -> list[dict]:
    q = select(VoiceAgent).order_by(VoiceAgent.created_at.desc())
    rows = (await db.execute(q)).scalars().all()
    if owner_id is not None:
        rows = [r for r in rows if r.owner_id is None or r.owner_id == owner_id]
    return [agent_out(r, await _wf_name(db, r.handler_workflow_id),
                      await _dataset_name(db, r.knowledge_dataset_id)) for r in rows]


async def get_agent(db: AsyncSession, agent_id: str, owner_id: str | None) -> dict:
    row = await _load(db, agent_id, owner_id)
    return agent_out(row, await _wf_name(db, row.handler_workflow_id),
                     await _dataset_name(db, row.knowledge_dataset_id))


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
    # v72: knowledge binding - '' clears the dataset (the handler_workflow_id
    # convention); a non-empty id is validated (exists + owner-readable)
    if "knowledge_dataset_id" in fields:
        kb_id = fields.get("knowledge_dataset_id") or ""
        if kb_id:
            kb_dataset, kb_text, kb_answer, kb_topk = await _validate_knowledge(
                db, owner_id, dataset_id=kb_id,
                text_column=fields.get("knowledge_text_column"),
                answer_column=fields.get("knowledge_answer_column"),
                top_k=fields.get("knowledge_top_k") or row.knowledge_top_k)
            row.knowledge_dataset_id, row.knowledge_text_column = kb_dataset, kb_text
            row.knowledge_answer_column, row.knowledge_top_k = kb_answer, kb_topk
        else:
            row.knowledge_dataset_id = None
            row.knowledge_text_column = None
            row.knowledge_answer_column = None
    else:
        # columns/top_k may be adjusted on an existing binding
        touched = False
        for key, val in (("knowledge_text_column", fields.get("knowledge_text_column")),
                         ("knowledge_answer_column", fields.get("knowledge_answer_column")),
                         ("knowledge_top_k", fields.get("knowledge_top_k"))):
            if val is not None and getattr(row, key) != val:
                setattr(row, key, val)
                touched = True
        if touched and row.knowledge_dataset_id:
            await _validate_knowledge(
                db, owner_id, dataset_id=row.knowledge_dataset_id,
                text_column=row.knowledge_text_column,
                answer_column=row.knowledge_answer_column,
                top_k=row.knowledge_top_k)
    if "handler_workflow_id" in fields:
        hwid = fields["handler_workflow_id"]
        if hwid:
            wf = await db.get(Workflow, hwid)
            if wf is None or (owner_id is not None and wf.owner_id is not None
                              and wf.owner_id != owner_id):
                raise VoiceAgentError(f"handler workflow {hwid!r} not found")
        row.handler_workflow_id = hwid or None
    # v73: brain changes. A custom handler workflow is NEVER replaced -
    # py8n refuses loudly instead. A scaffolded handler is re-scaffolded as
    # a NEW workflow (live sessions keep the one they copied at creation;
    # the old scaffold is left in the estate, inactive and tagged).
    brain_requested = fields.get("brain") or row.brain
    provider_requested = fields.get("brain_provider") or row.brain_provider
    model_requested = (fields.get("brain_model") if fields.get("brain_model") is not None
                       else row.brain_model) or ""
    _validate_brain(brain=brain_requested, brain_provider=provider_requested)
    brain_changed = (brain_requested != row.brain
                     or provider_requested != row.brain_provider
                     or model_requested != (row.brain_model or ""))
    if brain_changed:
        if not (row.context or {}).get("scaffolded_handler"):
            raise VoiceAgentError(
                "this agent runs a custom handler workflow - py8n will not replace it; "
                "bind your own ai_agent workflow, or clear handler_workflow_id first")
        wf = await _scaffold_handler(db, owner_id, row.name.strip()[:100],
                                     brain=brain_requested,
                                     brain_provider=provider_requested,
                                     brain_model=model_requested.strip()[:120])
        row.handler_workflow_id = wf.id
        ctx = dict(row.context or {})
        ctx["handler_regenerated"] = "v73 brain change"
        row.context = ctx
    row.brain = brain_requested
    row.brain_provider = provider_requested
    row.brain_model = model_requested.strip()[:120]
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return agent_out(row, await _wf_name(db, row.handler_workflow_id),
                     await _dataset_name(db, row.knowledge_dataset_id))


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
    ctx["voice_agent"] = _agent_ctx(row, await _dataset_name(db, row.knowledge_dataset_id))
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
