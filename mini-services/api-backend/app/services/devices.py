"""Device policy (v65 policy + v66 compute bridge) - GPU execution mode.

py8n trains from scratch on two backends: the raw-numpy core (CPU) and,
since v66, a torch mirror of the same architectures that runs wherever
torch runs - CUDA, Apple MPS, or torch-CPU.  "GPU execution mode" is
therefore a DEVICE POLICY plus a REAL compute bridge:

* detects what accelerators this environment actually has (torch CUDA /
  Apple MPS) and reports the inventory honestly,
* lets every training node declare a device intent (``cpu`` | ``auto`` |
  ``gpu`` | ``torch``), resolved against the environment,
* routes the numeric core: numpy on ``cpu``, the torch mirror whenever
  the resolved device is torch-backed (cuda / mps / explicit torch-cpu),
* FAILS LOUD when a requested accelerator cannot actually be honored -
  py8n never pretends a model trained on CPU "ran on GPU".

Resolution matrix (node intent wins over the platform default):

=========== ============== ===========================================
intent      result
cpu         numpy core on CPU - the safe default, no torch needed
auto        torch+cuda if available, else torch+mps, else numpy CPU
            (with a note) - the best device this environment offers
gpu         torch+cuda or torch+mps REQUIRED - refused with exact
            remediation when torch is missing or no accelerator shows
torch       the torch backend on its best device (cuda > mps > cpu) -
            an explicit opt-in that also covers torch-CPU
=========== ============== ===========================================
"""

from __future__ import annotations

import importlib.util
import os
import platform

from ..config import settings

DEVICE_CHOICES = ("cpu", "auto", "gpu", "torch")


def detect_devices() -> dict:
    """What accelerators does THIS environment actually have?  Read-only,
    honest, and cheap (torch is only imported when installed)."""
    torch_spec = importlib.util.find_spec("torch")
    cuda = {"available": False, "count": 0, "names": []}
    mps = {"available": False}
    torch_version = None
    if torch_spec is not None:
        try:  # guarded: a broken torch install must not take the API down
            import torch  # type: ignore

            torch_version = torch.__version__
            if getattr(torch, "cuda", None) is not None and torch.cuda.is_available():
                cuda["available"] = True
                cuda["count"] = int(torch.cuda.device_count())
                cuda["names"] = [torch.cuda.get_device_name(i) for i in range(cuda["count"])]
            if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
                mps["available"] = True
        except Exception as exc:  # noqa: BLE001 - report the brokenness
            return _inventory(torch_installed=True, torch_version=None, cuda=cuda, mps=mps,
                              notes=[f"torch is installed but could not be imported: {exc}"])
    return _inventory(torch_installed=torch_spec is not None, torch_version=torch_version,
                      cuda=cuda, mps=mps, notes=[])


def _inventory(*, torch_installed: bool, torch_version: str | None, cuda: dict, mps: dict,
               notes: list[str]) -> dict:
    accel = cuda["available"] or mps["available"]
    if torch_installed:
        backend = "numpy (cpu) + torch (cuda/mps/cpu)" if accel else "numpy (cpu) + torch (cpu)"
    else:
        backend = "numpy (cpu)"
    inv = {
        "cpu": {"cores": os.cpu_count() or 1, "arch": platform.machine(),
                "proc": platform.processor() or platform.machine()},
        "torch_installed": torch_installed,
        "torch_version": torch_version,
        "cuda": cuda,
        "mps": mps,
        "accelerator_present": accel,
        "compute_backend": backend,
        "notes": notes,
    }
    if not torch_installed and not notes:
        inv["notes"].append(
            "torch is not installed - the numpy CPU core is the only training backend in this build "
            "(install torch to unlock the gpu/torch device options)")
    elif torch_installed and not accel and not notes:
        inv["notes"].append(
            "torch is installed (no CUDA/MPS accelerator visible) - device=torch trains on the "
            "torch CPU backend; a CUDA/MPS torch build unlocks real GPU compute")
    elif accel and not notes:
        inv["notes"].append(
            "accelerator available - device=auto/gpu route training through the torch backend")
    return inv


def resolve_device(requested: str | None) -> dict:
    """Resolve a node's device intent into an honest placement verdict.

    Returns ``{"requested", "resolved", "backend", "usable", "note"}``
    where ``backend`` is ``numpy`` or ``torch`` (the node picks the matching
    numeric core) and ``resolved`` is ``cpu`` / ``cuda`` / ``mps``.
    Raises ``ValueError`` when the intent cannot be honored (fail loud,
    with the exact remediation in the message).
    """
    intent = (requested or "cpu").strip().lower() or "cpu"
    if intent not in DEVICE_CHOICES:
        raise ValueError(
            f"unknown device {requested!r} (allowed: {', '.join(DEVICE_CHOICES)})")

    if intent == "cpu":
        return {"requested": intent, "resolved": "cpu", "backend": "numpy",
                "usable": True, "note": ""}

    inv = detect_devices()

    if intent == "auto":
        # auto = the best device this environment actually offers
        if inv["cuda"]["available"]:
            name = inv["cuda"]["names"][0]
            return {"requested": intent, "resolved": "cuda", "backend": "torch",
                    "usable": True, "note": f"auto: training on {name} via the torch backend"}
        if inv["mps"]["available"]:
            return {"requested": intent, "resolved": "mps", "backend": "torch",
                    "usable": True, "note": "auto: training on Apple MPS via the torch backend"}
        note = "auto: no accelerator detected - running on CPU"
        if inv["torch_installed"]:
            note += " (numpy core; device=torch would use the torch CPU backend)"
        return {"requested": intent, "resolved": "cpu", "backend": "numpy",
                "usable": True, "note": note}

    if intent == "torch":
        if not inv["torch_installed"]:
            raise ValueError(
                "device=torch refused: torch is not installed in this environment. "
                "Install torch (the CPU wheel is enough for torch-cpu; use a CUDA build "
                "for GPU compute) - or set device=cpu for the numpy core.")
        if inv["cuda"]["available"]:
            return {"requested": intent, "resolved": "cuda", "backend": "torch",
                    "usable": True, "note": f"torch backend on {inv['cuda']['names'][0]}"}
        if inv["mps"]["available"]:
            return {"requested": intent, "resolved": "mps", "backend": "torch",
                    "usable": True, "note": "torch backend on Apple MPS"}
        return {"requested": intent, "resolved": "cpu", "backend": "torch",
                "usable": True, "note": "torch CPU backend (no accelerator visible)"}

    # intent == "gpu" - an explicit claim py8n will not fake.
    if not inv["torch_installed"]:
        raise ValueError(
            "device=gpu refused: torch is not installed in this environment, so no CUDA/MPS "
            "runtime exists. Install torch with a CUDA build (or run on Apple MPS) and retry - "
            "or set device=cpu / device=auto to train on the numpy CPU core.")
    if not inv["accelerator_present"]:
        raise ValueError(
            "device=gpu refused: torch is installed but no CUDA/MPS accelerator is visible. "
            "Check the driver / nvidia-smi, or set device=cpu / device=auto / device=torch.")
    if inv["cuda"]["available"]:
        return {"requested": intent, "resolved": "cuda", "backend": "torch",
                "usable": True, "note": f"GPU training on {inv['cuda']['names'][0]} via the torch backend"}
    return {"requested": intent, "resolved": "mps", "backend": "torch",
            "usable": True, "note": "GPU training on Apple MPS via the torch backend"}


def device_mode_report() -> dict:
    """The /ops/devices payload: platform mode + honest inventory."""
    return {
        "device_mode": settings.device_mode,
        "allowed_modes": DEVICE_CHOICES,
        "usage": "lm_train / neural_train / lm_generate nodes accept "
                 "device=cpu|auto|gpu|torch; PY8N_DEVICE_MODE sets the platform default",
        **detect_devices(),
    }
