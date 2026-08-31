"""V30 feature tests: Forms & Business Rules.

* Rules engine: block / warn / set (constant or safe AST formula) evaluated
  on record create/update; events create|update|always; block short-circuits.
* Rules management: GET/PUT /apps/{ref}/rules - PUT works on PUBLISHED apps
  (rules are governance, the layout lock does not apply); 12 invalid-rule
  shapes rejected; rules also validated inside PATCH /apps config.
* Form field options: string | object fields (required, default, options,
  placeholder, label) - server-enforced on create (all form fields) and on
  update (touched fields only, legacy gaps don't block unrelated edits).
* Standalone forms: GET /apps/{slug}/form descriptor + POST form-submit for
  anonymous collection; rules fire on form submissions too.
* POST /apps/{ref}/rules/test - dry-run without mutating data.

Runs the FastAPI app in-process via httpx ASGITransport (same harness as v4-v29).
"""

from __future__ import annotations

import asyncio

import httpx

from app.main import app

API = "http://testserver/api/v1"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


async def _cleanup(app_refs: list[str], dataset_refs: list[str]) -> None:
    async with _client() as client:
        for ref in app_refs:
            try:
                await client.delete(f"/apps/{ref}")
            except Exception:
                pass
        for ref in dataset_refs:
            try:
                await client.delete(f"/datasets/{ref}")
            except Exception:
                pass


ROWS = [
    {"name": "Alice", "plan": "starter", "ltv": 1200, "commission": 120, "active": True},
    {"name": "Bob", "plan": "pro", "ltv": 3400, "commission": 340, "active": True},
    {"name": "Cara", "plan": "enterprise", "ltv": 9800, "commission": 980, "active": False},
    {"name": "Dan", "plan": "starter", "ltv": 900, "commission": 90, "active": True},
    {"name": "Eve", "plan": "enterprise", "ltv": 12450, "commission": 1245, "active": True},
    {"name": "Finn", "plan": "pro", "ltv": 3100, "commission": 310, "active": False},
]

RULES = [
    {
        "id": "r_block",
        "name": "LTV cap",
        "event": "create",
        "when": {"all": [{"field": "ltv", "op": "gt", "value": 15000}]},
        "action": "block",
        "message": "LTV above 15000 needs sign-off",
    },
    {
        "id": "r_warn",
        "name": "Big deal",
        "event": "always",
        "when": {"all": [{"field": "ltv", "op": "gte", "value": 9000}]},
        "action": "warn",
        "message": "Big deal - call the customer",
    },
    {
        "id": "r_set",
        "name": "Commission",
        "event": "create",
        "when": {"all": [{"field": "plan", "op": "eq", "value": "pro"}]},
        "action": "set",
        "field": "commission",
        "formula": "ltv * 0.1",
    },
]

FORM_CONFIG = {
    "components": [
        {
            "id": "form_main",
            "type": "form",
            "title": "Add client",
            "submit_label": "Save client",
            "fields": [
                {"name": "name", "required": True, "placeholder": "Full name"},
                {"name": "plan", "required": True, "options": ["starter", "pro", "enterprise"], "default": "starter"},
                {"name": "ltv"},
                "active",
            ],
        },
        {"id": "table_main", "type": "table", "title": "Clients", "columns": ["name", "plan", "ltv"], "page_size": 5},
    ]
}


async def _make_dataset(client: httpx.AsyncClient, name: str) -> dict:
    r = await client.post("/datasets", json={"name": name, "rows": ROWS})
    assert r.status_code == 201, r.text
    return r.json()


async def _make_app(client: httpx.AsyncClient, name: str, dataset_id: str, config: dict | None = None) -> dict:
    r = await client.post("/apps", json={"name": name, "dataset_id": dataset_id, "config": config})
    assert r.status_code == 201, r.text
    return r.json()


def test_v30_rules_crud_and_validation():
    async def _go():
        async with _client() as c:
            r = await c.get("/health")
            # strict pin lives in the latest wave's tests only (v31 convention)
            assert r.json()["version"] >= "1.30.0"

            # node registry untouched by v30 (app-platform wave, no new nodes)
            r = await c.get("/node-definitions")
            assert len(r.json()["definitions"]) == 37

            ds = await _make_dataset(c, "v30 Rules DS")
            app_row = await _make_app(c, "v30 Rules", ds["id"])
            refs = (["v30 Rules"], [ds["id"]])

            # empty ruleset + the known vocabulary
            r = await c.get("/apps/v30 Rules/rules")
            assert r.status_code == 200 and r.json()["rules"] == []
            assert {"block", "set", "warn"} <= set(r.json()["actions"])
            assert {"eq", "gt", "contains", "empty", "not_empty"} <= set(r.json()["ops"])
            assert {"create", "update", "always"} == set(r.json()["events"])

            # PUT valid ruleset
            r = await c.put("/apps/v30 Rules/rules", json={"rules": RULES})
            assert r.status_code == 200, r.text
            assert [x["id"] for x in r.json()["rules"]] == ["r_block", "r_warn", "r_set"]

            # persist + visible through the app object
            r = await c.get("/apps/v30 Rules")
            assert r.json()["config"]["rules"][0]["name"] == "LTV cap"

            # bad rules → 400 with context
            bad = [
                {"action": "explode"},  # unknown action
                {"action": "block", "event": "sometimes"},  # unknown event
                {"action": "block", "when": {"all": [{"field": "ltv", "op": "==="}]}},  # unknown op
                {"action": "block", "when": {"all": [{"field": "nope", "op": "eq", "value": 1}]}},  # clause field
                {"action": "block", "when": {"all": [{"field": "ltv", "op": "gt"}]}},  # op needs value
                {"action": "set", "field": "nope", "value": 1},  # set field unknown
                {"action": "set", "field": "commission"},  # set needs value or formula
                {"action": "set", "field": "commission", "formula": "ltv * nope"},  # formula unknown field
                {"action": "set", "field": "commission", "formula": "ltv * (1+"},  # formula syntax
                {"action": "set", "field": "commission", "formula": "__import__('os')"},  # not arithmetic
                [RULES[0], RULES[0]],  # duplicate id
                {"action": "block", "when": "ltv > 5"},  # when must be {"all": [...]}
            ]
            for i, rules in enumerate(bad):
                rr = await c.put("/apps/v30 Rules/rules", json={"rules": rules if isinstance(rules, list) else [rules]})
                assert rr.status_code == 400, f"case {i}: {rr.text}"
            # rules survive (last PUT failed)
            r = await c.get("/apps/v30 Rules/rules")
            assert len(r.json()["rules"]) == 3

            # >50 rules rejected
            rr = await c.put("/apps/v30 Rules/rules", json={"rules": [RULES[0]] * 51})
            assert rr.status_code == 400 and "max 50" in rr.json()["detail"]

            # config PATCH validates rules too (draft)
            bad_cfg = {**FORM_CONFIG, "rules": [{"action": "nope"}]}
            rr = await c.patch("/apps/v30 Rules", json={"config": bad_cfg})
            assert rr.status_code == 400

            # the layout lock does NOT extend to rules
            rr = await c.post("/apps/v30 Rules/publish")
            assert rr.status_code == 200, rr.text
            rr = await c.patch("/apps/v30 Rules", json={"config": FORM_CONFIG})  # components locked
            assert rr.status_code == 409
            rr = await c.put("/apps/v30 Rules/rules", json={"rules": RULES[:1]})  # rules open
            assert rr.status_code == 200 and len(rr.json()["rules"]) == 1
            rr = await c.put("/apps/v30 Rules/rules", json={"rules": RULES})
            assert rr.status_code == 200

            # no dataset → 409
            await _make_app(c, "v30 Bare", None, config=None)
            rr = await c.put("/apps/v30 Bare/rules", json={"rules": []})
            assert rr.status_code == 409
            await c.delete("/apps/v30 Bare")

            await _cleanup(*refs)

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(["v30 Rules", "v30 Bare"], ["v30 Rules DS"]))


def test_v30_rules_enforcement_create():
    async def _go():
        async with _client() as c:
            ds = await _make_dataset(c, "v30 Create DS")
            await _make_app(c, "v30 Create", ds["id"], config=FORM_CONFIG)
            await c.put("/apps/v30 Create/rules", json={"rules": RULES})
            r = await c.post("/apps/v30 Create/publish"); assert r.status_code == 200, r.text
            refs = (["v30 Create"], [ds["id"]])

            # block: rejected with the rule message, no row written
            r = await c.post("/apps/v30-create/records", json={"record": {"name": "Zed", "plan": "starter", "ltv": 20000}})
            assert r.status_code == 400 and "15000" in r.json()["detail"]
            r = await c.get("/apps/v30-create/records")
            assert r.json()["row_count"] == 6

            # warn: accepted, warning surfaced
            r = await c.post("/apps/v30-create/records", json={"record": {"name": "Yara", "plan": "starter", "ltv": 9500}})
            assert r.status_code == 201, r.text
            assert r.json()["warnings"] == ["Big deal - call the customer"]

            # set via formula: pro plan → commission = ltv * 0.1, cast to int col
            r = await c.post("/apps/v30-create/records", json={"record": {"name": "Xavi", "plan": "pro", "ltv": 2000}})
            assert r.status_code == 201
            r = await c.get("/apps/v30-create/records?offset=7&limit=1")
            row = r.json()["rows"][0]
            assert row["commission"] == 200 and row["plan"] == "pro"

            # set with a constant value: enterprise → tier tag (no formula)
            r = await c.put(
                "/apps/v30 Create/rules",
                json={"rules": RULES + [{"id": "r_const", "name": "Tier", "event": "create",
                                         "when": {"all": [{"field": "plan", "op": "eq", "value": "enterprise"}]},
                                         "action": "set", "field": "commission", "value": 999}]},
            )
            assert r.status_code == 200, r.text
            r = await c.post("/apps/v30-create/records", json={"record": {"name": "Walt", "plan": "enterprise", "ltv": 700}})
            assert r.status_code == 201
            r = await c.get("/apps/v30-create/records?offset=8&limit=1")
            assert r.json()["rows"][0]["commission"] == 999

            # event gating: create-only block rule must NOT fire on update…
            r = await c.patch("/apps/v30-create/records/0", json={"record": {"ltv": 20000}})
            assert r.status_code == 200, r.text
            # …while the always-on warn rule DOES fire on update
            assert r.json()["warnings"] == ["Big deal - call the customer"]

            # non-numeric formula input → set skipped, submitted value kept
            r = await c.put(
                "/apps/v30 Create/rules",
                json={"rules": [{"id": "r_bad", "name": "Bad math", "event": "create",
                                 "when": {"all": [{"field": "plan", "op": "eq", "value": "starter"}]},
                                 "action": "set", "field": "commission", "formula": "name * 2"}]},
            )
            assert r.status_code == 200
            r = await c.post("/apps/v30-create/records", json={"record": {"name": "Vic", "plan": "starter", "ltv": 500, "commission": 55}})
            assert r.status_code == 201
            r = await c.get("/apps/v30-create/records?offset=9&limit=1")
            assert r.json()["rows"][0]["commission"] == 55

            # block short-circuits: a later warn never runs
            r = await c.put(
                "/apps/v30 Create/rules",
                json={"rules": [RULES[0], {"id": "r_late", "name": "Late", "action": "warn", "message": "never"}]},
            )
            r = await c.post("/apps/v30-create/records", json={"record": {"name": "Uma", "plan": "starter", "ltv": 99999}})
            assert r.status_code == 400
            assert "15000" in r.json()["detail"]

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(["v30 Create"], ["v30 Create DS"]))


def test_v30_rules_enforcement_update():
    async def _go():
        async with _client() as c:
            ds = await _make_dataset(c, "v30 Update DS")
            await _make_app(c, "v30 Update", ds["id"], config=FORM_CONFIG)
            update_rules = [
                {"id": "u_block", "name": "No enterprise edits", "event": "update",
                 "when": {"all": [{"field": "plan", "op": "eq", "value": "enterprise"}]},
                 "action": "block", "message": "Enterprise rows are locked"},
                {"id": "u_set", "name": "Recommission", "event": "update",
                 "when": {"all": [{"field": "plan", "op": "eq", "value": "pro"}]},
                 "action": "set", "field": "commission", "formula": "ltv * 0.2"},
            ]
            await c.put("/apps/v30 Update/rules", json={"rules": update_rules})
            r = await c.post("/apps/v30 Update/publish"); assert r.status_code == 200, r.text
            refs = (["v30 Update"], [ds["id"]])

            # block on update evaluates the MERGED row (plan patched to enterprise)
            r = await c.patch("/apps/v30-update/records/0", json={"record": {"plan": "enterprise"}})
            assert r.status_code == 400 and "locked" in r.json()["detail"]

            # set on update recomputes from the merged ltv (3400 → patch 5000 → 1000)
            r = await c.patch("/apps/v30-update/records/1", json={"record": {"ltv": 5000}})
            assert r.status_code == 200, r.text
            assert r.json()["record"]["commission"] == 1000
            assert r.json()["warnings"] == []

            # create-only rules never fire on update (no block!), while the
            # always-on warn rule does - event gating verified both ways
            r = await c.put("/apps/v30 Update/rules", json={"rules": RULES})
            assert r.status_code == 200, r.text
            r = await c.patch("/apps/v30-update/records/2", json={"record": {"ltv": 20000}})
            assert r.status_code == 200
            assert r.json()["warnings"] == ["Big deal - call the customer"]

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(["v30 Update"], ["v30 Update DS"]))


def test_v30_form_fields_and_descriptor():
    async def _go():
        async with _client() as c:
            ds = await _make_dataset(c, "v30 Forms DS")
            app_row = await _make_app(c, "v30 Forms", ds["id"], config=FORM_CONFIG)
            await c.post("/apps/v30 Forms/publish")
            refs = (["v30 Forms"], [ds["id"]])

            # descriptor: objects normalized, strings humanized
            r = await c.get("/apps/v30-forms/form")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["app"]["name"] == "v30 Forms"
            assert body["form"]["title"] == "Add client" and body["form"]["submit_label"] == "Save client"
            fields = body["form"]["fields"]
            assert [f["name"] for f in fields] == ["name", "plan", "ltv", "active"]
            assert fields[0]["required"] is True and fields[0]["placeholder"] == "Full name"
            assert fields[1]["options"] == ["starter", "pro", "enterprise"] and fields[1]["default"] == "starter"
            assert fields[3]["label"] == "Active" and fields[3]["required"] is False

            # required (absent counts as empty)
            r = await c.post("/apps/v30-forms/records", json={"record": {"plan": "pro", "ltv": 100}})
            assert r.status_code == 400 and "'name' is required" in r.json()["detail"]

            # required (explicit empty)
            r = await c.post("/apps/v30-forms/records", json={"record": {"name": "  ", "plan": "pro"}})
            assert r.status_code == 400 and "'name' is required" in r.json()["detail"]

            # options enforcement
            r = await c.post("/apps/v30-forms/records", json={"record": {"name": "Zed", "plan": "ultra"}})
            assert r.status_code == 400 and "must be one of" in r.json()["detail"]

            # options loose-matching is case/space tolerant
            r = await c.post("/apps/v30-forms/records", json={"record": {"name": "Zed", "plan": "  PRO "}})
            assert r.status_code == 201, r.text

            # default applied when absent
            r = await c.post("/apps/v30-forms/records", json={"record": {"name": "Yara"}})
            assert r.status_code == 201
            r = await c.get("/apps/v30-forms/records?offset=7&limit=1")
            assert r.json()["rows"][0]["plan"] == "starter"

            # update: untouched required field does not block a rename…
            r = await c.patch("/apps/v30-forms/records/0", json={"record": {"name": "Alice Cooper"}})
            assert r.status_code == 200, r.text
            # …but clearing a touched required field does
            r = await c.patch("/apps/v30-forms/records/0", json={"record": {"name": ""}})
            assert r.status_code == 400 and "'name' is required" in r.json()["detail"]
            # options re-checked on touched fields
            r = await c.patch("/apps/v30-forms/records/0", json={"record": {"plan": "free"}})
            assert r.status_code == 400 and "must be one of" in r.json()["detail"]

            # draft app → descriptor 404; published without a form → 409
            await _make_app(c, "v30 NoForm", ds["id"], config={"components": [
                {"id": "t", "type": "table", "title": "T", "columns": ["name"], "page_size": 5}]})
            r = await c.get("/apps/v30-noform/form")
            assert r.status_code == 404  # draft (runtime is slug-addressable)
            r = await c.post("/apps/v30 NoForm/publish")
            assert r.status_code == 200, r.text
            r = await c.get("/apps/v30-noform/form")
            assert r.status_code == 409 and "no form component" in r.json()["detail"]

            # invalid field objects rejected at config time
            bad_fields = [
                [{"name": "nope"}],  # unknown column
                [{"name": "name"}, {"name": "name"}],  # duplicate
                [{"name": "name", "required": "yes"}],  # required must be bool
                [{"name": "plan", "options": []}],  # empty options
                [{"name": "plan", "options": ["a", {"x": 1}]}],  # non-scalar options
                [42],  # not a string/object
            ]
            for i, fields in enumerate(bad_fields):
                cfg = {"components": [{"id": "f", "type": "form", "title": "F", "fields": fields}]}
                rr = await c.patch("/apps/v30 NoForm", json={"config": cfg})
                assert rr.status_code == 409  # published → config locked
            await c.post("/apps/v30 NoForm/unpublish")
            for i, fields in enumerate(bad_fields):
                cfg = {"components": [{"id": "f", "type": "form", "title": "F", "fields": fields}]}
                rr = await c.patch("/apps/v30 NoForm", json={"config": cfg})
                assert rr.status_code == 400, f"case {i}: {rr.text}"

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(["v30 Forms", "v30 NoForm"], ["v30 Forms DS"]))


def test_v30_form_submit_endpoint():
    async def _go():
        async with _client() as c:
            ds = await _make_dataset(c, "v30 Submit DS")
            await _make_app(c, "v30 Submit", ds["id"], config=FORM_CONFIG)
            await c.put("/apps/v30 Submit/rules", json={"rules": RULES})
            await c.post("/apps/v30 Submit/publish")
            refs = (["v30 Submit"], [ds["id"]])

            # happy path: lands in the dataset, default fills plan
            r = await c.post("/apps/v30-submit/form-submit", json={"record": {"name": "Form Guy", "ltv": 800}})
            assert r.status_code == 201, r.text
            assert r.json()["row_count"] == 7 and r.json()["warnings"] == []
            r = await c.get("/apps/v30-submit/records?offset=6&limit=1")
            assert r.json()["rows"][0]["plan"] == "starter"

            # rules fire on form submissions: warn…
            r = await c.post("/apps/v30-submit/form-submit", json={"record": {"name": "Form Whale", "ltv": 12000}})
            assert r.status_code == 201 and r.json()["warnings"] == ["Big deal - call the customer"]
            # …and block
            r = await c.post("/apps/v30-submit/form-submit", json={"record": {"name": "Form Zed", "ltv": 99999}})
            assert r.status_code == 400 and "15000" in r.json()["detail"]

            # form options enforced here too
            r = await c.post("/apps/v30-submit/form-submit", json={"record": {"ltv": 100}})
            assert r.status_code == 400 and "'name' is required" in r.json()["detail"]

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(["v30 Submit"], ["v30 Submit DS"]))


def test_v30_rules_test_dry_run():
    async def _go():
        async with _client() as c:
            ds = await _make_dataset(c, "v30 Dry DS")
            await _make_app(c, "v30 Dry", ds["id"], config=FORM_CONFIG)
            await c.put("/apps/v30 Dry/rules", json={"rules": RULES})
            await c.post("/apps/v30 Dry/publish")
            refs = (["v30 Dry"], [ds["id"]])

            # pro create → set matches, nothing else
            r = await c.post("/apps/v30 Dry/rules/test", json={"record": {"name": "T", "plan": "pro", "ltv": 3400}, "event": "create"})
            body = r.json()
            assert body["blocked"] is False and body["warnings"] == []
            assert len(body["matches"]) == 1
            assert body["matches"][0]["id"] == "r_set" and "commission = 340" in body["matches"][0]["result"]

            # huge ltv → block short-circuits (warn after it never listed)
            r = await c.post("/apps/v30 Dry/rules/test", json={"record": {"name": "T", "plan": "starter", "ltv": 20000}, "event": "create"})
            body = r.json()
            assert body["blocked"] is True
            assert [m["id"] for m in body["matches"]] == ["r_block"]
            assert "15000" in body["matches"][0]["message"]

            # event gating in dry-run: update skips create-only rules
            r = await c.post("/apps/v30 Dry/rules/test", json={"record": {"name": "T", "plan": "starter", "ltv": 9500}, "event": "update"})
            body = r.json()
            assert [m["id"] for m in body["matches"]] == ["r_warn"]
            assert body["warnings"] == ["Big deal - call the customer"]

            # dry run mutates nothing
            r = await c.get("/apps/v30-dry/records")
            assert r.json()["row_count"] == 6

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(["v30 Dry"], ["v30 Dry DS"]))
