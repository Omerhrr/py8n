"""Storage API (v51) - status of the dataset storage backend.

GET /storage    which backend holds dataset parquet today + liveness probe
PUT /storage    NOT exposed: switching backends is a deployment decision
                (PY8N_STORAGE_BACKEND + bucket/credentials env), not a
                runtime toggle - mixing backends mid-flight would strand
                blobs. The status endpoint exists so operators (and the
                datasets UI badge) can SEE what is configured.
"""

from __future__ import annotations

from fastapi import APIRouter

from ..services import storage as storage_svc

# Auth: the router is registered with the ENFORCED dependency pair in
# main.py (like every other admin surface), so anonymous callers get 401
# when PY8N_REQUIRE_AUTH=true and read access stays open in single-user
# mode - exactly the contract of the datasets router itself.
router = APIRouter(prefix="/storage", tags=["storage"])


@router.get("")
async def storage_status():
    """Configured dataset storage backend + live liveness probe."""
    return storage_svc.describe_storage(include_ping=True)
