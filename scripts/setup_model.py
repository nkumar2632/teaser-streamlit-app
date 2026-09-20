"""Create or verify the disposable pinned checkout within this app workspace."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / ".model_reference"
URL = "https://github.com/nkumar2632/Football-Teasers-v1.0"
BRANCH = "claude/teaser-model-v1-foundation-b7akky"
PIN = "94e412ed5bedc03a60b0b86f1146ff80c9f9e195"


def git(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()


def main() -> None:
    if not REFERENCE.exists():
        git("clone", "--no-checkout", "--single-branch", "--branch", BRANCH, URL,
            str(REFERENCE))
        git("remote", "set-url", "--push", "origin", "DISABLED", cwd=REFERENCE)
        git("checkout", "--detach", PIN, cwd=REFERENCE)
    if git("rev-parse", "HEAD", cwd=REFERENCE) != PIN:
        raise SystemExit("Model reference is not at the pinned commit; inspect it manually")
    if git("status", "--porcelain", "--untracked-files=all", cwd=REFERENCE):
        raise SystemExit("Model reference is dirty; inspect it manually")
    if git("remote", "get-url", "--push", "origin", cwd=REFERENCE) != "DISABLED":
        raise SystemExit("Model reference push URL is not disabled")
    print(f"Model ready: {PIN}; clean; push disabled")


if __name__ == "__main__":
    main()
