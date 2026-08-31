"""V35 feature tests: brand and polish wave.

Three concerns:
* health pin moves to 1.35.0 (strict pin lives in the latest wave only)
* dash hygiene - the product source carries ZERO em dashes (U+2014) and
  en dashes (U+2013); the sweep replaced them all with plain hyphens and
  this test keeps it that way (guards every future wave)
* brand assets - the Py8n logo exists as a component (inline SVG mark:
  figure-8 workflow loops + amber node dot on an orange-rose tile), as a
  standalone /logo.svg served from public/, and is wired as the favicon
"""

from __future__ import annotations

import asyncio

import httpx

from app.main import app

API = "http://testserver/api/v1"

EM = "\u2014"  # -
EN = "\u2013"  # -

REPO_ROOT = "/home/z/my-project"

# product source only (what ships to users); build dirs, artifacts and the
# platform's own skills/ tree are out of scope
SCAN_TARGETS = [
    "mini-services/api-backend/app",
    "mini-services/api-backend/tests",
    "mini-services/api-backend/scripts",
    "pages",
    "components",
    "composables",
    "layouts",
    "stores",
    "types",
    "nuxt.config.ts",
    "README.md",
    "package.json",
    "docker-compose.yml",
]
SCAN_EXT = {
    ".py", ".ts", ".tsx", ".mjs", ".js", ".vue", ".md", ".json", ".yml",
    ".yaml", ".txt", ".sh", ".css", ".html", ".toml",
}


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


async def _drain_background() -> None:
    from app.services import executor as executor_mod

    tasks = [t for t in executor_mod._background_tasks if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# ------------------------------------------------------------------ test 1
def test_v35_health_pin():
    """Strict version pin lives in the latest wave only (v35 convention)."""

    async def _go():
        async with _client() as client:
            res = await client.get("/health")
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["app"] == "Py8n" and body["version"] >= "1.35.0", body

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


# ------------------------------------------------------------------ test 2
def test_no_em_dashes_in_product_source():
    """Zero em/en dashes across product source; offenders listed on failure."""

    import os

    offenders: list[str] = []
    for target in SCAN_TARGETS:
        path = os.path.join(REPO_ROOT, target)
        if os.path.isfile(path):
            files = [path]
        else:
            files = []
            for dirpath, dirnames, filenames in os.walk(path):
                dirnames[:] = [d for d in dirnames if d not in {"__pycache__", "node_modules"}]
                for name in filenames:
                    if os.path.splitext(name)[1].lower() in SCAN_EXT:
                        files.append(os.path.join(dirpath, name))
        for file_path in files:
            try:
                with open(file_path, "r", encoding="utf-8") as fh:
                    text = fh.read()
            except (UnicodeDecodeError, OSError):
                continue
            if EM in text or EN in text:
                offenders.append(os.path.relpath(file_path, REPO_ROOT))
    assert not offenders, (
        f"em/en dashes found in {len(offenders)} file(s): {offenders[:10]} - "
        "run scripts/remove_em_dashes.py to sweep them"
    )


# ------------------------------------------------------------------ test 3
def test_logo_brand_assets():
    """The Py8n mark exists as component + public SVG and is the favicon."""

    import os

    # component: inline SVG mark with the signature geometry
    comp = os.path.join(REPO_ROOT, "components", "Py8nLogo.vue")
    assert os.path.isfile(comp), "components/Py8nLogo.vue missing"
    comp_src = open(comp, encoding="utf-8").read()
    for fragment in ('viewBox="0 0 64 64"', "py8n-tile", "#F97316", "#F43F5E", "#FCD34D", "Py8n"):
        assert fragment in comp_src, f"logo component missing {fragment!r}"

    # standalone asset: valid SVG, same gradient + title for a11y
    svg_path = os.path.join(REPO_ROOT, "public", "logo.svg")
    assert os.path.isfile(svg_path), "public/logo.svg missing"
    svg_src = open(svg_path, encoding="utf-8").read()
    assert svg_src.lstrip().startswith("<?xml") or "<svg" in svg_src[:200]
    for fragment in ('viewBox="0 0 64 64"', "py8n-tile", "<title>Py8n</title>"):
        assert fragment in svg_src, f"logo.svg missing {fragment!r}"

    # favicon wired in the Nuxt head config
    nuxt_src = open(os.path.join(REPO_ROOT, "nuxt.config.ts"), encoding="utf-8").read()
    assert "/logo.svg" in nuxt_src, "favicon link to /logo.svg missing in nuxt.config.ts"
