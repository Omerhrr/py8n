"""Device policy (v65) - honest GPU execution mode for training nodes.

py8n's training cores (model_train / neural_train / lm_train) compute in
numpy/sklearn on the CPU.  "GPU execution mode" therefore is NOT a silent
re-route: it is a DEVICE POLICY that

* detects what accelerators this environment actually has (torch CUDA /
  Apple MPS) and reports the inventory honestly,
* lets every training node declare a device intent (``cpu`` | ``auto`` |
  ``gpu``), resolved against the environment and the platform mode,
* FAILS LOUD when a requested accelerator cannot actually be honored -
  py8n never pretends a model trained on the numpy CPU core "ran on GPU".

Resolution matrix (node intent wins over the platform default):

=========== ============== ===========================================
intent      env            result
=========== ============== ===========================================
cpu         any            cpu (numpy core) - the safe default
auto        -              best USABLE device for this build = cpu, with
                           a note when accelerators exist but no numeric
                           bridge is wired in
gpu         accelerator+   REFUSED with honest guidance (either no
                           accelerator -> install torch with CUDA, or an
                           accelerator exists but this build's training
                           core has no GPU numeric path -> do not fake it)
=========== ============== ===========================================
"""

from __future__ import annotations

import importlib.util
import os
import platform

from ..config import settings

DEVICE_CHOICES = ("cpu", "auto", "gpu")


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
    inv = {
        "cpu": {"cores": os.cpu_count() or 1, "arch": platform.machine(),
                "proc": platform.processor() or platform.machine()},
        "torch_installed": torch_installed,
        "torch_version": torch_version,
        "cuda": cuda,
        "mps": mps,
        "accelerator_present": accel,
        "compute_backend": "numpy (cpu)",
        "notes": notes,
    }
    if not torch_installed and not notes:
        inv["notes"].append(
            "torch is not installed - the numpy CPU core is the only training backend in this build")
    elif accel and not notes:
        inv["notes"].append(
            "an accelerator is present, but this build's training cores compute in numpy on CPU - "
            "GPU execution requires a torch-backend training build")
    return inv


def resolve_device(requested: str | None) -> dict:
    """Resolve a node's device intent into an honest placement verdict.

    Returns ``{"requested", "resolved", "backend", "usable", "note"}``.
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
        # auto = best USABLE device for THIS build.  The numeric training
        # core is numpy, so the only usable device is the CPU - even when
        # an accelerator is physically present (no GPU numeric bridge).
        if inv["accelerator_present"]:
            name = (inv["cuda"]["names"] or ["Apple MPS"])[0] if inv["cuda"]["available"] else "Apple MPS"
            return {"requested": intent, "resolved": "cpu", "backend": "numpy",
                    "usable": True,
                    "note": f"auto: {name} detected but unusable by this build's numpy training core - running on CPU"}
        return {"requested": intent, "resolved": "cpu", "backend": "numpy",
                "usable": True, "note": "auto: no accelerator detected - running on CPU"}

    # intent == "gpu" - an explicit claim py8n will not fake.
    if not inv["torch_installed"]:
        raise ValueError(
            "device=gpu refused: torch is not installed in this environment, so no CUDA/MPS "
            "runtime exists. Install torch with a CUDA build (or run on Apple MPS) and retry - "
            "or set device=cpu / device=auto to train on the numpy CPU core.")
    if not inv["accelerator_present"]:
        raise ValueError(
            "device=gpu refused: torch is installed but no CUDA/MPS accelerator is visible. "
            "Check the driver / nvidia-smi, or set device=cpu / device=auto.")
    raise ValueError(
        "device=gpu refused: an accelerator IS present, but this build's training cores "
        "compute in numpy on the CPU - silently faking GPU placement is worse than refusing. "
        "Deploy a torch-backend training build for real GPU compute, or set device=cpu.")


def device_mode_report() -> dict:
    """The /ops/devices payload: platform mode + honest inventory."""
    return {
        "device_mode": settings.device_mode,
        "allowed_modes": DEVICE_CHOICES,
        "usage": "lm_train / neural_train nodes accept device=cpu|auto|gpu; "
                 "PY8N_DEVICE_MODE sets the platform default (cpu|auto|gpu)",
        **detect_devices(),
    }
