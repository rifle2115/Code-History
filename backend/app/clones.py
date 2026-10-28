"""Temporary clones of remote repositories.

A pasted URL is cloned into the system temp folder (never the project folder,
which may be synced), without a working tree since everything is read through
git. Clones are deleted when the user closes the repository, when the server
stops, after CLONE_IDLE_MINUTES unused, and any left over from a crash are
swept at startup. Local folders the user opens are never touched.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import uuid
from pathlib import Path

from . import config


class CloneError(Exception):
    pass


def _is_ours(path: Path) -> bool:
    try:
        return path.resolve().is_relative_to(config.CLONES_DIR.resolve()) and path.resolve() != config.CLONES_DIR.resolve()
    except OSError:
        return False


def _force_writable(func, path, _exc) -> None:
    # Git marks pack files read-only, which stops rmtree on Windows.
    os.chmod(path, stat.S_IWRITE)
    func(path)


def remove(path: str | Path | None) -> bool:
    """Delete a clone. Refuses anything outside the clones folder."""
    if not path:
        return False
    p = Path(path)
    if not p.exists() or not _is_ours(p):
        return False
    shutil.rmtree(p, onexc=_force_writable)
    return True


def clone(url: str, label: str) -> Path:
    config.CLONES_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.CLONES_DIR / f"{label}-{uuid.uuid4().hex[:6]}"
    proc = subprocess.run(
        ["git", "clone", "-q", "--no-checkout", "--single-branch", url, str(dest)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        remove(dest)
        raise CloneError(proc.stderr.strip()[:300] or "git clone failed")
    return dest


def remove_all() -> int:
    """Delete every clone in the clones folder."""
    if not config.CLONES_DIR.exists():
        return 0
    count = 0
    for child in config.CLONES_DIR.iterdir():
        if child.is_dir() and remove(child):
            count += 1
    return count


def size_mb(path: str | Path) -> float:
    total = sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file())
    return round(total / 1_048_576, 1)
