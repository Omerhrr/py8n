"""Bump visible node-count assertions 36 → 37 for the v32 wave (document_extract).

Patches test files and the smoke test IN THIS REPO ONLY, asserting the exact
old text is present before each replacement so a stale run can't double-apply.
"""

import pathlib

ROOT = pathlib.Path("/home/z/my-project")

TARGETS = [
    # (relative path, old, new)
    ("mini-services/api-backend/tests/test_v10_features.py", None, None),  # probe only — v10 asserts via websocket, checked below
    ("mini-services/api-backend/tests/test_v22_features.py", "assert len(defs) == 36", "assert len(defs) == 37"),
    ("mini-services/api-backend/tests/test_v24_features.py", "assert len(defs) == 36, f\"expected 36 node types, got {len(defs)}\"", "assert len(defs) == 37, f\"expected 37 node types, got {len(defs)}\""),
    ("mini-services/api-backend/tests/test_v25_features.py", "assert len(types) == 36, f\"expected 36 visible types, got {len(types)}\"", "assert len(types) == 37, f\"expected 37 visible types, got {len(types)}\""),
    ("mini-services/api-backend/tests/test_v27_features.py", "assert len(types) == 36, f\"expected 36 visible types, got {len(types)}\"", "assert len(types) == 37, f\"expected 37 visible types, got {len(types)}\""),
    ("mini-services/api-backend/tests/test_v28_features.py", "assert len(types) == 36, f\"expected 36 visible types, got {len(types)}\"", "assert len(types) == 37, f\"expected 37 visible types, got {len(types)}\""),
    ("mini-services/api-backend/tests/test_v30_features.py", "assert len(r.json()[\"definitions\"]) == 36", "assert len(r.json()[\"definitions\"]) == 37"),
    ("mini-services/api-backend/tests/test_v31_features.py", "assert len(r.json()[\"definitions\"]) == 36", "assert len(r.json()[\"definitions\"]) == 37"),
    ("scripts/smoke_test.py", "assert len(types) == 36, \"expected 36 node types after v28 wave\"", "assert len(types) == 37, \"expected 37 node types after v32 wave\""),
    ("scripts/smoke_test.py", "assert len(types21) == 36 and \"respond_to_webhook\" in types21, types21  # 36 after v28", "assert len(types21) == 37 and \"respond_to_webhook\" in types21, types21  # 37 after v32"),
    ("scripts/smoke_test.py", "assert len(types24) == 36, f\"expected 36 node types after v28, got {len(types24)}\"", "assert len(types24) == 37, f\"expected 37 node types after v32, got {len(types24)}\""),
    ("scripts/smoke_test.py", "assert len(types27) == 36, len(types27)", "assert len(types27) == 37, len(types27)"),
    ("scripts/smoke_test.py", "assert len(types28) == 36, len(types28)", "assert len(types28) == 37, len(types28)"),
]

changed = 0
for rel, old, new in TARGETS:
    p = ROOT / rel
    if old is None:
        continue
    text = p.read_text()
    if new in text and old not in text:
        print(f"SKIP (already bumped): {rel}")
        continue
    if old not in text:
        raise SystemExit(f"PATCH FAILED — old text not found in {rel}:\n  {old}")
    p.write_text(text.replace(old, new, 1))
    changed += 1
    print(f"patched: {rel}")

# v10 check: does it assert a node count?
v10 = (ROOT / "mini-services/api-backend/tests/test_v10_features.py").read_text()
print("v10 has '36' assertion:", " == 36" in v10 or "== 36" in v10)

# any remaining '== 36' count assertions anywhere?
import subprocess

res = subprocess.run(
    ["rg", "-n", r"== 36\b", str(ROOT / "mini-services/api-backend/tests"), str(ROOT / "scripts")],
    capture_output=True, text=True,
)
print("remaining 36-assertions:\n" + (res.stdout or "(none)"))
print(f"done — {changed} files patched")
