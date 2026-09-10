"""Model classes: SystemDraft, Solution, Py8nSystem, SystemOperation, SystemComponent, SystemMember, SystemEvent, SystemDeployment, SystemApiKey.

Split from the original app/models.py (task #3) - moved verbatim, no
behavior change.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ._shared import Base, JSONVariant, _now, _uuid

class SystemDraft(Base):
    """One AI System Builder session (v59).

    The roadmap's Describe -> Discover -> Clarify -> Design -> Build loop:
    the user describes what they want in plain language, the builder
    synthesizes a SystemSpec (purpose, persona, component checklist with
    selected flags, clarifying questions), the interview + toggles refine
    it, and the build step translates the SELECTED components into real
    py8n primitives - datasets, workflow graphs, contracts, policies,
    dashboards, reports and notification rules.

    ``spec_json`` is the living SystemSpec; ``messages_json`` is the
    interview transcript; ``built_json`` holds the refs the build created
    (so review is one GET). Nothing here is derived - it IS the source of
    truth for the conversation - but every BUILT artifact is a normal
    py8n object owned by the user.
    """

    __tablename__ = "system_drafts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    description: Mapped[str] = mapped_column(Text, default="")  # the original ask
    persona: Mapped[str] = mapped_column(String(20), default="business")  # business|data_engineer
    status: Mapped[str] = mapped_column(String(20), default="interview", index=True)  # interview|built
    spec_json: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    messages_json: Mapped[list] = mapped_column(JSONVariant, default=list)
    built_json: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class Solution(Base):
    """A marketplace solution (v60) - the outcome-named layer over packs.

    Templates say "Webhook workflow"; solutions say "Customer Support
    Automation" and show WHAT YOU GET (the capability checklist) instead
    of what nodes they contain. The ``pack_json`` payload is a standard
    py8n-pack document (workflows + datasets), so installing a solution
    reuses the exact pack-import machinery - and anyone can author one
    from their own workflows/datasets.

    ``owner_id`` NULL = system-curated (seeded showcase solutions);
    otherwise the author, who may unlist it.
    """

    __tablename__ = "solutions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(String(140), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(140), nullable=False)
    tagline: Mapped[str] = mapped_column(String(300), default="")
    category: Mapped[str] = mapped_column(String(60), default="Operations", index=True)
    icon: Mapped[str] = mapped_column(String(60), default="package")
    color: Mapped[str] = mapped_column(String(20), default="#22d3ee")
    # the capability checklist - the roadmap's "Includes: ✓ ..." list
    outcomes_json: Mapped[list] = mapped_column(JSONVariant, default=list)
    # a standard py8n-pack document (format "py8n-pack")
    pack_json: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    docs: Mapped[str] = mapped_column(Text, default="")
    installs: Mapped[int] = mapped_column(Integer, default=0)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Py8nSystem(Base):
    """A Py8n System (v61) - the operating unit above workflows.

    Where a workflow automates a TASK, a system RUNS A PART OF THE
    BUSINESS: it binds the workflows, datasets, apps, dashboards, models
    and reports that belong together into one named, health-scored,
    ownable unit. Membership is a curated grouping (like folders), so it
    IS stored - but everything the system REPORTS about itself (health,
    activity, freshness) is derived from the member objects at read
    time and can never drift.

    v81 - the SYSTEM RUNTIME: a system is not just a grouping, it is a
    RUNNING ENTITY with a lifecycle. ``lifecycle`` gates every reactive
    path its workflows have (event triggers, schedule ticks, webhooks):
    a system that is ``paused`` or ``stopped`` holds its workflows still
    WITHOUT touching their own ``is_active`` (the user's per-workflow
    intent survives the operation; starting the system restores exactly
    what was active). ``source_solution_slug`` remembers the marketplace
    solution the system was installed from, so ``upgrade`` can re-apply
    that solution's pack and reconcile the components.
    """

    __tablename__ = "py8n_systems"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(140), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    icon: Mapped[str] = mapped_column(String(60), default="boxes")
    color: Mapped[str] = mapped_column(String(20), default="#f97316")
    # v81 runtime: running | paused | stopped (pre-v81 systems have been
    # operating since creation, so the honest default is running)
    lifecycle: Mapped[str] = mapped_column(String(20), nullable=False, default="running", index=True)
    # v81: the marketplace solution this system was installed from (upgrade door)
    source_solution_slug: Mapped[str | None] = mapped_column(String(140), nullable=True)
    upgraded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    components: Mapped[list["SystemComponent"]] = relationship(
        back_populates="system",
        cascade="all, delete-orphan",
        order_by="SystemComponent.added_at",
    )


class SystemOperation(Base):
    """One lifecycle/management operation on a system (v81).

    The system's OPERATIONS LOG - the durable audit of the verbs the
    runtime accepted: installed, started, stopped, paused, resumed,
    upgraded, component_added, component_removed. Like campaign targets
    and serving-token hits, this is the deliberate exception to
    derived-never-stored: an operations log is state about TRAFFIC (who
    did what, when, with what result), so it is written, never rebuilt.

    ``actor`` is the user id (or ``system`` when the platform itself
    performed the step, e.g. an install). ``detail`` carries the verb's
    own facts (lifecycle from/to, activated workflow count, upgrade diff).
    """

    __tablename__ = "system_operations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    system_id: Mapped[str] = mapped_column(ForeignKey("py8n_systems.id", ondelete="CASCADE"), index=True)
    verb: Mapped[str] = mapped_column(String(40), nullable=False)
    actor: Mapped[str] = mapped_column(String(180), nullable=False, default="")
    detail: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


class SystemComponent(Base):
    """One object bound to a system (v61).

    ``kind`` is one of workflow | dataset | app | dashboard | model |
    report | model_system - resolved and validated against the live table
    on attach, so a system can never reference an object that does not
    exist. v81 adds the interaction layer: voice_agent | queue | meeting
    (the support line's phone agent, waiting room and room are components
    too - the system covers Interactions, not just data).
    """

    __tablename__ = "system_components"
    __table_args__ = (UniqueConstraint("system_id", "kind", "ref_id", name="uq_system_component"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    system_id: Mapped[str] = mapped_column(ForeignKey("py8n_systems.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    ref_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    system: Mapped[Py8nSystem] = relationship(back_populates="components")


class SystemMember(Base):
    """System-level membership (v62) - who can touch a system, at what role.

    The CREATOR is not stored here: ``py8n_systems.owner_id`` remains the
    single source of truth for ownership (pre-v62 systems keep working with
    zero migration). This table holds the INVITED members:

    * ``viewer`` - can read the system (detail, health, dependency views)
    * ``editor`` - can also bind/unbind components and edit metadata
    * ownership is never shared - the creator anchor cannot be demoted or
      removed, and invites are editor/viewer only.

    Membership is a permission grant, so it IS stored; everything the
    system reports stays derived.
    """

    __tablename__ = "system_members"
    __table_args__ = (UniqueConstraint("system_id", "user_id", name="uq_system_member"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    system_id: Mapped[str] = mapped_column(ForeignKey("py8n_systems.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(10), nullable=False, default="viewer")  # editor|viewer
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class SystemEvent(Base):
    """A first-class real-time event (v80) - the primitive that makes the
    other primitives composable.

    Everything the real-time layers do generates one: calls start and
    end, callers wait and get seated, participants join rooms, tracks
    publish, recordings land, texts arrive, callbacks schedule. An event
    is TRAFFIC (it happened - the same deliberate exception to
    derived-never-stored as the call timeline), stored once and readable
    by anyone the owner hands the filter to:

    * ``source``  - the system component that emitted it (voice, queue,
      sms, meeting, video, recording, media, campaign, user);
    * ``type``    - the dotted, namespaced what-happened
      (``call.waiting``, ``queue.position_changed``, ``participant.joined``,
      ``meeting.ended``, ``recording.ready``, ``sms.received``);
    * ``actor``   - who did it (a session ref, a participant label, a
      user, ``system``);
    * ``target``  - the entity it happened to (type + id);
    * ``payload`` - the event's own data (positions, keywords, artifact
      pointers) so a reacting workflow never has to re-fetch blindly;
    * ``correlation_id`` - the thread that ties a journey together (one
      caller's queue wait across announcements, SMS and the callback);
    * ``session_id`` - the live call it rode on, when one existed.

    Workflows subscribe with the Event Trigger node (a type pattern like
    ``queue.*``); when an event lands, matching workflows are dispatched
    fire-and-forget with the event as the trigger payload - that is the
    moment py8n's primitives become systems: participant.joined can load
    context and greet, position >= 5 can offer a callback, meeting.ended
    can transcribe, summarize and create tasks.
    """

    __tablename__ = "system_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(180), nullable=False, default="")
    target_type: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    target_id: Mapped[str] = mapped_column(String(180), nullable=False, default="")
    correlation_id: Mapped[str] = mapped_column(String(180), nullable=False, default="", index=True)
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    payload: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now,
                                                 index=True)


class SystemDeployment(Base):
    """The deployed identity of a system (v102) - the commercial bridge.

    The thesis this table serves: a company should feel "this is OUR
    operations system", not "we are using a workflow tool". One row per
    system (unique ``system_id``) carrying the four facts a deployed
    system needs to present itself:

    * ``domain``       - the custom hostname the system answers on
      (normalized lowercase, globally unique, reserved names refused);
      the public identity resolver turns this back into the system.
    * ``environment``  - staging | production (a label the estate wears,
      honestly - the sandbox has one runtime, pretending otherwise
      would be a lie).
    * ``status``       - offline | live | paused, moved only by the loud
      verbs (deploy / pause / retire) that write the operations log.
    * ``branding``     - the login surface's own voice (accent color,
      tagline, login headline, logo glyph) - what users see before they
      authenticate.

    The system's USERS and ROLES are NOT stored here: ``system_members``
    (v62) already owns membership (owner / editor / viewer), and the
    deployment rides it - "system-specific login" is the platform login
    resolved against the domain's system, with the member's role in the
    answer. ``url`` is derived at read time (https:// + domain), never
    stored, so it can never drift from the domain.
    """

    __tablename__ = "system_deployments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    system_id: Mapped[str] = mapped_column(
        ForeignKey("py8n_systems.id", ondelete="CASCADE"),
        nullable=False, unique=True, index=True)
    domain: Mapped[str | None] = mapped_column(String(253), nullable=True,
                                               unique=True, index=True)
    environment: Mapped[str] = mapped_column(String(20), nullable=False,
                                             default="staging")
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="offline")
    branding: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    deployed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                         nullable=True)
    # v103: the liveness probe - what the domain answered the last time
    # somebody asked. Derived evidence (stamped per probe), never a stored
    # "healthy" flag that could silently rot.
    last_ping_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                          nullable=True)
    last_ping_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_ping_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_ping_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_ping_detail: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now,
                                                 onupdate=_now)


class SystemApiKey(Base):
    """A deployed system's machine credential (v104) - the system's own voice.

    Where v41's ``api_keys`` authenticate AS A USER (inheriting that user's
    whole estate), a system key authenticates AS THE SYSTEM: the key resolves
    to exactly one ``py8n_systems`` row and speaks only on it - the machine
    identity for the integrations a deployed system needs (monitoring agents,
    upstream pushers, the company's other software talking to ITS operations
    system). ``py8n_sys_`` is a RESERVED prefix: keys carrying it never match
    the user-key table.

    The role the key speaks with is derived from its scopes at read time -
    ``write`` in scopes -> editor, otherwise viewer - the same role ladder
    the human members use, so a key can never do something its scope does
    not spell. The full key is shown exactly once at creation; storage keeps
    only the sha256 hash and a display prefix. Revoke = stamp revoked_at.
    A machine can never mint or manage keys (owner-only, human doors), and
    a key on a foreign system looks nonexistent (404) - never a fall-through
    to the anonymous path.
    """

    __tablename__ = "system_api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    system_id: Mapped[str] = mapped_column(
        ForeignKey("py8n_systems.id", ondelete="CASCADE"),
        nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    prefix: Mapped[str] = mapped_column(String(24), default="")  # display form, e.g. py8n_sys_ab12cd34
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)  # sha256 hex
    # same ladder as v43: ["read", "write"] (default) or ["read"]; the role
    # is DERIVED at read time (system_keys.role_for), never stored separately
    scopes: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)  # the owner who minted it
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

