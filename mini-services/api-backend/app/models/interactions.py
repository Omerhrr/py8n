"""Model classes: InteractionConversation, InteractionMessage, ChannelEndpoint, ChannelQueue, ChannelQueueEntry.

Split from the original app/models.py (task #3) - moved verbatim, no
behavior change.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ._shared import Base, JSONVariant, _now, _uuid

class InteractionConversation(Base):
    """A conversation (v68) - the interaction layer's unit of continuity.

    Channels (voice, whatsapp, telegram, discord, web, app, api, sms,
    email) are interchangeable ADAPTERS; the conversation is the thing
    that persists underneath them. A participant can move between
    channels mid-conversation (conversation_ref rebinds) and the same
    handler workflow, history and context carry over - one customer, one
    context, one AI, regardless of how they reached us.
    """

    __tablename__ = "interaction_conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # the channel the conversation STARTED on (per-message channels live on the messages)
    channel: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    participant_id: Mapped[str] = mapped_column(String(180), nullable=False, default="")
    participant_name: Mapped[str] = mapped_column(String(180), nullable=False, default="")
    # the workflow that answers inbound text (last node's output supplies the reply)
    handler_workflow_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="open", index=True)  # open|closed
    outcome: Mapped[str] = mapped_column(String(180), nullable=False, default="")
    context: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class InteractionMessage(Base):
    """One message inside a conversation (v68).

    ``role`` is user | agent | human_agent | system; ``channel`` records
    the adapter the message actually traveled through, so a transcript
    shows the channel hops (phone -> whatsapp -> app) without the business
    logic ever caring about them.
    """

    __tablename__ = "interaction_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    conversation_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # user|agent|human_agent|system
    channel: Mapped[str] = mapped_column(String(30), nullable=False, default="")
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    payload: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ChannelEndpoint(Base):
    """A channel endpoint (v69) - the REAL provider adapter surface.

    v68's universal ingress accepted already-normalized messages; v69 makes
    py8n itself the adapter: a ChannelEndpoint is a stored handle that turns
    a provider's NATIVE webhook (Meta Cloud API, Telegram Bot API, Discord
    interactions) into interaction-layer ingests, and knows the exact
    outbound request each provider's send API expects.

    One endpoint = one provider connection = one owner = one handler
    workflow. The provider-specific secrets (verify_token, app_secret,
    bot_token, secret_token, public_key) live in ``config`` and are always
    MASKED in API output - the raw values are only used at verification and
    delivery time. The webhook URLs are public (providers can't log in);
    each receiver verifies its provider's credentials before anything runs.
    """

    __tablename__ = "channel_endpoints"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(140), nullable=False)
    # provider id inside PROVIDER_ADAPTERS: meta_cloud_api | telegram_bot_api | discord_bot
    provider: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    # the interaction channel this provider delivers: whatsapp | telegram | discord
    channel: Mapped[str] = mapped_column(String(30), nullable=False)
    handler_workflow_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # provider-specific secrets + settings; masked in every API output
    config: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    # derived-at-write event counters (the transcript lives in interactions)
    events_received: Mapped[int] = mapped_column(Integer, default=0)
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ChannelQueue(Base):
    """A channel-side waiting room (v76) - queueing and waiting as a
    first-class primitive.

    When every agent is busy and the room is full, a live call does not
    get dropped into the void: it WAITS. The queue holds the caller in
    the session state machine's own on_hold state (the primitive that
    already existed - no new call states invented), keeps FIFO order with
    derived positions and wait times, and seats the head into a
    destination meeting (or just releases it back to the line for a
    human to take). Configuration only; the entries are the traffic.
    """

    __tablename__ = "channel_queues"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(140), nullable=False)
    # open | closed (a closed queue refuses new entries honestly)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="open", index=True)
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # the meeting a seated caller is attached to (optional destination)
    meeting_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    config: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ChannelQueueEntry(Base):
    """One call waiting in a channel queue (v76).

    status: waiting -> seated (the head got released/attached), or left
    (the caller or an operator took them out). ``session_id`` is the held
    VoiceSession (state on_hold while it waits); a session that ENDED
    while waiting (the caller hung up) is derived abandoned at read time
    - the row keeps its history honestly. Position and waited-seconds are
    derived from joined_at order, never stored.
    """

    __tablename__ = "channel_queue_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    queue_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    label: Mapped[str] = mapped_column(String(140), nullable=False, default="")
    address: Mapped[str] = mapped_column(String(180), nullable=False, default="")
    # waiting | seated | left
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="waiting", index=True)
    meta: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

