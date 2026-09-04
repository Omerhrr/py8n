"""Rate-shaping and quotas on serving tokens (v69).

v68 gave deployments credentials (serving tokens); v69 gives those
tokens TRAFFIC POLICY. A token may carry:

* ``rate_per_min`` - a per-minute request cap enforced with a sliding
  60-second window (admitted requests only); over the cap = 429 with
  ``Retry-After`` and the ``X-RateLimit-Limit`` / ``X-RateLimit-Remaining``
  headers, the same grammar clients already know.
* ``daily_quota`` - a UTC calendar-day cap; exhausted = 429 naming the
  next UTC midnight as the reset.

Enforcement happens right after token auth succeeds on the serving
webhook and the SSE stream endpoint - one call site, one grammar. The
policy row is the only stored part; counters are in-process windows,
the same trade the v23 limiter made (per-process, honest, no hidden
shared state). A token without a policy stays unlimited - shaping is
opt-in per token, exactly like auth is opt-in per deployment.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import DeploymentTokenPolicy

_WINDOW_SECONDS = 60.0

# (token_id) -> deque[monotonic timestamps of admitted requests this minute]
_minute_hits: dict[str, deque] = defaultdict(deque)
# (token_id, utc_date_iso) -> admitted count today
_day_counts: dict[tuple[str, str], int] = defaultdict(int)


class LimitExceeded(Exception):
    """A request was refused by the token's policy -> HTTP 429."""

    def __init__(self, detail: str, retry_after: int, headers: dict[str, str]):
        super().__init__(detail)
        self.detail = detail
        self.retry_after = retry_after
        self.headers = {"Retry-After": str(retry_after), **headers}


def _day_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _next_utc_midnight() -> str:
    now = datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return tomorrow.isoformat()


def _prune_minute(token_id: str, now: float) -> deque:
    hits = _minute_hits[token_id]
    while hits and now - hits[0] >= _WINDOW_SECONDS:
        hits.popleft()
    return hits


def check(token_id: str, policy: DeploymentTokenPolicy | None) -> dict[str, str]:
    """Admit one request or raise LimitExceeded. Returns advisory headers."""
    headers: dict[str, str] = {}
    if policy is None:
        return headers
    now = time.monotonic()
    if policy.rate_per_min is not None and policy.rate_per_min > 0:
        hits = _prune_minute(token_id, now)
        headers["X-RateLimit-Limit"] = str(policy.rate_per_min)
        if len(hits) >= policy.rate_per_min:
            retry_after = max(1, int(_WINDOW_SECONDS - (now - hits[0])) + 1)
            raise LimitExceeded(
                f"rate limit exceeded - this token allows {policy.rate_per_min} "
                f"requests/minute", retry_after,
                {**headers, "X-RateLimit-Remaining": "0"})
        headers["X-RateLimit-Remaining"] = str(policy.rate_per_min - len(hits) - 1)
    if policy.daily_quota is not None and policy.daily_quota > 0:
        day = _day_key()
        used = _day_counts[(token_id, day)]
        if used >= policy.daily_quota:
            raise LimitExceeded(
                f"daily quota exhausted - this token allows {policy.daily_quota} "
                f"requests per UTC day; the quota resets at {_next_utc_midnight()}",
                3600, {"X-Quota-Limit": str(policy.daily_quota),
                       "X-Quota-Used": str(used),
                       "X-Quota-Reset": _next_utc_midnight()})
    return headers


def admit(token_id: str, policy: DeploymentTokenPolicy | None) -> dict[str, str]:
    """check() + count the request as admitted (call AFTER auth succeeded)."""
    headers = check(token_id, policy)
    _minute_hits[token_id].append(time.monotonic())
    _day_counts[(token_id, _day_key())] += 1
    return headers


def usage_snapshot(token_id: str, policy: DeploymentTokenPolicy | None) -> dict:
    """The live counters for the usage endpoint (tests/debug/ops)."""
    now = time.monotonic()
    minute_used = len(_prune_minute(token_id, now))
    day = _day_key()
    day_used = _day_counts.get((token_id, day), 0)
    return {
        "rate_per_min": policy.rate_per_min if policy else None,
        "minute_used": minute_used,
        "minute_remaining": (policy.rate_per_min - minute_used)
        if policy and policy.rate_per_min else None,
        "daily_quota": policy.daily_quota if policy else None,
        "day_used": day_used,
        "day_remaining": (policy.daily_quota - day_used)
        if policy and policy.daily_quota else None,
        "quota_day": day,
        "quota_resets_at": _next_utc_midnight() if policy and policy.daily_quota else None,
    }


def reset_all() -> None:
    """Drop every counter (tests call this between scenarios)."""
    _minute_hits.clear()
    _day_counts.clear()


async def policy_for_token(db: AsyncSession, token_id: str) -> DeploymentTokenPolicy | None:
    q = select(DeploymentTokenPolicy).where(DeploymentTokenPolicy.token_id == token_id)
    return (await db.execute(q)).scalars().first()
