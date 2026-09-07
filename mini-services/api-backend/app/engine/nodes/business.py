"""Business nodes (v85 + v86 + v87 + v88) - workflows run the business.

v84 made long-running entities first-class (BusinessProcess = the
machine, instances = the tracked entities that remember). These nodes are
how the rest of the platform READS, MOVES, STARTS, ANNOTATES and
ONBOARDS them:

* business_advance (v85): a workflow reacting to a real event (call.
  ended, sms.received, form submitted, schedule tick) advances the
  matching instance through the machine - the move is validated, on the
  record, and emits business.state_changed like any other advance.
* business_start (v86): the intake side - a workflow opens a NEW tracked
  instance (ref = the external key the business already tracks: the
  sender's phone, the case number, the order id). The machine watches
  the entity from the moment it exists; the escalation door inherits it.
* business_query (v87): the read side - workflows and agents pull the
  RUNNING entities into the flow (by process, state, ref, stuck-ness)
  and reason over the live operation.
* business_annotate (v88): the memory door - agents write facts DIRECTLY
  into an entity's running context (a budget number from a call, a new
  address from a text) without moving the machine; the write is on the
  record and emits business.annotated so workflows can react to a FACT
  landing (annotate -> the reactive advance the fact justifies).
* business_ack (v88): the receipt - a workflow (usually reacting to the
  handler's own reply: the "1" text, the "on it" email) acknowledges the
  instance's escalation episode; the door goes quiet for the stint.
* business_onboard (v88): the department's data on-ramp - a workflow
  reads a dataset (the spreadsheet that just landed, the App Builder
  rows the staff typed) and starts a tracked instance PER ROW at the
  row's own stage, idempotently (an open instance already carrying the
  ref skips). The machine watches the data, not just the channels.

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


class BusinessQueryNode(BaseNode):
    """Read the running business entities into the flow (v87)."""

    type = "business_query"
    name = "Business Query"
    description = (
        "Reads the running business entities (BusinessProcess instances) into the "
        "flow - filter by process (name or id), current state, external ref, or "
        "stuck-ness (past due_at while still open). Chain into an AI Agent node so "
        "the agent reasons over the live operation instead of a stale copy; the "
        "read side of the business_advance / business_start loop."
    )
    category = "actions"
    icon = "scan-search"
    color = "#818cf8"

    class ParamsModel(BaseModel):
        process: str = Field(default="", description="Process name or id (empty = all processes)")
        state: str = Field(default="", description="Only instances currently in this state")
        ref: str = Field(default="", description="Only instances with this external ref (exact match)")
        stuck_only: bool = Field(default=False, description="Only instances past their SLA (due_at passed, still open)")
        open_only: bool = Field(default=True, description="Exclude instances in terminal states (the journey is complete)")
        limit: int = Field(default=50, ge=1, le=500, description="Max instances returned (hard cap 500)")

    async def execute(self, context) -> NodeResult:
        from ...db import AsyncSessionLocal
        from ...services import business_processes as bp_svc

        p = self.params  # type: BusinessQueryNode.ParamsModel
        async with AsyncSessionLocal() as session:
            try:
                out = await bp_svc.query_instances(
                    session, owner_id=context.owner_id,
                    process=p.process, state=p.state, ref=p.ref,
                    stuck_only=p.stuck_only, open_only=p.open_only, limit=p.limit)
            except bp_svc.ProcessError as exc:
                raise NodeExecutionError(str(exc)) from exc
        return self._single(out)


class BusinessAnnotateNode(BaseNode):
    """Write facts directly into an entity's running memory (v88)."""

    type = "business_annotate"
    name = "Business Annotate"
    description = (
        "Writes facts DIRECTLY into a business process instance's running "
        "memory (context) - no state change, the machine untouched. This is "
        "the agents' memory door: what the agent learned mid-flight (a "
        "budget number from the call, a new address from the text, a "
        "sentiment from the transcript) lands on the entity the whole "
        "operation reads. On the record (an 'annotate' journey row) and "
        "emits business.annotated so workflows can react to a fact landing. "
        "The read side is business_query; the move side is business_advance."
    )
    category = "actions"
    icon = "pen-line"
    color = "#a78bfa"

    class ParamsModel(BaseModel):
        process: str = Field(default="", description="Process name (or id) - the machine whose entity remembers")
        instance_id: str = Field(default="", description="Direct instance id (wins over ref when set)")
        ref: str = Field(default="", description="The external key of the instance to annotate - the most recent OPEN instance carrying this ref")
        context_patch: dict = Field(default_factory=dict, description="The facts to merge into the entity's memory (required, non-empty)")
        note: str = Field(default="", description="Why these facts landed (journey log)")
        actor: str = Field(default="agent", description="Who annotated (journey log + event actor)")
        on_missing: str = Field(
            default="error",
            json_schema_extra={"widget": "select", "options": list(_ON_MISSING)},
            description="error = fail the run when no matching open instance exists; skip = continue with an honest miss",
        )

    async def execute(self, context) -> NodeResult:
        from ...db import AsyncSessionLocal
        from ...services import business_processes as bp_svc

        p = self.params  # type: BusinessAnnotateNode.ParamsModel
        process_ref = str(p.process or "").strip()
        if not process_ref:
            raise NodeExecutionError("a process name (or id) is required - which machine's entity remembers?")
        if not p.instance_id.strip() and not str(p.ref).strip():
            raise NodeExecutionError("name the instance: instance_id or ref (the external key the business tracks)")
        if not isinstance(p.context_patch, dict) or not p.context_patch:
            raise NodeExecutionError(
                "context_patch is required - the facts to remember "
                "(a non-empty object of {key: value})")
        if p.on_missing not in _ON_MISSING:
            raise NodeExecutionError(f"on_missing must be {'|'.join(_ON_MISSING)}")

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
                            "annotated": False, "skipped": True,
                            "reason": "no open instance carries this ref",
                            "process": proc.name, "ref": str(p.ref).strip(),
                        })
                    raise NodeExecutionError(
                        f"no open instance of {proc.name!r} carries ref "
                        f"{str(p.ref).strip()!r} - start one first "
                        "(business_start) or set on_missing=skip")
                instance_id = row.id
            try:
                out = await bp_svc.annotate_instance(
                    session, proc.id, instance_id, owner_id=context.owner_id,
                    context_patch=dict(p.context_patch or {}),
                    actor=p.actor or "agent", note=p.note)
                await session.commit()  # nodes own their sessions (the dataset_write rule)
            except bp_svc.ProcessError as exc:
                raise NodeExecutionError(str(exc)) from exc
        return self._single({
            "annotated": True,
            "instance_id": out["id"],
            "ref": out["ref"],
            "state": out["state"],
            "keys": sorted(str(k) for k in (p.context_patch or {})),
            "context": out["context"],
            "process": proc.name,
        })


class BusinessAckNode(BaseNode):
    """Acknowledge an escalation episode from a workflow (v88)."""

    type = "business_ack"
    name = "Business Acknowledge"
    description = (
        "Acknowledges an instance's escalation episode - the human's receipt "
        "taken by a workflow. Usually reacts to the handler's own reply (the "
        "'1' text, the 'on it' email): the workflow matches the replier to "
        "the entity, acks, and the escalation door goes quiet for the rest "
        "of that state stint. On the record (an 'escalation_acknowledged' "
        "journey row naming who) and emits business.escalation_acknowledged. "
        "The door must have knocked first - acknowledging silence refuses "
        "loudly (or skips honestly with on_missing=skip)."
    )
    category = "actions"
    icon = "check-check"
    color = "#34d399"

    class ParamsModel(BaseModel):
        process: str = Field(default="", description="Process name (or id) - the machine whose escalation is acknowledged")
        instance_id: str = Field(default="", description="Direct instance id (wins over ref when set)")
        ref: str = Field(default="", description="The external key of the instance to acknowledge - the most recent OPEN instance carrying this ref")
        by: str = Field(default="workflow", description="Who acknowledged (the receipt's name - journey log + event actor)")
        note: str = Field(default="", description="The handler's own words (journey log)")
        snooze_hours: float | None = Field(default=None, ge=0, description=(
            "v89: hold the door quiet for N hours, then it re-knocks on its "
            "cadence (omitted = the take owns the rest of the state stint)"))
        on_missing: str = Field(
            default="error",
            json_schema_extra={"widget": "select", "options": list(_ON_MISSING)},
            description="error = fail the run when nothing matches or nothing escalated; skip = continue with an honest miss",
        )

    async def execute(self, context) -> NodeResult:
        from ...db import AsyncSessionLocal
        from ...services import business_processes as bp_svc

        p = self.params  # type: BusinessAckNode.ParamsModel
        process_ref = str(p.process or "").strip()
        if not process_ref:
            raise NodeExecutionError("a process name (or id) is required - which machine's escalation?")
        if not p.instance_id.strip() and not str(p.ref).strip():
            raise NodeExecutionError("name the instance: instance_id or ref (the external key the business tracks)")
        if p.on_missing not in _ON_MISSING:
            raise NodeExecutionError(f"on_missing must be {'|'.join(_ON_MISSING)}")

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
                            "acknowledged": False, "skipped": True,
                            "reason": "no open instance carries this ref",
                            "process": proc.name, "ref": str(p.ref).strip(),
                        })
                    raise NodeExecutionError(
                        f"no open instance of {proc.name!r} carries ref "
                        f"{str(p.ref).strip()!r} - nothing to acknowledge")
                instance_id = row.id
            try:
                out = await bp_svc.acknowledge_escalation(
                    session, proc.id, instance_id, owner_id=context.owner_id,
                    by=p.by or "workflow", note=p.note,
                    snooze_hours=p.snooze_hours)
                await session.commit()  # nodes own their sessions (the dataset_write rule)
            except bp_svc.ProcessError as exc:
                if p.on_missing == "skip" and "no escalation episode" in str(exc):
                    return self._single({
                        "acknowledged": False, "skipped": True,
                        "reason": str(exc),
                        "process": proc.name, "instance_id": instance_id,
                    })
                raise NodeExecutionError(str(exc)) from exc
        return self._single({
            "acknowledged": True,
            "instance_id": out["instance"]["id"],
            "ref": out["instance"]["ref"],
            "state": out["instance"]["state"],
            "acknowledged_by": out["ack"]["by"],
            "snooze_until": out["ack"].get("snooze_until"),
            "attempt": ((out["instance"].get("context") or {})
                        .get("escalations", {}).get("count")),
            "process": proc.name,
        })


class BusinessOnboardNode(BaseNode):
    """Bulk-onboard a dataset's rows as tracked instances (v88)."""

    type = "business_onboard"
    name = "Business Onboard"
    description = (
        "Starts a tracked business-process instance PER ROW of a dataset - "
        "the department's data on-ramp. The spreadsheet that just landed "
        "(CSV upload, API append, App Builder rows) flows into the machine: "
        "each row's ref_column value becomes the external ref, an optional "
        "state_column names the row's own stage (the department's data "
        "arrives AS IT IS, not rewound to the start), title_columns build "
        "the human title. IDEMPOTENT by design: an open instance already "
        "carrying the ref skips (on_duplicate=skip) - re-runs and "
        "dataset-trigger fires never double-track. Rows without a ref are "
        "counted honestly, not silently dropped."
    )
    category = "actions"
    icon = "users"
    color = "#38bdf8"

    class ParamsModel(BaseModel):
        process: str = Field(default="", description="Process name (or id) - the machine that will track the rows")
        dataset: str = Field(default="", description="Dataset name (or id) to onboard from")
        ref_column: str = Field(default="", description="Column carrying the external key (phone, case number, order id)")
        state_column: str = Field(default="", description="Optional column naming each row's own stage (must be a state of the machine)")
        title_columns: list[str] = Field(default_factory=list, description="Columns joined into the human title (e.g. [name, company])")
        due_in_seconds: int | None = Field(default=None, ge=1, description="The SLA promise set on every started instance")
        limit: int = Field(default=200, ge=1, le=500, description="Max rows scanned per run (most recent first, hard cap 500)")
        actor: str = Field(default="onboarding", description="Who onboarded (journey log)")
        on_duplicate: str = Field(
            default="skip",
            json_schema_extra={"widget": "select", "options": list(_ON_MISSING)},
            description="skip = an open instance already carrying the ref continues honestly (idempotent re-runs); error = fail the run loud",
        )

    async def execute(self, context) -> NodeResult:
        from ...db import AsyncSessionLocal
        from ...services import business_processes as bp_svc
        from ...services import datasets as ds_svc

        p = self.params  # type: BusinessOnboardNode.ParamsModel
        process_ref = str(p.process or "").strip()
        ds_ref = str(p.dataset or "").strip()
        ref_col = str(p.ref_column or "").strip()
        if not process_ref:
            raise NodeExecutionError("a process name (or id) is required - which machine tracks?")
        if not ds_ref:
            raise NodeExecutionError("a dataset is required - which data onboards?")
        if not ref_col:
            raise NodeExecutionError("a ref_column is required - the column carrying the external key")
        if p.on_duplicate not in _ON_MISSING:
            raise NodeExecutionError(f"on_duplicate must be {'|'.join(_ON_MISSING)}")

        async with AsyncSessionLocal() as session:
            proc = await _resolve_process(session, process_ref, context.owner_id)
            if proc is None:
                raise NodeExecutionError(f"Process {process_ref!r} not found")
            ds = await ds_svc.get_dataset(session, ds_ref, context.owner_id)
            if ds is None:
                raise NodeExecutionError(f"Dataset {ds_ref!r} not found")
            if ds.row_count:
                df = ds_svc.read_parquet_df(ds_svc.parquet_path(ds.id))
                rows = ds_svc.jsonable_rows(df)
            else:
                rows = []
            # most recent first - the rows that just landed onboard first
            scanned = list(reversed(rows))[: max(1, min(int(p.limit), 500))]

            definition = proc.definition or {}
            states = definition.get("states") or []
            started: list[dict] = []
            refused: list[dict] = []
            already: list[dict] = []
            no_ref = 0
            try:
                for row in scanned:
                    if not isinstance(row, dict):
                        continue
                    ref = str(row.get(ref_col) or "").strip()
                    if not ref:
                        no_ref += 1
                        continue
                    existing = await _open_instance_by_ref(session, proc.id, ref)
                    if existing is not None:
                        if p.on_duplicate == "error":
                            raise NodeExecutionError(
                                f"an open instance of {proc.name!r} already "
                                f"carries ref {ref!r} (on_duplicate=error)")
                        already.append({"ref": ref, "instance_id": existing.id,
                                        "state": existing.state})
                        continue
                    title = " - ".join(str(row.get(c) or "").strip()
                                       for c in (p.title_columns or [])
                                       if str(row.get(c) or "").strip())
                    begin = (str(row.get(p.state_column) or "").strip()
                             if p.state_column else "") or None
                    if begin is not None and begin not in states:
                        # one bad row must not fail the batch - the row is
                        # refused BY NAME, the rest onboard
                        refused.append({"ref": ref,
                                        "reason": f"stage {begin!r} is not a "
                                                  f"state of this machine"})
                        continue
                    out = await bp_svc.start_instance(
                        session, proc.id, owner_id=context.owner_id, ref=ref,
                        title=title[:200], context=dict(row),
                        due_in_seconds=p.due_in_seconds,
                        state=begin, actor=p.actor or "onboarding")
                    started.append({"id": out["id"], "ref": out["ref"],
                                    "state": out["state"], "title": out["title"]})
                await session.commit()  # nodes own their sessions (the dataset_write rule)
            except bp_svc.ProcessError as exc:
                raise NodeExecutionError(str(exc)) from exc
        return self._single({
            "process": proc.name, "dataset": ds.name,
            "rows_scanned": len(scanned),
            "started": len(started), "instances": started[:50],
            "already_tracked": len(already),
            "no_ref_rows": no_ref,
            "refused": refused,
        })
