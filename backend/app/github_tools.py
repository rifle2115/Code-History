"""Minimal GitHub REST client for pull requests, reviews and issues.

Responses are cached on disk so repeat traces don't burn the rate limit
(60 requests/hour without a token, 5000 with one).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import httpx

from . import config


class GitHubError(Exception):
    pass


class GitHub:
    API = "https://api.github.com"

    def __init__(self, owner: str, name: str, token: str = "", cache_dir: Path = config.CACHE_DIR):
        self.owner, self.name = owner, name
        self.cache_dir = cache_dir / "github" / f"{owner}__{name}"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "code-history-rationale-tracer",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.client = httpx.Client(base_url=self.API, headers=headers, timeout=20)

    def close(self):
        self.client.close()

    # -- transport ---------------------------------------------------------

    def _get(self, path: str, paginate: bool = False, max_pages: int = 3):
        key = hashlib.sha1(f"{path}|{paginate}".encode()).hexdigest()
        cached = self.cache_dir / f"{key}.json"
        if cached.exists():
            return json.loads(cached.read_text(encoding="utf-8"))

        results, url, page = [], path, 0
        params = {"per_page": 100} if paginate else None
        while url and page < max_pages:
            resp = self.client.get(url, params=params)
            if resp.status_code == 404:
                return [] if paginate else None
            if resp.status_code in (403, 429):
                remaining = resp.headers.get("x-ratelimit-remaining")
                if remaining == "0" or resp.status_code == 429:
                    raise GitHubError(
                        "GitHub rate limit reached. Add GITHUB_TOKEN to .env for 5000 requests/hour."
                    )
                raise GitHubError(f"GitHub refused the request ({resp.status_code}).")
            if resp.status_code >= 400:
                raise GitHubError(f"GitHub API error {resp.status_code} on {path}")
            data = resp.json()
            if not paginate:
                results = data
                break
            results.extend(data)
            url = resp.links.get("next", {}).get("url")
            params = None
            page += 1

        cached.write_text(json.dumps(results), encoding="utf-8")
        return results

    @property
    def _repo(self) -> str:
        return f"/repos/{self.owner}/{self.name}"

    # -- endpoints ---------------------------------------------------------

    def prs_for_commit(self, sha: str) -> list[dict]:
        return self._get(f"{self._repo}/commits/{sha}/pulls") or []

    def pull(self, number: int) -> dict | None:
        return self._get(f"{self._repo}/pulls/{number}")

    def review_comments(self, number: int) -> list[dict]:
        return self._get(f"{self._repo}/pulls/{number}/comments", paginate=True)

    def reviews(self, number: int) -> list[dict]:
        return self._get(f"{self._repo}/pulls/{number}/reviews", paginate=True)

    def issue(self, number: int) -> dict | None:
        return self._get(f"{self._repo}/issues/{number}")

    def issue_comments(self, number: int) -> list[dict]:
        return self._get(f"{self._repo}/issues/{number}/comments", paginate=True)


_ISSUE_REF = re.compile(r"(?<![\w/])#(\d+)\b")


def linked_issue_numbers(text: str, owner: str, name: str) -> list[int]:
    nums = [int(n) for n in _ISSUE_REF.findall(text or "")]
    url_rx = re.compile(
        rf"github\.com/{re.escape(owner)}/{re.escape(name)}/(?:issues|pull)/(\d+)", re.I
    )
    nums += [int(n) for n in url_rx.findall(text or "")]
    seen, out = set(), []
    for n in nums:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out
