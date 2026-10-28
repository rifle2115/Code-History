"""HTTP API and static frontend."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import clones, config, git_tools, llm
from .graph import GRAPH

_REGISTRY = config.WORKSPACE / "repos.json"
_LOCK = threading.RLock()
_ACTIVE: dict[str, int] = {}  # repo id -> traces in progress; never swept mid-trace


def _load_registry() -> dict:
    if _REGISTRY.exists():
        try:
            return json.loads(_REGISTRY.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save_registry(reg: dict) -> None:
    _REGISTRY.write_text(json.dumps(reg, indent=2), encoding="utf-8")


def _release(repo_id: str, reg: dict) -> bool:
    """Delete a repository's temporary clone; keep it listed under Recent."""
    entry = reg.get(repo_id)
    if not entry or not entry.get("ephemeral") or not entry.get("root"):
        return False
    removed = clones.remove(entry["root"])
    entry["root"] = None
    return removed


def _sweep_idle() -> None:
    while True:
        time.sleep(60)
        cutoff = time.time() - config.CLONE_IDLE_MINUTES * 60
        with _LOCK:
            reg = _load_registry()
            stale = [
                rid for rid, e in reg.items()
                if e.get("ephemeral") and e.get("root") and e.get("last_used", 0) < cutoff and not _ACTIVE.get(rid)
            ]
            for rid in stale:
                _release(rid, reg)
            if stale:
                _save_registry(reg)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Clones never outlive the server: clear leftovers from a crash or a
    # previous run, and everything again on shutdown.
    def release_all():
        with _LOCK:
            reg = _load_registry()
            for rid in reg:
                _release(rid, reg)
            _save_registry(reg)
            clones.remove_all()

    release_all()
    threading.Thread(target=_sweep_idle, daemon=True).start()
    yield
    release_all()


app = FastAPI(title="Code-History", lifespan=lifespan)


@app.middleware("http")
async def require_password(request: Request, call_next):
    # A public deployment would otherwise let anyone spend the model and
    # GitHub quotas. Browsers prompt once and resend the credentials,
    # including for the EventSource trace stream.
    password = config.app_password()
    if not password or request.url.path == "/healthz":
        return await call_next(request)
    header = request.headers.get("authorization", "")
    if header.startswith("Basic "):
        try:
            _user, _, given = base64.b64decode(header[6:]).decode().partition(":")
        except (ValueError, UnicodeDecodeError):
            given = ""
        if secrets.compare_digest(given.encode(), password.encode()):
            return await call_next(request)
    return Response("Password required.", status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="Code-History", charset="UTF-8"'})


@app.get("/healthz")
def healthz():
    return {"ok": True}


def _repo_path(repo_id: str) -> Path:
    with _LOCK:
        reg = _load_registry()
        entry = reg.get(repo_id)
        if not entry:
            raise HTTPException(404, "Unknown repository. Open it again.")
        if not entry.get("root"):
            raise HTTPException(410, "This repository's temporary clone was removed. Open it again.")
        if entry.get("ephemeral"):
            entry["last_used"] = time.time()
            _save_registry(reg)
        return Path(entry["root"])


def _describe(repo_id: str, root: Path) -> dict:
    gh = git_tools.parse_github(git_tools.remote_url(root))
    return {
        "id": repo_id,
        "root": str(root),
        "name": f"{gh['owner']}/{gh['name']}" if gh else root.name,
        "github": gh,
        "head": git_tools.head_info(root),
    }


# ---------------------------------------------------------------------------


class OpenRequest(BaseModel):
    source: str


_URL_RE = re.compile(r"^(https?://|git@)")
_SLUG_RE = re.compile(r"^[\w.-]+/[\w.-]+$")


@app.get("/api/status")
def status():
    config.reload_env()
    return {
        "providers": llm.status(),
        "allow_local": config.allow_local_repos(),
        "github_token": bool(config.github_token()),
    }


@app.get("/api/repos")
def repos():
    reg = _load_registry()
    return sorted(reg.values(), key=lambda r: r.get("opened", 0), reverse=True)


@app.post("/api/repos")
def open_repo(req: OpenRequest):
    source = req.source.strip().strip('"')
    if not source:
        raise HTTPException(400, "Enter a local path or a GitHub URL.")

    if not config.allow_local_repos():
        # Public server: never read its own disk or clone from arbitrary hosts.
        if _SLUG_RE.match(source):
            source = f"https://github.com/{source}"
        if not re.match(r"^https://github\.com/[\w.-]+/[\w.-]+?(\.git)?/?$", source):
            raise HTTPException(400, "Enter a public GitHub repository, e.g. https://github.com/encode/httpx")

    local = Path(source).expanduser()
    ephemeral = not config.allow_local_repos() or not local.exists()
    if not ephemeral:
        try:
            root = git_tools.repo_root(local)
        except git_tools.GitError:
            raise HTTPException(400, "That folder is not a git repository.")
        repo_id = hashlib.sha1(str(root.resolve()).lower().encode()).hexdigest()[:10]
    else:
        if _SLUG_RE.match(source):
            source = f"https://github.com/{source}"
        if not _URL_RE.match(source):
            raise HTTPException(400, "Path not found, and it doesn't look like a repository URL.")
        # Identify remote repositories by URL so Recent and links survive re-clones.
        repo_id = hashlib.sha1(source.lower().rstrip("/").removesuffix(".git").encode()).hexdigest()[:10]
        with _LOCK:
            existing = _load_registry().get(repo_id, {}).get("root")
        if existing and Path(existing).exists():
            root = Path(existing)
        else:
            slug = git_tools.parse_github(source)
            label = f"{slug['owner']}__{slug['name']}" if slug else repo_id
            try:
                root = clones.clone(source, label)
            except clones.CloneError as exc:
                raise HTTPException(400, f"Clone failed: {exc}")

    try:
        git_tools.head_info(root)
    except git_tools.GitError:
        if ephemeral:
            clones.remove(root)
        raise HTTPException(400, "Repository has no commits yet.")

    info = _describe(repo_id, root)
    now = time.time()
    info.update(opened=now, last_used=now, ephemeral=ephemeral, source=source if ephemeral else str(root))
    if ephemeral:
        info["clone_mb"] = clones.size_mb(root)
    with _LOCK:
        reg = _load_registry()
        reg[repo_id] = info
        _save_registry(reg)
    return info


@app.delete("/api/repos/{repo_id}/clone")
def close_repo(repo_id: str):
    """Called when the user is done with a repository."""
    with _LOCK:
        if _ACTIVE.get(repo_id):
            return {"removed": False, "reason": "trace in progress"}
        reg = _load_registry()
        removed = _release(repo_id, reg)
        _save_registry(reg)
    return {"removed": removed}


@app.get("/api/repos/{repo_id}/files")
def files(repo_id: str):
    return {"files": git_tools.list_files(_repo_path(repo_id))}


@app.get("/api/repos/{repo_id}/file")
def file(repo_id: str, path: str = Query(...)):
    root = _repo_path(repo_id)
    try:
        text = git_tools.read_file(root, path)
    except git_tools.GitError as exc:
        raise HTTPException(400, str(exc))
    if len(text) > config.MAX_FILE_BYTES:
        raise HTTPException(413, "File is too large to display.")
    return {"path": path, "content": text}


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


@app.get("/api/trace")
def trace(
    repo: str,
    path: str,
    start: int,
    end: int,
    question: str = "",
):
    root = _repo_path(repo)
    if end < start:
        start, end = end, start

    def events():
        state: dict = {"warnings": [], "trail": [], "llm_calls": []}
        inputs = {"repo": str(root), "path": path, "start": start, "end": end, "question": question}
        with _LOCK:
            _ACTIVE[repo] = _ACTIVE.get(repo, 0) + 1
        try:
            for update in GRAPH.stream(inputs, stream_mode="updates"):
                for node, delta in update.items():
                    if not delta:
                        continue
                    for key, value in delta.items():
                        if key in ("warnings", "trail", "llm_calls"):
                            state[key].extend(value)
                        else:
                            state[key] = value
                    for step in delta.get("trail", []):
                        yield _sse({"type": "step", **step})
                    for w in delta.get("warnings", []):
                        yield _sse({"type": "warning", "message": w})
        except Exception as exc:
            yield _sse({"type": "error", "message": str(exc)})
            return
        finally:
            with _LOCK:
                _ACTIVE[repo] -= 1

        history = [
            {k: c[k] for k in ("sha", "author", "date", "subject", "kind", "blamed", "examined", "is_merge", "removed", "added")}
            for c in state.get("history", [])
        ]
        yield _sse({
            "type": "result",
            "answer": state.get("answer"),
            "evidence": state.get("evidence", []),
            "history": history,
            "blame": state.get("blame", []),
            "warnings": state["warnings"],
            "llm_calls": state["llm_calls"],
            "github": state.get("github"),
        })

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=config.FRONTEND_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(config.FRONTEND_DIR / "index.html")
