# Code-History

Recover the recorded reasoning behind a line of code. Select a line, and Code-History traces it back through
git history to the commits, pull requests, review threads and linked issues that shaped it. It separates
design reasoning from procedural chatter, answers only with cited sources, and says so plainly when no
rationale was ever written down.

## Quick start

```powershell
.\run.ps1
```

Then open http://localhost:8000. The first run creates `.venv`, installs dependencies and copies
`.env.example` to `.env`. Put your keys in `.env`:

```
GROQ_API_KEY=gsk_...
GEMINI_API_KEY=          # optional; used automatically while Groq is over its daily limit
GITHUB_TOKEN=            # optional; raises GitHub's limit from 60 to 5000 requests/hour
```

Keys are re-read on every trace, so you don't need to restart after adding it.

In the app, enter a local repository path, a GitHub URL, or `owner/name`.

Remote repositories are cloned temporarily into the system temp folder (`%TEMP%\code-history-clones`),
never into the project, and without a working copy since everything is read through git. A clone is
deleted when you close the repository or open another one, after `CLONE_IDLE_MINUTES` (default 30)
unused, and when the server stops. Leftovers from a crash are removed at the next start. Local folders
you open are never modified or deleted. Re-opening a repository from Recent clones it again; GitHub
and model responses stay cached in `workspace/cache/`, so a repeat trace costs no model calls.

## How a trace works

The pipeline is a LangGraph `StateGraph` ([backend/app/graph.py](backend/app/graph.py)):

```
locate → blame → history → discussions → filter ─┬─→ synthesize → verify → finalize
                                                 └─→ no_rationale
```

| Node | What it does |
|---|---|
| `locate` | Reads the file at `HEAD`, validates the range, detects a GitHub remote. |
| `blame` | `git blame -w -M -C`, honouring `.git-blame-ignore-revs`, so whitespace changes and moved code don't hide the real author. |
| `history` | `git log -L` over the range. Each commit is classified as *introduced*, *modified* or *formatting only*. Formatting-only commits are skipped, which handles the common case where blame lands on a reformat. |
| `discussions` | For each substantive commit: its message, the pull request (GitHub API, falling back to `(#123)` in the subject), review comments on that file, reviews, PR comments, and issues the PR or commit references. Responses are cached under `workspace/cache/`. |
| `filter` | Model-free: heuristics drop bots and approvals, and at most 24 items (commits, PRs and issues first) go on to the model, to stay within free-tier request sizes. |
| `no_rationale` | Runs when nothing relevant survives. This answer is deterministic, so no model call can invent a reason. |
| `synthesize` | One model call labels each item *rationale*, *context* or *noise* and writes the answer from the evidence only, reason first. Every claim must cite evidence ids; citations to noise or to ids that don't exist are discarded, and a claim citing an issue also gets the PR that links to it. |
| `verify` | A second call checks each claim against the exact text it cites. Unsupported claims are removed or narrowed to what the source says, and the summary is rebuilt from what survives. If nothing survives, the answer becomes "No recorded rationale". |

## Answers

- **Rationale recorded**: at least one verified claim cites a source labelled as rationale.
- **Partially recorded**: the record shows what changed and some background, but not the full reason.
  Open questions say what to ask a maintainer.
- **No recorded rationale**: nothing written down explains the code. This is a correct answer, not a failure.

## Configuration

| Variable | Default | |
|---|---|---|
| `GROQ_API_KEY` | | Primary model, for analysis and verification. |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | Any Groq chat model that supports tool calling. |
| `GEMINI_API_KEY` | | Optional fallback. Used automatically while Groq is over a daily limit, until Groq's quota returns. |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Any Gemini model that supports tool calling. |
| `GITHUB_TOKEN` | | Optional. Read-only access to public repositories is enough. |
| `MAX_HISTORY_COMMITS` | `25` | How far back `git log -L` walks. |
| `MAX_DECISION_COMMITS` | `5` | Substantive commits whose pull requests and issues are fetched. |
| `CLONE_IDLE_MINUTES` | `30` | Unused temporary clones are deleted after this long. |

## Layout

```
backend/app/
  graph.py         LangGraph pipeline and node logic
  git_tools.py     blame, line history, change classification
  github_tools.py  pull requests, reviews, issues (cached)
  llm.py           Groq with Gemini fallback, rate-limit handling, response cache
  prompts.py       analysis and verification prompts
  clones.py        temporary clones and their cleanup
  main.py          FastAPI routes, server-sent events for live progress
frontend/          static UI, no build step
tests/             offline tests (no model or GitHub calls)
```

## Model usage

A new trace makes two model calls: one to analyse the evidence and draft the answer, one to check it.
Repeating a trace costs nothing, because responses are cached. Per-minute rate limits are waited out;
when Groq reports a daily limit, traces move to Gemini until Groq's quota returns. The app shows which
model answered each trace.

## Tests

```powershell
.venv\Scripts\python -m unittest discover -s tests -v
```

The suite builds a throwaway git repository and uses stand-in models, so it spends no API quota.

## Limits

- Only GitHub is supported for pull requests and issues. Other hosts get commit messages only.
- Review comments are matched by file path, not exact line, because line numbers shift between a PR and `HEAD`.
- Selections are capped at 60 lines.
