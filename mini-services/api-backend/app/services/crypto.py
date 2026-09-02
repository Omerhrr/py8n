"""Fernet-encrypted credential vault (Phase 6 security layer)."""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

from ..config import settings
from ..models import Credential

_fernet: Fernet | None = None


def _write_key_file(path, key: str) -> None:
    """Persist the Fernet key with owner-only permissions (0600).

    os.open with mode 0o600 creates the file already restricted (no world-
    readable window between create and chmod); the explicit os.chmod also
    repairs a pre-existing file that was written loose (older versions).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(key)
    os.chmod(path, 0o600)  # umask-independent guarantee


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is not None:
        return _fernet
    key = settings.fernet_key
    if not key:
        # Generate once and persist next to the database (self-hosted default).
        if settings.secret_key_file.exists():
            key = settings.secret_key_file.read_text().strip()
            try:
                # Repair perms on key files written by older, loose versions.
                os.chmod(settings.secret_key_file, 0o600)
            except OSError:  # pragma: no cover - best effort
                pass
        else:
            key = Fernet.generate_key().decode()
            _write_key_file(settings.secret_key_file, key)
    _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def encrypt_payload(data: dict) -> str:
    import json

    token = _get_fernet().encrypt(json.dumps(data).encode())
    return token.decode()


def decrypt_payload(token: str) -> dict:
    import json

    try:
        raw = _get_fernet().decrypt(token.encode())
    except InvalidToken as exc:
        raise ValueError("Credential decryption failed (wrong FERNET_KEY?)") from exc
    return json.loads(raw)


def mask_hint(data: dict) -> str:
    """Human-safe hint, e.g. 'sk-...abcd' - never the secret itself."""
    for key in ("api_key", "value", "token"):
        v = data.get(key)
        if isinstance(v, str) and v:
            return f"••••{v[-4:]}" if len(v) > 4 else "••••"
    return "••••"


async def decrypt_credential(context, credential_id: str, owner_id: str | None = None) -> dict:
    """Resolve a credential id inside a node execution.

    Creates a short-lived engine/session so it works both inside the FastAPI
    event loop and inside Celery workers (which run fresh event loops per task).
    Every successful resolution writes a ``used`` row to the vault audit trail
    (v43) - the audit is the point of the vault, so it records the workflow
    that touched the secret, never the secret itself.

    With ``owner_id`` set, a credential claimed by a DIFFERENT owner is
    treated as not found (and never audited/decrypted).
    """
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from ..config import settings as cfg
    from ..models import Credential as CredentialModel
    from ..models import CredentialEvent

    engine = create_async_engine(cfg.database_url)
    try:
        maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with maker() as session:
            row = (
                await session.execute(select(CredentialModel).where(CredentialModel.id == credential_id))
            ).scalar_one_or_none()
            if (
                row is not None
                and owner_id is not None
                and row.owner_id is not None
                and row.owner_id != owner_id
            ):
                row = None  # cross-owner credential == not found (never decrypt)
            if row is not None:
                session.add(
                    CredentialEvent(
                        credential_id=credential_id,
                        owner_id=row.owner_id,
                        credential_name=row.name,
                        action="used",
                        detail={
                            "workflow_id": getattr(context, "workflow_id", None),
                            "workflow_name": getattr(context, "workflow_name", None),
                        },
                    )
                )
                await session.commit()
    finally:
        await engine.dispose()

    if row is None:
        raise LookupError(f"Credential {credential_id} not found")
    return decrypt_payload(row.data_encrypted) | {"type": row.type, "name": row.name}


def encrypt_value(value: str) -> str:
    """Encrypt a single string value (v15 environment variables)."""
    return _get_fernet().encrypt(value.encode()).decode()


def decrypt_value(token: str) -> str:
    try:
        return _get_fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Value decryption failed (wrong FERNET_KEY?)") from exc
