"""v102 tests - the deployed-system identity, the estate health overview,
and the operator update lifecycle.

Three platform pillars this round, all serving the thesis "build a
system -> deploy it -> people actually use it":

* DEPLOYED-SYSTEM IDENTITY: a system_deployments row per system carries
  the custom domain (normalized, reserved-refused, globally unique),
  the environment (staging | production), the LOUD status machine
  (offline -> live -> paused -> offline, every move on the operations
  log + the event thread) and the login surface's branding. The public
  domain door (NO auth on the route - a login surface shows who it is
  BEFORE authentication) resolves a live domain back to the system's
  identity; a paused/offline deployment answers 404, never a pretty
  lie. Users/roles are NOT re-invented: the deployment rides v62's
  system_members (the authed domain answer carries my_role).
* ESTATE HEALTH OVERVIEW: one row per visible system - the status dot
  (running / attention / hold), the 7d success rate, failed workflows,
  overdue instances, open escalations - composed from what the platform
  already keeps (derived, never stored), batched (not N queries).
* THE UPDATE LIFECYCLE: "what's going to change if I upgrade?" is a
  first-class read (the SAME reconcile plan the upgrade runs - zero
  drift), and an applied-but-unruled upgrade is PENDING until a human
  accepts it (settled history) or rolls it back (unbind exactly what it
  bound; the imported objects stay in the estate, unbound).
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402

API = "http://testserver/api/v1"


@pytest.fixture(autouse=True)
def _clean_event_subscribers():
    from app.services import system_events as events_svc

    events_svc._subscribers.clear()
    yield
    events_svc._subscribers.clear()


@pytest.fixture(autouse=True)
def _fresh_rate_limit():
    from app.api import _ratelimit

    _ratelimit.reset_all()
    yield
    _ratelimit.reset_all()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


async def _drain_background() -> None:
    from app.services import executor as executor_mod
    from app.services import system_events as events_svc

    for _ in range(5):
        tasks = [t for t in events_svc._DISPATCH_TASKS if not t.done()]
        if not tasks:
            break
        await asyncio.gather(*tasks, return_exceptions=True)
    tasks = [t for t in executor_mod._background_tasks if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _sync(coro):
    return asyncio.run(coro)


async def _wrap(coro):
    try:
        return await coro
    finally:
        await _drain_background()


async def _mk_user(client: httpx.AsyncClient, tag: str) -> dict:
    res = await client.post("/auth/register", json={
        "email": f"v102-{tag}-{uuid.uuid4().hex[:6]}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v102 {tag}",
    })
    assert res.status_code == 201, res.text
    return {"token": res.json()["token"], "id": res.json()["user"]["id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _node(nid: str, ntype: str, params: dict | None = None) -> dict:
    return {"id": nid, "type": ntype, "name": nid,
            "position": {"x": 0, "y": 0}, "parameters": params or {}}


def _graph() -> dict:
    return {"nodes": [_node("t1", "manual_trigger")], "edges": []}


async def _mk_system(client: httpx.AsyncClient, headers: dict, name: str) -> dict:
    res = await client.post("/systems", headers=headers, json={"name": name})
    assert res.status_code == 201, res.text
    return res.json()


def _db():
    from app.db import AsyncSessionLocal

    return AsyncSessionLocal()


# ---------------------------------------------------------------------------
# 1. the deployed identity: domain discipline, the loud status machine,
#    the public domain door
# ---------------------------------------------------------------------------

def test_v102_deployment_identity():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "deploy")
            h = _auth(user["token"])
            s1 = await _mk_system(client, h, "Acme Sales")
            s2 = await _mk_system(client, h, "Acme Finance")

            # a deployment does not exist until it is PUT
            res = await client.get(f"/systems/{s1['id']}/deployment", headers=h)
            assert res.status_code == 200
            assert res.json()["deployment"] is None

            # deploying before configuring refuses loud
            res = await client.post(f"/systems/{s1['id']}/deployment/deploy", headers=h)
            assert res.status_code == 400
            assert "no deployment record" in res.json()["detail"]

            # the domain is NORMALIZED (case + trailing dot) on the way in
            res = await client.put(f"/systems/{s1['id']}/deployment", headers=h,
                                   json={"domain": "Sales.Acme.COM.",
                                         "environment": "production",
                                         "branding": {"accent": "#38bdf8",
                                                      "tagline": "The Acme revenue line",
                                                      "login_headline": "Acme Operations",
                                                      "logo": "boxes"}})
            assert res.status_code == 200, res.text
            dep = res.json()["deployment"]
            assert dep["domain"] == "sales.acme.com"
            assert dep["url"] == "https://sales.acme.com"
            assert dep["environment"] == "production"
            assert dep["status"] == "offline"  # not deployed yet
            assert dep["branding"]["login_headline"] == "Acme Operations"

            # unusable hostnames refuse loud, naming the honest rule
            for bad in ("ops", "localhost", "foo.local", "bad domain", "acme..com"):
                res = await client.put(f"/systems/{s2['id']}/deployment", headers=h,
                                       json={"domain": bad})
                assert res.status_code == 400, bad
            # a reserved NAME refuses even when it is shaped like a domain
            res = await client.put(f"/systems/{s2['id']}/deployment", headers=h,
                                   json={"domain": "example.com"})
            assert res.status_code == 400
            # unknown branding keys refuse loud
            res = await client.put(f"/systems/{s2['id']}/deployment", headers=h,
                                   json={"branding": {"colr": "#38bdf8"}})
            assert res.status_code == 400
            # a broken accent refuses loud
            res = await client.put(f"/systems/{s2['id']}/deployment", headers=h,
                                   json={"branding": {"accent": "blue"}})
            assert res.status_code == 400

            # one domain, one system - the second claim is a 409
            res = await client.put(f"/systems/{s2['id']}/deployment", headers=h,
                                   json={"domain": "sales.acme.com"})
            assert res.status_code == 409
            assert "already answers" in res.json()["detail"]

            # s2 gets its own identity and deploys (production, explicitly)
            res = await client.put(f"/systems/{s2['id']}/deployment", headers=h,
                                   json={"domain": "finance.acme.com",
                                         "environment": "production"})
            assert res.status_code == 200
            res = await client.post(f"/systems/{s2['id']}/deployment/deploy", headers=h)
            assert res.status_code == 200
            dep2 = res.json()["deployment"]
            assert dep2["status"] == "live"
            assert dep2["deployed_at"]

            # the PUBLIC domain door (no auth header on purpose): a live
            # deployment presents its identity, with the branding defaults
            # filled from the system itself
            res = await client.get("/systems/by-domain/finance.acme.com")
            assert res.status_code == 200, res.text
            ident = res.json()
            assert ident["system"]["name"] == "Acme Finance"
            assert ident["environment"] == "production"
            assert ident["branding"]["accent"]  # falls back to the system color
            # auth-off mode (the dev convention): an anonymous caller holds
            # the platform's single-operator role - the door reports it
            assert ident["my_role"] == "owner"

            # with a token the door answers with the caller's role
            res = await client.get("/systems/by-domain/finance.acme.com", headers=h)
            assert res.json()["my_role"] == "owner"

            # an unknown domain is an honest 404, and so is a dark one
            res = await client.get("/systems/by-domain/nowhere.example.org")
            assert res.status_code == 404
            res = await client.post(f"/systems/{s2['id']}/deployment/pause", headers=h)
            assert res.status_code == 200
            assert res.json()["deployment"]["status"] == "paused"
            res = await client.get("/systems/by-domain/finance.acme.com")
            assert res.status_code == 404
            assert "not live" in res.json()["detail"]

            # pause from anything-but-live refuses; retire works from paused
            res = await client.post(f"/systems/{s2['id']}/deployment/pause", headers=h)
            assert res.status_code == 400
            res = await client.post(f"/systems/{s2['id']}/deployment/retire", headers=h)
            assert res.status_code == 200
            assert res.json()["deployment"]["status"] == "offline"
            res = await client.post(f"/systems/{s2['id']}/deployment/deploy", headers=h)
            assert res.status_code == 200  # re-deploy from offline is a real move

            # every move is on the record
            res = await client.get(f"/systems/{s2['id']}/operations", headers=h)
            verbs = [o["verb"] for o in res.json()["operations"]]
            for expected in ("deployment_updated", "deployed", "deployment_paused",
                             "deployment_retired"):
                assert expected in verbs, verbs
            res = await client.get(f"/systems/{s2['id']}/events", headers=h)
            types = [e["type"] for e in res.json()["events"]]
            assert "system.deployed" in types
            assert "system.deployment_retired" in types

            # clearing the domain is a real move (url never lies after it)
            res = await client.put(f"/systems/{s1['id']}/deployment", headers=h,
                                   json={"domain": ""})
            assert res.status_code == 200
            dep = res.json()["deployment"]
            assert dep["domain"] == ""
            assert dep["url"] == ""

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 2. the estate health overview - the sketch made real
# ---------------------------------------------------------------------------

def test_v102_estate_health_overview():
    async def _go():
        from app.models import (BusinessProcess, BusinessProcessInstance,
                                ExecutionLog, SystemComponent, Workflow)

        async with _client() as client:
            user = await _mk_user(client, "health")
            h = _auth(user["token"])
            sales = await _mk_system(client, h, "v102 Sales")
            finance = await _mk_system(client, h, "v102 Finance")

            async with _db() as db:
                # Sales: two workflows (one mostly-fine with a failure, one clean)
                wf1 = Workflow(name="v102 sales sync", graph=_graph(), is_active=True,
                               owner_id=user["id"])
                wf2 = Workflow(name="v102 sales tidy", graph=_graph(), is_active=True,
                               owner_id=user["id"])
                db.add_all([wf1, wf2])
                await db.flush()
                now = datetime.now(timezone.utc)
                for _ in range(3):
                    db.add(ExecutionLog(workflow_id=wf1.id, status="success",
                                        started_at=now - timedelta(hours=1)))
                db.add(ExecutionLog(workflow_id=wf1.id, status="error",
                                    error="boom", started_at=now - timedelta(hours=2)))
                db.add(ExecutionLog(workflow_id=wf2.id, status="success",
                                    started_at=now - timedelta(minutes=30)))
                # Sales: a process with TWO overdue open instances, one of
                # them carrying an UNACKED escalation episode
                proc = BusinessProcess(
                    name="v102 pipeline",
                    definition={"states": ["lead", "won"], "initial": "lead",
                                "transitions": [{"name": "close", "from": "lead", "to": "won"}]},
                    owner_id=user["id"])
                db.add(proc)
                await db.flush()
                inst_overdue_escalating = BusinessProcessInstance(
                    process_id=proc.id, owner_id=user["id"], ref="L-1",
                    title="Lead one", state="lead",
                    due_at=now - timedelta(hours=5),
                    context={"escalations": {"count": 2, "state": "lead",
                                             "last_at": (now - timedelta(hours=1)).isoformat()}})
                inst_overdue_quiet = BusinessProcessInstance(
                    process_id=proc.id, owner_id=user["id"], ref="L-2",
                    title="Lead two", state="lead",
                    due_at=now - timedelta(hours=2), context={})
                db.add_all([inst_overdue_escalating, inst_overdue_quiet])
                for kind, ref in (("workflow", wf1.id), ("workflow", wf2.id),
                                  ("process", proc.id)):
                    db.add(SystemComponent(system_id=sales["id"], kind=kind, ref_id=ref))
                # Finance: one clean workflow, no processes
                wf3 = Workflow(name="v102 fin close", graph=_graph(), is_active=True,
                               owner_id=user["id"])
                db.add(wf3)
                await db.flush()
                db.add(ExecutionLog(workflow_id=wf3.id, status="success",
                                    started_at=now - timedelta(minutes=10)))
                db.add(SystemComponent(system_id=finance["id"], kind="workflow",
                                       ref_id=wf3.id))
                # a deployment rides one row (identity in the health answer;
                # a UNIQUE domain per test - the suite shares one database)
                from app.models import SystemDeployment
                db.add(SystemDeployment(system_id=sales["id"],
                                        domain="sales.v102.test",
                                        environment="production",
                                        status="live"))
                await db.commit()
                wf1_id, wf2_id, wf3_id = wf1.id, wf2.id, wf3.id

            res = await client.get("/systems/health/overview", headers=h)
            assert res.status_code == 200, res.text
            body = res.json()
            rows = {r["id"]: r for r in body["systems"]}
            assert rows[sales["id"]]["status"] == "attention"
            assert rows[sales["id"]]["success_rate_7d"] == 80.0  # 4 of 5 (both workflows)
            assert rows[sales["id"]]["failed_workflows_7d"] == 1
            assert rows[sales["id"]]["overdue"] == 2
            assert rows[sales["id"]]["escalations"] == 1  # the unacked episode
            assert rows[sales["id"]]["deployment"]["domain"] == "sales.v102.test"
            assert rows[finance["id"]]["status"] == "running"
            assert rows[finance["id"]]["success_rate_7d"] == 100.0
            assert rows[finance["id"]]["overdue"] == 0
            assert rows[finance["id"]]["deployment"] is None
            assert body["counts"]["attention"] >= 1
            assert body["counts"]["running"] >= 1

            # a paused system wears hold, and a fresh one shows a dash (never 0%)
            res = await client.post(f"/systems/{finance['id']}/pause", headers=h, json={})
            assert res.status_code == 200
            res = await client.get("/systems/health/overview", headers=h)
            rows = {r["id"]: r for r in res.json()["systems"]}
            assert rows[finance["id"]]["status"] == "hold"
            fresh = await _mk_system(client, h, "v102 Fresh")
            rows = {r["id"]: r for r in (await client.get(
                "/systems/health/overview", headers=h)).json()["systems"]}
            assert rows[fresh["id"]]["status"] == "running"
            assert rows[fresh["id"]]["success_rate_7d"] is None

            # the row's identity is the system's own summary shape
            assert rows[sales["id"]]["name"] == "v102 Sales"
            assert rows[sales["id"]]["lifecycle"] == "running"
            assert wf1_id and wf2_id and wf3_id  # keep the linters honest

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 3. the update lifecycle: preview -> apply -> see changes -> accept/rollback
# ---------------------------------------------------------------------------

def _pack(workflows: list[tuple[str, str]]) -> dict:
    """A minimal honest pack - each entry (name, node type) becomes a
    one-node workflow graph the import machinery accepts."""
    return {
        "format": "py8n-pack",
        "pack_version": 1,
        "workflows": [
            {"name": name, "description": "",
             "graph": {"nodes": [_node("t1", ntype)], "edges": []}}
            for name, ntype in workflows
        ],
        "datasets": [],
    }


def test_v102_update_lifecycle():
    async def _go():
        from sqlalchemy import select

        from app.models import Solution

        async with _client() as client:
            user = await _mk_user(client, "update")
            h = _auth(user["token"])

            # a solution whose pack we control (direct row - the author
            # endpoint packs FROM the estate; this test needs to GROW a pack)
            async with _db() as db:
                sol = Solution(
                    slug=f"v102-ops-suite-{uuid.uuid4().hex[:6]}",
                    name="v102 Ops Suite", tagline="", category="Operations",
                    outcomes_json=["one workflow"], docs="",
                    pack_json=_pack([("v102 Handler", "manual_trigger")]),
                    owner_id=user["id"])
                db.add(sol)
                await db.commit()
                slug = sol.slug

            res = await client.post(f"/solutions/{slug}/install", headers=h,
                                    json={"as_system": True})
            assert res.status_code == 200, res.text
            sid = res.json()["system"]["id"]
            res = await client.get(f"/systems/{sid}", headers=h)
            assert res.json()["source_solution_slug"] == slug
            # the shape the install left: the pack's original binding
            wf_installed = {c["ref_id"] for c in res.json()["grouped"]["workflow"]}

            # everything the pack offers is already bound -> idempotent preview
            res = await client.get(f"/systems/{sid}/update/preview", headers=h)
            assert res.status_code == 200, res.text
            prev = res.json()
            assert prev["updatable"] is True
            assert prev["idempotent"] is True
            assert prev["adds"] == {"workflow": [], "dataset": []}
            assert prev["pending"] is None
            assert "change nothing" in prev["note"]

            # the pack GROWS - now the preview names exactly what will change
            async with _db() as db:
                row = (await db.execute(
                    select(Solution).where(Solution.slug == slug))).scalar_one()
                row.pack_json = _pack([("v102 Handler", "manual_trigger"),
                                       ("v102 Extra", "manual_trigger")])
                await db.commit()

            res = await client.get(f"/systems/{sid}/update/preview", headers=h)
            prev = res.json()
            assert prev["idempotent"] is False
            assert prev["adds"]["workflow"] == ["v102 Extra"]
            assert prev["already_bound"]["workflow"] == 1

            # applying puts the changeset ON THE TABLE - pending until ruled on
            res = await client.post(f"/systems/{sid}/upgrade", headers=h)
            assert res.status_code == 200, res.text
            assert res.json()["added"]["workflow"] == 1
            res = await client.get(f"/systems/{sid}/update/preview", headers=h)
            pending = res.json()["pending"]
            assert pending is not None
            assert [w["name"] for w in pending["added_refs"]["workflow"]] == ["v102 Extra"]

            # ROLLBACK: unbind exactly what it bound - the system returns to
            # its pre-upgrade shape (the INSTALL's shape); the imported
            # object stays in the estate
            res = await client.post(f"/systems/{sid}/update/rollback", headers=h)
            assert res.status_code == 200, res.text
            rb = res.json()
            assert rb["rolled_back"] is True
            assert len(rb["unbound"]) == 1
            assert rb["unbound"][0]["name"] == "v102 Extra"
            res = await client.get(f"/systems/{sid}", headers=h)
            wf_after = {c["ref_id"] for c in res.json()["grouped"]["workflow"]}
            assert wf_after == wf_installed
            # the rolled-back object survives in the estate (unbound, not deleted)
            res = await client.get("/workflows", headers=h, params={"limit": 100})
            assert any(w["name"] == "v102 Extra" for w in res.json())
            res = await client.get(f"/systems/{sid}/update/preview", headers=h)
            assert res.json()["pending"] is None

            # accept when nothing is pending refuses loud
            res = await client.post(f"/systems/{sid}/update/accept", headers=h)
            assert res.status_code == 409
            assert "nothing to accept" in res.json()["detail"]

            # upgrade again, this time ACCEPT: settled history, nothing pending
            res = await client.post(f"/systems/{sid}/upgrade", headers=h)
            assert res.status_code == 200
            res = await client.post(f"/systems/{sid}/update/accept", headers=h)
            assert res.status_code == 200, res.text
            assert res.json()["accepted"] is True
            res = await client.get(f"/systems/{sid}/update/preview", headers=h)
            assert res.json()["pending"] is None
            # accepting twice refuses - the trail moved on
            res = await client.post(f"/systems/{sid}/update/accept", headers=h)
            assert res.status_code == 409

            # the whole loop is on the operations log
            res = await client.get(f"/systems/{sid}/operations", headers=h)
            verbs = [o["verb"] for o in res.json()["operations"]]
            assert "upgrade_rolled_back" in verbs
            assert "upgrade_accepted" in verbs

            # a hand-built system answers honestly: nothing to update from
            hand = await _mk_system(client, h, "v102 hand-built")
            res = await client.get(f"/systems/{hand['id']}/update/preview", headers=h)
            prev = res.json()
            assert prev["updatable"] is False
            assert prev["path"] == "none"
            # an OPERATOR-installed system answers the honest reinstall path
            async with _db() as db:
                from app.models import Py8nSystem
                row = (await db.execute(
                    select(Py8nSystem).where(Py8nSystem.id == hand["id"]))).scalar_one()
                row.source_solution_slug = "operator:revenue"
                await db.commit()
            res = await client.get(f"/systems/{hand['id']}/update/preview", headers=h)
            prev = res.json()
            assert prev["updatable"] is False
            assert prev["path"] == "operator_reinstall"
            assert prev["operator"] == "revenue"
            assert "reinstalling" in prev["note"]

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 4. units + the version pin
# ---------------------------------------------------------------------------

def test_v102_units_and_version():
    from app.services.system_deployment import (
        DeploymentError,
        normalize_domain,
        validate_branding,
    )

    # normalization: case + one trailing dot fold away
    assert normalize_domain("  Ops.Acme.COM.  ") == "ops.acme.com"
    with pytest.raises(DeploymentError):
        normalize_domain("")
    with pytest.raises(DeploymentError):
        normalize_domain("ops")           # a bare label is not a domain
    with pytest.raises(DeploymentError):
        normalize_domain("has space.com")
    with pytest.raises(DeploymentError):
        normalize_domain("-bad.com")
    with pytest.raises(DeploymentError):
        normalize_domain("localhost")
    with pytest.raises(DeploymentError):
        normalize_domain("pane.example.local")  # reserved suffix
    with pytest.raises(DeploymentError):
        normalize_domain("example.org")   # reserved exact
    assert normalize_domain("a.b.c.d.example.io") == "a.b.c.d.example.io"

    # branding: only the four keys, accent is a hex, lengths bounded
    assert validate_branding(None) == {}
    assert validate_branding({"accent": "#38bdf8", "tagline": " hi "}) == {
        "accent": "#38bdf8", "tagline": "hi"}
    with pytest.raises(DeploymentError):
        validate_branding({"color": "#38bdf8"})
    with pytest.raises(DeploymentError):
        validate_branding({"accent": "blue"})
    with pytest.raises(DeploymentError):
        validate_branding({"tagline": "x" * 200})

    assert settings.version == "1.102.0"
