"""Agent conversation memory store (v23).

Buffer-window memory for AI Agent nodes: prior user/assistant turns are
persisted per session key and re-injected into the next run sharing that
key. Only FINAL turns are stored (not the tool-loop internals), so the
injected history stays small and predictable.
"""

from __future__ import annotations

from ..db import AsyncSessionLocal
from ..models import AgentMemory


async def load_history(session_key: str) -> list[dict]:
    """Stored turns for the key (oldest first); [] when the key is unknown."""
    async with AsyncSessionLocal() as session:
        row = await session.get(AgentMemory, session_key)
        return list(row.messages or []) if row else []


async def append_history(session_key: str, user: str, assistant: str, max_turns: int) -> list[dict]:
    """Append one finished turn and keep only the newest ``max_turns`` pairs.

    Returns the trimmed history that was persisted (handy for outputs/tests).
    """
    turn = [{"role": "user", "content": user}, {"role": "assistant", "content": assistant}]
    async with AsyncSessionLocal() as session:
        row = await session.get(AgentMemory, session_key)
        if row is None:
            row = AgentMemory(session_key=session_key, messages=turn)
            session.add(row)
        else:
            row.messages = list((row.messages or []) + turn)  # rebind: JSON columns don't track in-place edits
        # keep newest N turns (N pairs = 2N messages)
        row.messages = row.messages[-max_turns * 2 :]
        await session.commit()
        return list(row.messages or [])


async def clear_history(session_key: str) -> bool:
    """Drop the stored conversation; True when a row was deleted."""
    async with AsyncSessionLocal() as session:
        row = await session.get(AgentMemory, session_key)
        if row is None:
            return False
        await session.delete(row)
        await session.commit()
        return True
