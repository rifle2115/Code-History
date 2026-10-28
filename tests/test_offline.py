"""Offline checks: no Groq, Gemini or GitHub calls.

Builds a throwaway git repository whose history hides a real reason behind a
formatting commit, then exercises git parsing, the LangGraph pipeline with a
stand-in model, provider fallback, caching, clone cleanup and the HTTP API.

    .venv/Scripts/python -m unittest discover -s tests -v
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Isolate every file the app writes before it is imported.
_TMP = Path(tempfile.mkdtemp(prefix="code-history-tests-"))
os.environ["WORKSPACE_DIR"] = str(_TMP / "workspace")
os.environ["CLONES_DIR"] = str(_TMP / "clones")
os.environ["GROQ_API_KEY"] = "test-groq"
os.environ["GEMINI_API_KEY"] = "test-gemini"
os.environ["GITHUB_TOKEN"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app import clones, config, git_tools, graph, llm  # noqa: E402

config.reload_env = lambda: None  # keep the test keys; never read the real .env

from langchain_core.messages import HumanMessage  # noqa: E402
from pydantic import BaseModel  # noqa: E402


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _make_fixture() -> Path:
    repo = _TMP / "fixture"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Tester")
    _git(repo, "config", "core.autocrlf", "false")
    f = repo / "client.py"
    f.write_text("def fetch(url):\n    retries = 3\n    return get(url, retries)\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "Add fetch client")
    f.write_text("def fetch(url):\n    retries = 7\n    return get(url, retries)\n")
    _git(repo, "commit", "-qam", "Raise retries to 7: upstream gateway drops ~2% of requests during deploys")
    f.write_text("def fetch(url):\n        retries  =  7\n        return get(url, retries)\n")
    _git(repo, "commit", "-qam", "Reformat with black")
    return repo


FIXTURE = _make_fixture()


class Script:
    """Stand-in for a model: answers or raises from a queue, per provider."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.queue: list = []
        self.answer = None  # callable(schema, messages) -> model, when queue is empty

    def __call__(self, provider, schema, messages, temperature):
        self.calls.append((provider.name, schema.__name__))
        if self.queue:
            item = self.queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return self.answer(schema, messages)


def _ids(messages) -> list[str]:
    return re.findall(r"^\[(E\d+)\]", messages[-1].content, re.M)


def honest_model(labels: dict, claims: list, supported: dict | None = None):
    """Build a stand-in that labels evidence and drafts `claims`, then checks them."""

    def answer(schema, messages):
        if schema is graph._Answer:
            return schema(
                labels=[{"id": i, "label": labels.get(i, "context"), "reason": "r"} for i in _ids(messages)],
                status="found", summary="draft", claims=claims, open_questions=[],
            )
        n = len(re.findall(r"^Claim \d+:", messages[-1].content, re.M))
        ok = supported or {}
        return schema(checks=[{"index": i, "supported": ok.get(i, True)} for i in range(n)], revised_summary="checked")

    return answer


class Base(unittest.TestCase):
    def setUp(self):
        llm._paused.clear()
        llm._CACHE_DIR = Path(tempfile.mkdtemp(dir=_TMP))
        self.model = Script()
        self._real = llm._structured
        llm._structured = self.model

    def tearDown(self):
        llm._structured = self._real

    def trace(self, start, end=None):
        return graph.build_graph().invoke(
            {"repo": str(FIXTURE), "path": "client.py", "start": start, "end": end or start, "question": ""}
        )


class GitTests(unittest.TestCase):
    def test_blame_ignores_whitespace_reformat(self):
        rows = git_tools.blame(FIXTURE, "client.py", 2, 2)
        self.assertIn("Raise retries", rows[0]["summary"])

    def test_history_skips_formatting_and_tracks_the_line(self):
        commits = git_tools.line_history(FIXTURE, "client.py", 2, 2, 20)
        git_tools.classify_history(commits, ["        retries  =  7"])
        kinds = {c["subject"].split(":")[0]: c["kind"] for c in commits}
        self.assertEqual(kinds, {"Reformat with black": "cosmetic", "Raise retries to 7": "modified",
                                 "Add fetch client": "introduced"})

    def test_neighbouring_line_change_is_not_credited(self):
        commits = git_tools.line_history(FIXTURE, "client.py", 3, 3, 20)
        git_tools.classify_history(commits, ["        return get(url, retries)"])
        raise_commit = next(c for c in commits if c["subject"].startswith("Raise"))
        self.assertEqual(raise_commit["kind"], "untouched")

    def test_parse_github_remote(self):
        self.assertEqual(git_tools.parse_github("git@github.com:psf/requests.git"), {"owner": "psf", "name": "requests"})
        self.assertIsNone(git_tools.parse_github("https://gitlab.com/a/b"))


class PipelineTests(Base):
    def test_reason_found_and_cited(self):
        self.model.answer = honest_model(
            {"E1": "rationale", "E2": "context"},
            [{"statement": "Retries were raised to 7 because the gateway drops requests.", "evidence_ids": ["E1"], "kind": "why"}],
        )
        out = self.trace(2)
        self.assertEqual(out["answer"]["status"], "found")
        self.assertEqual(out["answer"]["claims"][0]["evidence_ids"], ["E1"])
        self.assertEqual(out["llm_calls"], ["groq", "groq"])  # one analysis call, one check

    def test_unsupported_claim_is_removed(self):
        self.model.answer = honest_model(
            {"E1": "rationale"},
            [{"statement": "Raised to 7 because of gateway drops.", "evidence_ids": ["E1"], "kind": "why"},
             {"statement": "Required for PCI compliance.", "evidence_ids": ["E1"], "kind": "why"},
             {"statement": "Cites an id that does not exist.", "evidence_ids": ["E99"], "kind": "why"}],
            supported={0: True, 1: False},
        )
        a = self.trace(2)["answer"]
        self.assertEqual([c["statement"] for c in a["claims"]], ["Raised to 7 because of gateway drops."])
        self.assertEqual(a["dropped"], 2)

    def test_only_what_changed_means_no_recorded_rationale(self):
        self.model.answer = honest_model(
            {"E1": "context"},
            [{"statement": "A fetch client was added with 3 retries.", "evidence_ids": ["E1"], "kind": "what"}],
        )
        a = self.trace(3)["answer"]
        self.assertEqual(a["status"], "none")

    def test_all_noise_skips_synthesis_claims(self):
        self.model.answer = honest_model({"E1": "noise", "E2": "noise"}, [])
        a = self.trace(2)["answer"]
        self.assertEqual(a["status"], "none")
        self.assertEqual(a["claims"], [])

    def test_issue_claim_gets_linking_pr(self):
        useful = [{"id": "E1", "kind": "pr", "text": "Fixes #126."}, {"id": "E2", "kind": "issue", "issue": 126, "text": "bug"}]
        self.assertEqual(graph._with_links(["E2"], {e["id"]: e for e in useful}, useful), ["E2", "E1"])

    def test_near_duplicate_claims_are_merged(self):
        claims = [{"statement": "Retries raised to 7 because the gateway drops 2% of requests.", "evidence_ids": ["E1"]},
                  {"statement": "Retries were raised to 7 as the gateway drops about 2% of requests.", "evidence_ids": ["E1"]}]
        self.assertEqual(len(graph._dedupe(claims)), 1)

    def test_trim_must_keep_substance(self):
        self.assertTrue(graph._keeps_substance("The check rejects timestamps from the future.",
                                               "The check rejects timestamps from the future, preventing forgery."))
        self.assertFalse(graph._keeps_substance("The change was introduced (commit c30678d).",
                                                "This fixes issue #126 where future timestamps were accepted."))

    def test_prompt_is_capped(self):
        st = {"evidence": [{"id": f"E{i}", "kind": "pr_comment", "author": "x", "text": "a comment about the design"}
                           for i in range(30)]}
        labels = [e["label"] for e in graph.filter_noise(st)["evidence"]]
        self.assertEqual((labels.count("pending"), labels.count("skipped")), (graph.MAX_PROMPT_ITEMS, 30 - graph.MAX_PROMPT_ITEMS))


class Out(BaseModel):
    v: str


TPD = Exception("429 rate_limit_exceeded on tokens per day (TPD): Limit 200000. Please try again in 26m28.8s.")
TPM = Exception("429 rate_limit_exceeded tokens per minute (TPM). Please try again in 0.1s.")


class ProviderTests(Base):
    def test_rate_limit_parsing(self):
        self.assertEqual(llm.rate_limit(TPD), (1589.3, True))
        wait, daily = llm.rate_limit(Exception("429 RESOURCE_EXHAUSTED quota per day. retryDelay: 41s"))
        self.assertTrue(daily)
        self.assertIsNone(llm.rate_limit(ValueError("bad json")))

    def test_daily_limit_falls_back_to_gemini_and_stays(self):
        self.model.queue = [TPD, Out(v="from gemini"), Out(v="again gemini")]
        u = []
        self.assertEqual(llm.structured(Out, [HumanMessage("a")], usage=u).v, "from gemini")
        self.assertEqual(llm.structured(Out, [HumanMessage("b")], usage=u).v, "again gemini")
        self.assertEqual(u, ["gemini", "gemini"])
        self.assertEqual([p for p, _ in self.model.calls], ["groq", "gemini", "gemini"])

    def test_per_minute_limit_waits_on_groq(self):
        self.model.queue = [TPM, Out(v="groq")]
        u = []
        self.assertEqual(llm.structured(Out, [HumanMessage("c")], usage=u).v, "groq")
        self.assertEqual(u, ["groq"])

    def test_repeat_is_served_from_cache(self):
        self.model.queue = [Out(v="first")]
        llm.structured(Out, [HumanMessage("d")])
        u = []
        self.assertEqual(llm.structured(Out, [HumanMessage("d")], usage=u).v, "first")
        self.assertEqual(u, ["cached"])

    def test_both_exhausted_reports_when_back(self):
        self.model.queue = [TPD, TPD]
        with self.assertRaises(llm.LLMUnavailable) as ctx:
            llm.structured(Out, [HumanMessage("e")])
        self.assertIn("back in", str(ctx.exception))


class CloneTests(unittest.TestCase):
    def test_remove_refuses_paths_outside_clones_dir(self):
        outside = _TMP / "keep-me"
        outside.mkdir()
        self.assertFalse(clones.remove(outside))
        self.assertTrue(outside.exists())
        self.assertFalse(clones.remove(FIXTURE))

    def test_remove_deletes_read_only_git_objects(self):
        d = config.CLONES_DIR / "x__y-test"
        (d / "objects").mkdir(parents=True)
        f = d / "objects" / "pack"
        f.write_text("data")
        os.chmod(f, 0o444)
        self.assertTrue(clones.remove(d))
        self.assertFalse(d.exists())


class ApiTests(Base):
    def test_open_browse_trace_close(self):
        from fastapi.testclient import TestClient

        from app.main import app

        self.model.answer = honest_model(
            {"E1": "rationale"},
            [{"statement": "Retries were raised to 7 because the gateway drops requests.", "evidence_ids": ["E1"], "kind": "why"}],
        )
        with TestClient(app) as client:
            repo = client.post("/api/repos", json={"source": str(FIXTURE)}).json()
            self.assertFalse(repo["ephemeral"])
            self.assertIn("client.py", client.get(f"/api/repos/{repo['id']}/files").json()["files"])
            self.assertIn("retries", client.get(f"/api/repos/{repo['id']}/file", params={"path": "client.py"}).json()["content"])

            body = client.get("/api/trace", params={"repo": repo["id"], "path": "client.py", "start": 2, "end": 2}).text
            events = [json.loads(line[5:]) for line in body.splitlines() if line.startswith("data:")]
            result = next(e for e in events if e["type"] == "result")
            self.assertEqual(result["answer"]["status"], "found")
            self.assertTrue(any(e["type"] == "step" for e in events))

            # Closing a local folder never deletes it.
            self.assertEqual(client.delete(f"/api/repos/{repo['id']}/clone").json(), {"removed": False})
            self.assertTrue(FIXTURE.exists())

            status = client.get("/api/status").json()
            self.assertEqual([p["name"] for p in status["providers"]], ["groq", "gemini"])

            self.assertEqual(client.post("/api/repos", json={"source": "not a path or url"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
