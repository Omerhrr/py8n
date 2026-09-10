"""Model classes: VoiceSession, VoiceMeeting, VoiceMeetingParticipant, VoiceCampaign, VoiceCampaignTarget, VoiceEvent, VoiceMeetingMessage, VoiceMeetingRecording, VoiceAgent, MediaSession.

Split from the original app/models.py (task #3) - moved verbatim, no
behavior change.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ._shared import Base, JSONVariant, _now, _uuid

class VoiceSession(Base):
    """A voice session (v69) - the call as a first-class primitive.

    One phone conversation as py8n sees it: a call-state machine
    (initiated -> ringing -> in_progress -> ended, with no_answer / busy /
    voicemail endings), an optional link to the interaction-layer
    conversation (so the voice transcript lives in the SAME place as
    whatsapp/app transcripts), and the barge-in bookkeeping. The event
    timeline (VoiceEvent) is the record; everything reported about the
    call (duration, barge-in count, turn count) is derived from it.
    """

    __tablename__ = "voice_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    direction: Mapped[str] = mapped_column(String(10), nullable=False, default="inbound")  # inbound|outbound
    provider: Mapped[str] = mapped_column(String(40), nullable=False, default="twilio")
    # provider-side call identifiers (CallSid, provider call id, ...)
    call_ref: Mapped[str] = mapped_column(String(180), nullable=False, default="")
    from_ref: Mapped[str] = mapped_column(String(180), nullable=False, default="")
    to_ref: Mapped[str] = mapped_column(String(180), nullable=False, default="")
    # the answering workflow (voice_turn runs it exactly like the interaction handler)
    handler_workflow_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # optional link into the interaction layer - one customer, one transcript
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # initiated | ringing | in_progress | on_hold | voicemail | ended
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="initiated", index=True)
    end_reason: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    # context holds live call state (active_tts event id) + provider extras
    context: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class VoiceMeeting(Base):
    """A multi-party voice meeting (v74) - the room that owns legs.

    A meeting is not an audio mixer (mixing is the provider's job - a
    conference bridge, a SIP fork, a room in the provider's media plane);
    py8n is the SYSTEM layer: it owns the participant list, dials legs
    through the provider adapters, binds the SAME VoiceAgent persona to
    every leg, and derives the merged, speaker-attributed transcript from
    the legs' event timelines. Storage is real traffic state (like campaign
    targets and limit counters) - participants are calls that exist.
    """

    __tablename__ = "voice_meetings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    # active | ended
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="active", index=True)
    context: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class VoiceMeetingParticipant(Base):
    """One leg of a meeting (v74) - a participant and its session.

    ``channel`` is web (a browser/stream attaches to the leg's session
    media websocket), telnyx/sip (py8n dials the participant through the
    provider), or any channel adapter key later. ``session_id`` links the
    leg to a REAL VoiceSession - the full v69..v73 machinery (state
    machine, ASR engine, handler, analytics) runs per leg unchanged.
    """

    __tablename__ = "voice_meeting_participants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    meeting_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    label: Mapped[str] = mapped_column(String(140), nullable=False, default="")
    channel: Mapped[str] = mapped_column(String(30), nullable=False, default="web")
    address: Mapped[str] = mapped_column(String(180), nullable=False, default="")
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    call_control_id: Mapped[str] = mapped_column(String(180), nullable=False, default="")
    # joining | dialing | joined | left | skipped | failed
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="joining")
    last_error: Mapped[str] = mapped_column(String(400), nullable=False, default="")
    meta: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class VoiceCampaign(Base):
    """An outbound voice campaign (v74) - dial a list through an agent.

    The campaign is CONFIGURATION (agent + targets + the provider endpoint
    to dial through); the targets are TRAFFIC STATE - real dials that
    happened (the same deliberate exception to derived-never-stored as the
    deployment-token hit rows: you cannot derive that a call was placed).
    Progress is derived from the target rows; the answered conversations
    are real VoiceSessions bound to the campaign's agent.
    """

    __tablename__ = "voice_campaigns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    name: Mapped[str] = mapped_column(String(140), nullable=False)
    # draft | running | stopped | completed
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft", index=True)
    endpoint_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    config: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class VoiceCampaignTarget(Base):
    """One dial attempt slot in a campaign (v74).

    status: pending -> dialing -> answered -> completed, or
    no_answer / failed / skipped with last_error. call_control_id links
    the carrier's call to this row; session_id links the conversation
    the campaign opened when the call was answered.
    """

    __tablename__ = "voice_campaign_targets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    campaign_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    address: Mapped[str] = mapped_column(String(180), nullable=False)
    name: Mapped[str] = mapped_column(String(140), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    call_control_id: Mapped[str] = mapped_column(String(180), nullable=False, default="")
    last_error: Mapped[str] = mapped_column(String(400), nullable=False, default="")
    meta: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    dialed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class VoiceEvent(Base):
    """One event on a voice session (v69) - the append-only call timeline.

    Kinds: call.ringing | call.answered | speech.started | speech.ended |
    dtmf | asr.final | tts.started | tts.ended | barge_in | hold | unhold |
    transfer | no_answer | busy | voicemail_detected | hangup | failed.
    Barge-in semantics live here: a barge_in event references the
    tts.started event it interrupted, and the tts.ended carries
    cancelled=true.
    """

    __tablename__ = "voice_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class VoiceMeetingMessage(Base):
    """One in-meeting group-chat message (v76).

    The room's TEXT side channel: while the audio mix is the provider's
    media plane, the chat is py8n's own room surface - members who are
    muted (the room does not hear them) can still TYPE. A message is real
    traffic state (you cannot derive that someone said it): rows live in
    their own table, never stuffed into meeting.context. ``role`` is
    member | moderator | agent (agent rows are the room agent's replies
    when a member's post asks the agent - the reply ALSO lands on the
    asking leg's linked conversation, one customer one transcript).
    """

    __tablename__ = "voice_meeting_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    meeting_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    participant_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    author: Mapped[str] = mapped_column(String(140), nullable=False, default="")
    # member | moderator | agent
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="member")
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    meta: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class VoiceMeetingRecording(Base):
    """A meeting's recording/transcription ARCHIVE (v79).

    py8n is not the media plane (the browsers' WebRTC stacks carry the
    pixels peer-to-peer), so a py8n meeting archive is honest about what
    it can hold: the words and the audio that actually flowed THROUGH
    py8n. One row per recording pass over a room:

    * the TRANSCRIPT - the room's derived merged transcript (asr.final +
      tts.started across the legs) and the chat log, snapshotted into
      artifacts at stop time - the transcription archive;
    * the AUDIO of the web legs - the utterances the media websocket
      decoded (speech.ended segments) are accumulated in an in-process
      capture buffer while the recording runs and written as real WAV
      artifacts at stop time. Phone legs ride the carrier's media plane;
      py8n never has their audio and the archive says so per leg.

    The row is the handle; the artifacts are the content. State:
    recording -> stopped (or failed). Nothing derived is stored twice:
    participant lists, durations and counts are recomputed on read from
    the row's meta pointers and the meeting itself.
    """

    __tablename__ = "voice_meeting_recordings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    meeting_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    # recording | stopped | failed
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="recording",
                                       index=True)
    # pointers to the archived content (artifact ids), set at stop time
    meta: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class VoiceAgent(Base):
    """A voice agent (v71) - the composable configuration of a phone agent.

    v69/v70 built the voice PRIMITIVES (state machine, barge-in, ASR/TTS
    contracts, the websocket media transport); a VoiceAgent is the
    BUILDER object that composes them into one deployable persona:

    * ``greeting_text`` - spoken (interruptible, barge-in-able) the moment
      the call is answered, through the agent's own TTS configuration;
    * ``asr_provider`` / ``tts_provider`` / ``tts_voice`` / ``tts_format``
      / ``language`` - the speech configuration every session of this
      agent inherits (the media stream resolves its ASR engine and the
      turn loop resolves its TTS request from here; explicit per-call
      parameters still win);
    * ``barge_in`` - whether the caller may interrupt (False turns the
      greeting and turns into non-interruptible utterances);
    * ``system_prompt`` - the persona text injected into the handler
      envelope's metadata so AI handlers (ai_agent nodes) speak with the
      agent's voice;
    * ``handler_workflow_id`` - the workflow that answers. When none is
      bound, the builder SCAFFOLDS one (trigger -> code node preloaded
      with a voice-agent template) so a new agent is runnable immediately.

    An agent is CONFIGURATION, not state: sessions copy the relevant
    fields into their context at creation time, so editing an agent never
    rewrites history on live calls.
    """

    __tablename__ = "voice_agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(140), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    greeting_text: Mapped[str] = mapped_column(Text, default="")
    asr_provider: Mapped[str] = mapped_column(String(40), nullable=False, default="py8n_local")
    tts_provider: Mapped[str] = mapped_column(String(40), nullable=False, default="openai_tts")
    tts_voice: Mapped[str] = mapped_column(String(80), nullable=False, default="alloy")
    tts_format: Mapped[str] = mapped_column(String(10), nullable=False, default="wav")
    language: Mapped[str] = mapped_column(String(20), nullable=False, default="en-US")
    barge_in: Mapped[bool] = mapped_column(Boolean, default=True)
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    handler_workflow_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # v72: knowledge binding - a dataset the agent answers FROM. The binding
    # is config (copied into sessions at creation); the dataset's CONTENT is
    # read live at every turn, so FAQ updates take effect on live calls
    # without dropping them.
    knowledge_dataset_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    knowledge_text_column: Mapped[str | None] = mapped_column(String(80), nullable=True)
    knowledge_answer_column: Mapped[str | None] = mapped_column(String(80), nullable=True)
    knowledge_top_k: Mapped[int] = mapped_column(Integer, default=1)
    # v73: the brain behind the scaffolded handler. "scaffold" = the echo
    # code node; "ai_agent" = an LLM brain whose prompt is grounded on the
    # SAME knowledge binding (matches ride metadata.knowledge into the
    # agent's user message). Sessions copy the brain at creation.
    brain: Mapped[str] = mapped_column(String(20), nullable=False, default="scaffold")
    brain_provider: Mapped[str] = mapped_column(String(40), nullable=False,
                                                default="sandbox_bridge")
    brain_model: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    # v74: the REAL LLM credential behind an openai_compatible brain - a
    # vault credential (openai_compatible | anthropic) the scaffolded
    # ai_agent node routes through (services/llm_routing). Frozen into the
    # scaffolded workflow at scaffold time; sessions keep what they copied.
    llm_credential_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    context: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class MediaSession(Base):
    """The real-time runtime object (v80) - the MediaSession abstraction.

    v69..v79 grew the real-time layers as honest primitives: calls
    (state machine + ASR/TTS + barge-in), meetings (the room, its legs,
    mix/floor/chat), video (track registry + signaling relay), queues,
    recordings. This row is the RUNTIME handle that unifies them - the
    single object a client joins, watches and ends, whatever modality it
    carries:

        Voice Session = MediaSession + audio          (wraps a call)
        Video Session = MediaSession + audio + video  (a video-first room)
        Meeting       = MediaSession + participants   (the room)

    The row is the handle and its config; EVERYTHING else about the
    session is DERIVED at read time from the underlying primitive it
    wraps (``ref_kind``/``ref_id`` -> a VoiceSession call or a
    VoiceMeeting room): participants, audio/video/screen tracks, data
    channels, permissions, presence, the event timeline, the active
    recording and the transcript. Nothing is stored twice.
    """

    __tablename__ = "media_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # voice | video | meeting
    kind: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    # session (wraps a VoiceSession call) | meeting (wraps a VoiceMeeting room)
    ref_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    ref_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # active | ended
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="active", index=True)
    config: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

