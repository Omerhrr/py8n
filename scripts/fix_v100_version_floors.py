#!/usr/bin/env python3
"""One-shot: rewrite the old tests' lexicographic version-floor asserts.

The 3-digit minor (1.100.0) broke `X >= "1.NN.0"` string comparisons
("1.100.0" sorts BEFORE "1.30.0" lexicographically). The intent of those
asserts was always "at least version X" - compare parsed tuples instead,
which survives every future bump.
"""
import re
import sys
from pathlib import Path

TESTS = Path("/home/z/my-project/py8n/mini-services/api-backend/tests")

# shapes:  body["version"] / res.json()["version"] / r.json()["version"] /
#          health["version"] / settings.version  >= "1.NN.0"
SUBJECT = r'(?:[A-Za-z_]\w*\.json\(\)|[A-Za-z_]\w*|settings\.version)\["version"\]|settings\.version'
PAT = re.compile(
    rf'({SUBJECT}) >= "1\.(\d+)\.0"')


def _sub(m: re.Match) -> str:
    subj, minor = m.group(1), m.group(2)
    if subj == "settings.version":
        parsed = "tuple(int(p) for p in settings.version.split(\".\"))"
    elif '["version"]' in subj:
        base = subj[:-len('["version"]')]
        parsed = (f'tuple(int(p) for p in {base}["version"].split("."))')
    else:  # defensive - shouldn't happen
        parsed = subj
    return f"{parsed} >= (1, {minor}, 0)"


def main() -> int:
    changed = 0
    for f in sorted(TESTS.glob("test_*.py")):
        text = f.read_text()
        new = PAT.sub(_sub, text)
        if new != text:
            f.write_text(new)
            n = len(PAT.findall(text))
            changed += 1
            print(f"{f.name}: {n} rewrite(s)")
    print(f"--- {changed} file(s) rewritten")
    return 0


if __name__ == "__main__":
    sys.exit(main())
