"""Environment variables service (v15) - the engine-side loader.

Every workflow run loads the full variable map ONCE and hands it to the
Jinja context as ``env`` (``{{ env.APP_NAME }}``). Values are decrypted
from the Fernet vault; a row that fails to decrypt (rotated key) becomes
an empty string with a warning rather than failing unrelated runs.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from ..db import AsyncSessionLocal
from ..models import EnvVariable
from .crypto import decrypt_value

logger = logging.getLogger("py8n.env")


async def load_env_map(owner_id: str | None = None) -> dict[str, str]:
    """Return {key: value} for every visible variable (decrypted).

    With ``owner_id`` set, only variables owned by that caller or unclaimed
    (``owner_id IS NULL``) are loaded - another owner's variables are never
    decrypted into a run's template context. ``owner_id=None`` keeps the
    legacy all-visible behavior.
    """
    from sqlalchemy import or_

    q = select(EnvVariable)
    if owner_id is not None:
        q = q.where(or_(EnvVariable.owner_id.is_(None), EnvVariable.owner_id == owner_id))
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(q)).scalars().all()
    env: dict[str, str] = {}
    for row in rows:
        try:
            env[row.key] = decrypt_value(row.value_encrypted)
        except ValueError:
            logger.warning("env variable %r failed to decrypt - substituting empty string", row.key)
            env[row.key] = ""
    return env
