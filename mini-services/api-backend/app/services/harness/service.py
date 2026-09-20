"""The harness service (v109) - sessions, turns, and the fail-closed gate.

Everything the API doors need lives here:

* session CRUD (owner-scoped by the doors, the house discipline),
* ``start_turn`` - one user message becomes a persistent turn driven by
  loop.drive until it completes, exhausts, or pauses at the approval gate,
* ``decide`` - the human's answer to a slip: APPROVE runs the sensitive
  tool NOW (at decision time - the move happens when the human said yes,
  not when the model asked) and resumes the loop with the real result;
  REJECT feeds the refusal back so the model can answer gracefully,
* the fail-closed sweep - a slip left pending past
  ``harness_approval_ttl_seconds`` expires and its turn REFUSES: silence
  is never consent. (ttl 0 = slips never expire; the default for installs
  that decide on their own rhythm.)

The turn state machine survives restarts: resume rebuilds from
``turn.wire`` + the decision, never from live memory.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import settings
from ...models import HarnessApproval, HarnessPatrol, HarnessSession, HarnessTurn
from . import tools as harness_tools
from . import loop as harness_loop
from . import dispatch as dispatch_svc
from .guard import TurnGuard


class HarnessError(Exception):
    """A harness problem the API answers with 400/404/409 - never a 500."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def build_guard() -> TurnGuard:
    return TurnGuard(
        repeat_limit=int(settings.harness_guard_repeat_limit),
        max_iterations=int(settings.harness_max_iterations),
        timeout_seconds=float(settings.harness_turn_timeout_seconds),
    )


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------

async def create_session(db: AsyncSession, **fields) -> HarnessSession:
    row = HarnessSession(**fields)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def list_sessions(db: AsyncSession, owner_id: str | None) -> list[HarnessSession]:
    q = select(HarnessSession).order_by(HarnessSession.updated_at.desc())
    rows = (await db.execute(q)).scalars().all()
    if owner_id is not None:
        return [r for r in rows if r.owner_id in (owner_id, None)]
    return list(rows)


async def get_session(db: AsyncSession, session_id: str) -> HarnessSession:
    row = await db.get(HarnessSession, session_id)
    if row is None:
        raise HarnessError("Harness session not found", status_code=404)
    return row


# ---------------------------------------------------------------------------
# turns - the state machine
# ---------------------------------------------------------------------------

async def start_turn(db: AsyncSession, session: HarnessSession,
                     message: str, *, patrol: HarnessPatrol | None = None) -> HarnessTurn:
    """One message becomes a persistent turn driven by loop.drive until it
    completes, exhausts, or pauses at the approval gate.

    ``patrol`` (v111) marks the round as SYSTEM-fired: the turn carries
    the patrol id and a patrol_start frame, so the transcript and the
    patrol receipt board tell human rounds from autonomous ones. The
    discipline is identical - same loop, same guard, same gate."""
    if not (message or "").strip():
        raise HarnessError("message must not be empty")
    if session.is_active is False:
        raise HarnessError("session is inactive - activate it to run",
                           status_code=409)

    registry = harness_tools.build_registry()
    turn = HarnessTurn(session_id=session.id, owner_id=session.owner_id,
                       user_message=message.strip(),
                       patrol_id=patrol.id if patrol is not None else None)
    if patrol is not None:
        harness_loop._frame(turn, {"event": "patrol_start",  # noqa: SLF001
                                   "patrol_id": patrol.id,
                                   "patrol": patrol.name})
    db.add(turn)
    await db.flush()

    history: list[dict] = []
    if (session.memory or "none") == "buffer":
        completed = (await db.execute(
            select(HarnessTurn)
            .where(HarnessTurn.session_id == session.id,
                   HarnessTurn.status == "completed")
            .order_by(HarnessTurn.created_at.asc()))).scalars().all()
        history = harness_loop.history_messages(completed, session.max_history_turns)

    messages = [
        harness_loop._system_message(session, registry),  # noqa: SLF001 - same package
        *history,
        {"role": "user", "content": turn.user_message},
    ]
    await db.commit()
    try:
        return await harness_loop.drive(db, session, turn, messages, registry, build_guard())
    except Exception as exc:  # noqa: BLE001 - the turn records its own death
        turn.status = "failed"
        turn.error = f"drive failed: {type(exc).__name__}: {exc}"
        harness_loop._frame(turn, {"event": "guard_stop", "kind": "drive",  # noqa: SLF001
                                   "reason": turn.error})
        await db.commit()
        raise HarnessError(turn.error, status_code=502) from exc


async def list_turns(db: AsyncSession, session_id: str) -> list[HarnessTurn]:
    return list((await db.execute(
        select(HarnessTurn)
        .where(HarnessTurn.session_id == session_id)
        .order_by(HarnessTurn.created_at.asc()))).scalars().all())


async def get_turn(db: AsyncSession, turn_id: str) -> HarnessTurn:
    row = await db.get(HarnessTurn, turn_id)
    if row is None:
        raise HarnessError("Harness turn not found", status_code=404)
    return row


# ---------------------------------------------------------------------------
# the fail-closed gate
# ---------------------------------------------------------------------------

def _expiry_deadline() -> datetime | None:
    ttl = int(settings.harness_approval_ttl_seconds or 0)
    if ttl <= 0:
        return None
    return _utcnow() - timedelta(seconds=ttl)


async def sweep_expired(db: AsyncSession, owner_id: str | None) -> int:
    """Expire pending slips past the TTL and refuse their turns (fail-closed).

    A turn may hold several slips over its life, but at most ONE pending at
    a time (the loop pauses on the first) - so expiring one slip refuses
    exactly its paused turn."""
    deadline = _expiry_deadline()
    if deadline is None:
        return 0
    q = select(HarnessApproval).where(HarnessApproval.status == "pending",
                                      HarnessApproval.created_at < deadline)
    if owner_id is not None:
        q = q.where(HarnessApproval.owner_id.in_((owner_id, None)))
    slips = (await db.execute(q)).scalars().all()
    for slip in slips:
        slip.status = "expired"
        slip.decided_at = _utcnow()
        turn = await db.get(HarnessTurn, slip.turn_id)
        if turn is not None and turn.status == "waiting_approval":
            turn.status = "refused"
            turn.error = ("approval expired unanswered - the harness refuses "
                          "on silence (fail-closed)")
            harness_loop._frame(turn, {  # noqa: SLF001
                "event": "approval_expired", "approval_id": slip.id,
                "tool": slip.tool})
            # v112: the receipt board must not keep saying waiting_approval
            # after silence refused the round - and the refusal walks out
            # with the rest (fail-closed outcomes are findings too)
            if turn.patrol_id:
                patrol = await db.get(HarnessPatrol, turn.patrol_id)
                if patrol is not None:
                    patrol.last_status = turn.status
                    patrol.last_run_turn_id = turn.id
                    await dispatch_svc.dispatch_round(db, patrol, turn)
    if slips:
        await db.commit()
    return len(slips)


async def list_approvals(db: AsyncSession, owner_id: str | None,
                         status: str | None = None) -> list[HarnessApproval]:
    await sweep_expired(db, owner_id)
    q = select(HarnessApproval).order_by(HarnessApproval.created_at.desc())
    if status:
        q = q.where(HarnessApproval.status == status)
    rows = (await db.execute(q)).scalars().all()
    if owner_id is not None:
        return [r for r in rows if r.owner_id in (owner_id, None)]
    return list(rows)


async def get_approval(db: AsyncSession, approval_id: str) -> HarnessApproval:
    row = await db.get(HarnessApproval, approval_id)
    if row is None:
        raise HarnessError("Approval not found", status_code=404)
    return row


async def decide(db: AsyncSession, slip: HarnessApproval, *,
                 approve: bool, decided_by: str | None = None) -> HarnessTurn:
    """The human's answer. Approve runs the tool NOW and resumes the loop;
    reject feeds the refusal to the model. Deciding a non-pending slip is a
    loud 409 - one decision, one run. ``decided_by`` (v110) stamps the
    receipt: the slip remembers WHO decided, not only that it was decided.
    """
    await sweep_expired(db, slip.owner_id)
    await db.refresh(slip)
    if slip.status != "pending":
        raise HarnessError(f"approval is already {slip.status}", status_code=409)

    turn = await db.get(HarnessTurn, slip.turn_id)
    if turn is None:
        raise HarnessError("the turn behind this approval is gone", status_code=404)

    slip.status = "approved" if approve else "rejected"
    slip.decided_at = _utcnow()
    slip.decided_by = decided_by

    if not approve:
        harness_loop._frame(turn, {"event": "approval_decided",  # noqa: SLF001
                                   "approval_id": slip.id, "decision": "rejected",
                                   "decided_by": decided_by})
        result_text = ("DECLINED by the human operator - the move was not "
                       "made. Do not retry it; answer with what you know.")
    else:
        registry = harness_tools.build_registry()
        tool = registry[slip.tool]
        outcome = await harness_loop._execute_tool(  # noqa: SLF001
            db, registry, tool, dict(slip.arguments or {}), turn.owner_id)
        result_text = outcome.text
        turn.tool_calls = [*(turn.tool_calls or []),
                           {"tool": slip.tool, "arguments": dict(slip.arguments or {}),
                            "status": outcome.status, "result": outcome.text}]
        harness_loop._frame(turn, {"event": "approval_decided",  # noqa: SLF001
                                   "approval_id": slip.id, "decision": "approved",
                                   "tool": slip.tool, "status": outcome.status,
                                   "decided_by": decided_by})

    messages = [*(turn.wire or []),
                {"role": "user", "content": f"TOOL RESULT {slip.tool}: {result_text}"}]
    turn.status = "running"
    turn.wire = []
    session = await db.get(HarnessSession, turn.session_id)
    if session is None:
        raise HarnessError("the session behind this turn is gone", status_code=404)
    await db.commit()
    try:
        resumed = await harness_loop.drive(db, session, turn, messages,
                                           harness_tools.build_registry(), build_guard())
        await _refresh_patrol_receipt(db, resumed)
        return resumed
    except Exception as exc:  # noqa: BLE001 - the turn records its own death
        turn.status = "failed"
        turn.error = f"resume failed: {type(exc).__name__}: {exc}"
        harness_loop._frame(turn, {"event": "guard_stop", "kind": "resume",
                                   "reason": turn.error})  # noqa: SLF001
        await db.commit()
        await _refresh_patrol_receipt(db, turn)
        raise HarnessError(turn.error, status_code=502) from exc


async def _refresh_patrol_receipt(db: AsyncSession, turn: HarnessTurn) -> None:
    """v111: when the turn belongs to a patrol, the decision's outcome
    lands on the patrol's receipt board too - the round ended where the
    human's answer left it (completed, refused, waiting again).
    v112: a terminal outcome also walks OUT - the gated round's answer
    mails home exactly when the decision made it real."""
    if not turn.patrol_id:
        return
    patrol = await db.get(HarnessPatrol, turn.patrol_id)
    if patrol is not None:
        patrol.last_status = turn.status
        patrol.last_run_turn_id = turn.id
        await db.commit()
        if turn.status in dispatch_svc.TERMINAL:
            await dispatch_svc.dispatch_round(db, patrol, turn)


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------

async def harness_health(db: AsyncSession) -> dict:
    sessions = len((await db.execute(select(HarnessSession))).scalars().all())
    turns = len((await db.execute(select(HarnessTurn))).scalars().all())
    pending = len((await db.execute(
        select(HarnessApproval).where(HarnessApproval.status == "pending"))).scalars().all())
    patrols = (await db.execute(select(HarnessPatrol))).scalars().all()
    return {
        "ok": True,
        "sessions": sessions,
        "turns": turns,
        "pending_approvals": pending,
        "patrols": len(patrols),
        "active_patrols": len([p for p in patrols if p.is_active]),
        "guard": {
            "max_iterations": int(settings.harness_max_iterations),
            "repeat_limit": int(settings.harness_guard_repeat_limit),
            "turn_timeout_seconds": int(settings.harness_turn_timeout_seconds),
            "approval_ttl_seconds": int(settings.harness_approval_ttl_seconds),
            "patrol_tick_seconds": int(settings.patrol_tick_seconds),
        },
    }
