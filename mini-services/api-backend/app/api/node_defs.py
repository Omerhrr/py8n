"""Node definition registry endpoint - Pydantic JSON schemas for the UI."""

from __future__ import annotations

from fastapi import APIRouter

from ..engine.registry import all_definitions

router = APIRouter(tags=["nodes"])


@router.get("/node-definitions")
async def get_node_definitions():
    """Export every node's metadata + parameter JSON schema.

    The frontend renders configuration forms directly from these Pydantic
    schemas - new node types appear in the palette with zero UI changes.
    """
    return {"definitions": all_definitions()}
