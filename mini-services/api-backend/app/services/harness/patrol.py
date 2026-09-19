"""The patrol service (v111) - the harness scheduling its OWN rounds.

Until v111 the harness was purely reactive: a human typed, the loop ran.
A patrol binds a MISSION to a SESSION and a RHYTHM, and the system fires
the rounds itself - the sweep walks the active patrols on
``patrol_tick_seconds`` and every due mission becomes a REAL harness
turn through the SAME ``start_turn`` path the doors use.

Nothing about the discipline changes when nobody is watching:

* a patrol round rides the same loop, the same guard rails (repeat /
  budget / wall clock) and the same FAIL-CLOSED gate - a patrol that
  wants to move the business parks a slip and waits, exactly like an
  interactive turn; silence expires it when a TTL is set,
* the receipt board on the patrol row is stamped BEFORE the round runs
  (last_run_at + run_count - the rhythm stays honest even across a
  crash) and the turn's terminal state lands after it,
* a round against a missing or inactive session is HELD, not counted -
  the run_count only counts real attempts.

The sweep never wedges the scheduler (the house rule): one patrol's
failure is a receipt, never an exception.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...models import HarnessPatrol, HarnessSession, HarnessTurn
from . import dispatch as dispatch_svc
from .service import HarnessError, start_turn

# the floor for a patrol rhythm - faster than this is not a patrol, it is
# a flood (the sweep tick itself is slower by default anyway)
MIN_INTERVAL_SECONDS = 5


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    # sqlite naive (v38 GOTCHA): stored timestamps come back without a
    # tzinfo - they ARE utc, say so before doing arithmetic on them
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

async def create_patrol(db: AsyncSession, **fields) -> HarnessPatrol:
    if fields.get("interval_seconds") is None:
        fields["interval_seconds"] = 3600
    fields["interval_seconds"] = max(MIN_INTERVAL_SECONDS,
                                     int(fields["interval_seconds"]))
    row = HarnessPatrol(**fields)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def list_patrols(db: AsyncSession, owner_id: str | None) -> list[HarnessPatrol]:
    q = select(HarnessPatrol).order_by(HarnessPatrol.created_at.desc())
    rows = (await db.execute(q)).scalars().all()
    if owner_id is not None:
        return [r for r in rows if r.owner_id in (owner_id, None)]
    return list(rows)


async def get_patrol(db: AsyncSession, patrol_id: str) -> HarnessPatrol:
    row = await db.get(HarnessPatrol, patrol_id)
    if row is None:
        raise HarnessError("Patrol not found", status_code=404)
    return row


async def list_patrol_turns(db: AsyncSession, patrol_id: str) -> list:
    """The patrol's rounds - the turns the SYSTEM fired, oldest first."""
    return list((await db.execute(
        select(HarnessTurn)
        .where(HarnessTurn.patrol_id == patrol_id)
        .order_by(HarnessTurn.created_at.asc()))).scalars().all())


# ---------------------------------------------------------------------------
# one round - the system speaking through the session
# ---------------------------------------------------------------------------

async def run_round(db: AsyncSession, patrol: HarnessPatrol,
                    *, now: datetime | None = None) -> dict[str, Any]:
    """Fire ONE patrol round: a real harness turn carrying the mission.

    The receipt is stamped before the run (rhythm honesty across a
    crash); the turn's terminal state lands on the row after. A missing
    or inactive session is HELD - recorded honestly, never counted as a
    run. Every failure mode ends as a receipt, never as an exception.
    """
    now = now or _utcnow()
    receipt: dict[str, Any] = {"patrol_id": patrol.id, "name": patrol.name,
                               "turn_id": None, "error": ""}

    session = await db.get(HarnessSession, patrol.session_id)
    if session is None:
        patrol.last_status = "failed"
        patrol.last_error = "the session behind this patrol is gone"
        await db.commit()
        await dispatch_svc.dispatch_round(db, patrol, error_text=patrol.last_error)
        receipt["status"] = "failed"
        receipt["error"] = patrol.last_error
        return receipt
    if session.is_active is False:
        # a pause is not a finding - held rounds never dispatch
        patrol.last_status = "held"
        patrol.last_error = "session is inactive - rounds held"
        await db.commit()
        receipt["status"] = "held"
        receipt["error"] = patrol.last_error
        return receipt

    # the rhythm stamp BEFORE the run: even a crash keeps the cadence
    patrol.run_count = (patrol.run_count or 0) + 1
    patrol.last_run_at = now
    patrol.last_error = ""
    await db.commit()

    try:
        turn = await start_turn(db, session, patrol.mission, patrol=patrol)
    except HarnessError as exc:
        patrol.last_status = "failed"
        patrol.last_error = str(exc)
        await db.commit()
        await dispatch_svc.dispatch_round(db, patrol, error_text=patrol.last_error)
        receipt["status"] = "failed"
        receipt["error"] = patrol.last_error
        return receipt
    except Exception as exc:  # noqa: BLE001 - a receipt, never a wedged sweep
        patrol.last_status = "failed"
        patrol.last_error = f"{type(exc).__name__}: {exc}"
        await db.commit()
        await dispatch_svc.dispatch_round(db, patrol, error_text=patrol.last_error)
        receipt["status"] = "failed"
        receipt["error"] = patrol.last_error
        return receipt

    patrol.last_status = turn.status
    patrol.last_run_turn_id = turn.id
    await db.commit()
    # v112: the receipt reached a terminal state - the outcome walks out
    # (a paused round waits for the decision; the decision mails instead)
    if turn.status in dispatch_svc.TERMINAL:
        await dispatch_svc.dispatch_round(db, patrol, turn)
    receipt.update({"status": turn.status, "turn_id": turn.id})
    return receipt


# ---------------------------------------------------------------------------
# the sweep - the system's own rhythm
# ---------------------------------------------------------------------------

async def run_due(db: AsyncSession, *,
                  now: datetime | None = None) -> dict[str, Any]:
    """Fire every ACTIVE patrol whose rhythm is due. Called by the
    scheduler tick (patrol_tick_seconds) and by tests with a controlled
    ``now``. One db session, sequential rounds - the loop's guard and
    the commit trail keep each round independent."""
    now = now or _utcnow()
    patrols = (await db.execute(
        select(HarnessPatrol).where(HarnessPatrol.is_active == True))).scalars().all()  # noqa: E712
    due: list[HarnessPatrol] = []
    for p in patrols:
        last = _aware(p.last_run_at)
        if last is None or \
                (now - last).total_seconds() >= max(
                    MIN_INTERVAL_SECONDS, int(p.interval_seconds or 0)):
            due.append(p)
    receipts = []
    for p in due:
        receipts.append(await run_round(db, p, now=now))
    return {"due": len(due), "active_patrols": len(patrols), "receipts": receipts}
