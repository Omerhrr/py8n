"""Py8n FastAPI application entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .db import init_db
from .seed import seed_if_empty
from .services import retention
from .services.scheduler import (
    resync_all_jobs,
    resync_all_report_jobs,
    shutdown_scheduler,
    start_scheduler,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("py8n")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- startup -------------------------------------------------------
    await init_db()
    from .db import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        await seed_if_empty(session)
        await session.commit()
    start_scheduler()
    await resync_all_jobs()  # register schedule_trigger jobs from saved workflows
    await resync_all_report_jobs()  # v48: register scheduled report export jobs
    # v19: execution data retention - best-effort purge at boot + daily job
    try:
        await retention.purge_execution_data()
        retention.schedule_daily_purge()
    except Exception:  # noqa: BLE001 - retention must never block startup
        logger.exception("retention purge failed at startup")
    logger.info("Py8n v%s ready - execution_mode=%s, db=%s", settings.version, settings.execution_mode, settings.database_url.split("@")[-1])
    yield
    # --- shutdown ------------------------------------------------------
    await shutdown_scheduler()


app = FastAPI(
    title="Py8n API",
    description="Python-native visual workflow automation - n8n, but Python.",
    version=settings.version,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------- routers
from .api.agents import router as agents_router  # noqa: E402
from .api.auth import router as auth_router  # noqa: E402 (v37)
from .api.credentials import router as credentials_router  # noqa: E402
from .api.apps import router as apps_router  # noqa: E402
from .api.artifacts import router as artifacts_router  # noqa: E402
from .api.catalog import router as catalog_router  # noqa: E402 (v50)
from .api.storage import router as storage_router  # noqa: E402 (v51)
from .api.chat import router as chat_router  # noqa: E402
from .api.dashboards import router as dashboards_router  # noqa: E402
from .api.datasets import router as datasets_router  # noqa: E402
from .api.documents import router as documents_router  # noqa: E402
from .api.env_vars import router as env_vars_router  # noqa: E402
from .api.executions import router as executions_router  # noqa: E402
from .api.folders import router as folders_router  # noqa: E402
from .api.insights import router as insights_router  # noqa: E402
from .api.keys import router as keys_router  # noqa: E402 (v41)
from .api.models import router as models_router  # noqa: E402 (v46)
from .api.node_defs import router as node_defs_router  # noqa: E402
from .api.notifications import router as notifications_router  # noqa: E402 (v44)
from .api.observability import router as observability_router  # noqa: E402 (v53)
from .api.builder import router as builder_router  # noqa: E402 (v59)
from .api.solutions import router as solutions_router  # noqa: E402 (v60)
from .api.systems import router as systems_router  # noqa: E402 (v61)
from .api.model_systems import router as model_systems_router  # noqa: E402 (v63)
from .api.deployments import router as deployments_router  # noqa: E402 (v67)
from .api.channels import router as channels_router  # noqa: E402 (v69)
from .api.voice import router as voice_router  # noqa: E402 (v69)
from .api.interactions import router as interactions_router  # noqa: E402 (v68)
from .api.platform import router as platform_router  # noqa: E402 (v67)
from .api.ops import router as ops_router  # noqa: E402 (v57)
from .api.packs import router as packs_router  # noqa: E402 (v39)
from .api.registries import router as registries_router  # noqa: E402 (v43)
from .api.reports import router as reports_router  # noqa: E402 (v48)
from .api.tags import router as tags_router  # noqa: E402 (v44)
from .api.schedules import router as schedules_router  # noqa: E402
from .api.settings import router as settings_router  # noqa: E402
from .api.templates import router as templates_router  # noqa: E402
from .api.webhooks import router as webhooks_router  # noqa: E402
from .api.workflows import router as workflows_router  # noqa: E402
from .api.ws import router as ws_router  # noqa: E402

API = "/api/v1"

# v37: enforced-mode gate. Build/admin surfaces require a Bearer token when
# PY8N_REQUIRE_AUTH=true; machine + published-runtime surfaces (webhooks,
# chat, app/dashboard runtimes, artifact content, embedded dataset SQL) stay
# reachable - see auth.is_public_path. In the default open mode this is a
# no-op (anonymous still works, tokens just scope what they touch).
from .auth import enforce_auth, enforce_key_scopes  # noqa: E402 (scopes v43)

# v43: enforce_key_scopes rides every enforced router so read-only API
# keys are rejected with 403 on mutating methods (JWT users unaffected).
ENFORCED = [Depends(enforce_auth), Depends(enforce_key_scopes)]

app.include_router(workflows_router, prefix=API, dependencies=ENFORCED)
app.include_router(agents_router, prefix=API, dependencies=ENFORCED)  # v34
app.include_router(executions_router, prefix=API, dependencies=ENFORCED)
app.include_router(schedules_router, prefix=API, dependencies=ENFORCED)
app.include_router(webhooks_router, prefix=API)
app.include_router(chat_router, prefix=API, dependencies=[Depends(enforce_key_scopes)])  # v43: chat runs workflows, so read-only keys are gated
app.include_router(auth_router, prefix=API)  # v37: register/login/me/status
app.include_router(artifacts_router, prefix=API, dependencies=ENFORCED)
app.include_router(datasets_router, prefix=API, dependencies=ENFORCED)
app.include_router(apps_router, prefix=API, dependencies=ENFORCED)
app.include_router(dashboards_router, prefix=API, dependencies=ENFORCED)
app.include_router(documents_router, prefix=API, dependencies=ENFORCED)
app.include_router(credentials_router, prefix=API, dependencies=ENFORCED)
app.include_router(env_vars_router, prefix=API, dependencies=ENFORCED)
app.include_router(folders_router, prefix=API, dependencies=ENFORCED)
app.include_router(insights_router, prefix=API, dependencies=ENFORCED)
app.include_router(settings_router, prefix=API, dependencies=ENFORCED)
app.include_router(node_defs_router, prefix=API, dependencies=ENFORCED)
app.include_router(templates_router, prefix=API, dependencies=ENFORCED)
app.include_router(packs_router, prefix=API, dependencies=ENFORCED)  # v39
app.include_router(keys_router, prefix=API, dependencies=ENFORCED)  # v41
app.include_router(registries_router, prefix=API, dependencies=ENFORCED)  # v43
app.include_router(tags_router, prefix=API, dependencies=ENFORCED)  # v44
app.include_router(notifications_router, prefix=API, dependencies=ENFORCED)  # v44
app.include_router(models_router, prefix=API, dependencies=ENFORCED)  # v46
app.include_router(reports_router, prefix=API, dependencies=ENFORCED)  # v48
app.include_router(catalog_router, prefix=API, dependencies=ENFORCED)  # v50
app.include_router(storage_router, prefix=API, dependencies=ENFORCED)  # v51
app.include_router(observability_router, prefix=API, dependencies=ENFORCED)  # v53
app.include_router(builder_router, prefix=API, dependencies=ENFORCED)  # v59
app.include_router(solutions_router, prefix=API, dependencies=ENFORCED)  # v60
app.include_router(systems_router, prefix=API, dependencies=ENFORCED)  # v61
app.include_router(model_systems_router, prefix=API, dependencies=ENFORCED)  # v63
app.include_router(deployments_router, prefix=API, dependencies=ENFORCED)  # v67
app.include_router(interactions_router, prefix=API, dependencies=ENFORCED)  # v68 interaction layer
app.include_router(channels_router, prefix=API)  # v69: endpoint mgmt ENFORCED + public provider receivers
app.include_router(voice_router, prefix=API, dependencies=ENFORCED)  # v69 voice primitives
app.include_router(platform_router, prefix=API, dependencies=ENFORCED)  # v67
app.include_router(ops_router, prefix=API, dependencies=ENFORCED)  # v57
app.include_router(ws_router)  # /ws/...


@app.get(f"{API}/health")
async def health():
    return {
        "status": "ok",
        "app": settings.app_name,
        "version": settings.version,
        "execution_mode": settings.execution_mode,
        "require_auth": settings.require_auth,
    }


if settings.debug:
    # DEV-ONLY helper (sandbox): launch helper daemons from a persistent
    # process tree. Triple-gated (audit hardening):
    #   * settings.debug must be true (PY8N_DEBUG=true, default false);
    #   * settings.spawn_enabled must be true (PY8N_SPAWN_ENABLED=true);
    #   * the token is per-boot RANDOM unless PY8N_SPAWN_TOKEN pins it -
    #     the old committed static token is gone (it was effectively public).
    # The route is hidden from OpenAPI and answers 404 while disabled so it
    # does not even advertise its existence.
    import secrets
    import subprocess

    from fastapi import HTTPException
    from pydantic import BaseModel as _BM

    from .config import BASE_DIR as _BACKEND_DIR

    class _SpawnReq(_BM):
        token: str
        cmd: str

    _SPAWN_TOKEN = settings.spawn_token or secrets.token_urlsafe(32)

    if settings.spawn_enabled:
        # Ops bootstrap: the token is only discoverable from this log line.
        logging.warning("[py8n] /_spawn ENABLED - per-boot token: %s", _SPAWN_TOKEN)

    @app.post(f"{API}/_spawn", include_in_schema=False)
    async def _spawn(body: _SpawnReq):
        if not settings.spawn_enabled:
            raise HTTPException(status_code=404, detail="Not Found")
        if not secrets.compare_digest(body.token or "", _SPAWN_TOKEN):
            raise HTTPException(status_code=401, detail="unauthorized")
        cmd = (body.cmd or "").strip()
        if not cmd or len(cmd) > 4000:
            raise HTTPException(status_code=400, detail="cmd missing or too long")
        proc = subprocess.Popen(
            ["/bin/bash", "-lc", cmd],
            cwd=str(_BACKEND_DIR.parent.parent),  # project root
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return {"ok": True, "pid": proc.pid}
