"""One-off cleanup: remove v4-api-test-* workflows and their executions from the dev DB."""
import asyncio
import sys

sys.path.insert(0, "/home/z/my-project/mini-services/api-backend")

from sqlalchemy import delete, select  # noqa: E402

from app.db import AsyncSessionLocal, init_db  # noqa: E402
from app.models import ExecutionLog, Workflow  # noqa: E402


async def main() -> None:
    await init_db()
    async with AsyncSessionLocal() as s:
        wfs = (await s.execute(select(Workflow).where(Workflow.name.like("v4-api-test-%")))).scalars().all()
        ids = [w.id for w in wfs]
        if ids:
            n_exec = (
                await s.execute(delete(ExecutionLog).where(ExecutionLog.workflow_id.in_(ids)))
            ).rowcount
            n_wf = (await s.execute(delete(Workflow).where(Workflow.id.in_(ids)))).rowcount
            await s.commit()
            print(f"deleted {n_wf} temp workflows, {n_exec} executions")
        else:
            print("nothing to clean")
        # also delete orphaned executions (workflow gone)
        orphan_ids = (
            await s.execute(
                select(ExecutionLog.id)
                .outerjoin(Workflow, Workflow.id == ExecutionLog.workflow_id)
                .where(Workflow.id.is_(None))
            )
        ).scalars().all()
        if orphan_ids:
            await s.execute(delete(ExecutionLog).where(ExecutionLog.id.in_(orphan_ids)))
            await s.commit()
            print(f"deleted {len(orphan_ids)} orphaned executions")


asyncio.run(main())
