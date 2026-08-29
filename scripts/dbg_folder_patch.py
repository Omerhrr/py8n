import asyncio
import httpx
from app.main import app

API = "http://testserver/api/v1"

async def go():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API) as c:
        r = await c.post("/folders", json={"name": "dbg root"})
        root = r.json(); print("root:", r.status_code, root.get("id"))
        r = await c.post("/folders", json={"name": "dbg child", "parent_id": root["id"]})
        child = r.json(); print("child:", r.status_code)
        r = await c.post("/folders", json={"name": "dbg gc", "parent_id": child["id"]})
        gc = r.json(); print("gc:", r.status_code, gc.get("id"))
        r = await c.patch(f"/folders/{gc['id']}", json={"parent_id": root["id"]})
        print("reparent gc->root:", r.status_code, r.text[:200])
        # cleanup
        for fid in (gc["id"], child["id"], root["id"]):
            await c.delete(f"/folders/{fid}")

asyncio.run(go())
