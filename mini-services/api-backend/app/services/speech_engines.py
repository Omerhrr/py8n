"""Live speech engine bridges (v72) - whisper.cpp / vosk / piper in-process.

v69 defined the ASR/TTS CONTRACTS; v70 built the transport with a
pluggable ASR registry (``voice_transport.register_asr_engine``) but
shipped no real engine - the honest ``asr.unavailable`` was the only
honest answer. This module closes that gap with REAL local bridges:

* ASR ``vosk``          - the vosk python package + a model directory;
                          recognition runs entirely in-process (the
                          KaldiRecognizer accepts the stream's linear16
                          chunks directly).
* ASR ``whisper.cpp``   - the whisper.cpp binary + a ggml model file;
                          utterance PCM is resampled to 16 kHz, wrapped
                          in a temp wav, and transcribed by the CLI.
* TTS  ``piper``        - the piper binary + an .onnx voice; text in,
                          wav bytes out.

Design rules (the same honesty the rest of py8n keeps):

* NOTHING is faked. A bridge is "available" only when its binary/package
  AND its model are actually present on this machine - the inventory
  reports exact remediation when they are not.
* Binding is explicit and best-effort at boot: engines that cannot run
  are simply not registered, and the transport keeps reporting
  ``asr.unavailable`` for them. No invented words, ever.
* Results go through the v69 contracts (``validate_asr_result`` /
  ``validate_tts_result``) before anything downstream sees them, so a
  misbehaving engine fails loud instead of lying quietly.
* Environment overrides: ``PY8N_WHISPER_CPP_BIN``, ``PY8N_WHISPER_MODEL``,
  ``PY8N_VOSK_MODEL``, ``PY8N_PIPER_BIN``, ``PY8N_PIPER_VOICE``; defaults
  are discovered under ``data/models/``.
"""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import struct
import subprocess
import tempfile
import wave
from pathlib import Path

from . import voice as voice_svc

ASR_BACKENDS = ("vosk", "whisper.cpp")
TTS_BACKENDS = ("piper",)

# The name the voice contracts already know: voice.ASR_PROVIDERS describes
# py8n_local as the "in-process whisper.cpp / vosk binding".
LOCAL_ASR_NAME = "py8n_local"
LOCAL_TTS_NAME = "piper_local"

_PREFER_ASR = ("vosk", "whisper.cpp")


def models_root() -> Path:
    from .datasets import datasets_dir

    return datasets_dir().parent / "models"


# ---------------------------------------------------------------------------
# Linear16 resampling - the small deterministic core every bridge needs
# ---------------------------------------------------------------------------


def resample_linear16(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Resample mono 16-bit little-endian PCM by linear interpolation.

    Deterministic and dependency-free: exactly ``n * dst/src`` samples out
    (streaming time mapping, integer-rational positions so float drift can
    never skew a sample index). Same rate -> the exact input bytes.
    """
    if src_rate <= 0 or dst_rate <= 0:
        raise ValueError(f"sample rates must be positive, got {src_rate}->{dst_rate}")
    if src_rate == dst_rate:
        return pcm
    n = len(pcm) // 2
    if n == 0:
        return b""
    samples = struct.unpack(f"<{n}h", pcm[: n * 2])
    out_n = n * dst_rate // src_rate
    if out_n <= 0:
        return b""
    out = []
    for j in range(out_n):
        i, frac_num = divmod(j * src_rate, dst_rate)
        if i >= n - 1:
            out.append(samples[n - 1])
        else:
            a, b = samples[i], samples[i + 1]
            out.append(int(round(a + (b - a) * (frac_num / dst_rate))))
    return struct.pack(f"<{out_n}h", *[max(-32768, min(32767, s)) for s in out])


def wav_bytes(pcm: bytes, sample_rate: int) -> bytes:
    """Wrap linear16 mono PCM in a minimal RIFF/WAVE container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Bridge factories - each returns an ASREngine / TTSEngine callable
# ---------------------------------------------------------------------------


def make_vosk_engine(model_dir: str, language: str = ""):
    """vosk in-process recognizer: (pcm, sample_rate) -> asr result.

    The model is loaded ONCE at bind time (Kaldi models are expensive);
    recognition creates a recognizer per utterance, which is the pattern
    vosk itself documents for short segments.

    v73: word data is ON - the utterance's confidence is the MEAN of the
    per-word confidences vosk's acoustic model produces, so voice session
    analytics get REAL numbers from a local engine instead of the honest
    0.0 "not reported" default. A build that still reports no words falls
    back to the payload's top-level confidence, then to 0.0.
    """
    from vosk import KaldiRecognizer, Model  # noqa: import guarded by probe

    model_path = str(model_dir)
    model = Model(model_path)

    def _recognize(pcm: bytes, sample_rate: int) -> dict:
        data = pcm if sample_rate == 16000 else resample_linear16(pcm, sample_rate, 16000)
        rec = KaldiRecognizer(model, 16000)
        rec.SetWords(True)
        rec.AcceptWaveform(data)
        raw = rec.FinalResult()
        payload = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        transcript = str(payload.get("text") or "").strip()
        words = [w for w in (payload.get("result") or []) if isinstance(w, dict)]
        confs = []
        for w in words:
            try:
                confs.append(float(w.get("conf", w.get("confidence", 0.0))))
            except (TypeError, ValueError):
                continue
        if confs:
            confidence = sum(confs) / len(confs)
        else:
            # vosk historically reports no confidence on FinalResult; when the
            # build does, use it - otherwise the contract's honest default (0.0)
            # stands. An empty transcript raises through validate_asr_result,
            # which the transport surfaces as asr.error (silence is not words).
            try:
                confidence = float(payload.get("confidence", 0.0))
            except (TypeError, ValueError):
                confidence = 0.0
        return voice_svc.validate_asr_result({
            "transcript": transcript,
            "confidence": confidence,
            "language": language,
            "is_final": True,
        })

    return _recognize


def make_whispercpp_engine(binary: str, model_path: str, language: str = ""):
    """whisper.cpp CLI recognizer: (pcm, sample_rate) -> asr result.

    Utterance PCM is resampled to the 16 kHz whisper.cpp expects, wrapped
    in a temp wav (cleaned up in finally), and handed to
    ``<binary> -m <model> -f <wav> -nt -of -`` style invocation; the
    stdout text (minus the CLI's prompt noise) is the transcript.
    """

    def _recognize(pcm: bytes, sample_rate: int) -> dict:
        data = pcm if sample_rate == 16000 else resample_linear16(pcm, sample_rate, 16000)
        if not data:
            raise voice_svc.VoiceError("asr result requires a non-empty 'transcript'")
        fd, tmp = tempfile.mkstemp(suffix=".wav", prefix="py8n-asr-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(wav_bytes(data, 16000))
            cmd = [str(binary), "-m", str(model_path), "-f", tmp, "-nt"]
            if language:
                cmd += ["-l", language]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                                  check=False)
            if proc.returncode != 0:
                raise voice_svc.VoiceError(
                    f"whisper.cpp failed (exit {proc.returncode}): "
                    f"{(proc.stderr or '').strip()[:300]}")
            transcript = _strip_whisper_output(proc.stdout)
            return voice_svc.validate_asr_result({
                "transcript": transcript,
                "confidence": 0.0,  # whisper.cpp prints no confidence - honest default
                "language": language,
                "is_final": True,
            })
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    return _recognize


def _strip_whisper_output(stdout: str) -> str:
    """whisper.cpp with -nt prints the text (plus possible system lines)."""
    lines = [ln.strip() for ln in (stdout or "").splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith(("whisper_", "system_info",
                                                             "main:", "error"))]
    return " ".join(lines).strip()


def make_piper_engine(binary: str, voices: dict[str, str], default_voice: str = ""):
    """piper synthesizer: (text, voice, fmt) -> wav bytes.

    ``voices`` maps a voice name to its .onnx path (the file stem is the
    name, e.g. ``en_US-amy-medium``). Only wav comes out of piper - any
    other requested format fails loud instead of being silently wrapped.
    """

    def _synthesize(text: str, voice: str = "", fmt: str = "wav") -> bytes:
        if not str(text or "").strip():
            raise voice_svc.VoiceError("tts request requires text")
        if fmt not in ("wav", "mulaw"):
            raise voice_svc.VoiceError(
                f"piper produces wav - requested {fmt!r} must be transcoded upstream")
        # an explicit voice that is not on disk FAILS LOUD - silently falling
        # back would let the caller hear a different voice than configured
        chosen = (voice or "").strip() or default_voice or next(iter(voices), "")
        model = voices.get(chosen or "")
        if not model:
            raise voice_svc.VoiceError(
                f"piper voice {voice!r} not on disk - available: {', '.join(sorted(voices))}")
        fd, out = tempfile.mkstemp(suffix=".wav", prefix="py8n-tts-")
        os.close(fd)
        try:
            proc = subprocess.run([str(binary), "--model", str(model),
                                   "--output_file", out],
                                  input=str(text).encode("utf-8"),
                                  capture_output=True, timeout=120, check=False)
            if proc.returncode != 0:
                raise voice_svc.VoiceError(
                    f"piper failed (exit {proc.returncode}): "
                    f"{(proc.stderr or b'').decode('utf-8', 'replace').strip()[:300]}")
            data = Path(out).read_bytes()
            if not data:
                raise voice_svc.VoiceError("piper produced no audio")
            return data
        finally:
            try:
                os.unlink(out)
            except OSError:
                pass

    return _synthesize


# ---------------------------------------------------------------------------
# TTS engine registry - the v70 ASR registry's sibling
# ---------------------------------------------------------------------------

# A TTS engine maps (text, voice, fmt) -> audio bytes (wav).
TTSEngine = object  # structural: any callable with that signature

_TTS_ENGINES: dict[str, object] = {}


def register_tts_engine(name: str, engine) -> None:
    if not callable(engine):
        raise ValueError("a tts engine must be callable (text, voice, fmt) -> audio bytes")
    _TTS_ENGINES[str(name).strip()] = engine


def unregister_tts_engine(name: str) -> bool:
    return _TTS_ENGINES.pop(str(name).strip(), None) is not None


def get_tts_engine(name: str):
    return _TTS_ENGINES.get(str(name).strip())


def registered_tts_engines() -> list[str]:
    return sorted(_TTS_ENGINES)


def wav_duration_ms(data: bytes) -> int:
    """Parse a RIFF wav's fmt + data chunks for an honest duration."""
    try:
        if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
            return 0
        pos = 12
        rate = 0
        bits = 0
        while pos + 8 <= len(data):
            cid = data[pos:pos + 4]
            size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
            body = data[pos + 8:pos + 8 + size]
            if cid == b"fmt ":
                rate = struct.unpack("<I", body[4:8])[0]
                bits = struct.unpack("<H", body[14:16])[0]
            elif cid == b"data":
                if rate and bits:
                    bytes_per_frame = max(1, (bits // 8))
                    return int(1000 * len(body) / (rate * bytes_per_frame))
                break
            pos += 8 + size + (size & 1)
    except Exception:  # noqa: BLE001 - duration is best-effort metadata
        return 0
    return 0


def synthesize(engine_name: str, text: str, voice: str = "", fmt: str = "wav") -> dict:
    """Run a registered TTS engine and return the v69 TTS contract result."""
    engine = get_tts_engine(engine_name)
    if engine is None:
        raise voice_svc.VoiceError(
            f"no TTS engine is registered for {engine_name!r} in this process - "
            f"registered: {', '.join(registered_tts_engines()) or '(none)'}; "
            "install piper (PY8N_PIPER_BIN / PY8N_PIPER_VOICE) or point the agent "
            "at a hosted provider (openai_tts / elevenlabs)")
    audio = engine(text, voice, fmt)
    return voice_svc.validate_tts_result({
        "audio_b64": base64.b64encode(audio).decode("ascii"),
        "format": "wav",
        "duration_estimate_ms": wav_duration_ms(audio),
    })


# ---------------------------------------------------------------------------
# Inventory + boot-time binding - honest probing, never faked
# ---------------------------------------------------------------------------


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip()
    return Path(raw) if raw else None


def _which(*candidates: str) -> Path | None:
    for c in candidates:
        found = shutil.which(c)
        if found:
            return Path(found)
    return None


def _first_match(patterns: list[str]) -> Path | None:
    root = models_root()
    if not root.exists():
        return None
    for pat in patterns:
        hits = sorted(root.glob(pat))
        if hits:
            return hits[0]
    return None


def _bin_dir(*names: str) -> Path | None:
    """v73: installed binaries live under data/models/bin/ (the model
    installer's drop location) - durable across restarts, no env needed."""
    root = models_root() / "bin"
    if not root.exists():
        return None
    for name in names:
        hit = root / name
        if hit.exists() and hit.is_file():
            return hit
    return None


def probe_vosk() -> dict:
    try:
        import vosk  # noqa: F401
    except Exception:  # noqa: BLE001 - probing must never raise
        return {"available": False,
                "note": "the vosk package is not installed in this process - "
                        "pip install vosk, then set PY8N_VOSK_MODEL to a model directory"}
    model = _env_path("PY8N_VOSK_MODEL") or _first_match(["vosk*", "vosk/*"])
    if model is None or not Path(model).exists():
        return {"available": False,
                "note": "vosk is importable but no model directory was found - "
                        "set PY8N_VOSK_MODEL (or drop one under data/models/vosk-<lang>)"}
    return {"available": True, "model": str(model),
            "note": f"vosk model {Path(model).name!r} ready (16 kHz in-process recognition)"}


def probe_whispercpp() -> dict:
    env_bin = _env_path("PY8N_WHISPER_CPP_BIN")
    binary = (env_bin if env_bin and env_bin.exists() else None) \
        or _which("whisper-cli", "whisper.cpp") or _bin_dir("whisper-cli", "whisper.cpp")
    if binary is None:
        return {"available": False,
                "note": "no whisper.cpp binary found - build whisper-cli and set "
                        "PY8N_WHISPER_CPP_BIN (or put it on PATH, or install it "
                        "through POST /voice/speech/models/install when the "
                        "catalog carries a binary for this platform)"}
    model = _env_path("PY8N_WHISPER_MODEL") or _first_match(["ggml-*.bin", "whisper/*.bin"])
    if model is None or not Path(model).exists():
        return {"available": False,
                "note": f"binary {binary.name!r} found but no ggml model - set "
                        "PY8N_WHISPER_MODEL, or install one through POST "
                        "/voice/speech/models/install (slug whisper-tiny-en)"}
    return {"available": True, "binary": str(binary), "model": str(model),
            "note": f"whisper.cpp {binary.name!r} + {Path(model).name!r} ready (16 kHz CLI)"}


def probe_piper() -> dict:
    env_bin = _env_path("PY8N_PIPER_BIN")
    binary = (env_bin if env_bin and env_bin.exists() else None) \
        or _which("piper") or _bin_dir("piper")
    if binary is None:
        return {"available": False,
                "note": "no piper binary found - install piper-tts and set PY8N_PIPER_BIN "
                        "(or put it on PATH)"}
    voices: dict[str, str] = {}
    explicit = _env_path("PY8N_PIPER_VOICE")
    if explicit and Path(explicit).exists():
        voices[Path(explicit).stem] = str(explicit)
    else:
        root = models_root()
        if root.exists():
            for p in sorted(root.glob("*.onnx")):
                voices[p.stem] = str(p)
    if not voices:
        return {"available": False,
                "note": f"binary {binary.name!r} found but no .onnx voice - set "
                        "PY8N_PIPER_VOICE (or drop *.onnx under data/models/)"}
    return {"available": True, "binary": str(binary), "voices": voices,
            "note": f"piper {binary.name!r} + voices: {', '.join(sorted(voices))}"}


def speech_inventory() -> dict:
    """The honest machine inventory (the devices.py pattern, for speech)."""
    vosk = probe_vosk()
    whisper = probe_whispercpp()
    piper = probe_piper()
    asr_pref = next((b for b in _PREFER_ASR
                     if (vosk if b == "vosk" else whisper).get("available")), None)
    return {
        "asr": {"vosk": vosk, "whisper.cpp": whisper,
                "preferred_backend": asr_pref,
                "local_engine_registered": LOCAL_ASR_NAME in _registered_asr()},
        "tts": {"piper": piper,
                "local_engine_registered": LOCAL_TTS_NAME in registered_tts_engines()},
        "registered": {"asr": _registered_asr(), "tts": registered_tts_engines()},
        "note": "engines bind at boot when their binary/package + model are present; "
                "unregistered engines keep the transport honest (asr.unavailable / "
                "tts refused) - nothing is faked",
    }


def _registered_asr() -> list[str]:
    from . import voice_transport

    return voice_transport.registered_asr_engines()


def bind_local_engines() -> dict:
    """Probe the machine and register every bridge that can actually run.

    Best-effort by design: returns what was bound (and why not, for the
    rest). Called at startup; also safe to call again (re-binding replaces).
    """
    from . import voice_transport

    bound: dict = {"asr": None, "tts": None}
    vosk, whisper = probe_vosk(), probe_whispercpp()
    asr_choice = next((p for p in _PREFER_ASR
                       if (vosk if p == "vosk" else whisper).get("available")), None)
    if asr_choice == "vosk":
        try:
            voice_transport.register_asr_engine(
                LOCAL_ASR_NAME, make_vosk_engine(vosk["model"]))
            bound["asr"] = {"name": LOCAL_ASR_NAME, "backend": "vosk",
                            "model": vosk["model"]}
        except Exception as exc:  # noqa: BLE001 - binding stays honest
            bound["asr"] = {"name": LOCAL_ASR_NAME, "backend": "vosk",
                            "error": f"{type(exc).__name__}: {exc}"}
    elif asr_choice == "whisper.cpp":
        try:
            voice_transport.register_asr_engine(
                LOCAL_ASR_NAME, make_whispercpp_engine(whisper["binary"], whisper["model"]))
            bound["asr"] = {"name": LOCAL_ASR_NAME, "backend": "whisper.cpp",
                            "model": whisper["model"]}
        except Exception as exc:  # noqa: BLE001
            bound["asr"] = {"name": LOCAL_ASR_NAME, "backend": "whisper.cpp",
                            "error": f"{type(exc).__name__}: {exc}"}

    piper = probe_piper()
    if piper.get("available"):
        try:
            register_tts_engine(LOCAL_TTS_NAME, make_piper_engine(
                piper["binary"], piper["voices"],
                default_voice=next(iter(piper["voices"]), "")))
            bound["tts"] = {"name": LOCAL_TTS_NAME, "backend": "piper",
                            "voices": sorted(piper["voices"])}
        except Exception as exc:  # noqa: BLE001
            bound["tts"] = {"name": LOCAL_TTS_NAME, "backend": "piper",
                            "error": f"{type(exc).__name__}: {exc}"}
    return bound


# ---------------------------------------------------------------------------
# v74: the bridge VERIFICATION round trip - piper speaks, the ASR hears
# ---------------------------------------------------------------------------

VERIFY_PHRASE = "What are your opening hours on saturday"


def pcm_from_wav(data: bytes) -> tuple[bytes, int]:
    """Unwrap a RIFF/wav produced by a TTS engine into (linear16 pcm, rate).

    The same chunk-walk ``wav_duration_ms`` uses - the verifier feeds REAL
    audio into the ASR bridges, so the container must be parsed honestly
    (fmt: rate/bits/channels; data: the payload). Anything but mono 16-bit
    fails loud - transcoding is the caller's business, not a silent guess.
    """
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise voice_svc.VoiceError("verify needs a RIFF/wav - this TTS output is not one")
    pos = 12
    rate = 0
    bits = 0
    channels = 0
    payload = b""
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            channels = struct.unpack("<H", body[2:4])[0]
            rate = struct.unpack("<I", body[4:8])[0]
            bits = struct.unpack("<H", body[14:16])[0]
        elif cid == b"data":
            payload = body
            break
        pos += 8 + size + (size & 1)
    if not rate or bits != 16 or channels != 1 or not payload:
        raise voice_svc.VoiceError(
            f"verify needs mono 16-bit wav (got rate={rate}, bits={bits}, "
            f"channels={channels}, {len(payload)} data bytes)")
    return payload, rate


def _tokens(text: str) -> list[str]:
    """Normalized word tokens: lowercase, punctuation stripped."""
    import re as _re

    return _re.sub(r"[^a-z0-9\s]", " ", (text or "").lower()).split()


def _resolve_verify_tts(name: str):
    """The verifier's TTS: a registered engine by name, or 'piper' built
    straight from the machine probe (bypassing the registry, like the
    explicit ASR backends do)."""
    if name and name != "piper":
        engine = get_tts_engine(name)
        if engine is None:
            raise voice_svc.VoiceError(
                f"no TTS engine is registered for {name!r} - registered: "
                f"{', '.join(registered_tts_engines()) or '(none)'}")
        return name, engine, "registry"
    probe = probe_piper()
    if not probe.get("available"):
        raise voice_svc.VoiceError(
            "piper is not available on this machine - " + probe.get("note", ""))
    engine = make_piper_engine(probe["binary"], probe["voices"],
                               default_voice=next(iter(probe["voices"]), ""))
    return ("piper" if not name else name), engine, "probe"


def _resolve_verify_asr(name: str):
    """The verifier's ASR: a registered engine by name, or a BACKEND key
    ('whisper.cpp' / 'vosk') built straight from the machine probe.

    The backend keys are the point of the verifier: the registry binds ONE
    py8n_local engine (vosk preferred when both are present), but verifying
    THE WHISPER BRIDGE means running whisper.cpp even when vosk would win
    the registry slot."""
    from . import voice_transport

    if name in ("", "py8n_local"):
        engine = voice_transport.get_asr_engine(LOCAL_ASR_NAME)
        if engine is None:
            raise voice_svc.VoiceError(
                "no local ASR engine is registered in this process - install a model "
                "through POST /voice/speech/models/install and rebind, or pass "
                "asr='whisper.cpp' / 'vosk' to verify a specific backend")
        return LOCAL_ASR_NAME, engine, _last_asr_backend()
    if name == "whisper.cpp":
        probe = probe_whispercpp()
        if not probe.get("available"):
            raise voice_svc.VoiceError(
                "the whisper.cpp bridge cannot run on this machine - "
                + probe.get("note", ""))
        return name, make_whispercpp_engine(probe["binary"], probe["model"]), "probe"
    if name == "vosk":
        probe = probe_vosk()
        if not probe.get("available"):
            raise voice_svc.VoiceError(
                "the vosk bridge cannot run on this machine - " + probe.get("note", ""))
        return name, make_vosk_engine(probe["model"]), "probe"
    engine = voice_transport.get_asr_engine(name) if \
        hasattr(voice_transport, "get_asr_engine") else None
    if engine is None:
        registered = voice_transport.registered_asr_engines()
        raise voice_svc.VoiceError(
            f"no ASR engine is registered for {name!r} - registered: "
            f"{', '.join(registered) or '(none)'}; backends 'whisper.cpp' and 'vosk' "
            "resolve straight from the machine probe")
    return name, engine, "registry"


def _last_asr_backend() -> str:
    """Which backend the bound py8n_local engine uses (from the inventory)."""
    inv = speech_inventory()
    pref = (inv.get("asr") or {}).get("preferred_backend") or ""
    return str(pref)


def verify_bridge(*, asr: str = "", tts: str = "", phrase: str = VERIFY_PHRASE) -> dict:
    """Verify the SPEECH LOOP for real: synthesize the phrase through a TTS
    engine, feed the produced wav to an ASR engine, and score what came
    back against what was spoken.

    This is the bridge proof the model installer's ``after`` notes point
    at: install whisper-tiny-en on a machine with whisper-cli, then
    ``verify_bridge(asr='whisper.cpp')`` runs the REAL binary over REAL
    audio (piper-synthesized) and reports the transcript, the engine's
    confidence and a token-level match ratio. No simulation anywhere -
    when an engine cannot run, the error says exactly why.
    """
    tts_name, tts_engine, tts_source = _resolve_verify_tts(str(tts or "").strip())
    audio = tts_engine(phrase, "", "wav")
    duration_ms = wav_duration_ms(audio)
    pcm, rate = pcm_from_wav(audio)
    if not pcm:
        raise voice_svc.VoiceError("the TTS engine produced an empty wav - nothing to hear")

    asr_name, asr_engine, asr_source = _resolve_verify_asr(str(asr or "").strip())
    result = asr_engine(pcm, rate)
    heard = str(result.get("transcript") or "")
    confidence = float(result.get("confidence") or 0.0)

    spoken_tokens = _tokens(phrase)
    heard_tokens = _tokens(heard)
    if spoken_tokens:
        overlap = set(spoken_tokens) & set(heard_tokens)
        ratio = round(len(overlap) / len(set(spoken_tokens)), 3)
    else:
        ratio = 0.0
    return {
        "ok": bool(heard) and ratio >= 0.5,
        "spoken": phrase,
        "heard": heard,
        "exact": spoken_tokens == heard_tokens and bool(spoken_tokens),
        "match_ratio": ratio,
        "confidence": confidence,
        "tts": {"engine": tts_name, "source": tts_source,
                "audio_ms": duration_ms, "sample_rate": rate, "pcm_bytes": len(pcm)},
        "asr": {"engine": asr_name, "source": asr_source,
                "backend": asr_name if asr_name in ASR_BACKENDS else _last_asr_backend(),
                "language": result.get("language") or ""},
        "note": ("real round trip: TTS synthesized the phrase, ASR transcribed the "
                 "audio it produced, nothing was faked" if heard else
                 "the ASR engine returned no transcript - the loop is NOT verified"),
    }
