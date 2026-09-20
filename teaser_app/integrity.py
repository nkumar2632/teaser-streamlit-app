"""Fail closed if the disposable model checkout or import state changes."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / ".model_reference"
SOURCE = REFERENCE / "src"
PIN = "94e412ed5bedc03a60b0b86f1146ff80c9f9e195"


class ModelIntegrityError(RuntimeError):
    pass


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(REFERENCE), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if result.returncode:
        raise ModelIntegrityError(f"model reference Git check failed: {' '.join(args)}")
    return result.stdout.strip()


def verify_model() -> None:
    if not SOURCE.is_dir():
        raise ModelIntegrityError("pinned model reference is missing")
    if _git("rev-parse", "HEAD") != PIN:
        raise ModelIntegrityError("model reference is not at the pinned commit")
    if _git("status", "--porcelain", "--untracked-files=all"):
        raise ModelIntegrityError("model reference contains changed or untracked files")
    if _git("diff", "--name-only", "HEAD"):
        raise ModelIntegrityError("tracked model files differ from the pinned commit")
    if _git("remote", "get-url", "--push", "origin") != "DISABLED":
        raise ModelIntegrityError("model reference push remote is not disabled")
    source = SOURCE.resolve()
    for name, module in tuple(sys.modules.items()):
        if name == "teaser_model_v1" or name.startswith("teaser_model_v1."):
            path = getattr(module, "__file__", None)
            if path is not None and not Path(path).resolve().is_relative_to(source):
                raise ModelIntegrityError(f"foreign model module is already loaded: {name}")


def load_model_path() -> None:
    verify_model()
    sys.dont_write_bytecode = True
    source = str(SOURCE.resolve())
    if source not in sys.path:
        sys.path.insert(0, source)
