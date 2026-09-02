"""The data catalog API (v50) - one inventory of every dataset.

The catalog is DERIVED, not stored: entries assemble identity (name,
description, owner, tags), shape (rows/columns/versions), freshness
(from the version timeline), governance (contract presence) and the
producer/consumer workflow graph (from version lineage + a scan of
active workflow graphs). It can therefore never drift from reality.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_optional_user
from ..db import get_db
from ..services import catalog as catalog_svc

router = APIRouter(prefix="/catalog", tags=["catalog"])


@router.get("")
async def catalog_entries(
    q: str = Query(default="", max_length=200, description="Search name + description"),
    tag: str = Query(default="", max_length=100, description="Filter by tag"),
    user=Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """Catalog cards for every dataset visible to the caller.

    Visibility mirrors the estate: signed-in users see their own datasets
    plus unclaimed ones; anonymous callers see unclaimed only.
    """
    owner_id = getattr(user, "id", None) if user is not None else None
    entries = await catalog_svc.build_catalog(db, owner_id=owner_id, q=q, tag=tag)
    return {"entries": entries, "count": len(entries)}
