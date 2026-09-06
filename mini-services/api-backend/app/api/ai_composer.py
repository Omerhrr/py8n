"""AI System Composer API (v82) - the roadmap's AI System Builder stage.

"Build me a customer support system" becomes real, composable primitives:

* ``GET  /ai-composer/catalog``   - the honest inventory: kinds, node types,
                                     triggers, archetypes (feeds the LLM AND the UI)
* ``POST /ai-composer/propose``   - description -> spec (LLM-first through a real
                                     credential; deterministic archetype fallback)
* ``POST /ai-composer/build``     - spec -> REAL primitives bound into a RUNNING
                                     Py8nSystem (datasets, workflows, agents, rooms,
                                     queues) - never a blob of generated code
* ``POST /ai-composer/generate``  - propose + build in one door (Describe -> Deploy)
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_optional_user
from ..db import get_db
from ..services import ai_composer as composer
from ..services.scheduler import resync_workflow_jobs

router = APIRouter(prefix="/ai-composer", tags=["ai-composer"])


class ProposeRequest(BaseModel):
    description: str = Field(..., min_length=8, max_length=4000)
    credential_id: str | None = Field(
        default=None,
        description="An openai_compatible vault credential - the model composes the "
                    "spec. Omitted = the deterministic archetype composer.")
    model: str = Field(default="", description="Optional model override for the credential")


class BuildRequest(BaseModel):
    spec: dict = Field(..., description="The composed spec (from /propose or your own hand)")


class GenerateRequest(BaseModel):
    description: str = Field(..., min_length=8, max_length=4000)
    credential_id: str | None = None
    model: str = ""


def _http(exc: Exception, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail=str(exc))


@router.get("/catalog")
async def composer_catalog():
    """What the composer can build - the same catalog the LLM sees."""
    from ..engine.registry import all_definitions

    registered = {d.get("type") for d in all_definitions()}
    return {
        "kinds": composer.COMPOSER_KINDS,
        "trigger_types": list(composer.TRIGGER_TYPES),
        "node_types": {
            "composable": list(composer.COMPOSER_NODE_TYPES),
            "registered_in_this_build": sorted(t for t in registered if t),
        },
        "archetypes": composer.archetypes_out(),
        "limits": {"steps_per_workflow": composer.MAX_STEPS,
                   "components_per_spec": composer.MAX_COMPONENTS},
        "notes": [
            "The builder COMPOSES primitives - it never generates a blob of code.",
            "Workflows install INACTIVE; the system's Start/Boot door opens them loudly.",
            "brain=ai_agent agents need llm_credential_id; scaffold brains answer from "
            "the bound knowledge deterministically.",
        ],
    }


async def _resolve_credential(db: AsyncSession, credential_id: str | None,
                              user) -> dict:
    """Vault credential -> ``{type, data}`` routing payload (loud 404/400)."""
    if not credential_id:
        raise HTTPException(status_code=400, detail="credential_id is required for LLM-first proposals")
    from sqlalchemy import select

    from ..models import Credential
    from ..services.crypto import decrypt_payload
    from ..services.llm_routing import credential_type_matches, resolve_credential

    row = (await db.execute(
        select(Credential).where(Credential.id == credential_id))).scalar_one_or_none()
    owner = getattr(user, "id", None)
    if row is None or (owner is not None and row.owner_id is not None and row.owner_id != owner):
        raise HTTPException(status_code=404, detail=f"credential {credential_id!r} not found")
    if not credential_type_matches(row.type):
        raise HTTPException(
            status_code=400,
            detail=f"credential {row.name!r} is of type {row.type!r} - the composer composes "
                   "through openai_compatible or anthropic credentials (presets at "
                   "GET /credentials/providers)")
    try:
        data = decrypt_payload(row.data_encrypted)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"credential unreadable: {exc}") from exc
    try:
        resolved = resolve_credential(data)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"credential unusable: {exc}") from exc
    return {"type": row.type, "data": data, "label": resolved["label"]}


@router.post("/propose")
async def composer_propose(body: ProposeRequest, user=Depends(get_optional_user),
                           db: AsyncSession = Depends(get_db)):
    """Description -> spec. LLM-first when a credential is given (fail-loud
    on model errors); the deterministic archetype composer otherwise."""
    try:
        if body.credential_id:
            cred = await _resolve_credential(db, body.credential_id, user)
            spec = await asyncio.to_thread(
                composer.propose_with_llm_sync, body.description,
                {**cred["data"], "type": cred["type"]},
                model=body.model,
                notes=[f"design proposed by {cred['label']}"])
        else:
            spec = composer.synthesize_spec(body.description)
    except composer.AIComposerError as exc:
        raise _http(exc) from exc
    # every proposal is validated before the user ever sees it
    try:
        spec = composer.validate_spec(spec)
    except composer.AIComposerError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"spec": spec, "validated": True}


@router.post("/build", status_code=201)
async def composer_build(body: BuildRequest, user=Depends(get_optional_user),
                         db: AsyncSession = Depends(get_db)):
    """Spec -> real primitives + a RUNNING system. Owner-scoped."""
    owner = getattr(user, "id", None)
    try:
        built = await composer.build_system(db, body.spec, owner_id=owner)
    except composer.AIComposerError as exc:
        raise _http(exc) from exc
    await db.commit()
    for wf in built.get("workflows", []):
        try:
            await resync_workflow_jobs(wf["id"])
        except Exception:  # a schedule resync never fails the build
            pass
    return built


@router.post("/generate", status_code=201)
async def composer_generate(body: GenerateRequest, user=Depends(get_optional_user),
                            db: AsyncSession = Depends(get_db)):
    """Describe -> Deploy in one door: propose (LLM or deterministic) then build."""
    owner = getattr(user, "id", None)
    try:
        if body.credential_id:
            cred = await _resolve_credential(db, body.credential_id, user)
            spec = await asyncio.to_thread(
                composer.propose_with_llm_sync, body.description,
                {**cred["data"], "type": cred["type"]}, model=body.model)
        else:
            spec = composer.synthesize_spec(body.description)
        built = await composer.build_system(db, spec, owner_id=owner)
    except composer.AIComposerError as exc:
        raise _http(exc) from exc
    await db.commit()
    for wf in built.get("workflows", []):
        try:
            await resync_workflow_jobs(wf["id"])
        except Exception:
            pass
    return built
