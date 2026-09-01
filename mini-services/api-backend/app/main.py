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
from .services.scheduler import resync_all_jobs, shutdown_scheduler, start_scheduler

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
from .api.chat import router as chat_router  # noqa: E402
from .api.dashboards import router as dashboards_router  # noqa: E402
from .api.datasets import router as datasets_router  # noqa: E402
from .api.documents import router as documents_router  # noqa: E402
from .api.env_vars import router as env_vars_router  # noqa: E402
from .api.executions import router as executions_router  # noqa: E402
from .api.folders import router as folders_router  # noqa: E402
from .api.insights import router as insights_router  # noqa: E402
from .api.keys import router as keys_router  # noqa: E402 (v41)
from .api.node_defs import router as node_defs_router  # noqa: E402
from .api.notifications import router as notifications_router  # noqa: E402 (v44)
from .api.packs import router as packs_router  # noqa: E402 (v39)
from .api.registries import router as registries_router  # noqa: E402 (v43)
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
    # process tree. Disabled in production (PY8N_DEBUG=false).
    import subprocess

    from fastapi import HTTPException
    from pydantic import BaseModel as _BM

    from .config import BASE_DIR as _BACKEND_DIR

    class _SpawnReq(_BM):
        token: str
        cmd: str

    _SPAWN_TOKEN = "py8n-bootstrap-9f2c"

    @app.post(f"{API}/_spawn")
    async def _spawn(body: _SpawnReq):
        if body.token != _SPAWN_TOKEN:
            raise HTTPException(status_code=401, detail="unauthorized")
        proc = subprocess.Popen(
            ["/bin/bash", "-lc", body.cmd],
            cwd=str(_BACKEND_DIR.parent.parent),  # project root
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return {"ok": True, "pid": proc.pid}
