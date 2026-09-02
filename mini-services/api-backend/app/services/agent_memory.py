"""Agent conversation memory store (v23).

Buffer-window memory for AI Agent nodes: prior user/assistant turns are
persisted per session key and re-injected into the next run sharing that
key. Only FINAL turns are stored (not the tool-loop internals), so the
injected history stays small and predictable.

Owner scoping (audit hardening): the AgentMemory table keys rows by
session_key alone, so all functions accept an optional ``owner_id``. When
given, the storage key is namespaced under the owner (``{owner_id}::{key}``),
which makes one owner's memory unreachable from another owner's runs even
if their session keys collide. ``owner_id=None`` keeps the legacy global
keyspace (existing rows and callers unchanged).
"""

from __future__ import annotations

from ..db import AsyncSessionLocal
from ..models import AgentMemory


def _scoped_key(session_key: str, owner_id: str | None) -> str:
    if owner_id:
        return f"{owner_id}::{session_key}"
    return session_key


async def load_history(session_key: str, owner_id: str | None = None) -> list[dict]:
    """Stored turns for the key (oldest first); [] when the key is unknown."""
    async with AsyncSessionLocal() as session:
        row = await session.get(AgentMemory, _scoped_key(session_key, owner_id))
        return list(row.messages or []) if row else []


async def append_history(
    session_key: str, user: str, assistant: str, max_turns: int, owner_id: str | None = None
) -> list[dict]:
    """Append one finished turn and keep only the newest ``max_turns`` pairs.

    Returns the trimmed history that was persisted (handy for outputs/tests).
    """
    turn = [{"role": "user", "content": user}, {"role": "assistant", "content": assistant}]
    async with AsyncSessionLocal() as session:
        key = _scoped_key(session_key, owner_id)
        row = await session.get(AgentMemory, key)
        if row is None:
            row = AgentMemory(session_key=key, messages=turn)
            session.add(row)
        else:
            row.messages = list((row.messages or []) + turn)  # rebind: JSON columns don't track in-place edits
        # keep newest N turns (N pairs = 2N messages)
        row.messages = row.messages[-max_turns * 2 :]
        await session.commit()
        return list(row.messages or [])


async def clear_history(session_key: str, owner_id: str | None = None) -> bool:
    """Drop the stored conversation; True when a row was deleted."""
    async with AsyncSessionLocal() as session:
        row = await session.get(AgentMemory, _scoped_key(session_key, owner_id))
        if row is None:
            return False
        await session.delete(row)
        await session.commit()
        return True
