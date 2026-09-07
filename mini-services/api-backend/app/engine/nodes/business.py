"""Business nodes (v85 + v86) - workflows move the business state machine.

v84 made long-running entities first-class (BusinessProcess = the
machine, instances = the tracked entities that remember). These nodes are
how the rest of the platform MOVES and STARTS them:

* business_advance (v85): a workflow reacting to a real event (call.
  ended, sms.received, form submitted, schedule tick) advances the
  matching instance through the machine - the move is validated, on the
  record, and emits business.state_changed like any other advance.
* business_start (v86): the intake side - a workflow opens a NEW tracked
  instance (ref = the external key the business already tracks: the
  sender's phone, the case number, the order id). The machine watches
  the entity from the moment it exists; the escalation door inherits it.

The killer wiring is ref-based: the workflow names the process and the
external key it already tracks (the caller's number from the event's
actor, the case number from a form field, the lead id from a dataset
row); the advance node finds the OPEN instance carrying that ref and
takes the machine's move. Unknown refs skip honestly when
on_missing=skip - a call from someone not in the pipeline must not fail
the run; start never doubles an entity that is already tracked when
on_duplicate=skip.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .base import BaseNode, NodeExecutionError

_ON_MISSING = ("error", "skip")


class BusinessAdvanceNode(BaseNode):
    """Advance a business process instance (v84 machines) from a workflow."""

    type = "business_advance"
    name = "Business Advance"
    description = (
        "Moves a business process instance through its state machine - by "
        "instance id, or by ref within a process (the external key the "
        "business already tracks: phone, case number, lead id). The machine "
        "validates the move, the journey records it, and business."
        "state_changed fires so other workflows can react."
    )
    category = "actions"
    icon = "git-branch"
    color = "#0ea5e9"

    class ParamsModel(BaseModel):
        process: str = Field(default="", description="Process name (or id) - the machine to move")
        instance_id: str = Field(default="", description="Direct instance id (wins over ref when set)")
        ref: str = Field(default="", description="The external key of the instance to move - the most recent OPEN instance carrying this ref")
        transition: str = Field(default="", description="Move by transition name (e.g. 'reach_out')")
        to_state: str = Field(default="", description="...or move by target state (used when transition is empty)")
        note: str = Field(default="", description="Why the move happened - lands on the journey log")
        context_patch: dict = Field(default_factory=dict, description="Facts to merge into the instance's running memory")
        actor: str = Field(default="workflow", description="Who moved it (journey log + event actor)")
        on_missing: str = Field(
            default="error",
            json_schema_extra={"widget": "select", "options": list(_ON_MISSING)},
            description="error = fail the run when no matching open instance exists; skip = continue with an honest miss",
        )
        on_refusal: str = Field(
            default="error",
            json_schema_extra={"widget": "select", "options": list(_ON_MISSING)},
            description="error = fail the run when the machine refuses the move (not allowed from the current state, terminal); skip = continue honestly - an event that arrives after the journey moved on must not fail",
        )

    async def execute(self, context) -> NodeResult:
        from ...db import AsyncSessionLocal
        from ...services import business_processes as bp_svc
        from ...services.business_processes import ProcessError

        p = self.params  # type: BusinessAdvanceNode.ParamsModel
        process_ref = str(p.process or "").strip()
        if not process_ref:
            raise NodeExecutionError("a process name (or id) is required - which machine moves?")
        if not p.instance_id.strip() and not str(p.ref).strip():
            raise NodeExecutionError("name the instance: instance_id or ref (the external key the business tracks)")
        if not p.transition.strip() and not p.to_state.strip():
            raise NodeExecutionError("name the move: transition or to_state")
        if p.on_missing not in _ON_MISSING:
            raise NodeExecutionError(f"on_missing must be {'|'.join(_ON_MISSING)}")
        if p.on_refusal not in _ON_MISSING:
            raise NodeExecutionError(f"on_refusal must be {'|'.join(_ON_MISSING)}")

        async with AsyncSessionLocal() as session:
            proc = await _resolve_process(session, process_ref, context.owner_id)
            if proc is None:
                raise NodeExecutionError(f"Process {process_ref!r} not found")
            instance_id = p.instance_id.strip()
            if not instance_id:
                row = await _open_instance_by_ref(session, proc.id, str(p.ref).strip())
                if row is None:
                    if p.on_missing == "skip":
                        return self._single({
                            "moved": False, "skipped": True,
                            "reason": "no open instance carries this ref",
                            "process": proc.name, "ref": str(p.ref).strip(),
                        })
                    raise NodeExecutionError(
                        f"no open instance of {proc.name!r} carries ref "
                        f"{str(p.ref).strip()!r} - start one first "
                        f"(POST /processes/{proc.id}/instances) or set on_missing=skip")
                instance_id = row.id
            try:
                out = await bp_svc.advance_instance(
                    session, instance_id, owner_id=context.owner_id,
                    transition=p.transition.strip() or None,
                    to_state=p.to_state.strip() or None,
                    actor=p.actor or "workflow", note=p.note,
                    context_patch=dict(p.context_patch or {}))
                await session.commit()  # nodes own their sessions (the dataset_write rule)
            except ProcessError as exc:
                if p.on_refusal == "skip":
                    return self._single({
                        "moved": False, "skipped": True,
                        "reason": f"the machine refused the move: {exc}",
                        "process": proc.name,
                    })
                raise NodeExecutionError(str(exc)) from exc
        return self._single({
            "moved": True,
            "instance_id": out["id"],
            "ref": out["ref"],
            "from": (out.get("journey") or [{}])[-1].get("from_state") if out.get("journey") else None,
            "to": out["state"],
            "state": out["state"],
            "transition": (out.get("journey") or [{}])[-1].get("transition") if out.get("journey") else None,
            "is_terminal": out["is_terminal"],
            "process": proc.name,
        })


async def _resolve_process(session, ref: str, owner_id: str | None):
    """id first, then case-insensitive name - the dataset resolution rule."""
    from sqlalchemy import func, select

    from ...models import BusinessProcess

    row = (await session.execute(
        select(BusinessProcess).where(BusinessProcess.id == ref))).scalar_one_or_none()
    if row is not None:
        if owner_id is not None and row.owner_id not in (None, owner_id):
            return None
        return row
    row = (await session.execute(
        select(BusinessProcess).where(
            func.lower(BusinessProcess.name) == ref.lower()))).scalar_one_or_none()
    if row is not None and owner_id is not None and row.owner_id not in (None, owner_id):
        return None
    return row


async def _open_instance_by_ref(session, process_id: str, ref: str):
    """The most recent OPEN instance carrying the ref (open = not ended)."""
    from sqlalchemy import select

    from ...models import BusinessProcessInstance

    if not ref:
        return None
    q = (select(BusinessProcessInstance)
         .where(BusinessProcessInstance.process_id == process_id,
                BusinessProcessInstance.ref == ref,
                BusinessProcessInstance.ended_at.is_(None))
         .order_by(BusinessProcessInstance.entered_state_at.desc())
         .limit(1))
    return (await session.execute(q)).scalar_one_or_none()


class BusinessStartNode(BaseNode):
    """Start a tracked instance of a business process machine (v86)."""

    type = "business_start"
    name = "Business Start"
    description = (
        "Starts a NEW tracked instance of a business process - the intake "
        "wiring for operators: an inbound text, call or form opens the "
        "entity the business tracks (ref = the external key: the sender's "
        "phone, the case number, the order id) and the machine watches it "
        "from the moment it exists. An open instance already carrying the "
        "ref is not doubled: on_duplicate=skip continues honestly."
    )
    category = "actions"
    icon = "flag"
    color = "#6366f1"

    class ParamsModel(BaseModel):
        process: str = Field(default="", description="Process name (or id) - the machine that will track the entity")
        ref: str = Field(default="", description="The external key of the new entity (required) - the phone, case number, order id the business already speaks in")
        title: str = Field(default="", description="Human title for the tracked entity")
        context: dict = Field(default_factory=dict, description="Starting memory for the instance (facts the machine should remember)")
        due_in_seconds: int | None = Field(default=None, description="The SLA promise - when the escalation door should first knock")
        actor: str = Field(default="workflow", description="Who started it (journey log)")
        on_duplicate: str = Field(
            default="error",
            json_schema_extra={"widget": "select", "options": list(_ON_MISSING)},
            description="error = fail the run when an open instance already carries this ref; skip = continue honestly - the entity is already tracked",
        )

    async def execute(self, context) -> NodeResult:
        from ...db import AsyncSessionLocal
        from ...services import business_processes as bp_svc
        from ...services.business_processes import ProcessError

        p = self.params  # type: BusinessStartNode.ParamsModel
        process_ref = str(p.process or "").strip()
        if not process_ref:
            raise NodeExecutionError("a process name (or id) is required - which machine tracks?")
        ref = str(p.ref or "").strip()
        if not ref:
            raise NodeExecutionError(
                "a ref is required - the external key the business tracks "
                "(phone, case number, order id)")
        if p.on_duplicate not in _ON_MISSING:
            raise NodeExecutionError(f"on_duplicate must be {'|'.join(_ON_MISSING)}")

        async with AsyncSessionLocal() as session:
            proc = await _resolve_process(session, process_ref, context.owner_id)
            if proc is None:
                raise NodeExecutionError(f"Process {process_ref!r} not found")
            existing = await _open_instance_by_ref(session, proc.id, ref)
            if existing is not None:
                if p.on_duplicate == "skip":
                    return self._single({
                        "started": False, "skipped": True,
                        "reason": "an open instance already carries this ref",
                        "process": proc.name, "ref": ref,
                        "instance_id": existing.id, "state": existing.state,
                    })
                raise NodeExecutionError(
                    f"an open instance of {proc.name!r} already carries ref "
                    f"{ref!r} - the entity is already tracked (set "
                    "on_duplicate=skip to pass honestly)")
            try:
                out = await bp_svc.start_instance(
                    session, proc.id, owner_id=context.owner_id, ref=ref,
                    title=str(p.title or "")[:200],
                    context=dict(p.context or {}),
                    due_in_seconds=p.due_in_seconds,
                    actor=p.actor or "workflow")
                await session.commit()  # nodes own their sessions (the dataset_write rule)
            except ProcessError as exc:
                raise NodeExecutionError(str(exc)) from exc
        return self._single({
            "started": True,
            "instance_id": out["id"],
            "ref": out["ref"],
            "state": out["state"],
            "process": proc.name,
            "is_terminal": out["is_terminal"],
        })
