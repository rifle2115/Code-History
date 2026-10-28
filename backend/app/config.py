"""Runtime configuration, read from environment / .env at the project root."""

import os
import tempfile
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIR = ROOT / "frontend"


def reload_env() -> None:
    """Re-read .env so keys added while the server is running take effect."""
    load_dotenv(ROOT / ".env", override=True)


reload_env()


def groq_api_key() -> str:
    return os.getenv("GROQ_API_KEY", "").strip()


def groq_model() -> str:
    return os.getenv("GROQ_MODEL", "openai/gpt-oss-120b").strip()


def gemini_api_key() -> str:
    return (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()


def gemini_model() -> str:
    return os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()


def github_token() -> str:
    return os.getenv("GITHUB_TOKEN", "").strip()


WORKSPACE = Path(os.getenv("WORKSPACE_DIR", str(ROOT / "workspace")))
# Temporary clones live outside the project (which may be cloud-synced) and
# are deleted when no longer needed; see clones.py.
CLONES_DIR = Path(os.getenv("CLONES_DIR", str(Path(tempfile.gettempdir()) / "code-history-clones")))
CLONE_IDLE_MINUTES = int(os.getenv("CLONE_IDLE_MINUTES", "30"))
CACHE_DIR = WORKSPACE / "cache"

# How far back to walk a line's history, and how many substantive commits to
# dig into for pull requests and issues.
MAX_HISTORY_COMMITS = int(os.getenv("MAX_HISTORY_COMMITS", "25"))
MAX_DECISION_COMMITS = int(os.getenv("MAX_DECISION_COMMITS", "5"))
MAX_LINKED_ISSUES = 4
MAX_FILE_BYTES = 1_500_000

for d in (WORKSPACE, CACHE_DIR):
    d.mkdir(parents=True, exist_ok=True)
