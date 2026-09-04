"""Rate-shaping and quotas on serving tokens (v69 -> v70: cross-process).

v68 gave deployments credentials (serving tokens); v69 gave those tokens
TRAFFIC POLICY: ``rate_per_min`` (sliding 60-second window) and
``daily_quota`` (UTC calendar day). Enforcement happens right after token
auth succeeds on the serving webhook and the SSE stream endpoint - a
shape-limited request gets 429 with ``Retry-After`` and the
``X-RateLimit-Limit`` / ``X-RateLimit-Remaining`` headers; an exhausted
quota gets 429 naming the next UTC midnight. A token without a policy
stays unlimited - shaping is opt-in per token, exactly like auth is
opt-in per deployment.

v70 moved the COUNTERS. v69 kept them as in-process deques - correct
arithmetic inside one worker, but N uvicorn workers (or two boxes behind
a balancer) meant N independent allowances: a 10/min token under 4
workers admitted up to 40. The counters now live in
``deployment_token_hits`` (one row per admitted request, keyed by token
id) - the SAME database every process already shares, so all workers
enforce ONE limit. This is the deliberate exception to "derived never
stored": limits are state about TRAFFIC, not a derivation from the
estate, and they must survive process boundaries to mean anything.

HOW THE WINDOW STAYS EXACT (the insert-first pattern):

1. INSERT the request's own hit row FIRST - on SQLite this takes the
   database's single writer lock, so every concurrent admit (in this or
   any other process) serializes behind it;
2. COUNT the window inside the same transaction - it sees every hit
   committed before the write lock was granted plus the row just
   inserted, which under SQLite's one-writer-at-a-time discipline is the
   exact, race-free total;
3. if the policy is exceeded, DELETE the just-inserted row, commit the
   deletion, and raise ``LimitExceeded`` (the refused request is not
   traffic - the v69 semantics kept).

On a multi-writer SQL backend the same shape wants
``SELECT ... FOR UPDATE`` / ``BEGIN IMMEDIATE``; SQLite's writer lock
provides it for free. Under pathological contention a write can queue
past the sqlite busy timeout - the honest scaling note is that a token
hot enough to queue writers has outgrown row-per-hit storage and should
move to a counter store; the grammar (429 + headers) would not change.

Rows older than two days are pruned opportunistically on every admit, so
the table stays tiny without a sweeper job. Admitted requests only:
refused requests are answered 429 but their (rolled back) row never
commits.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import AsyncSessionLocal
from ..models import DeploymentTokenHit, DeploymentTokenPolicy

_WINDOW_SECONDS = 60.0
_HIT_RETENTION = timedelta(days=2)  # hits older than this are pruned on admit


class LimitExceeded(Exception):
    """A request was refused by the token's policy -> HTTP 429."""

    def __init__(self, detail: str, retry_after: int, headers: dict[str, str]):
        super().__init__(detail)
        self.detail = detail
        self.retry_after = retry_after
        self.headers = {"Retry-After": str(retry_after), **headers}


def _day_key(at: datetime | None = None) -> str:
    return (at or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


def _next_utc_midnight() -> str:
    now = datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return tomorrow.isoformat()


async def _count_hits(db: AsyncSession, token_id: str, *, since: datetime | None = None,
                      day: str | None = None) -> int:
    q = select(func.count()).select_from(DeploymentTokenHit).where(
        DeploymentTokenHit.token_id == token_id)
    if since is not None:
        q = q.where(DeploymentTokenHit.admitted_at >= since)
    if day is not None:
        q = q.where(DeploymentTokenHit.quota_day == day)
    return (await db.execute(q)).scalar_one()


async def _prune(db: AsyncSession, now: datetime) -> None:
    """Opportunistic housekeeping: drop hits older than the retention window."""
    await db.execute(delete(DeploymentTokenHit)
                     .where(DeploymentTokenHit.admitted_at < now - _HIT_RETENTION))


async def admit(token_id: str, policy: DeploymentTokenPolicy | None) -> dict[str, str]:
    """Record one admitted request and apply the token's policy.

    Call AFTER auth succeeded (v69 call sites unchanged in shape, now
    awaited). Opens its own short-lived session - the request session
    stays read-only (the single-writer SQLite discipline every serving
    path already follows). Returns advisory headers; raises
    ``LimitExceeded`` (mapped to 429 by the API layers) when the policy
    refuses. Every token-authenticated request lands a hit row, policy
    or not - so applying a policy to an already-busy token shapes the
    traffic it has ALREADY served this window, exactly like v69.
    """
    headers: dict[str, str] = {}
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        # 1) the insert-first write: takes SQLite's writer lock, so every
        #    concurrent admit (any process) serializes behind this row
        hit = DeploymentTokenHit(token_id=token_id, admitted_at=now,
                                 quota_day=_day_key(now))
        db.add(hit)
        await db.flush()

        rate = policy.rate_per_min if policy else None
        if rate is not None and rate > 0:
            # 2) the exact window count: committed rows + this one
            minute_used = await _count_hits(
                db, token_id, since=now - timedelta(seconds=_WINDOW_SECONDS))
            headers["X-RateLimit-Limit"] = str(rate)
            if minute_used > rate:
                # 3) this request is the overflow - take it back, then refuse
                oldest = (await db.execute(
                    select(DeploymentTokenHit.admitted_at)
                    .where(DeploymentTokenHit.token_id == token_id,
                           DeploymentTokenHit.admitted_at >= now - timedelta(seconds=_WINDOW_SECONDS))
                    .order_by(DeploymentTokenHit.admitted_at)
                    .limit(1))).scalar()
                await db.delete(hit)
                await db.commit()
                if oldest is not None and oldest.tzinfo is None:
                    # SQLite reads back what it stored without the offset
                    oldest = oldest.replace(tzinfo=timezone.utc)
                retry_after = max(1, int(_WINDOW_SECONDS
                                         - (now - oldest).total_seconds()) + 1) if oldest else 60
                raise LimitExceeded(
                    f"rate limit exceeded - this token allows {rate} "
                    f"requests/minute", retry_after,
                    {**headers, "X-RateLimit-Remaining": "0"})
            headers["X-RateLimit-Remaining"] = str(max(0, rate - minute_used))

        quota = policy.daily_quota if policy else None
        if quota is not None and quota > 0:
            day = _day_key(now)
            day_used = await _count_hits(db, token_id, day=day)
            if day_used > quota:
                await db.delete(hit)
                await db.commit()
                raise LimitExceeded(
                    f"daily quota exhausted - this token allows {quota} "
                    f"requests per UTC day; the quota resets at {_next_utc_midnight()}",
                    3600, {"X-Quota-Limit": str(quota),
                           "X-Quota-Used": str(day_used - 1),
                           "X-Quota-Reset": _next_utc_midnight()})

        await _prune(db, now)
        await db.commit()
    return headers


async def usage_snapshot(token_id: str, policy: DeploymentTokenPolicy | None) -> dict:
    """The live counters for the usage endpoint (ops/tests) - from the DB.

    Reads the same rows every process writes, so the numbers are the
    platform-wide truth, not one worker's private view.
    """
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        minute_used = await _count_hits(
            db, token_id, since=now - timedelta(seconds=_WINDOW_SECONDS))
        day = _day_key(now)
        day_used = await _count_hits(db, token_id, day=day)
    rate = policy.rate_per_min if policy else None
    quota = policy.daily_quota if policy else None
    return {
        "rate_per_min": rate,
        "minute_used": minute_used,
        "minute_remaining": (rate - minute_used) if rate else None,
        "daily_quota": quota,
        "day_used": day_used,
        "day_remaining": (quota - day_used) if quota else None,
        "quota_day": day,
        "quota_resets_at": _next_utc_midnight() if quota else None,
    }


async def reset_all() -> None:
    """Drop every hit row (tests call this between scenarios)."""
    async with AsyncSessionLocal() as db:
        await db.execute(delete(DeploymentTokenHit))
        await db.commit()


async def policy_for_token(db: AsyncSession, token_id: str) -> DeploymentTokenPolicy | None:
    q = select(DeploymentTokenPolicy).where(DeploymentTokenPolicy.token_id == token_id)
    return (await db.execute(q)).scalars().first()
