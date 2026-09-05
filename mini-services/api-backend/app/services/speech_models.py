"""Speech model installs (v73) - real vosk / whisper.cpp / piper models.

v72's bridges probed the machine and reported exact remediation when a
model was missing. This module closes the loop: an API-driven INSTALLER
that downloads REAL models from their official homes, verifies them,
lays them out exactly where the probes look, and re-binds the engines -
so ``POST /voice/speech/models/install`` turns a bare machine into a
FULLY OFFLINE PHONE (in-process vosk ASR + piper TTS + the deterministic
knowledge brain never touch the network again).

The catalog (curated, explicit URLs - no guessed mirrors):

* ``vosk-small-en-us``    - vosk-model-small-en-us-0.15.zip from
                            alphacephei.com (41,205,931 bytes), extracted
                            under data/models/ (Kaldi layout verified);
                            in-process ASR, needs the ``vosk`` package.
* ``whisper-tiny-en``     - ggml-tiny.en.bin from the whisper.cpp HF repo
                            (ggml magic verified); needs a whisper.cpp
                            binary (PY8N_WHISPER_CPP_BIN or on PATH).
* ``piper-lessac-medium`` - en_US-lessac-medium .onnx voice + its .onnx.json
                            config from the rhasspy/piper-voices repo.
* ``piper-binary-linux``  - the piper release tarball (x86_64), extracted
                            whole (its libs + espeak-ng-data stay beside the
                            binary) and linked under data/models/bin/piper.

Honesty rules (the rest of the platform's):

* every download streams to ``<dest>.part`` and is renamed ATOMICALLY on
  success - a half-downloaded model can never probe as available;
* verification is REAL: zip CRC test + Kaldi layout, the ggml magic, the
  onnx/json pair parse - a truncated or corrupted artifact FAILS LOUD;
* zip/tar extraction is slip-guarded (no absolute paths, no ``..``, no
  symlink escapes, byte + member caps);
* the vosk PACKAGE is never pip-installed behind the user's back - the
  inventory keeps reporting ``pip install vosk`` as the remediation;
* the fetcher is a parameter: tests inject local bytes, the API uses
  real HTTP - the same code path both ways.
"""

from __future__ import annotations

import json
import os
import shutil
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

from .speech_engines import models_root

MAX_EXTRACT_BYTES = 500 * 1024 * 1024   # uncompressed extraction cap
MAX_MEMBERS = 20000                     # extraction member cap
# Per-SOURCE download budget (seconds): a source that cannot deliver within
# it loses its turn and the next source (mirror) is tried - one crawling
# mirror must not hold the install hostage. Override with
# PY8N_MODEL_DL_BUDGET for genuinely slow networks.
DL_BUDGET_DEFAULT = 180.0

HF_WHISPER = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main"
HF_PIPER = "https://huggingface.co/rhasspy/piper-voices/resolve/main"

VOICE_NAME = "en_US-lessac-medium"


class SpeechModelError(ValueError):
    """Honest 4xx-grade install failures (unknown slug, bad artifact)."""


MirrorNote = "the first source is the official home; the rest are mirrors tried in order"


CATALOG: dict[str, dict] = {
    "vosk-small-en-us": {
        "engine": "vosk",
        "title": "Vosk small English (US) 0.15",
        "description": "In-process streaming ASR - the Kaldi model the local bridge loads "
                       "into this process. No audio leaves the machine.",
        "kind": "zip",
        "urls": ["https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip",
                 "https://huggingface.co/ambind/vosk-model-small-en-us-0.15/resolve/main/"
                 "vosk-model-small-en-us-0.15_c_.zip"],
        "exact_bytes": 41205931,
        "requires": "the vosk python package (pip install vosk)",
        "after": "the py8n_local ASR engine binds at the next boot or rebind",
    },
    "whisper-tiny-en": {
        "engine": "whisper.cpp",
        "title": "whisper.cpp ggml tiny (English)",
        "description": "The 77 MB ggml model for the whisper.cpp CLI bridge "
                       "(77 MB, English). Needs a whisper-cli binary on this machine.",
        "kind": "ggml",
        "urls": [f"{HF_WHISPER}/ggml-tiny.en.bin"],
        "min_bytes": 60_000_000,
        "requires": "a whisper.cpp binary (PY8N_WHISPER_CPP_BIN or on PATH)",
        "after": "the whisper.cpp bridge prefers vosk when both are available",
    },
    "piper-lessac-medium": {
        "engine": "piper",
        "title": f"Piper voice {VOICE_NAME}",
        "description": "The medium-quality English (US) voice for the piper TTS bridge - "
                       "the .onnx model plus the .onnx.json config piper requires.",
        "kind": "piper_voice",
        "urls": [f"{HF_PIPER}/en/en_US/lessac/medium/{VOICE_NAME}.onnx"],
        "companion_urls": [f"{HF_PIPER}/en/en_US/lessac/medium/{VOICE_NAME}.onnx.json"],
        "min_bytes": 15_000_000,
        "requires": "the piper binary (slug piper-binary-linux, PY8N_PIPER_BIN or PATH)",
        "after": "the piper_local TTS engine binds with this voice",
    },
    "piper-binary-linux": {
        "engine": "piper",
        "title": "Piper binary (linux x86_64, 2023.11.14-2)",
        "description": "The official piper release tarball - the binary, its onnxruntime "
                       "libraries and espeak-ng-data, extracted whole under data/models/.",
        "kind": "targz",
        "urls": ["https://github.com/rhasspy/piper/releases/download/2023.11.14-2/"
                 "piper_linux_x86_64.tar.gz"],
        "min_bytes": 5_000_000,
        "requires": "linux x86_64 (the release is platform-specific)",
        "after": "linked under data/models/bin/piper so the probe finds it after restarts",
    },
}


# ---------------------------------------------------------------------------
# Download + verification primitives
# ---------------------------------------------------------------------------


def _dl_budget() -> float:
    raw = os.environ.get("PY8N_MODEL_DL_BUDGET", "").strip()
    try:
        v = float(raw) if raw else DL_BUDGET_DEFAULT
    except ValueError:
        v = DL_BUDGET_DEFAULT
    return max(5.0, v)


def _default_fetch(url: str, dest: Path, *, min_bytes: int = 0) -> int:
    """Stream a URL to ``dest`` (atomic via <dest>.part). Returns bytes.

    Enforces the per-source download budget: a source that is still
    crawling when the budget expires raises, and the multi-source fetch
    moves on to the next mirror."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    total = 0
    deadline = time.monotonic() + _dl_budget()
    try:
        with urllib.request.urlopen(url, timeout=60) as resp, \
                open(part, "wb") as fh:
            while True:
                if time.monotonic() > deadline:
                    raise SpeechModelError(
                        f"source exceeded its {_dl_budget():.0f}s download budget "
                        f"after {total} bytes")
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                total += len(chunk)
                fh.write(chunk)
    except Exception:
        part.unlink(missing_ok=True)
        raise
    if min_bytes and total < min_bytes:
        part.unlink(missing_ok=True)
        raise SpeechModelError(
            f"downloaded {total} bytes but the artifact must be at least "
            f"{min_bytes} - refusing to install a truncated model")
    part.replace(dest)
    return total


def _fetch_any(spec_file: str, urls: list[str], dest: Path, *, min_bytes: int = 0,
               fetch=None) -> int:
    """Fetch an artifact trying each source in order (official home first,
    mirrors after). Download-level failures (network, truncation below
    min_bytes) move on to the next source; VERIFICATION of the downloaded
    artifact happens in the caller and fails loud - a complete but corrupt
    artifact is reported, not silently retried. All sources failing = one
    honest error carrying the last cause."""
    fetch = fetch or _default_fetch
    last: Exception | None = None
    for url in urls:
        try:
            return fetch(url, dest, min_bytes=min_bytes)
        except Exception as exc:  # noqa: BLE001 - network + validation errors both
            last = exc
            try:
                dest.unlink()
            except OSError:
                pass
    raise SpeechModelError(
        f"every source for {spec_file!r} failed - last error "
        f"({type(last).__name__}: {last})") from last


def _verify_zip(path: Path, dest_dir: Path) -> list[str]:
    """CRC-check + extract a Kaldi model zip with slip guards.

    The official vosk zips carry ONE top-level directory (the model's own
    folder, e.g. ``vosk-model-small-en-us-0.15/``); some mirrors keep the
    flat ``am/``/``conf/`` layout. Either is accepted: a single common
    top-level directory is STRIPPED so the extraction always lands as
    ``<dest>/am``, ``<dest>/conf`` - exactly the layout probe_vosk hands
    to the recognizer.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            # SAFETY FIRST: every member name is vetted before anything else
            for name in names:
                if name.startswith("/") or ".." in Path(name).parts:
                    raise SpeechModelError(f"refusing unsafe zip member {name!r}")
            bad = zf.testzip()
            if bad is not None:
                raise SpeechModelError(f"zip member {bad!r} fails its CRC - corrupted download")
            # a single common top-level directory is the model's own folder
            tops = {n.split("/")[0] for n in names if n.split("/")[0]}
            prefix = ""
            if len(tops) == 1:
                only = next(iter(tops))
                if all(n == only or n.startswith(only + "/") for n in names):
                    prefix = only + "/"
            stripped = [n[len(prefix):] for n in names if n[len(prefix):]]
            if not any(s.startswith("am/") for s in stripped) or \
                    not any(s.startswith("conf/") for s in stripped):
                raise SpeechModelError(
                    "the zip does not carry a Kaldi layout (am/ + conf/ members) - "
                    "this is not a vosk model")
            if len(names) > MAX_MEMBERS:
                raise SpeechModelError(f"zip carries {len(names)} members - over the safety cap")
            total = sum(zi.file_size for zi in zf.infolist())
            if total > MAX_EXTRACT_BYTES:
                raise SpeechModelError(f"zip expands to {total} bytes - over the safety cap")
            dest_dir.mkdir(parents=True, exist_ok=True)
            extracted: list[str] = []
            for zi in zf.infolist():
                name = zi.filename
                rel = name[len(prefix):] if prefix and name.startswith(prefix) else name
                if not rel or rel.endswith("/"):
                    if rel:
                        (dest_dir / rel).mkdir(parents=True, exist_ok=True)
                    continue
                target = (dest_dir / rel).resolve()
                if not str(target).startswith(str(dest_dir.resolve())):
                    raise SpeechModelError(f"refusing zip escape member {name!r}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(zi) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
                extracted.append(rel)
            return extracted
    except zipfile.BadZipFile as exc:
        raise SpeechModelError(f"not a valid zip: {exc}") from exc


def _verify_ggml(path: Path) -> None:
    """A whisper.cpp ggml model starts with the b'ggml' magic."""
    with open(path, "rb") as fh:
        magic = fh.read(4)
    if magic != b"ggml":
        raise SpeechModelError(
            f"missing the ggml magic (got {magic!r}) - this is not a whisper.cpp model")


def _verify_piper_pair(onnx: Path, config: Path) -> None:
    """piper needs the .onnx AND its .onnx.json; both must look real."""
    if onnx.stat().st_size < 1024:
        raise SpeechModelError("the .onnx voice is implausibly small - refusing")
    try:
        data = json.loads(config.read_text("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SpeechModelError(f"the .onnx.json config does not parse: {exc}") from exc
    if not isinstance(data, dict):
        raise SpeechModelError("the .onnx.json config is not a JSON object")
    if "num_symbols" not in data and "phoneme_id_map" not in data:
        raise SpeechModelError("the .onnx.json config lacks piper's phoneme fields - "
                               "this is not a piper voice config")


def _verify_tarball(path: Path, dest_dir: Path) -> Path:
    """Extract the piper release tarball whole; return the binary's path."""
    binary: Path | None = None
    try:
        with tarfile.open(path, "r:gz") as tf:
            members = tf.getmembers()
            if len(members) > MAX_MEMBERS:
                raise SpeechModelError(f"tarball carries {len(members)} members - over the cap")
            dest_dir.mkdir(parents=True, exist_ok=True)
            for m in members:
                if m.name.startswith("/") or ".." in Path(m.name).parts:
                    raise SpeechModelError(f"refusing unsafe tar member {m.name!r}")
                if m.issym():
                    # SONAME links (libfoo.so.1 -> libfoo.so.1.2.0) are REQUIRED
                    # for the binary to load - extract them, but only when the
                    # resolved target stays inside the extraction root
                    target = dest_dir / m.name
                    resolved = (target.parent / m.linkname).resolve()
                    if not str(resolved).startswith(str(dest_dir.resolve())):
                        raise SpeechModelError(
                            f"refusing unsafe symlink {m.name!r} -> {m.linkname!r}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists() or target.is_symlink():
                        target.unlink()
                    os.symlink(m.linkname, target)
                    continue
                if not (m.isfile() or m.isdir()):
                    # hardlinks / devices are skipped, never extracted
                    continue
                target = (dest_dir / m.name).resolve()
                if not str(target).startswith(str(dest_dir.resolve())):
                    raise SpeechModelError(f"refusing tar escape member {m.name!r}")
                if m.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if m.size > MAX_EXTRACT_BYTES:
                    raise SpeechModelError(f"tar member {m.name!r} is over the cap")
                target.parent.mkdir(parents=True, exist_ok=True)
                with tf.extractfile(m) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
                # PRESERVE THE MODE: umask would land the binary as 0644 and
                # exec() would refuse it with PermissionDenied
                os.chmod(target, m.mode & 0o777)
                import stat as _stat

                if m.name.endswith("/piper") and (m.mode & _stat.S_IXUSR):
                    binary = target
    except tarfile.TarError as exc:
        raise SpeechModelError(f"not a valid tar.gz: {exc}") from exc
    if binary is None:
        raise SpeechModelError("no executable 'piper' found in the tarball - "
                               "this is not the piper release archive")
    return binary


# ---------------------------------------------------------------------------
# Install + inventory
# ---------------------------------------------------------------------------


def _dest_paths(root: Path, slug: str) -> dict[str, Path]:
    models = root
    if slug == "vosk-small-en-us":
        return {"dir": models / "vosk-model-small-en-us-0.15"}
    if slug == "whisper-tiny-en":
        return {"file": models / "ggml-tiny.en.bin"}
    if slug == "piper-lessac-medium":
        return {"onnx": models / f"{VOICE_NAME}.onnx",
                "config": models / f"{VOICE_NAME}.onnx.json"}
    if slug == "piper-binary-linux":
        return {"dir": models / "piper-bin", "link": models / "bin" / "piper"}
    raise SpeechModelError(f"unknown layout for slug {slug!r}")


def slug_installed(slug: str, root: Path | None = None) -> bool:
    """Is this catalog entry already on disk (derived, never stored)?"""
    root = root or models_root()
    paths = _dest_paths(root, slug)
    if "dir" in paths and "link" in paths:  # piper binary: extracted dir + bin link
        return paths["link"].exists() and Path(paths["dir"]).exists()
    if "dir" in paths:
        return (paths["dir"] / "conf").exists()
    if "file" in paths:
        return paths["file"].exists()
    return paths["onnx"].exists() and paths["config"].exists()


def install_model(slug: str, *, fetch=None, root: Path | None = None) -> dict:
    """Download + verify + install one catalog entry, then report the layout.

    ``fetch(url, dest_path, min_bytes=...)`` is injectable for tests; the
    default streams real HTTP. Returns the exact files laid down - the
    CALLER re-binds the engines (bind_local_engines()).
    """
    spec = CATALOG.get(slug)
    if spec is None:
        raise SpeechModelError(
            f"unknown model slug {slug!r} - the catalog carries: "
            f"{', '.join(sorted(CATALOG))}")
    root = root or models_root()
    fetch = fetch or _default_fetch
    kind = spec["kind"]
    min_bytes = spec.get("min_bytes", 0)
    downloaded: dict[str, int] = {}
    installed: list[str] = []
    env_set: dict[str, str] = {}

    with tempfile.TemporaryDirectory(prefix="py8n-model-") as tmp:
        tmp_dir = Path(tmp)
        if kind == "zip":
            raw = tmp_dir / "model.zip"
            downloaded["archive"] = _fetch_any(
                slug, spec["urls"], raw, min_bytes=min_bytes, fetch=fetch)
            dest = _dest_paths(root, slug)["dir"]
            if dest.exists():
                shutil.rmtree(dest)
            installed = [str(dest / n) for n in _verify_zip(raw, dest)]
        elif kind == "ggml":
            raw = tmp_dir / "model.bin"
            downloaded["model"] = _fetch_any(
                slug, spec["urls"], raw, min_bytes=min_bytes, fetch=fetch)
            dest = _dest_paths(root, slug)["file"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            _verify_ggml(raw)
            shutil.move(str(raw), str(dest))
            installed = [str(dest)]
        elif kind == "piper_voice":
            onnx = tmp_dir / "voice.onnx"
            conf = tmp_dir / "voice.onnx.json"
            downloaded["voice"] = _fetch_any(
                slug, spec["urls"], onnx, min_bytes=min_bytes, fetch=fetch)
            downloaded["config"] = _fetch_any(
                f"{slug} config", spec["companion_urls"], conf,
                min_bytes=200, fetch=fetch)
            dests = _dest_paths(root, slug)
            root.mkdir(parents=True, exist_ok=True)
            _verify_piper_pair(onnx, conf)
            shutil.move(str(onnx), str(dests["onnx"]))
            shutil.move(str(conf), str(dests["config"]))
            installed = [str(dests["onnx"]), str(dests["config"])]
        elif kind == "targz":
            raw = tmp_dir / "release.tar.gz"
            downloaded["archive"] = _fetch_any(
                slug, spec["urls"], raw, min_bytes=min_bytes, fetch=fetch)
            dests = _dest_paths(root, slug)
            if dests["dir"].exists():
                shutil.rmtree(dests["dir"])
            binary = _verify_tarball(raw, dests["dir"])
            dests["link"].parent.mkdir(parents=True, exist_ok=True)
            if dests["link"].exists() or dests["link"].is_symlink():
                dests["link"].unlink()
            os.symlink(binary, dests["link"])
            # the documented env override now points at the REAL binary (not the
            # symlink) so bind_local_engines() picks it up immediately; the bin/
            # link keeps the install discoverable after restarts
            os.environ["PY8N_PIPER_BIN"] = str(binary)
            env_set["PY8N_PIPER_BIN"] = str(binary)
            installed = [str(dests["dir"]), f"{dests['link']} -> {binary}"]

    return {"slug": slug, "engine": spec["engine"], "kind": kind,
            "title": spec["title"], "bytes": downloaded,
            "installed_paths": installed, "models_root": str(root),
            "env_set": env_set,
            "requires": spec["requires"], "after": spec["after"],
            "note": "verified + installed - run the engines endpoint to see the "
                    "fresh probe, and bind_local_engines has re-run at the API layer"}


def catalog_out(root: Path | None = None) -> dict:
    """The honest installer surface: catalog + what is already on disk."""
    root = root or models_root()
    models = []
    for slug in sorted(CATALOG):
        spec = CATALOG[slug]
        models.append({
            "slug": slug,
            "engine": spec["engine"],
            "title": spec["title"],
            "description": spec["description"],
            "size_hint": spec.get("exact_bytes") or spec.get("min_bytes"),
            "requires": spec["requires"],
            "after": spec["after"],
            "installed": slug_installed(slug, root),
        })
    return {
        "models": models,
        "models_root": str(root),
        "install": "POST /voice/speech/models/install {slug} - downloads, verifies, "
                   "installs under the models root, then re-binds the engines",
    }
