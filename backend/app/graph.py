"""LangGraph pipeline: trace a line range back to the discussion that shaped it.

    locate -> blame -> history -> discussions -> filter --+--> synthesize -> verify -> END
                                                          |
                                                          +--> no_rationale -> END

The no_rationale branch is deterministic on purpose: when nothing in the
record explains the code, the answer says so without asking a model.
"""

from __future__ import annotations

import operator
import re
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from . import config, git_tools, prompts
from .github_tools import GitHub, GitHubError, linked_issue_numbers
from .llm import LLMUnavailable, structured


class TraceState(TypedDict, total=False):
    # input
    repo: str
    path: str
    start: int
    end: int
    question: str
    # derived
    github: dict | None
    target_lines: list[str]
    blame: list[dict]
    history: list[dict]
    decision_shas: list[str]
    evidence: list[dict]
    answer: dict
    warnings: Annotated[list[str], operator.add]
    trail: Annotated[list[dict], operator.add]
    llm_calls: Annotated[list[str], operator.add]  # "groq", "gemini" or "cached", one per model call


def _step(node: str, title: str, detail: str = "") -> list[dict]:
    return [{"node": node, "title": title, "detail": detail}]


def _short(sha: str) -> str:
    return sha[:7]


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


def locate(state: TraceState) -> dict:
    repo, path = state["repo"], state["path"]
    text = git_tools.read_file(repo, path)
    lines = text.splitlines()
    start, end = state["start"], state["end"]
    if start < 1 or end > len(lines) or start > end:
        raise ValueError(f"Lines {start}-{end} are outside {path} ({len(lines)} lines).")
    gh = git_tools.parse_github(git_tools.remote_url(repo))
    where = f"{gh['owner']}/{gh['name']} on GitHub" if gh else "local history only"
    span = f"line {start}" if start == end else f"lines {start}-{end}"
    return {
        "target_lines": lines[start - 1 : end],
        "github": gh,
        "warnings": [] if gh else ["No GitHub remote: pull requests and issues can't be searched."],
        "trail": _step("locate", f"Target {path}, {span}", where),
    }


def blame(state: TraceState) -> dict:
    rows = git_tools.blame(state["repo"], state["path"], state["start"], state["end"])
    shas = list(dict.fromkeys(r["sha"] for r in rows))
    detail = ", ".join(f"{_short(s)}" for s in shas[:4])
    return {
        "blame": rows,
        "trail": _step("blame", f"Blame points to {len(shas)} commit{'s' if len(shas) != 1 else ''}", detail),
    }


def history(state: TraceState) -> dict:
    commits = git_tools.line_history(
        state["repo"], state["path"], state["start"], state["end"], config.MAX_HISTORY_COMMITS
    )
    blamed = {r["sha"] for r in state.get("blame", [])}
    git_tools.classify_history(commits, state.get("target_lines", []))
    for c in commits:
        c["blamed"] = c["sha"] in blamed
    # Neighbouring-line commits that never touched the selection add noise only.
    commits = [c for c in commits if c["kind"] != "untouched" or c["blamed"]]

    substantive = [c for c in commits if c["kind"] in ("introduced", "modified") and not c["is_merge"]]
    # Newest substantive changes first, but always keep the commit that
    # introduced the lines; that's where the original reasoning usually lives.
    chosen = substantive[: config.MAX_DECISION_COMMITS]
    introduced = next((c for c in reversed(commits) if c["kind"] == "introduced"), None)
    if introduced and introduced not in chosen:
        chosen = chosen[: config.MAX_DECISION_COMMITS - 1] + [introduced]
    for c in commits:
        c["examined"] = c in chosen

    skipped = [c for c in commits if c["kind"] == "cosmetic"]
    detail = f"{len(substantive)} substantive, {len(skipped)} formatting-only skipped"
    blamed_cosmetic = [c for c in skipped if c["blamed"]]
    if blamed_cosmetic:
        detail += f"; blame landed on {_short(blamed_cosmetic[0]['sha'])} (formatting), walked past it"
    return {
        "history": commits,
        "decision_shas": [c["sha"] for c in chosen],
        "trail": _step("history", f"{len(commits)} commit{'s' if len(commits) != 1 else ''} touched these lines", detail),
    }


def _clip(text: str, n: int = 1800) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n].rstrip() + " …"


def discussions(state: TraceState) -> dict:
    by_sha = {c["sha"]: c for c in state.get("history", [])}
    evidence: list[dict] = []
    warnings: list[str] = []

    def add(kind: str, text: str, **meta) -> None:
        if not (text or "").strip():
            return
        evidence.append({"id": f"E{len(evidence) + 1}", "kind": kind, "text": _clip(text), **meta})

    for sha in state.get("decision_shas", []):
        c = by_sha[sha]
        msg = c["subject"] + ("\n\n" + c["body"] if c["body"] else "")
        add("commit", msg, author=c["author"], date=c["date"], sha=sha, title=c["subject"], url=None)

    gh_info = state.get("github")
    prs_seen: set[int] = set()
    issues_seen: set[int] = set()
    pr_count = issue_count = 0

    if gh_info:
        gh = GitHub(gh_info["owner"], gh_info["name"], config.github_token())
        base = f"https://github.com/{gh_info['owner']}/{gh_info['name']}"
        for ev in evidence:
            if ev["kind"] == "commit":
                ev["url"] = f"{base}/commit/{ev['sha']}"
        try:
            for sha in state.get("decision_shas", []):
                c = by_sha[sha]
                numbers = [p["number"] for p in gh.prs_for_commit(sha) if p.get("number")]
                if not numbers:
                    n = git_tools.pr_number_from_message(c["subject"])
                    numbers = [n] if n else []
                for number in numbers[:2]:
                    if number in prs_seen:
                        continue
                    pr = gh.pull(number)
                    if not pr:
                        continue
                    prs_seen.add(number)
                    pr_count += 1
                    add(
                        "pr", f"{pr['title']}\n\n{pr.get('body') or ''}",
                        author=pr["user"]["login"], date=pr["created_at"], url=pr["html_url"],
                        title=f"PR #{number}: {pr['title']}", pr=number, sha=sha,
                    )
                    paths = {state["path"], c.get("path", state["path"])}
                    for rc in gh.review_comments(number):
                        if rc.get("path") not in paths:
                            continue
                        hunk = "\n".join((rc.get("diff_hunk") or "").splitlines()[-4:])
                        add(
                            "review_comment", rc.get("body", ""),
                            author=rc["user"]["login"], date=rc["created_at"], url=rc["html_url"],
                            title=f"Review on {rc['path']}", pr=number, code=hunk,
                            reply_to=rc.get("in_reply_to_id"),
                        )
                    for rv in gh.reviews(number):
                        add(
                            "review", rv.get("body", ""),
                            author=(rv.get("user") or {}).get("login", "?"),
                            date=rv.get("submitted_at"), url=rv.get("html_url"),
                            title=f"Review ({(rv.get('state') or '').lower().replace('_', ' ')})", pr=number,
                        )
                    for ic in gh.issue_comments(number)[:40]:
                        add(
                            "pr_comment", ic.get("body", ""),
                            author=ic["user"]["login"], date=ic["created_at"], url=ic["html_url"],
                            title=f"Comment on PR #{number}", pr=number,
                        )
                    refs = linked_issue_numbers(
                        (pr.get("body") or "") + "\n" + c["body"] + "\n" + c["subject"],
                        gh_info["owner"], gh_info["name"],
                    )
                    for ref in refs:
                        if ref in prs_seen or ref in issues_seen or len(issues_seen) >= config.MAX_LINKED_ISSUES:
                            continue
                        issues_seen.add(ref)
                        issue = gh.issue(ref)
                        if not issue or issue.get("pull_request"):
                            continue
                        issue_count += 1
                        add(
                            "issue", f"{issue['title']}\n\n{issue.get('body') or ''}",
                            author=issue["user"]["login"], date=issue["created_at"], url=issue["html_url"],
                            title=f"Issue #{ref}: {issue['title']}", issue=ref,
                        )
                        for ic in gh.issue_comments(ref)[:20]:
                            add(
                                "issue_comment", ic.get("body", ""),
                                author=ic["user"]["login"], date=ic["created_at"], url=ic["html_url"],
                                title=f"Comment on issue #{ref}", issue=ref,
                            )
        except GitHubError as exc:
            warnings.append(str(exc))
        except Exception as exc:  # network trouble shouldn't sink the trace
            warnings.append(f"GitHub lookup failed: {exc}")
        finally:
            gh.close()

    commits_n = sum(1 for e in evidence if e["kind"] == "commit")
    detail = f"{commits_n} commit messages, {pr_count} pull request{'s' if pr_count != 1 else ''}, " \
             f"{issue_count} linked issue{'s' if issue_count != 1 else ''}"
    return {
        "evidence": evidence,
        "warnings": warnings,
        "trail": _step("discussions", f"Collected {len(evidence)} piece{'s' if len(evidence) != 1 else ''} of discussion", detail),
    }


# -- noise filtering --------------------------------------------------------

_PROCEDURAL = re.compile(
    r"^\s*(lgtm|sgtm|ship\s*it|\+1|:\+1:|👍|thanks?( you)?|thx|ty|approved?|merged?|merging|"
    r"rebased?|done|fixed|ok(ay)?|nice|great|looks good( to me)?|cc @\S+|ping|bump|"
    r"/?(retest|rerun|test this please))[\s.!]*$",
    re.I,
)
_BOTS = ("[bot]", "codecov", "netlify", "vercel", "dependabot", "renovate", "github-actions", "coveralls")
_TRIVIAL_COMMIT = re.compile(r"^(wip|fix|fixes|update|updates|changes|misc|tmp|minor|cleanup)\.?$", re.I)


def _heuristic_noise(ev: dict) -> str | None:
    author = (ev.get("author") or "").lower()
    if any(b in author for b in _BOTS):
        return "automated account"
    text = ev["text"].strip()
    if ev["kind"] == "commit":
        return "uninformative commit message" if _TRIVIAL_COMMIT.match(text) else None
    if _PROCEDURAL.match(text):
        return "procedural"
    if len(text) < 12:
        return "too short to carry reasoning"
    return None


class _Label(BaseModel):
    id: str
    label: Literal["rationale", "context", "noise"]
    reason: str = Field(description="Under 15 words.")


def _target_block(state: TraceState) -> str:
    numbered = "\n".join(
        f"{state['start'] + i:>5} | {line}" for i, line in enumerate(state.get("target_lines", []))
    )
    return f"File: {state['path']}\n{numbered}"


# What the model sees, kept well under Groq's free-tier 8k tokens/minute so a
# trace's two calls fit in one minute.
MAX_PROMPT_ITEMS = 24
MAX_PROMPT_CHARS = 14_000
_KIND_PRIORITY = ("commit", "pr", "issue", "review_comment", "review", "pr_comment", "issue_comment")


def filter_noise(state: TraceState) -> dict:
    """Cheap, model-free pass: drop obvious noise and cap what the model sees."""
    evidence = [dict(e) for e in state.get("evidence", [])]
    pending = []
    for ev in evidence:
        reason = _heuristic_noise(ev)
        if reason:
            ev["label"], ev["reason"] = "noise", reason
        else:
            ev["label"], ev["reason"] = "pending", ""
            pending.append(ev)

    pending.sort(key=lambda e: _KIND_PRIORITY.index(e["kind"]))
    for ev in pending[MAX_PROMPT_ITEMS:]:
        ev["label"], ev["reason"] = "skipped", "not sent to the model, to stay within the request size"

    noise = sum(1 for e in evidence if e["label"] == "noise")
    skipped = len(pending[MAX_PROMPT_ITEMS:])
    detail = f"{noise} procedural or bot item{'s' if noise != 1 else ''} removed"
    if skipped:
        detail += f", {skipped} lower-priority item{'s' if skipped != 1 else ''} left out"
    return {"evidence": evidence, "trail": _step("filter", "Removed obvious noise", detail)}


USEFUL = ("rationale", "context", "unclassified")


def route_after_filter(state: TraceState) -> str:
    pending = [e for e in state.get("evidence", []) if e.get("label") == "pending"]
    return "synthesize" if pending else "no_rationale"


# -- answer -----------------------------------------------------------------


class _Claim(BaseModel):
    statement: str
    evidence_ids: list[str]
    kind: Literal["why", "what"] = Field(
        default="what", description='"why" if the claim states a reason, "what" if it only describes a change.'
    )


class _Answer(BaseModel):
    labels: list[_Label] = Field(description="One label for every evidence item, by id.")
    status: Literal["found", "partial", "none"]
    summary: str
    claims: list[_Claim] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


def _searched_sentence(state: TraceState) -> str:
    hist = state.get("history", [])
    ev = state.get("evidence", [])
    prs = len({e.get("pr") for e in ev if e.get("pr")})
    issues = len({e.get("issue") for e in ev if e.get("issue")})
    return (
        f"Searched {len(hist)} commit{'s' if len(hist) != 1 else ''}, {prs} pull request{'s' if prs != 1 else ''} and "
        f"{issues} linked issue{'s' if issues != 1 else ''}."
    )


def no_rationale(state: TraceState) -> dict:
    answer = {
        "status": "none",
        "summary": "No commit message, pull request, review or linked issue explains why this code "
        "is written this way. The reason was never recorded, so treat it as unknown rather than "
        "assuming one. " + _searched_sentence(state),
        "claims": [],
        "open_questions": [
            "Ask the author of the introducing commit, or the current code owner, what this protects against.",
        ],
        "dropped": 0,
    }
    return {"answer": answer, "trail": _step("answer", "No recorded rationale", "Reported as unknown")}


def _changes_block(state: TraceState) -> str:
    return "\n\n".join(
        f"{_short(c['sha'])} ({c['kind']}):\n" + "\n".join(
            [f"- {l}" for l in c["removed"][:12]] + [f"+ {l}" for l in c["added"][:12]]
        )
        for c in state.get("history", []) if c.get("examined")
    )


def _with_links(ids: list[str], by_id: dict, useful: list[dict]) -> list[str]:
    """A claim that cites an issue also needs the PR or commit that says it
    fixes that issue; otherwise "this change addresses #N" has no source."""
    out = list(ids)
    for issue in {by_id[i].get("issue") for i in ids if by_id[i].get("issue")}:
        ref = re.compile(rf"(?<![\w/])#{issue}\b|/issues/{issue}\b")
        for e in useful:
            if e["kind"] in ("pr", "commit") and e["id"] not in out and ref.search(e["text"]):
                out.append(e["id"])
    return out


def _dedupe(claims: list[dict]) -> list[dict]:
    """Drop a claim that repeats an earlier one from the same sources."""
    from difflib import SequenceMatcher

    out: list[dict] = []
    for c in claims:
        same = any(
            set(c["evidence_ids"]) <= set(k["evidence_ids"])
            and SequenceMatcher(None, c["statement"].lower(), k["statement"].lower()).ratio() > 0.55
            for k in out
        )
        if not same:
            out.append(c)
    return out


def synthesize(state: TraceState) -> dict:
    """One model call: label the remaining evidence and draft the answer."""
    evidence = [dict(e) for e in state["evidence"]]
    pending = [e for e in evidence if e.get("label") == "pending"]
    history_lines = "\n".join(
        f"- {_short(c['sha'])} {c['date'][:10]} [{c['kind']}] {c['subject']}" for c in state.get("history", [])
    )
    diffs = _changes_block(state)
    per_item = max(300, min(1200, MAX_PROMPT_CHARS // max(1, len(pending))))
    listing = "\n\n".join(
        f"[{e['id']}] {e['kind'].replace('_', ' ')} by {e.get('author', '?')} ({(e.get('date') or '')[:10]})\n"
        f"{e['text'][:per_item]}"
        + (f"\nOn code:\n{e['code']}" if e.get("code") else "")
        for e in pending
    )
    question = state.get("question") or "Why is this code written this way?"
    usage: list[str] = []

    def failed(title: str, message: str) -> dict:
        for e in pending:
            e["label"], e["reason"] = "unclassified", ""
        return {"evidence": evidence, "llm_calls": usage,
                "answer": {"status": "error", "summary": message, "claims": [], "open_questions": []},
                "trail": _step("answer", title, message)}

    try:
        out = structured(
            _Answer,
            [
                SystemMessage(prompts.ANALYZE_SYSTEM),
                HumanMessage(
                    f"QUESTION\n{question}\n\nTARGET CODE (at HEAD)\n{_target_block(state)}\n\n"
                    f"LINE HISTORY (newest first)\n{history_lines}\n\nCHANGES TO THESE LINES\n{diffs}\n\n"
                    f"EVIDENCE\n{listing}"
                ),
            ],
            usage=usage,
        )
    except LLMUnavailable as exc:
        return failed("Model unavailable", str(exc))
    except Exception as exc:
        return failed("Synthesis failed", f"Synthesis failed: {exc}")

    labels = {l.id: l for l in out.labels}
    for e in pending:
        l = labels.get(e["id"])
        e["label"], e["reason"] = (l.label, l.reason) if l else ("context", "not labelled by the model")
    useful = [e for e in evidence if e.get("label") in USEFUL]
    counts = {k: sum(1 for e in evidence if e.get("label") == k) for k in ("rationale", "context", "noise")}

    valid_ids = {e["id"] for e in useful}
    by_id = {e["id"]: e for e in useful}
    claims = []
    for c in out.claims:
        ids = [i for i in c.evidence_ids if i in valid_ids]
        if ids:
            claims.append({"statement": c.statement, "kind": c.kind,
                           "evidence_ids": _with_links(ids, by_id, useful)})
    claims = _dedupe(claims)
    claims.sort(key=lambda c: c["kind"] != "why")  # the verifier's summary follows this order
    answer = {
        "status": out.status,
        "summary": out.summary,
        "claims": claims,
        "open_questions": out.open_questions,
        "dropped": len(out.claims) - len(claims),
    }
    return {
        "evidence": evidence,
        "answer": answer,
        "llm_calls": usage,
        "trail": _step(
            "synthesize",
            "Separated reasoning from noise and drafted an answer",
            f"{counts['rationale']} rationale, {counts['context']} context, {counts['noise']} noise; "
            f"{len(claims)} cited claim{'s' if len(claims) != 1 else ''}",
        ),
    }


def route_after_synthesize(state: TraceState) -> str:
    a = state.get("answer", {})
    return "verify" if a.get("status") != "error" and a.get("claims") else "finalize"


class _Check(BaseModel):
    index: int
    supported: bool
    note: str = Field(default="", description="Why, in under 20 words. Give it whenever supported is false.")
    corrected: str = Field(
        default="",
        description="When supported is false but part of the claim is backed by its sources, the claim "
        "rewritten to keep only that part. Empty if nothing in it is supported.",
    )


class _Checks(BaseModel):
    checks: list[_Check]
    revised_summary: str


_STOP = set("a an the this that these it its is was were be been to of in on for by with as and or "
             "from at which when where who than then so not no now also".split())


def _keeps_substance(corrected: str, original: str) -> bool:
    """A trimmed claim is worth keeping only if it still talks about what the
    original did. "The change was introduced (commit c30678d)." does not."""
    words = lambda s: {w for w in re.findall(r"[a-z0-9_#<>]+", s.lower()) if w not in _STOP}
    new, old = words(corrected), words(original)
    return len(new) >= 4 and len(new & old) / len(new) >= 0.6


def verify(state: TraceState) -> dict:
    answer = dict(state["answer"])
    usage: list[str] = []
    by_id = {e["id"]: e for e in state["evidence"]}
    blocks = []
    for i, c in enumerate(answer["claims"]):
        cited = "\n".join(
            f"  [{eid}] ({by_id[eid].get('label', 'context')}) {by_id[eid]['text'][:1200]}"
            for eid in c["evidence_ids"]
        )
        blocks.append(f"Claim {i}: {c['statement']}\nCited evidence:\n{cited}")
    try:
        out = structured(
            _Checks,
            [
                SystemMessage(prompts.VERIFY_SYSTEM),
                HumanMessage(
                    f"CHANGES TO THE TARGET LINES (facts about what changed)\n{_changes_block(state)}\n\n"
                    + "\n\n".join(blocks)
                ),
            ],
            usage=usage,
        )
    except Exception as exc:
        answer["unverified"] = True
        return {"answer": answer, "warnings": [f"Grounding check skipped: {exc}"], "llm_calls": usage,
                "trail": _step("verify", "Grounding check skipped", str(exc))}

    verdict = {c.index: c for c in out.checks}
    kept, dropped, trimmed = [], answer.get("dropped", 0), 0
    for i, c in enumerate(answer["claims"]):
        chk = verdict.get(i)
        if chk and chk.supported:
            kept.append(c)
        elif chk and _keeps_substance(chk.corrected, c["statement"]):
            kept.append({**c, "statement": chk.corrected.strip()})
            trimmed += 1
        else:
            dropped += 1
    # Claims backed by stated reasons lead; "what changed" claims follow.
    rationale = {e["id"] for e in state["evidence"] if e.get("label") == "rationale"}
    kept.sort(key=lambda c: (c.get("kind") != "why", -len(set(c["evidence_ids"]) & rationale)))
    before = len(kept)
    kept = _dedupe(kept)  # trimmed claims can collapse into an existing one
    trimmed -= min(trimmed, before - len(kept))
    dropped += before - len(kept)
    answer["claims"], answer["dropped"] = kept, dropped

    if not kept:
        answer["status"] = "none"
        answer["summary"] = (
            "Related discussion exists, but none of it states why this code is written this way. "
            "The reason was not recorded. " + _searched_sentence(state)
        )
        answer["verified_empty"] = True
    else:
        # The draft summary was never checked; the verifier's is built from
        # surviving claims only. Fall back to the claims themselves.
        answer["summary"] = out.revised_summary.strip() or " ".join(c["statement"] for c in kept)
    answer["trimmed"] = trimmed
    detail = f"{len(kept)} supported, {dropped} removed" + (f", {trimmed} trimmed to what the source says" if trimmed else "")
    return {
        "answer": answer,
        "llm_calls": usage,
        "trail": _step("verify", "Checked every claim against its sources", detail),
    }


def finalize(state: TraceState) -> dict:
    a = dict(state.get("answer", {}))
    if a.get("status") == "error":
        return {"answer": a}
    if not a.get("claims"):
        # Nothing survived with a citation: whatever the draft said is unbacked.
        if not a.get("verified_empty"):
            a["summary"] = (
                "The history describes what changed, but nothing recorded states why. "
                + _searched_sentence(state)
            )
        a["status"] = "none"
    else:
        # The verdict follows the sources, not the model's self-assessment:
        # claims backed only by "what changed" context don't count as a reason.
        rationale_ids = {e["id"] for e in state.get("evidence", []) if e.get("label") == "rationale"}
        if not any(set(c["evidence_ids"]) & rationale_ids for c in a["claims"]):
            a["status"] = "none"
        elif a.get("status") not in ("found", "partial"):
            a["status"] = "partial"
    return {"answer": a, "trail": _step("answer", "Answer ready", a["status"])}


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------


def build_graph():
    g = StateGraph(TraceState)
    g.add_node("locate", locate)
    g.add_node("blame", blame)
    g.add_node("history", history)
    g.add_node("discussions", discussions)
    g.add_node("filter", filter_noise)
    g.add_node("synthesize", synthesize)
    g.add_node("verify", verify)
    g.add_node("no_rationale", no_rationale)
    g.add_node("finalize", finalize)

    g.add_edge(START, "locate")
    g.add_edge("locate", "blame")
    g.add_edge("blame", "history")
    g.add_edge("history", "discussions")
    g.add_edge("discussions", "filter")
    g.add_conditional_edges("filter", route_after_filter, {"synthesize": "synthesize", "no_rationale": "no_rationale"})
    g.add_conditional_edges("synthesize", route_after_synthesize, {"verify": "verify", "finalize": "finalize"})
    g.add_edge("verify", "finalize")
    g.add_edge("no_rationale", END)
    g.add_edge("finalize", END)
    return g.compile()


GRAPH = build_graph()
