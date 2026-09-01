"""V42 feature tests: the readymade gallery ships as importable packs.

New machinery:
    GET /templates/{id}/pack   one template as a py8n-pack document
                               (manifest.source == "py8n-gallery", the
                               template id rides manifest.template_ids)
    GET /templates/gallery/pack  the WHOLE gallery in one file; declared
                               before the parameterized routes so "gallery"
                               is never eaten as a template id
    Import story reuses /packs/inspect + /packs/import unchanged - a gallery
    pack is just a pack, so cross-instance portability comes for free.

Same harness as v4-v41: httpx ASGITransport in-process, per-test asyncio.run,
uuid-suffixed data, finally-cleanup + background drain. Fully offline.
"""

from __future__ import annotations

import asyncio
import uuid

import httpx

from app.main import app

API = "http://testserver/api/v1"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


async def _drain_background() -> None:
    from app.services import executor as executor_mod

    tasks = [t for t in executor_mod._background_tasks if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _cleanup(workflow_ids: list[str]) -> None:
    async with _client() as client:
        for wid in workflow_ids:
            try:
                await client.delete(f"/workflows/{wid}")
            except Exception:
                pass
    await _drain_background()


# ------------------------------------------------------------------ test 1
def test_v42_health_pin():
    """Strict version pin lives in the latest wave only (convention)."""

    async def _go():
        async with _client() as client:
            res = await client.get("/health")
            assert res.status_code == 200, res.text
            return res.json()

    body = asyncio.run(_go())
    assert body["app"] == "Py8n"
    assert body["version"] >= "1.42.0", f"expected at least 1.42.0, got {body['version']}"


# ------------------------------------------------------------------ test 2
def test_v42_single_template_pack_roundtrip():
    """One template -> pack -> inspect -> import as an inactive workflow."""
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []

    async def _go():
        async with _client() as client:
            catalog = (await client.get("/templates")).json()
            tpl = next(t for t in catalog if t["id"] == "ai-writer")

            res = await client.get("/templates/ai-writer/pack")
            assert res.status_code == 200, res.text
            pack = res.json()

            # the pack speaks the standard format and passes the standard gates
            res = await client.post("/packs/inspect", json=pack)
            assert res.status_code == 200, res.text
            ins = res.json()
            assert ins["workflow_count"] == 1 and ins["workflows"][0]["valid"] is True

            res = await client.post("/packs/import", json=pack)
            assert res.status_code == 201, res.text
            imp = res.json()
            assert len(imp["workflows"]) == 1 and imp["skipped"] == []
            wf_ids.append(imp["workflows"][0]["id"])
            detail = (await client.get(f"/workflows/{wf_ids[0]}")).json()
            assert detail["is_active"] is False
            assert len(detail["graph"]["nodes"]) == tpl["node_count"]
            return pack

    try:
        pack = asyncio.run(_go())
        assert pack["format"] == "py8n-pack" and pack["pack_version"] == 1
        assert pack["manifest"]["source"] == "py8n-gallery"
        assert pack["manifest"]["template_ids"] == ["ai-writer"]
        assert pack["manifest"]["workflow_count"] == 1 and pack["manifest"]["dataset_count"] == 0
        assert pack["datasets"] == []
        assert any(w["name"] == "AI Copywriter" for w in pack["workflows"])
    finally:
        asyncio.run(_cleanup(wf_ids))


# ------------------------------------------------------------------ test 3
def test_v42_gallery_pack_whole_catalog():
    """The gallery pack carries every template and imports as a batch."""
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []

    async def _go():
        async with _client() as client:
            catalog = (await client.get("/templates")).json()
            res = await client.get("/templates/gallery/pack")
            assert res.status_code == 200, res.text
            pack = res.json()

            res = await client.post("/packs/inspect", json=pack)
            ins = res.json()
            assert ins["workflow_count"] == len(catalog)
            assert all(w["valid"] for w in ins["workflows"]), [w for w in ins["workflows"] if not w["valid"]]

            # import a SUBSET (first three) to keep cleanup light
            subset = {**pack, "workflows": pack["workflows"][:3]}
            res = await client.post("/packs/import", json=subset)
            assert res.status_code == 201, res.text
            imp = res.json()
            assert len(imp["workflows"]) == 3 and imp["skipped"] == []
            wf_ids.extend(w["id"] for w in imp["workflows"])
            return pack, catalog, imp

    try:
        pack, catalog, imp = asyncio.run(_go())
        assert pack["manifest"]["source"] == "py8n-gallery"
        assert pack["manifest"]["workflow_count"] == len(catalog)
        assert set(pack["manifest"]["template_ids"]) == {t["id"] for t in catalog}
        assert "ai_agent" in pack["manifest"]["node_types"]  # the v34 agent template rides along
        created_names = {w["name"] for w in imp["workflows"]}
        assert created_names and created_names <= {t["name"] for t in catalog}
    finally:
        asyncio.run(_cleanup(wf_ids))


# ------------------------------------------------------------------ test 4
def test_v42_unknown_template_404():
    async def _go():
        async with _client() as client:
            res = await client.get("/templates/does-not-exist/pack")
            assert res.status_code == 404, res.text
            # gallery/pack must never be captured as a template id
            res = await client.get("/templates/gallery/pack")
            assert res.status_code == 200, res.text
            return res.json()["manifest"]["source"]

    assert asyncio.run(_go()) == "py8n-gallery"
