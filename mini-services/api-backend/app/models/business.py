"""Model classes: BusinessProcess, BusinessProcessInstance, BusinessProcessTransitionLog.

Split from the original app/models.py (task #3) - moved verbatim, no
behavior change.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ._shared import Base, JSONVariant, _now, _uuid

class BusinessProcess(Base):
    """A business state machine (v84) - the durable shape of a long-running
    operation.

    Where workflows are moments (trigger in, run, done), a business process
    is the shape of something that stays open for days or months: a lead
    (lead -> contacted -> interested -> demo -> proposal -> negotiating ->
    won), a support case, an insurance claim, an appointment, a delivery.
    The DEFINITION is the machine:

        definition = {
            "states": ["lead", "contacted", ...],
            "initial": "lead",
            "transitions": [{"name": "reach_out", "from": "lead",
                             "to": "contacted", "description": "..."}, ...],
        }

    Instances are the tracked entities that REMEMBER state (and context -
    the running memory: notes, scores, owners, anything JSON) across
    restarts, deploys and weeks. Every advance is on the record
    (BusinessProcessTransitionLog) and emits business.state_changed, so
    workflows and agents react to the business moving.
    """

    __tablename__ = "business_processes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(140), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    definition: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class BusinessProcessInstance(Base):
    """One tracked entity inside a business process (v84) - the memory.

    ``ref`` is the external key the business already uses (the lead's id,
    the caller's number, the case number). ``context`` is the running
    memory the team and the agents read and write: discovered facts, next
    steps, scores. ``state`` + ``entered_state_at`` are the present;
    the transition log is the past; ``due_at`` is the SLA promise the
    analytics derive stuck-ness from. Nothing about the journey is
    re-derived from anything else - the row IS the memory.
    """

    __tablename__ = "business_process_instances"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    process_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    ref: Mapped[str] = mapped_column(String(180), nullable=False, default="")
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    state: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    context: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    entered_state_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class BusinessProcessTransitionLog(Base):
    """Every advance on the record (v84) - the journey, audit-grade.

    The deliberate stored exception to derived-never-stored: a business's
    history IS the product (who moved what, when, why). Rows are written
    on start (from None -> initial) and on every accepted advance.
    """

    __tablename__ = "business_process_transitions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    process_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    instance_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    from_state: Mapped[str | None] = mapped_column(String(60), nullable=True)
    to_state: Mapped[str] = mapped_column(String(60), nullable=False)
    transition: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    actor: Mapped[str] = mapped_column(String(140), nullable=False, default="")
    note: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    payload: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

