#!/usr/bin/env python3
"""v35 polish: remove all em dashes (U+2014) and en dashes (U+2013) from repo text files.

Replacement is a plain hyphen "-": spaces around the dash are preserved, so
"alpha - beta" keeps its rhythm and list-like " - note" keeps reading as a bullet.

Excludes: .git, node_modules, .nuxt, .output, .venv, download (binary artifacts),
__pycache__, package-lock.json (third-party descriptions, keep the diff clean).
"""
import os
import sys

ROOT = "/home/z/my-project"
SKIP_DIRS = {".git", "node_modules", ".nuxt", ".output", ".venv", "download", "__pycache__", ".zai"}
SKIP_FILES = {"package-lock.json"}
TEXT_EXT = {
    ".py", ".ts", ".tsx", ".mjs", ".js", ".vue", ".md", ".json", ".yml", ".yaml",
    ".txt", ".sh", ".css", ".scss", ".html", ".toml", ".svg", ".cfg", ".ini",
    ".env", ".example", ".sql",
}

EM = "\u2014"   # -
EN = "\u2013"   # -


def main() -> int:
    changed_files = 0
    em_total = 0
    en_total = 0
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name in SKIP_FILES:
                continue
            path = os.path.join(dirpath, name)
            ext = os.path.splitext(name)[1].lower()
            if ext not in TEXT_EXT:
                continue
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    text = fh.read()
            except (UnicodeDecodeError, PermissionError, OSError):
                continue
            em_n = text.count(EM)
            en_n = text.count(EN)
            if em_n == 0 and en_n == 0:
                continue
            new_text = text.replace(EM, "-").replace(EN, "-")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(new_text)
            rel = os.path.relpath(path, ROOT)
            print(f"{rel}: {em_n} em, {en_n} en")
            changed_files += 1
            em_total += em_n
            en_total += en_n
    print(f"\nDONE: {changed_files} files changed, {em_total} em dashes, {en_total} en dashes replaced")
    return 0


if __name__ == "__main__":
    sys.exit(main())
