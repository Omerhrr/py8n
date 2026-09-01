"""Credential vault API - Fernet-encrypted at rest, secrets never returned.

Endpoints
---------
POST   /credentials            create
GET    /credentials            list (masked)
PATCH  /credentials/{id}       rename and/or replace the encrypted payload
POST   /credentials/{id}/test  live per-type probe (connect / auth / HTTP call)
GET    /credentials/{id}/usage workflows whose graphs reference the credential
DELETE /credentials/{id}       409 while referenced unless ?force=true

Every write commits explicitly before returning - FastAPI yield-dependency
teardown commits run AFTER the response is sent, so immediate follow-up
reads on the live server would otherwise race (v4 lesson).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_optional_user, own_or_404, scope_rows
from ..db import get_db
from ..models import Credential, CredentialEvent, Workflow
from ..schemas import (
    CredentialCreate,
    CredentialDetail,
    CredentialEventOut,
    CredentialOut,
    CredentialRotate,
    CredentialTestRequest,
    CredentialTestResult,
    CredentialUpdate,
    CredentialUsage,
    CredentialUsageWorkflow,
)
from ..services.credential_probe import probe_credential
from ..services.crypto import decrypt_payload, encrypt_payload, mask_hint

router = APIRouter(prefix="/credentials", tags=["credentials"])

# Fields never echoed back to any client (blanked in the edit-time view).
SECRET_FIELDS: dict[str, set[str]] = {
    "openai_compatible": {"api_key"},
    "header_auth": {"value"},
    "basic_auth": {"password"},
    "smtp": {"password"},
    "slack": {"webhook_url", "token"},
    "generic": {"token", "webhook_url"},
}
KEEP_MARKER = "__keep__"


def _log_event(db: AsyncSession, cred: Credential, action: str, detail: dict | None = None) -> None:
    """Append a vault audit row (v43). Detail carries FIELD NAMES only -
    callers must never put secret values in here. Caller owns the commit."""
    db.add(
        CredentialEvent(
            credential_id=cred.id,
            owner_id=cred.owner_id,
            credential_name=cred.name,
            action=action,
            detail=detail or {},
        )
    )


def _out(cred: Credential, data: dict) -> CredentialOut:
    return CredentialOut(
        id=cred.id, name=cred.name, type=cred.type,
        masked_hint=mask_hint(data), created_at=cred.created_at,
        rotated_at=cred.rotated_at,
    )


def _detail(cred: Credential, data: dict) -> CredentialDetail:
    """Edit-time view - non-secret fields visible, secrets blanked."""
    secrets = SECRET_FIELDS.get(cred.type, set())
    visible = {k: ("" if k in secrets else v) for k, v in data.items()}
    return CredentialDetail(
        id=cred.id, name=cred.name, type=cred.type,
        masked_hint=mask_hint(data), created_at=cred.created_at,
        rotated_at=cred.rotated_at, data=visible,
    )


@router.post("", response_model=CredentialOut, status_code=201)
async def create_credential(body: CredentialCreate, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    cred = Credential(
        name=body.name,
        type=body.type,
        data_encrypted=encrypt_payload(body.data),
    )
    cred.owner_id = user.id if user else None  # v37
    db.add(cred)
    await db.flush()
    await db.refresh(cred)
    _log_event(db, cred, "created", {"type": cred.type, "fields": sorted(body.data.keys())})
    await db.commit()  # explicit: teardown commit runs after the response
    return _out(cred, body.data)


@router.get("", response_model=list[CredentialOut])
async def list_credentials(user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Credential).order_by(Credential.created_at.desc()))).scalars().all()
    return [_out(c, decrypt_payload(c.data_encrypted)) for c in scope_rows(rows, user)]  # v37


@router.get("/{credential_id}", response_model=CredentialDetail)
async def get_credential(credential_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Edit-time view: non-secret fields visible, secrets blanked - the client
    re-sends untouched secrets as ``__keep__`` and the vault substitutes them."""
    cred = await db.get(Credential, credential_id)
    if cred is None:
        raise HTTPException(status_code=404, detail="Credential not found")
    own_or_404(cred.owner_id, user)  # v37
    return _detail(cred, decrypt_payload(cred.data_encrypted))


@router.patch("/{credential_id}", response_model=CredentialOut)
async def update_credential(
    credential_id: str, body: CredentialUpdate, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)
):
    """Rename and/or replace the secret payload. The payload is re-encrypted
    wholesale - the client sends the full field set (secrets are never
    echoed back, so partial merges are impossible by design)."""
    cred = await db.get(Credential, credential_id)
    if cred is None:
        raise HTTPException(status_code=404, detail="Credential not found")
    own_or_404(cred.owner_id, user)  # v37

    data = decrypt_payload(cred.data_encrypted)
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Credential name cannot be empty")
        if name[:200] != cred.name:
            _log_event(db, cred, "renamed", {"from": cred.name, "to": name[:200]})
        cred.name = name[:200]
    if body.data is not None:
        if not isinstance(body.data, dict) or not body.data:
            raise HTTPException(status_code=400, detail="Credential data must be a non-empty object")
        # __keep__ marker → substitute the stored value (secrets never leave
        # the vault, so partial edits re-send everything with markers).
        merged = {
            k: (data.get(k, "") if v == KEEP_MARKER else v)
            for k, v in body.data.items()
        }
        _log_event(db, cred, "updated", {"fields": sorted(body.data.keys())})
        data = merged
        cred.data_encrypted = encrypt_payload(data)
    await db.commit()
    return _out(cred, data)


@router.post("/{credential_id}/rotate", response_model=CredentialOut)
async def rotate_credential(
    credential_id: str, body: CredentialRotate, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)
):
    """v43 secret rotation - replace ONLY the provided fields; every other
    field (endpoints, usernames, header names) carries over untouched, so a
    leaked key can be swapped without re-entering the whole config. The
    rotated_at stamp and the audit row record field names, never values."""
    cred = await db.get(Credential, credential_id)
    if cred is None:
        raise HTTPException(status_code=404, detail="Credential not found")
    own_or_404(cred.owner_id, user)  # v37

    if not isinstance(body.secrets, dict) or not body.secrets:
        raise HTTPException(status_code=400, detail="Provide at least one field to rotate")
    data = decrypt_payload(cred.data_encrypted)
    changed = sorted(k for k in body.secrets if data.get(k) != body.secrets[k])
    data.update(body.secrets)
    cred.data_encrypted = encrypt_payload(data)
    cred.rotated_at = datetime.now(timezone.utc)
    _log_event(db, cred, "rotated", {"fields": sorted(body.secrets.keys()), "changed": changed})
    await db.commit()
    await db.refresh(cred)
    return _out(cred, data)


@router.get("/{credential_id}/events", response_model=list[CredentialEventOut])
async def credential_events(
    credential_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)
):
    """Vault audit trail for one credential, newest first (capped at 200)."""
    cred = await db.get(Credential, credential_id)
    if cred is None:
        raise HTTPException(status_code=404, detail="Credential not found")
    own_or_404(cred.owner_id, user)  # v37
    rows = (
        await db.execute(
            select(CredentialEvent)
            .where(CredentialEvent.credential_id == credential_id)
            .order_by(CredentialEvent.created_at.desc(), CredentialEvent.id.desc())
            .limit(200)
        )
    ).scalars().all()
    return [
        CredentialEventOut(
            id=r.id, action=r.action, credential_name=r.credential_name,
            detail=r.detail or {}, created_at=r.created_at,
        )
        for r in rows
    ]


@router.post("/{credential_id}/test", response_model=CredentialTestResult)
async def test_credential(
    credential_id: str,
    body: CredentialTestRequest | None = None,
    user=Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """Run the live probe for this credential type. Network/auth failures are
    reported as ok=false results (never as 500s); unknown id → 404, unknown
    type or structurally incomplete data → 400."""
    cred = await db.get(Credential, credential_id)
    if cred is None:
        raise HTTPException(status_code=404, detail="Credential not found")
    own_or_404(cred.owner_id, user)  # v37

    data = decrypt_payload(cred.data_encrypted)
    try:
        result = await probe_credential(cred.type, data, (body.test_url if body else None) or None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    _log_event(db, cred, "tested", {"ok": bool(result.get("ok")), "message": str(result.get("message", ""))[:200]})
    await db.commit()

    return CredentialTestResult(
        ok=bool(result.get("ok")),
        message=str(result.get("message", "")),
        latency_ms=int(result.get("latency_ms") or 0),
        probed_at=datetime.now(timezone.utc),
    )


# ----------------------------------------------------------------------
# usage scan - find workflows whose graph references the credential
# ----------------------------------------------------------------------
def _find_cred_nodes(graph: dict, credential_id: str) -> list[str]:
    """Node ids whose config references the credential id - scans parameters
    (credential_id widget params) and settings, any exact string match."""
    hits: list[str] = []
    for node in graph.get("nodes", []):
        pools = (node.get("parameters") or {}, node.get("settings") or {})
        if any(v == credential_id for pool in pools for v in pool.values() if isinstance(v, str)):
            hits.append(node.get("id", "?"))
    return hits


async def _usage(credential_id: str, db: AsyncSession) -> CredentialUsage:
    rows = (await db.execute(select(Workflow.id, Workflow.name, Workflow.is_active, Workflow.graph))).all()
    workflows: list[CredentialUsageWorkflow] = []
    for wf_id, name, active, graph_json in rows:
        try:
            graph = graph_json if isinstance(graph_json, dict) else json.loads(graph_json or "{}")
        except (TypeError, ValueError):
            graph = {}
        node_ids = _find_cred_nodes(graph, credential_id)
        if node_ids:
            workflows.append(
                CredentialUsageWorkflow(
                    id=wf_id, name=name, active=bool(active), nodes=node_ids,
                )
            )
    return CredentialUsage(credential_id=credential_id, workflow_count=len(workflows), workflows=workflows)


@router.get("/{credential_id}/usage", response_model=CredentialUsage)
async def credential_usage(credential_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    cred = await db.get(Credential, credential_id)
    if cred is None:
        raise HTTPException(status_code=404, detail="Credential not found")
    own_or_404(cred.owner_id, user)  # v37
    return await _usage(credential_id, db)


@router.delete("/{credential_id}", status_code=204)
async def delete_credential(
    credential_id: str,
    force: bool = Query(default=False, description="Delete even when referenced by workflows"),
    user=Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    cred = await db.get(Credential, credential_id)
    if cred is None:
        raise HTTPException(status_code=404, detail="Credential not found")
    own_or_404(cred.owner_id, user)  # v37
    usage = await _usage(credential_id, db)
    if usage.workflow_count and not force:
        names = ", ".join(w.name for w in usage.workflows[:3])
        more = f" (+{usage.workflow_count - 3} more)" if usage.workflow_count > 3 else ""
        raise HTTPException(
            status_code=409,
            detail=f"Credential is used by {usage.workflow_count} workflow(s): {names}{more}. Delete with force=true to break the link.",
        )
    _log_event(db, cred, "deleted", {"fields": sorted(decrypt_payload(cred.data_encrypted).keys())})
    await db.delete(cred)
    await db.commit()  # explicit: teardown commit runs after the response
