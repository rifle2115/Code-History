"""Thin wrappers around the git CLI: blame, line history, file access."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


class GitError(Exception):
    pass


def run_git(repo: str | Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and proc.returncode != 0:
        raise GitError(proc.stderr.strip() or f"git {args[0]} failed")
    return proc.stdout


def repo_root(path: str | Path) -> Path:
    out = run_git(path, "rev-parse", "--show-toplevel").strip()
    return Path(out)


def head_info(repo: str | Path) -> dict:
    sha = run_git(repo, "rev-parse", "HEAD").strip()
    branch = run_git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    count = run_git(repo, "rev-list", "--count", "HEAD").strip()
    return {"sha": sha, "branch": branch, "commits": int(count or 0)}


def remote_url(repo: str | Path) -> str:
    return run_git(repo, "config", "--get", "remote.origin.url", check=False).strip()


_GITHUB_RE = re.compile(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")


def parse_github(url: str) -> dict | None:
    m = _GITHUB_RE.search(url.strip())
    if not m:
        return None
    return {"owner": m.group(1), "name": m.group(2)}


def list_files(repo: str | Path) -> list[str]:
    out = run_git(repo, "ls-tree", "-r", "-z", "--name-only", "HEAD")
    return [p for p in out.split("\0") if p]


def read_file(repo: str | Path, path: str, rev: str = "HEAD") -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), "show", f"{rev}:{path}"],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise GitError(proc.stderr.decode("utf-8", "replace").strip())
    data = proc.stdout
    if b"\0" in data[:8000]:
        raise GitError("Binary file")
    return data.decode("utf-8", "replace")


# --------------------------------------------------------------------------
# Blame
# --------------------------------------------------------------------------

_PORCELAIN_HEAD = re.compile(r"^([0-9a-f]{40}) (\d+) (\d+)(?: (\d+))?$")


def blame(repo: str | Path, path: str, start: int, end: int) -> list[dict]:
    """Blame a line range at HEAD, ignoring whitespace and following moves."""
    args = ["blame", "--porcelain", "-w", "-M", "-C", "-L", f"{start},{end}"]
    ignore = Path(repo) / ".git-blame-ignore-revs"
    if ignore.exists():
        args += ["--ignore-revs-file", str(ignore)]
    out = run_git(repo, *args, "HEAD", "--", path)

    meta: dict[str, dict] = {}
    rows: list[dict] = []
    current: str | None = None
    final_line = 0
    for line in out.splitlines():
        m = _PORCELAIN_HEAD.match(line)
        if m:
            current = m.group(1)
            final_line = int(m.group(3))
            meta.setdefault(current, {"sha": current})
            continue
        if current is None:
            continue
        if line.startswith("\t"):
            rows.append({"line": final_line, "sha": current, "content": line[1:]})
            continue
        key, _, value = line.partition(" ")
        if key in ("author", "author-mail", "author-time", "summary", "filename"):
            meta[current][key.replace("-", "_")] = value
        elif key == "boundary":
            meta[current]["boundary"] = True

    for row in rows:
        row.update({k: v for k, v in meta[row["sha"]].items() if k != "sha"})
    return rows


# --------------------------------------------------------------------------
# Line history (git log -L)
# --------------------------------------------------------------------------

_REC, _FLD, _END = "\x1e", "\x1f", "\x1d"


def line_history(repo: str | Path, path: str, start: int, end: int, limit: int) -> list[dict]:
    """Every commit that touched the given line range, newest first, with the
    removed/added lines for that range so the change can be classified."""
    fmt = f"{_REC}%H{_FLD}%an{_FLD}%ae{_FLD}%aI{_FLD}%P{_FLD}%s{_FLD}%b{_END}"
    out = run_git(
        repo, "log", f"-L{start},{end}:{path}", f"--format={fmt}", f"-n{limit}", "HEAD"
    )

    commits = []
    for record in out.split(_REC):
        if not record.strip():
            continue
        header, _, diff = record.partition(_END)
        parts = header.split(_FLD)
        if len(parts) < 7:
            continue
        sha, author, email, date, parents, subject, body = parts[:7]
        removed, added, file_at_commit = [], [], path
        for dl in diff.splitlines():
            if dl.startswith("+++ "):
                target = dl[4:].strip()
                if target.startswith("b/"):
                    file_at_commit = target[2:]
            elif dl.startswith("--- "):
                continue
            elif dl.startswith("-"):
                removed.append(dl[1:])
            elif dl.startswith("+"):
                added.append(dl[1:])
        commits.append(
            {
                "sha": sha,
                "author": author,
                "email": email,
                "date": date,
                "subject": subject.strip(),
                "body": body.strip(),
                "is_merge": len(parents.split()) > 1,
                "path": file_at_commit,
                "removed": removed,
                "added": added,
                "diff": diff.strip()[:4000],
            }
        )
    return commits


def _normalize(lines: list[str]) -> str:
    s = "".join(lines)
    s = re.sub(r"\s+", "", s)
    s = s.replace("'", '"').replace("`", '"')
    return re.sub(r"[,;]", "", s)


def classify_history(commits: list[dict], target_lines: list[str]) -> None:
    """Set commit["kind"] by following the selected lines' content backwards.

    `git log -L` hunks can widen to neighbouring lines (a reformat that joins
    two lines is enough), so a commit appearing in the log doesn't mean it
    touched the selection. We track the normalised text of each target line
    and only credit a commit when it produced one of them.
    """
    tracked = {n for n in (_normalize([l]) for l in target_lines) if n}
    for c in commits:  # newest first
        if not tracked:
            c["kind"] = "untouched"
            continue
        added = {n for n in (_normalize([l]) for l in c["added"]) if n}
        removed = [n for n in (_normalize([l]) for l in c["removed"]) if n]
        removed_joined = "".join(removed)
        hits = tracked & added
        if not hits:
            c["kind"] = "untouched"
            continue
        changed = [h for h in hits if h not in removed and h not in removed_joined]
        if not changed:
            c["kind"] = "cosmetic"
            continue
        c["kind"] = "modified" if removed else "introduced"
        for h in changed:
            tracked.discard(h)
            best = max(removed, key=lambda r: _similarity(r, h), default=None)
            if best and _similarity(best, h) >= 0.5:
                tracked.add(best)  # keep following the earlier form of this line


def _similarity(a: str, b: str) -> float:
    from difflib import SequenceMatcher

    return SequenceMatcher(None, a, b).ratio()


_PR_IN_MSG = [
    re.compile(r"Merge pull request #(\d+)"),
    re.compile(r"\(#(\d+)\)\s*$"),
    re.compile(r"(?:^|\s)#(\d+)\s*$"),
]


def pr_number_from_message(subject: str) -> int | None:
    for rx in _PR_IN_MSG:
        m = rx.search(subject)
        if m:
            return int(m.group(1))
    return None
