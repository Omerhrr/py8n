"""Service-layer hardening tests (audit task 4-b).

Direct service-level tests (no HTTP) mirroring the suite's asyncio.run style:

* datasets.run_sql  - read-only gate (keyword + multi-statement), row cap
  with ``truncated`` flag, owner-scoped views.
* retention         - ``waiting`` executions never purged (age + volume),
  orphan artifact FILES swept and counted.
* rules             - dunder names / oversized formulas rejected, math errors
  surface as ValueError (never OverflowError/ZeroDivisionError), valid rules
  unchanged.
* crypto            - Fernet key file written 0600; cross-owner credential
  resolution is "not found".
* owner scoping     - datasets.get_dataset, env_vars.load_env_map,
  agent_memory (namespaced keys), executor.execute_workflow.
* executor safety net - a runner crash finalizes the row as ``error``.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import time
import uuid
from datetime import datetime, timedelta, timezone

import pandas as pd
from sqlalchemy import delete, select

from app.config import settings
from app.db import AsyncSessionLocal
from app.models import (
    AgentMemory,
    Credential,
    Dataset,
    DatasetVersion,
    EnvVariable,
    ExecutionLog,
    Workflow,
)

DS_NAMES: list[str] = []  # registered in _mk_dataset, cleaned in _cleanup


async def _mk_dataset(name: str, rows: list[dict], owner_id: str | None = None) -> Dataset:
    from app.services import datasets as ds_svc

    DS_NAMES.append(name)
    async with AsyncSessionLocal() as db:
        ds = await ds_svc.create_from_df(db, name, pd.DataFrame(rows), source="api", owner_id=owner_id)
        await db.commit()
        return ds


async def _cleanup_datasets() -> None:
    from app.services import datasets as ds_svc

    async with AsyncSessionLocal() as db:
        rows = (
            (await db.execute(select(Dataset).where(Dataset.name.in_(DS_NAMES)))).scalars().all()
        )
        for row in rows:
            await db.execute(delete(DatasetVersion).where(DatasetVersion.dataset_id == row.id))
            shutil.rmtree(ds_svc.version_dir(row.id), ignore_errors=True)
            p = ds_svc.parquet_path(row.id)
            if p.exists():
                p.unlink()
            await db.delete(row)
        await db.commit()


# =====================================================================
# 1) run_sql: read-only gate, row cap, owner-scoped views
# =====================================================================


def test_run_sql_readonly_gate():
    async def _go():
        await _mk_dataset("AuditA", [{"n": i, "created": f"r{i}"} for i in range(8)], owner_id="owner-1")

        from app.services import datasets as ds_svc

        async with AsyncSessionLocal() as db:
            ok = await ds_svc.run_sql(db, "SELECT * FROM audita")
            assert ok["row_count"] == 8 and ok["truncated"] is False and "audita" in ok["views"]

            # comments + single trailing semicolon are fine
            ok = await ds_svc.run_sql(db, "/* lead */ SELECT COUNT(*) AS n FROM audita; -- tail")
            assert ok["rows"][0]["n"] == 8

            # whole-word verbs: a column named 'created' must NOT trip the gate
            ok = await ds_svc.run_sql(db, "SELECT created FROM audita WHERE n = 1")
            assert ok["rows"][0]["created"] == "r1"

            rejected = [
                "INSERT INTO audita VALUES (1)",
                "UPDATE audita SET n = 0",
                "DELETE FROM audita",
                "CREATE TABLE x (a int)",
                "DROP TABLE audita",
                "COPY audita TO 'x.parquet' (FORMAT PARQUET)",
                "ATTACH ':memory:' AS evil",
                "INSTALL httpfs",
                "LOAD httpfs",
                "PRAGMA database_list",
                "SET memory_limit = '1GB'",
                "EXPORT DATABASE 'dir'",
                "WITH t AS (DELETE FROM audita) SELECT * FROM t",
                "SELECT 1; SELECT 2",  # multi-statement
                "SELECT 1; DROP TABLE audita",
                "SELEKT broken",
                "",
            ]
            for sql in rejected:
                try:
                    await ds_svc.run_sql(db, sql)
                except ValueError:
                    pass
                else:
                    raise AssertionError(f"run_sql accepted: {sql!r}")

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup_datasets())


def test_run_sql_row_cap_truncates():
    async def _go():
        await _mk_dataset("AuditCap", [{"n": i} for i in range(9)], owner_id=None)

        from app.services import datasets as ds_svc

        original = settings.max_sql_rows
        try:
            settings.max_sql_rows = 5
            async with AsyncSessionLocal() as db:
                out = await ds_svc.run_sql(db, "SELECT * FROM auditcap")
                assert out["row_count"] == 5 and out["truncated"] is True
                assert [r["n"] for r in out["rows"]] == [0, 1, 2, 3, 4]

                out = await ds_svc.run_sql(db, "SELECT * FROM auditcap WHERE n < 2")
                assert out["row_count"] == 2 and out["truncated"] is False

                settings.max_sql_rows = 0  # 0 = unlimited (documented convention)
                out = await ds_svc.run_sql(db, "SELECT * FROM auditcap")
                assert out["row_count"] == 9 and out["truncated"] is False
        finally:
            settings.max_sql_rows = original

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup_datasets())


def test_run_sql_owner_scoped_views():
    async def _go():
        await _mk_dataset("AuditOwnA", [{"n": 1}], owner_id="owner-1")
        await _mk_dataset("AuditOwnB", [{"n": 2}], owner_id="owner-2")

        from app.services import datasets as ds_svc

        async with AsyncSessionLocal() as db:
            out = await ds_svc.run_sql(db, "SELECT * FROM auditowna", owner_id="owner-1")
            assert out["row_count"] == 1 and "auditownb" not in out["views"]

            # another owner's dataset is not even registered as a view
            try:
                await ds_svc.run_sql(db, "SELECT * FROM auditownb", owner_id="owner-1")
            except ValueError as exc:
                assert "auditownb" in str(exc)
            else:
                raise AssertionError("cross-owner dataset was queryable")

            # legacy call (no owner) still sees everything
            out = await ds_svc.run_sql(db, "SELECT COUNT(*) AS n FROM auditownb")
            assert out["rows"][0]["n"] == 1

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup_datasets())


# =====================================================================
# 2) retention: waiting never purged + orphan artifact file sweep
# =====================================================================


def _past(days: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


async def _mk_workflow(name: str) -> Workflow:
    async with AsyncSessionLocal() as db:
        wf = Workflow(name=name, graph={"nodes": [], "edges": []})
        db.add(wf)
        await db.commit()
        return wf


async def _rm_workflow(wf_id: str) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(delete(ExecutionLog).where(ExecutionLog.workflow_id == wf_id))
        await db.execute(delete(Workflow).where(Workflow.id == wf_id))
        await db.commit()


def test_retention_waiting_executions_are_never_purged():
    from app.services import retention

    async def _go():
        await retention.set_policy({"retention_days": 30, "max_executions_per_workflow": 0})
        wf = await _mk_workflow("Audit Retention WF")
        try:
            async with AsyncSessionLocal() as db:
                db.add(
                    ExecutionLog(
                        id="audit-old-success",
                        workflow_id=wf.id,
                        status="success",
                        started_at=_past(40),
                        finished_at=_past(40),
                        duration_ms=1,
                    )
                )
                db.add(
                    ExecutionLog(
                        id="audit-waiting",
                        workflow_id=wf.id,
                        status="waiting",  # webhook wait / human resume wait
                        started_at=_past(40),
                        finished_at=None,
                    )
                )
                await db.commit()

            out = await retention.purge_execution_data()
            assert out["deleted_by_age"] >= 1  # the old finished run went

            async with AsyncSessionLocal() as db:
                assert await db.get(ExecutionLog, "audit-old-success") is None
                assert await db.get(ExecutionLog, "audit-waiting") is not None

            # volume cap: the waiting row still survives (cap applies to
            # finished rows only) - two finished rows + cap 1 -> older deleted
            await retention.set_policy({"max_executions_per_workflow": 1})
            async with AsyncSessionLocal() as db:
                db.add(
                    ExecutionLog(
                        id="audit-mid-success",
                        workflow_id=wf.id,
                        status="success",
                        started_at=_past(2),
                        finished_at=_past(2),
                        duration_ms=1,
                    )
                )
                db.add(
                    ExecutionLog(
                        id="audit-new-success",
                        workflow_id=wf.id,
                        status="success",
                        started_at=_past(1),
                        finished_at=_past(1),
                        duration_ms=1,
                    )
                )
                await db.commit()
            out = await retention.purge_execution_data()
            assert out["deleted_by_volume"] >= 1
            async with AsyncSessionLocal() as db:
                assert await db.get(ExecutionLog, "audit-mid-success") is None
                assert await db.get(ExecutionLog, "audit-new-success") is not None
                assert await db.get(ExecutionLog, "audit-waiting") is not None
        finally:
            await _rm_workflow(wf.id)
            await retention.set_policy(  # restore defaults
                {"retention_days": 30, "max_executions_per_workflow": 0}
            )

    asyncio.run(_go())


def test_retention_sweeps_orphan_artifact_files(tmp_path):
    from app.services import artifacts as art_svc
    from app.services import retention

    async def _go():
        original_dir = settings.artifacts_dir
        settings.artifacts_dir = str(tmp_path)
        try:
            # DB-orphan: artifact row whose execution is long gone
            async with AsyncSessionLocal() as db:
                art = await art_svc.save_artifact(
                    db,
                    kind="chart",
                    data=b"orphan-row",
                    content_type="image/png",
                    execution_id="audit-gone-exec",
                )
                orphan_row_id = art.id
                await db.commit()
            orphan_file = tmp_path / f"{orphan_row_id}.png"
            assert orphan_file.exists()
            past_ts = time.time() - retention.ORPHAN_FILE_GRACE_SECONDS * 2
            os.utime(orphan_file, (past_ts, past_ts))  # aged out of the grace window

            # file-only orphans in the artifacts dir (no DB row at all):
            stale = tmp_path / ("f" * 32 + ".png")  # backdated -> swept
            stale.write_bytes(b"stale orphan")
            os.utime(stale, (past_ts, past_ts))
            fresh = tmp_path / ("e" * 32 + ".pkl")  # recent -> grace window keeps it
            fresh.write_bytes(b"fresh orphan")

            out = await retention.purge_execution_data()
            assert out["artifacts_deleted"] >= 1  # DB-orphan row (its file unlinked too)
            assert out["orphan_files_deleted"] == 1  # the row-less stale file
            assert not stale.exists() and fresh.exists() and not orphan_file.exists()

            async with AsyncSessionLocal() as db:
                assert await db.get(type(art), orphan_row_id) is None
        finally:
            settings.artifacts_dir = original_dir

    asyncio.run(_go())


# =====================================================================
# 3) rules: expression guards
# =====================================================================


def test_rules_formula_guards():
    from app.services import rules

    # dunder names rejected even when the record carries such a key
    for expr in ("__class__", "__import__('os')", "__builtins__ + 1"):
        try:
            rules.eval_formula(expr, {"__class__": 1, "__builtins__": 1})
        except ValueError as exc:
            assert (
                "forbidden name" in str(exc)
                or "allows only" in str(exc)
                or "invalid formula" in str(exc)
            ), str(exc)
        else:
            raise AssertionError(f"dunder formula accepted: {expr!r}")

    # attribute/call access still rejected
    for expr in ("a.b", "len(a)"):
        try:
            rules.eval_formula(expr, {"a": 1})
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe formula accepted: {expr!r}")

    # length cap
    long_expr = "1+" * 600 + "1"
    assert len(long_expr) > rules.MAX_FORMULA_LEN
    try:
        rules.eval_formula(long_expr, {})
    except ValueError as exc:
        assert "too long" in str(exc)
    else:
        raise AssertionError("oversized formula accepted")
    try:
        rules.validate_rules(
            [{"action": "set", "field": "x", "formula": long_expr}], [{"name": "x", "dtype": "number"}]
        )
    except ValueError as exc:
        assert "too long" in str(exc)
    else:
        raise AssertionError("oversized formula accepted by validate_rules")

    # math errors surface as ValueError (were OverflowError / ZeroDivisionError)
    for expr in ("1 / 0", "1e308 ** 2"):
        try:
            rules.eval_formula(expr, {})
        except ValueError:
            pass
        except (OverflowError, ZeroDivisionError) as exc:  # pragma: no cover
            raise AssertionError(f"{expr!r} raised {exc!r} instead of ValueError")

    # apply_rules: a crashing formula is rule fallout, NOT a 500
    schema = [{"name": "n", "dtype": "number"}]
    out, warnings = rules.apply_rules(
        [{"id": "r", "action": "set", "field": "n", "formula": "1 / 0"}],
        {"n": 5},
        "create",
        schema,
    )
    assert out["n"] == 5 and warnings == []

    # valid rules behave exactly as before
    out, warnings = rules.apply_rules(
        [{"id": "r", "action": "set", "field": "n", "formula": "n * 0.1"}],
        {"n": 100},
        "create",
        schema,
    )
    assert out["n"] == 10.0
    assert rules.eval_formula("n * 2 + 1", {"n": 3}) == 7.0


# =====================================================================
# 4) crypto: key file permissions + credential owner scoping
# =====================================================================


def test_fernet_key_file_written_0600(tmp_path, monkeypatch):
    from app.services import crypto

    key_file = tmp_path / ".fernet.key"
    monkeypatch.setattr(settings, "secret_key_file", key_file)
    monkeypatch.setattr(settings, "fernet_key", "")
    saved = crypto._fernet
    crypto._fernet = None
    try:
        crypto._get_fernet()
        assert key_file.exists()
        mode = stat.S_IMODE(key_file.stat().st_mode)
        assert mode == 0o600, f"key file mode {oct(mode)}, expected 0o600"

        # a loose pre-existing key file is repaired on read
        key_file.chmod(0o644)
        crypto._fernet = None
        crypto._get_fernet()
        assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    finally:
        crypto._fernet = saved  # restore global state untouched


def test_decrypt_credential_owner_scoping():
    from app.services import crypto

    async def _go():
        async with AsyncSessionLocal() as db:
            cred = Credential(
                name="audit cred",
                owner_id="owner-1",
                type="generic",
                data_encrypted=crypto.encrypt_payload({"api_key": "sk-audit-123"}),
            )
            db.add(cred)
            await db.commit()
            cred_id = cred.id

        class _Ctx:
            workflow_id = "wf-x"
            workflow_name = "WF X"

        try:
            got = await crypto.decrypt_credential(_Ctx(), cred_id, owner_id="owner-1")
            assert got["api_key"] == "sk-audit-123"
            try:
                await crypto.decrypt_credential(_Ctx(), cred_id, owner_id="owner-2")
            except LookupError:
                pass
            else:
                raise AssertionError("cross-owner credential was decrypted")
        finally:
            async with AsyncSessionLocal() as db:
                row = await db.get(Credential, cred_id)
                if row is not None:
                    await db.delete(row)
                await db.commit()

    asyncio.run(_go())


# =====================================================================
# 5) owner scoping on service loaders
# =====================================================================


def test_get_dataset_owner_scoping():
    from app.services import datasets as ds_svc

    async def _go():
        await _mk_dataset("AuditGet", [{"n": 1}], owner_id="owner-1")
        await _mk_dataset("AuditFree", [{"n": 2}], owner_id=None)

        async with AsyncSessionLocal() as db:
            row = await ds_svc.get_dataset(db, "AuditGet", owner_id="owner-1")
            assert row is not None
            # claimed by someone else -> not found
            assert await ds_svc.get_dataset(db, "AuditGet", owner_id="owner-2") is None
            # unclaimed rows stay visible to everyone
            assert await ds_svc.get_dataset(db, "AuditFree", owner_id="owner-2") is not None
            # legacy behavior preserved
            assert await ds_svc.get_dataset(db, "AuditGet") is not None

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup_datasets())


def test_load_env_map_owner_scoping():
    from app.services import env_vars

    async def _go():
        from app.services.crypto import encrypt_value

        async with AsyncSessionLocal() as db:
            db.add(EnvVariable(key="AUDIT_EV_OWNED", value_encrypted=encrypt_value("secret"), owner_id="owner-1"))
            db.add(EnvVariable(key="AUDIT_EV_FREE", value_encrypted=encrypt_value("open"), owner_id=None))
            await db.commit()
        try:
            env = await env_vars.load_env_map(owner_id="owner-2")
            assert "AUDIT_EV_FREE" in env and "AUDIT_EV_OWNED" not in env
            env = await env_vars.load_env_map(owner_id="owner-1")
            assert env["AUDIT_EV_OWNED"] == "secret" and env["AUDIT_EV_FREE"] == "open"
            env = await env_vars.load_env_map()  # legacy: everything
            assert "AUDIT_EV_OWNED" in env
        finally:
            async with AsyncSessionLocal() as db:
                await db.execute(
                    delete(EnvVariable).where(EnvVariable.key.in_(["AUDIT_EV_OWNED", "AUDIT_EV_FREE"]))
                )
                await db.commit()

    asyncio.run(_go())


def test_agent_memory_owner_namespacing():
    from app.services import agent_memory

    async def _go():
        try:
            await agent_memory.append_history("audit-mem", "hi", "hello", 10, owner_id="owner-1")
            got = await agent_memory.load_history("audit-mem", owner_id="owner-1")
            assert [m["role"] for m in got] == ["user", "assistant"]

            # a different owner sharing the same key sees nothing...
            assert await agent_memory.load_history("audit-mem", owner_id="owner-2") == []
            # ...and the legacy (unscoped) keyspace is untouched too
            assert await agent_memory.load_history("audit-mem") == []

            assert await agent_memory.clear_history("audit-mem", owner_id="owner-1") is True
            assert await agent_memory.clear_history("audit-mem", owner_id="owner-1") is False
        finally:
            for oid in (None, "owner-1", "owner-2"):
                await agent_memory.clear_history("audit-mem", owner_id=oid)

    asyncio.run(_go())


def test_agent_memory_rows_are_namespaced_in_db():
    from app.services import agent_memory

    async def _go():
        await agent_memory.append_history("audit-ns", "q", "a", 10, owner_id="owner-9")
        try:
            async with AsyncSessionLocal() as db:
                row = await db.get(AgentMemory, "owner-9::audit-ns")
                assert row is not None and len(row.messages) == 2
        finally:
            await agent_memory.clear_history("audit-ns", owner_id="owner-9")

    asyncio.run(_go())


def test_execute_workflow_owner_scoping():
    async def _go():
        wf = await _mk_workflow("Audit Owner WF")
        async with AsyncSessionLocal() as db:
            row = await db.get(Workflow, wf.id)
            row.owner_id = "owner-1"
            await db.commit()
        try:
            from app.services import executor

            # other owner's workflow == not found
            try:
                await executor.execute_workflow(wf.id, owner_id="owner-2")
            except LookupError:
                pass
            else:
                raise AssertionError("cross-owner workflow executed")

            # the owner gets past the gate (fails later on 'no trigger node')
            try:
                await executor.execute_workflow(wf.id, owner_id="owner-1")
            except ValueError as exc:
                assert "trigger" in str(exc).lower()
            else:
                raise AssertionError("expected the no-trigger validation error")

            async with AsyncSessionLocal() as db:
                logs = (
                    await db.execute(select(ExecutionLog).where(ExecutionLog.workflow_id == wf.id))
                ).scalars().all()
                assert logs == []  # rejected before any execution row is created
        finally:
            await _rm_workflow(wf.id)

    asyncio.run(_go())


def test_execute_workflow_crash_finalizes_row_as_error(monkeypatch):
    async def _go():
        from app.engine.runner import GraphRunner

        wf = await _mk_workflow("Audit Crash WF")
        async with AsyncSessionLocal() as db:
            row = await db.get(Workflow, wf.id)
            row.graph = {
                "nodes": [
                    {"id": "t", "type": "manual_trigger", "name": "T",
                     "position": {"x": 0, "y": 0}, "parameters": {"payload": {}}}
                ],
                "edges": [],
            }
            await db.commit()
        exec_id = "audit-crash-exec-1"

        async def _boom(self):
            raise RuntimeError("boom - engine blew up")

        monkeypatch.setattr(GraphRunner, "run", _boom)
        try:
            from app.services import executor

            result = await executor.execute_workflow(wf.id, execution_id=exec_id)
            assert result["status"] == "error" and "boom" in result["error"]
            async with AsyncSessionLocal() as db:
                log = await db.get(ExecutionLog, exec_id)
                assert log is not None
                assert log.status == "error" and "boom" in (log.error or "")
                assert log.finished_at is not None
        finally:
            monkeypatch.undo()
            await _rm_workflow(wf.id)

    asyncio.run(_go())
