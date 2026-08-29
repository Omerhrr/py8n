#!/usr/bin/env python3
"""Remove leftover test credentials (v10 pytest/smoke leftovers) from the dev vault."""

import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8000/api/v1"

TEST_PREFIXES = (
    "used-", "broken-", "bogus-", "probe-", "new-name-", "old-name-",
    "smtp-", "basic-", "v10-", "v10 live", "tmp v10",
)


def req(method: str, path: str):
    r = urllib.request.Request(BASE + path, method=method)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, resp.read().decode() or "{}"
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode() or "{}"


def main() -> None:
    status, body = req("GET", "/credentials")
    rows = json.loads(body)
    removed = 0
    for c in rows:
        if c["name"].startswith(TEST_PREFIXES):
            s, _ = req("DELETE", f"/credentials/{c['id']}?force=true")
            if s == 204:
                removed += 1
                print(f"removed {c['name']} ({c['id'][:8]})")
            else:
                print(f"FAILED {c['name']}: {s}")
    print(f"\n{removed} test credentials removed; {len(rows) - removed} kept")


if __name__ == "__main__":
    sys.exit(main())
