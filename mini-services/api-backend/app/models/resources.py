"""Model classes: Credential, CredentialEvent, EnvVariable, AppSetting, Folder, AgentMemory.

Split from the original app/models.py (task #3) - moved verbatim, no
behavior change.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ._shared import Base, JSONVariant, _now, _uuid

class Credential(Base):
    """Encrypted-at-rest credential (API keys, tokens) for node parameters."""

    __tablename__ = "credentials"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)  # v37
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    type: Mapped[str] = mapped_column(String(60), default="generic")  # header_auth|openai_compatible|generic
    data_encrypted: Mapped[str] = mapped_column(Text, nullable=False)  # Fernet token
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # v43 last secret rotation


class CredentialEvent(Base):
    """Vault audit trail (v43) - one row per lifecycle action on a credential.

    Written on created / renamed / updated (payload via PATCH) / rotated /
    tested / used (a node resolved the secret during an execution) / deleted.
    ``credential_name`` is snapshotted so the trail stays meaningful after the
    credential itself is gone. Detail dicts carry FIELD NAMES only - secret
    values never touch the audit log.
    """

    __tablename__ = "credential_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    credential_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    credential_name: Mapped[str] = mapped_column(String(200), default="")
    action: Mapped[str] = mapped_column(String(20), nullable=False)  # created|renamed|updated|rotated|tested|used|deleted
    detail: Mapped[dict] = mapped_column(JSON, default=dict)  # field names / workflow refs, never values
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


class EnvVariable(Base):
    """Global environment variable (v15) - exposed to templates as ``env.KEY``.

    Values are ALWAYS Fernet-encrypted at rest (uniform code path, no
    plaintext in the DB). ``is_secret`` rows are additionally masked in the
    API (write-only) so they can hold tokens / passwords.
    """

    __tablename__ = "env_variables"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)  # v37
    key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    value_encrypted: Mapped[str] = mapped_column(Text, nullable=False)  # Fernet token
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class AppSetting(Base):
    """Global key/value platform setting (v19) - retention policies etc.

    Values are JSON documents; a missing row means "use the built-in default"
    (declared next to the consumer, e.g. services/retention.py).
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class Folder(Base):
    """Organizational folder for workflows (v16).

    Supports nesting up to MAX_FOLDER_DEPTH (enforced in the API layer with
    a cycle-safe ancestor walk). Deleting a folder is refused while it still
    has subfolders; workflows inside fall back to the root (folder_id=None).
    """

    __tablename__ = "folders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)  # v37
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    parent_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class AgentMemory(Base):
    """Persisted conversation buffer for AI Agent nodes (v23).

    One row per session key; ``messages`` holds the rolling chat history
    (alternating user/assistant turns) that gets injected into the next
    agent run sharing the same key.
    """

    __tablename__ = "agent_memories"

    session_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    messages: Mapped[list] = mapped_column(JSONVariant, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class AgentModule(Base):
    """A first-class agent module (v108).

    Until now an agent only existed INSIDE a workflow graph (the ai_agent
    node) and the /agents console was a read-only inventory of those nodes.
    A module is the agent as its own resource: a system prompt, a provider
    (sandbox bridge or an OpenAI-compatible credential), a tool kit (the
    SAME ToolSpec shape the node speaks) and session memory - runnable
    directly over the API without building a graph first.

    The runtime reuses the node's proven machinery (wire protocol, tool
    execution, sandbox, read-only dataset SQL); the module only owns the
    persistent identity and its configuration.
    """

    __tablename__ = "agent_modules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    system_prompt: Mapped[str] = mapped_column(
        Text, default="You are a precise operations agent. Use the tools when they help, then answer.",
        nullable=False)
    # sandbox_bridge | openai_compatible - same transports the node speaks
    provider: Mapped[str] = mapped_column(String(30), default="sandbox_bridge", nullable=False)
    model: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    credential_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    temperature: Mapped[float] = mapped_column(Float, default=0.4, nullable=False)
    max_iterations: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    # none | buffer - the node's session-memory modes
    memory: Mapped[str] = mapped_column(String(10), default="none", nullable=False)
    max_history_turns: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    # list of ToolSpec-shaped dicts (kind/name/description/...)
    tools: Mapped[list] = mapped_column(JSONVariant, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class HarnessSession(Base):
    """A harness session (v109) - the system's own agentic runtime.

    Where an agent module (v108) speaks the ENGINE's toolchest
    (workflow / http / knowledge / dataset / code), a harness session
    speaks PY8N ITSELF: the estate overview, the machines and their
    instances, the escalation door, the chain map, the data platform -
    with harness-grade discipline the node loop does not have: guard
    rails (repeat / budget / wall-clock), fail-closed approvals on
    every sensitive move, and a persistent turn state machine that can
    pause for a human decision and resume.

    The brain is the SAME transport the node and the modules speak
    (sandbox_bridge or an OpenAI-compatible credential) - zero new LLM
    machinery; the harness owns the discipline, not the wire.
    """

    __tablename__ = "harness_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    system_prompt: Mapped[str] = mapped_column(
        Text,
        default="You are the operations harness. You can read the whole estate "
                "and you may move the business, but every sensitive move waits "
                "for a human decision first. Answer with what the tools returned.",
        nullable=False)
    # the SAME providers the ai_agent node and the modules speak
    provider: Mapped[str] = mapped_column(String(30), default="sandbox_bridge", nullable=False)
    model: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    credential_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    temperature: Mapped[float] = mapped_column(Float, default=0.4, nullable=False)
    # none | buffer - completed turns are injected as history on the next turn
    memory: Mapped[str] = mapped_column(String(10), default="buffer", nullable=False)
    max_history_turns: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class HarnessTurn(Base):
    """One harness turn - the persistent state machine of one request.

    status walks: running -> (waiting_approval ->)* completed
                                     |-> exhausted (budget) | failed | refused
    ``wire`` holds the EXACT chat message list at the pause moment, so an
    approval decision can resume the loop where it stopped - even after a
    server restart. ``trace`` holds the loop frames (iteration / reply /
    tool_call / tool_result / guard / approval_requested / answer) for the
    console and the API transcript.
    """

    __tablename__ = "harness_turns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    user_message: Mapped[str] = mapped_column(Text, nullable=False)
    # running | waiting_approval | completed | exhausted | failed | refused
    status: Mapped[str] = mapped_column(String(20), default="running", nullable=False, index=True)
    reply: Mapped[str] = mapped_column(Text, default="", nullable=False)
    iterations: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    guard_blocks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tool_calls: Mapped[list] = mapped_column(JSONVariant, default=list)
    trace: Mapped[list] = mapped_column(JSONVariant, default=list)
    # resume state: the message list at the approval pause
    wire: Mapped[list] = mapped_column(JSONVariant, default=list)
    error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # v111: set when the SYSTEM fired this round (a patrol), NULL for a
    # human-initiated turn - the patrol receipt points here
    patrol_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class HarnessApproval(Base):
    """A fail-closed approval gate (v109).

    When the model calls a SENSITIVE tool (advance / ack / start - the
    moves that change the estate), the turn pauses and one of these rows
    is the decision slip. Silence expires (fail-closed: ttl > 0 turns a
    pending slip into ``expired`` and the turn refuses); only an explicit
    approve runs the tool - at decision time, so the move happens when the
    human said yes, not when the model asked.
    """

    __tablename__ = "harness_approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    turn_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    tool: Mapped[str] = mapped_column(String(80), nullable=False)
    arguments: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    # pending | approved | rejected | expired
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # v110: the receipt - WHO decided (user id; NULL = system/expired or an
    # unauthenticated door on a trust-the-wire install)
    decided_by: Mapped[str | None] = mapped_column(String(36), nullable=True)


class HarnessPatrol(Base):
    """A harness patrol (v111) - the harness scheduling its OWN rounds.

    Until now the harness only ever answered: a human typed, the loop ran.
    A patrol flips that - the OWNER writes a mission once ("check the
    attention feed every morning", "walk anything stuck on the front
    desk"), binds it to a session and a rhythm (interval_seconds), and
    the SYSTEM fires the rounds itself. Every round is a REAL harness
    turn through the SAME start_turn path - the same loop, the same
    guard rails, the same fail-closed gate - so a patrol that wants to
    move the business still parks a slip for a human. Nothing about the
    discipline changes when nobody is watching; that is the point.

    The row is the receipt board: run_count, last_run_at, last_status
    (the turn's terminal state), last_run_turn_id (the full trace one
    hop away), last_error. The receipt is stamped BEFORE the round runs
    (the rhythm stays honest even across a crash) and the terminal
    status lands after.
    """

    __tablename__ = "harness_patrols"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # the message the patrol sends on every round - the mission
    mission: Mapped[str] = mapped_column(Text, nullable=False)
    interval_seconds: Mapped[int] = mapped_column(Integer, default=3600, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # the receipt board
    run_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # completed | exhausted | failed | refused | waiting_approval
    last_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_run_turn_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # v112 - THE DISPATCH: the round's outcome walks OUT of py8n over the
    # report envelope's own email discipline. Empty recipients = quiet.
    dispatch_to: Mapped[str] = mapped_column(Text, default="", nullable=False)
    last_dispatch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # ok | error | skipped (None = never dispatched)
    last_dispatch_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_dispatch_detail: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class ErpClose(Base):
    """A posted period close (v118) - the accounting close's receipt.

    The ERP doors always spoke read-only; v118 adds THE WRITE: closing
    entries land on the GL book (revenue and expense sweep through
    Income summary into Retained earnings) and this row is the receipt
    that locks the book - one close per dataset, a second close is a
    loud 409. The receipt carries the period's net, the retained
    earnings it produced, how many closing lines landed, and the lines
    themselves (detail_json) so the audit can replay the close without
    re-deriving it.
    """

    __tablename__ = "erp_closes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    dataset_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    period: Mapped[str] = mapped_column(String(60), default="", nullable=False)
    net: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    retained_after: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    entries: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    detail_json: Mapped[dict] = mapped_column(JSONVariant, default=dict)
